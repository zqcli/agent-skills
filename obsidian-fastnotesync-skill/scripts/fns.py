#!/usr/bin/env python3
"""Fast Note Sync REST CLI; Python 3.10+ on macOS/Linux with requests[socks]."""

from contextlib import contextmanager
import base64
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import signal
import ssl
import stat
import sys
import time
from urllib.parse import unquote, unquote_to_bytes, urlsplit

VERSION = "2.0.1"
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
FILE_COMMANDS = {"file-upload", "file-info"}
PUBLIC = {"doctor", "health", "version"}
CONTENT_COMMANDS = {"create", "update", "upsert", "append", "prepend"}
NOTE_COMMANDS = CONTENT_COMMANDS | {"read", "delete", "restore", "replace", "frontmatter", "rename", "recycle-clear"}
NOTE_OPTIONS = {"--path", "--path-hash"}
CONTENT_OPTIONS = {"--content", "--content-file", "--stdin"}
LIST_OPTIONS = {"--keyword", "--search-mode", "--page", "--page-size", "--sort-by", "--sort-order", "--recycle"}
OPTIONS = {command: set() for command in PUBLIC | {"vaults"}}
OPTIONS.update({command: NOTE_OPTIONS | CONTENT_OPTIONS for command in CONTENT_COMMANDS})
OPTIONS.update({command: OPTIONS[command] | {"--ctime", "--mtime"} for command in ("create", "update", "upsert")})
OPTIONS["update"] |= {"--expect-hash"}
OPTIONS.update({"list": LIST_OPTIONS, "search": LIST_OPTIONS, "read": NOTE_OPTIONS | {"--recycle"},
                "delete": NOTE_OPTIONS, "restore": NOTE_OPTIONS,
                "replace": NOTE_OPTIONS | {"--find", "--replace", "--regex", "--all", "--fail-if-no-match"},
                "frontmatter": NOTE_OPTIONS | {"--updates", "--remove"},
                "rename": NOTE_OPTIONS | {"--old-path", "--old-path-hash"},
                "recycle-clear": NOTE_OPTIONS | {"--all", "--confirm", "--confirm-vault"}})
OPTIONS["file-upload"] = {"--file", "--path", "--overwrite", "--ctime", "--mtime"}
OPTIONS["file-info"] = {"--path"}
FLAGS = {"--stdin", "--recycle", "--regex", "--all", "--fail-if-no-match", "--confirm", "--overwrite"}
TEXT_OPTIONS = {"--content", "--find", "--replace", "--keyword"}
BUSINESS_CODES = {0, 530}
for _start, _end in ((300, 315), (400, 414), (420, 423), (430, 451), (455, 467),
                     (470, 479), (480, 484), (491, 502), (510, 512), (520, 521)):
    BUSINESS_CODES.update(range(_start, _end + 1))

HELP = """Usage: fns.py [--vault V] COMMAND [OPTIONS]
Environment: FNS_BASE_URL, FNS_TOKEN, FNS_VAULT; optional FNS_PROXY, FNS_CLIENT.
Explicit password mode: FNS_AUTH_MODE=password, FNS_USERNAME, FNS_PASSWORD.
Public: doctor | health | version. Authenticated discovery: vaults.
list/search [--keyword K --search-mode path|content --page N --page-size 1..100]
            [--sort-by mtime|ctime|path --sort-order asc|desc --recycle]
read/delete/restore --path P [--path-hash H] (read also accepts --recycle)
create/update/upsert --path P CONTENT_SOURCE [--ctime MS --mtime MS --path-hash H]
update also accepts --expect-hash H (precheck only, NOT atomic CAS).
append/prepend --path P CONTENT_SOURCE [--path-hash H]
CONTENT_SOURCE: exactly one of --content TEXT, --content-file FILE, --stdin.
replace --path P --find STR --replace STR [--regex --all --fail-if-no-match]
frontmatter --path P [--updates JSON_OBJECT --remove JSON_STRING_ARRAY]
rename --old-path P --path Q [--old-path-hash H --path-hash H]
recycle-clear --path P --confirm [--path-hash H]
recycle-clear --all --confirm-vault EXACT_VAULT
file-upload --file LOCAL --path REL [--overwrite --ctime MS --mtime MS]
            Single binary snapshot, at most 10 MiB. Existing targets require
            --overwrite (precheck only, NOT atomic create-only or CAS).
file-info --path REL
--vault overrides FNS_VAULT anywhere. Secrets must never be CLI arguments.
"""


class Failure(Exception):
    def __init__(self, exit_code, kind, message, code=None, http=None):
        super().__init__(message)
        self.exit_code = exit_code
        self.error = {"type": kind, "message": message}
        if code is not None:
            self.error["code"] = code
        if http is not None:
            self.error["httpStatus"] = http


class DeadlineExceeded(Exception):
    pass


def usage_error(message):
    raise Failure(2, "usage", message)


def require(condition, message):
    if not condition:
        usage_error(message)


def validate_strings(value):
    """JSON permits escaped lone surrogates; the API text contract does not."""
    if isinstance(value, str):
        value.encode("utf-8", "strict")
    elif isinstance(value, float) and not math.isfinite(value):
        raise ValueError("Nonfinite JSON number")
    elif isinstance(value, dict):
        for key, item in value.items():
            validate_strings(key)
            validate_strings(item)
    elif isinstance(value, list):
        for item in value:
            validate_strings(item)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def reject_constant(_value):
    raise ValueError("Nonfinite JSON constant")


def strict_json(text):
    value = json.loads(text, object_pairs_hook=unique_object, parse_constant=reject_constant)
    validate_strings(value)
    return value


def integer(value, positive=True, digits=9):
    pattern = r"[1-9][0-9]*" if positive else r"(?:0|[1-9][0-9]*)"
    require(len(value) <= digits and re.fullmatch(pattern, value, re.ASCII) is not None, "Invalid integer option")
    return int(value)


def parse(argv):
    command, values, seen = None, {}, set()
    index = 0
    while index < len(argv):
        option = argv[index]
        if option in ("--help", "-h"):
            return None, None
        if not option.startswith("--"):
            require(command is None and option in OPTIONS, "Unknown command or unexpected positional argument")
            command = option
            index += 1
            continue
        require(option not in seen, "Duplicate option")
        require(option == "--vault" or (command is not None and option in OPTIONS[command]), "Unknown or inapplicable option")
        seen.add(option)
        if option in FLAGS:
            value = True
            index += 1
        else:
            require(index + 1 < len(argv), "Option requires a value")
            value = argv[index + 1]
            require(option in TEXT_OPTIONS or not value.startswith("--"), "Option requires a value")
            index += 2
        values[option[2:].replace("-", "_")] = value
    require(command is not None, "Command required")
    if command in NOTE_COMMANDS and not (command == "recycle-clear" and values.get("all")):
        require(bool(values.get("path")), "Nonempty --path required")
    if command in FILE_COMMANDS:
        attachment_path(values.get("path", ""))
    if command == "file-upload":
        require(bool(values.get("file")), "A local --file is required")
    if command in CONTENT_COMMANDS:
        require(len(CONTENT_OPTIONS & seen) == 1, "Choose exactly one explicit content source")
    if command == "replace":
        require(bool(values.get("find")) and "replace" in values, "Nonempty --find and explicit --replace required")
    if command == "rename":
        require(bool(values.get("old_path")), "Nonempty --old-path required")
    if command == "frontmatter":
        require("updates" in values or "remove" in values, "Frontmatter requires updates or remove")
        for key in ("updates", "remove"):
            if key not in values:
                continue
            try:
                values[key] = strict_json(values[key])
            except (ValueError, UnicodeError, RecursionError):
                usage_error("Invalid frontmatter JSON")
            if key == "updates":
                require(isinstance(values[key], dict), "Updates must be one JSON object")
            else:
                require(isinstance(values[key], list) and all(isinstance(item, str) for item in values[key]), "Remove must be one JSON string array")
    for key in ("ctime", "mtime", "page", "page_size"):
        if key in values:
            values[key] = integer(values[key], positive=key not in ("ctime", "mtime"), digits=16 if key in ("ctime", "mtime") else 9)
    if "page_size" in values:
        require(values["page_size"] <= 100, "Page size must be 1..100")
    for key, choices in (("search_mode", {"path", "content"}), ("sort_by", {"mtime", "ctime", "path"}), ("sort_order", {"asc", "desc"})):
        if key in values:
            require(values[key] in choices, "Unsupported search or sort option")
    if "expect_hash" in values:
        require(bool(values["expect_hash"]), "Expected hash cannot be empty")
    if command == "search":
        require(bool(values.get("keyword")), "Search requires a nonempty keyword")
    try:
        validate_strings(values)
    except UnicodeError:
        usage_error("Arguments must be valid UTF-8")
    return command, values


def validate_url(value, proxy=False):
    try:
        parsed = urlsplit(value)
        schemes = {"http", "https", "socks5", "socks5h"} if proxy else {"http", "https"}
        host = parsed.hostname
        port = parsed.port
        if not host or parsed.scheme not in schemes or parsed.query or parsed.fragment:
            raise ValueError()
        if re.search(r"[\x00-\x20\x7f\\{}]", value) or (port is not None and not 1 <= port <= 65535):
            raise ValueError()
        if ":" in host:
            ipaddress.IPv6Address(host)
        elif re.fullmatch(r"[A-Za-z0-9_.-]+", host, re.ASCII) is None:
            raise ValueError()
        if proxy:
            if parsed.path not in ("", "/"):
                raise ValueError()
        elif parsed.username is not None or parsed.password is not None:
            raise ValueError()
        return parsed
    except ValueError:
        raise Failure(2, "config", "Invalid proxy URL" if proxy else "Invalid server base URL") from None


def header_value(value):
    try:
        if not value or value != value.strip() or re.search(r"[\x00-\x1f\x7f]", value):
            raise ValueError()
        value.encode("latin-1", "strict")
    except (ValueError, UnicodeError):
        raise Failure(2, "config", "Invalid authentication or client header") from None
    return value


class Redactor:
    def __init__(self, environment):
        self.secrets = set()
        for name in ("FNS_TOKEN", "FNS_USERNAME", "FNS_PASSWORD"):
            self.add(environment.get(name))
        self.add_proxy(environment.get("FNS_PROXY"))

    def add(self, value):
        if isinstance(value, str) and value:
            self.secrets.add(value)

    def add_proxy(self, proxy):
        self.add(proxy)
        if not proxy:
            return
        try:
            parsed = urlsplit(proxy)
            for credential in (parsed.username, parsed.password):
                self.add(credential)
                if credential:
                    self.add(unquote(credential))
        except ValueError:
            pass

    def redact(self, value):
        if isinstance(value, str):
            for secret in sorted(self.secrets, key=len, reverse=True):
                value = value.replace(secret, "[REDACTED]")
            return value
        if isinstance(value, list):
            return [self.redact(item) for item in value]
        if isinstance(value, dict):
            return {self.redact(key): self.redact(item) for key, item in value.items()}
        return value


@contextmanager
def deadline(seconds):
    """Requests read timeouts are inactivity limits, not a wall-clock deadline."""
    old_handler = signal.getsignal(signal.SIGALRM)
    old_timer = signal.getitimer(signal.ITIMER_REAL)
    started = time.monotonic()

    def expired(_signum, _frame):
        raise DeadlineExceeded()

    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, min(seconds, old_timer[0]) if old_timer[0] else seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old_handler)
        remaining = max(0.000001, old_timer[0] - (time.monotonic() - started)) if old_timer[0] else 0
        signal.setitimer(signal.ITIMER_REAL, remaining, old_timer[1])


def verified_adapter(requests, verify):
    class ProxyAdapter(requests.adapters.HTTPAdapter):
        def proxy_headers(self, proxy):
            parsed = urlsplit(proxy)
            if parsed.username is None:
                return {}
            # Requests' default Basic auth uses Latin-1 strings; preserve curl's
            # percent-decoded credential bytes, including UTF-8 userinfo.
            credentials = unquote_to_bytes(parsed.username) + b":" + unquote_to_bytes(parsed.password or "")
            return {"Proxy-Authorization": "Basic " + base64.b64encode(credentials).decode("ascii")}

        def proxy_manager_for(self, proxy, **kwargs):
            if urlsplit(proxy).scheme == "https" and proxy not in self.proxy_manager:
                # The destination may be HTTP. Never inherit its CERT_NONE
                # policy for the separate TLS connection to an HTTPS proxy.
                ca = requests.certs.where() if verify is True else os.fspath(verify)
                context = ssl.create_default_context(capath=ca) if os.path.isdir(ca) else ssl.create_default_context(cafile=ca)
                kwargs["proxy_ssl_context"] = context
            return super().proxy_manager_for(proxy, **kwargs)

    return ProxyAdapter(max_retries=0)


class Client:
    def __init__(self, environment, command, values):
        self.environment = environment
        self.redactor = Redactor(environment)
        self.base = environment.get("FNS_BASE_URL", "").rstrip("/")
        parsed = validate_url(self.base)
        if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "::1") and environment.get("FNS_ALLOW_HTTP") != "1":
            raise Failure(2, "config", "Non-loopback HTTP requires FNS_ALLOW_HTTP=1")
        self.vault = values.get("vault", environment.get("FNS_VAULT", ""))
        self.mode = environment.get("FNS_AUTH_MODE") or "token"
        if self.mode not in ("token", "password"):
            raise Failure(2, "config", "Unsupported authentication mode")
        try:
            self.timeout = integer(environment.get("FNS_TIMEOUT") or "30")
            self.connect_timeout = min(integer(environment.get("FNS_CONNECT_TIMEOUT") or "10"), self.timeout)
        except Failure:
            raise Failure(2, "config", "Timeouts must be positive integer seconds") from None
        if os.name != "posix" or not hasattr(signal, "setitimer"):
            raise Failure(2, "config", "POSIX request deadlines require macOS or Linux")
        self.client = header_value(environment.get("FNS_CLIENT") or "obsidian-fastnotesync-skill")
        self.agent = header_value(environment.get("FNS_USER_AGENT") or "obsidian-fastnotesync-skill/" + VERSION)
        self.token = environment.get("FNS_TOKEN", "")
        if self.token:
            header_value(self.token)
        self.proxy = environment.get("FNS_PROXY", "")
        if self.proxy:
            validate_url(self.proxy, proxy=True)
        self.verify = environment.get("REQUESTS_CA_BUNDLE") or environment.get("CURL_CA_BUNDLE") or True
        if command not in PUBLIC:
            if not self.vault and command != "vaults":
                raise Failure(2, "config", "An existing vault must be selected")
            if self.mode == "token":
                if not self.token:
                    raise Failure(2, "config", "FNS_TOKEN required in token mode")
            else:
                if not environment.get("FNS_USERNAME") or not environment.get("FNS_PASSWORD"):
                    raise Failure(2, "config", "Username and password required in explicit password mode")
                self.client = "webgui"
                try:
                    validate_strings([environment["FNS_USERNAME"], environment["FNS_PASSWORD"]])
                except UnicodeError:
                    raise Failure(2, "config", "Login credentials must be valid UTF-8") from None
        try:
            import requests
            import socks  # noqa: F401 -- verify that the requested SOCKS extra is installed
            import urllib3
            if tuple(int(part) for part in urllib3.__version__.split(".")[:2]) < (2, 8):
                raise ImportError()
        except ImportError:
            raise Failure(2, "config", "Install requirements.txt with requests[socks] for this Python interpreter") from None
        self.requests = requests
        self.session = requests.Session()
        # Disable .netrc auth and implicit proxy/CA overrides. Select those
        # explicitly so environment auth cannot replace our Bearer header.
        self.session.trust_env = False
        for scheme in ("http://", "https://"):
            self.session.mount(scheme, verified_adapter(requests, self.verify))

    def request(self, method, endpoint, authenticated=True, body=None, params=None, *,
                form=None, files=None, missing_file_ok=False):
        if body is not None and (form is not None or files is not None):
            usage_error("JSON and multipart request bodies are mutually exclusive")
        if missing_file_ok and (method != "GET" or endpoint != "/api/file/info"):
            usage_error("Missing-file handling is restricted to the upload precheck")
        url = self.base + endpoint
        headers = {"User-Agent": self.agent, "x-client": self.client}
        if authenticated:
            headers["Authorization"] = "Bearer " + self.token
        try:
            with deadline(self.timeout):
                proxies = {"http": self.proxy, "https": self.proxy} if self.proxy else self.requests.utils.get_environ_proxies(url)
                for key, proxy in proxies.items():
                    if proxy and key in ("http", "https", "all"):
                        validate_url(proxy, proxy=True)
                        self.redactor.add_proxy(proxy)
                body_options = {"data": form, "files": files} if files is not None or form is not None else {"json": body}
                response = self.session.request(method, url, headers=headers, params=params,
                                                proxies=proxies, verify=self.verify,
                                                timeout=(self.connect_timeout, self.timeout), allow_redirects=False,
                                                **body_options)
                try:
                    http = response.status_code
                    if not 200 <= http < 300:
                        raise Failure(3, "http", "Unsuccessful HTTP response; redirects are not followed", http=http)
                    try:
                        value = strict_json(response.content.decode("utf-8-sig", "strict"))
                    except (ValueError, UnicodeError, RecursionError):
                        raise Failure(4, "protocol", "Invalid JSON response envelope", http=http) from None
                    if not isinstance(value, dict) or type(value.get("code")) is not int or ("status" in value and type(value["status"]) is not bool):
                        raise Failure(4, "protocol", "Invalid JSON response envelope", http=http)
                    code = value["code"]
                    # This pinned endpoint wraps a missing record as generic code 0.
                    # Never confuse arbitrary code-0 errors with permission to write.
                    if (missing_file_ok and http == 200 and code == 0 and
                            value.get("status") is False and value.get("data") is None and
                            value.get("details") == "record not found"):
                        return None
                    if code in range(1, 7):
                        if value.get("status") is False:
                            raise Failure(5, "business", "Server reported a business failure", code, http)
                    elif code in BUSINESS_CODES:
                        raise Failure(5, "business", "Server rejected the operation; consult the API error code", code, http)
                    else:
                        raise Failure(4, "protocol", "Unrecognized business code; server contract may be incompatible", code, http)
                    return {"success": True, "code": code, "data": value.get("data")}
                finally:
                    response.close()
        except (DeadlineExceeded, self.requests.exceptions.RequestException):
            raise Failure(3, "transport", "Request failed or timed out; write outcome may be unknown. Do not automatically retry.") from None
        except OSError:
            raise Failure(2, "config", "Unable to use the configured TLS certificate bundle") from None

    def login(self):
        result = self.request("POST", "/api/user/login", authenticated=False,
                              body={"credentials": self.environment["FNS_USERNAME"], "password": self.environment["FNS_PASSWORD"]})
        data = result["data"]
        token = data.get("token") if isinstance(data, dict) else None
        if not isinstance(token, str) or not token:
            raise Failure(4, "protocol", "Login did not return a nonempty token")
        try:
            self.token = header_value(token)
        except Failure:
            raise Failure(4, "protocol", "Login returned an invalid token") from None
        self.redactor.add(token)

    def close(self):
        self.session.close()


def content_input(values):
    if "content" in values:
        return values["content"]
    try:
        if "content_file" in values:
            path = Path(values["content_file"])
            if not path.is_file():
                usage_error("Content file must be a readable regular file")
            raw = path.read_bytes()
        else:
            raw = sys.stdin.buffer.read()
        return raw.decode("utf-8", "strict")
    except (OSError, UnicodeError):
        usage_error("Content source must be readable and valid UTF-8")


def attachment_path(path):
    require(isinstance(path, str) and bool(path) and
            re.search(r'[\x00-\x1f\x7f-\x9f\\%:*?"<>|]', path) is None and
            all(part not in ("", ".", "..") for part in path.split("/")),
            "Attachment path must be a safe relative POSIX file path")


def attachment_input(values):
    """Snapshot bounded binary input before authentication or network activity."""
    path = Path(values["file"])

    def signature(info):
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns

    try:
        before = path.stat()
        require(stat.S_ISREG(before.st_mode), "Attachment source must be a regular file")
        require(before.st_size <= MAX_UPLOAD_BYTES, "Attachment exceeds the 10 MiB limit")
        # Nonblocking open also avoids hanging if a regular file is replaced by a FIFO.
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        try:
            stream = os.fdopen(fd, "rb")
        except BaseException:
            os.close(fd)
            raise
        with stream:
            opened = os.fstat(stream.fileno())
            require(stat.S_ISREG(opened.st_mode) and signature(opened) == signature(before),
                    "Attachment source changed before reading")
            raw = stream.read(MAX_UPLOAD_BYTES + 1)
            require(len(raw) == before.st_size and
                    signature(os.fstat(stream.fileno())) == signature(before) and
                    signature(path.stat()) == signature(before),
                    "Attachment source changed while reading")
        return {"bytes": raw, "mtime": max(0, before.st_mtime_ns // 1000000)}
    except OSError:
        usage_error("Attachment source must be a readable regular file")


def hash32(units):
    result = 0
    for unit in units:
        result = (31 * result + unit) & 0xffffffff
    return str(result if result < 0x80000000 else result - 0x100000000)


def attachment_path_hash(path):
    raw = path.encode("utf-16-be", "strict")
    return hash32(int.from_bytes(raw[index:index + 2], "big") for index in range(0, len(raw), 2))


def file_metadata(data, path, *, timestamps=True):
    def valid_integer(value, minimum=0):
        return type(value) is int and minimum <= value <= 0x7fffffffffffffff

    def valid_hash(value):
        return (isinstance(value, str) and len(value) <= 11 and
                re.fullmatch(r"(?:0|-?[1-9][0-9]*)", value, re.ASCII) is not None and
                -0x80000000 <= int(value) <= 0x7fffffff)

    valid = (isinstance(data, dict) and valid_integer(data.get("id"), 1) and
             data.get("path") == path and data.get("pathHash") == attachment_path_hash(path) and
             valid_integer(data.get("size")) and valid_hash(data.get("contentHash")))
    if valid:
        valid = all(valid_integer(data.get(key)) for key in ("ctime", "mtime")
                    if timestamps or key in data)
    if not valid:
        raise Failure(4, "protocol", "Invalid or mismatched attachment metadata; no automatic retry")
    return data


def file_info(client, path, *, missing_ok=False):
    result = client.request("GET", "/api/file/info", params={"vault": client.vault, "path": path},
                            missing_file_ok=missing_ok)
    if result is None:
        return None
    if result["code"] != 1:
        raise Failure(4, "protocol", "Unexpected attachment response code")
    file_metadata(result["data"], path)
    return result


def upload_file(client, values, snapshot):
    path, raw = values["path"], snapshot["bytes"]
    current = file_info(client, path, missing_ok=True)
    if current is not None and not values.get("overwrite"):
        raise Failure(6, "precondition", "Attachment already exists; use --overwrite only when authorized")
    ctime = values.get("ctime", current["data"]["ctime"] if current else int(time.time() * 1000))
    mtime = values.get("mtime", snapshot["mtime"])
    expected_hash = hash32(raw)  # Inputs are capped at 10 MiB: upstream hashes all bytes.
    result = client.request("POST", "/api/file",
                            form={"vault": client.vault, "path": path, "ctime": str(ctime), "mtime": str(mtime)},
                            files={"file": (path.rsplit("/", 1)[-1], raw, "application/octet-stream")})
    if result["code"] != 1:
        raise Failure(4, "protocol", "Unexpected upload response code; write outcome may be unknown")
    uploaded = file_metadata(result["data"], path, timestamps=False)
    if uploaded["size"] != len(raw) or uploaded["contentHash"] != expected_hash:
        raise Failure(4, "protocol", "Upload acknowledgement mismatch; write outcome may be unknown")
    verified = file_info(client, path)
    actual = verified["data"]
    for key in ("id", "path", "pathHash", "size", "contentHash", "ctime", "mtime"):
        if key in uploaded and actual[key] != uploaded[key]:
            raise Failure(4, "protocol", "Upload readback mismatch; write outcome may be unknown")
    # Zero timestamps request server-assigned times, not preservation of zero.
    if (ctime and actual["ctime"] != ctime) or (mtime and actual["mtime"] != mtime):
        raise Failure(4, "protocol", "Upload timestamp mismatch; write outcome may be unknown")
    return verified


def clear_guard(values, vault):
    if values.get("all"):
        require(bool(vault) and values.get("confirm_vault") == vault and not values.get("confirm")
                and "path" not in values and "path_hash" not in values,
                "Full clear requires only --all and an exact --confirm-vault")
    else:
        require(bool(values.get("path")) and values.get("confirm") and "confirm_vault" not in values,
                "Single clear requires --path and --confirm")


def execute(client, command, values, content):
    if command not in OPTIONS:
        usage_error("Unsupported command dispatch")
    request = client.request
    if command == "doctor":
        health = request("GET", "/api/health", False)
        version = request("GET", "/api/version", False)
        return {"success": True, "code": 1, "data": {"health": health["data"], "version": version["data"]}}
    if command in PUBLIC:
        return request("GET", "/api/" + command, False)
    if client.mode == "password":
        client.login()
    if command == "vaults":
        return request("GET", "/api/vault")
    if command == "file-info":
        return file_info(client, values["path"])
    if command == "file-upload":
        return upload_file(client, values, content)
    if command in ("list", "search"):
        params = {"vault": client.vault, "keyword": values.get("keyword", ""),
                  "searchMode": values.get("search_mode", "content" if command == "search" else "path"),
                  "page": values.get("page", 1), "pageSize": values.get("page_size", 10),
                  "sortBy": values.get("sort_by", "mtime"), "sortOrder": values.get("sort_order", "desc"),
                  "isRecycle": "true" if values.get("recycle") else "false"}
        result = request("GET", "/api/notes", params=params)
        data = result["data"]
        if not isinstance(data, dict) or "list" not in data or (data["list"] is not None and not isinstance(data["list"], list)):
            raise Failure(4, "protocol", "Invalid note list response")
        if data["list"] is None:
            pager = data.get("pager")
            if not isinstance(pager, dict) or type(pager.get("totalRows")) is not int or pager["totalRows"] != 0:
                raise Failure(4, "protocol", "Null note list with nonzero or missing count")
            data["list"] = []
        return result
    note = {"vault": client.vault, "path": values.get("path", "")}
    if values.get("path_hash"):
        note["pathHash"] = values["path_hash"]
    if command in ("read", "delete"):
        if command == "read":
            note["isRecycle"] = "true" if values.get("recycle") else "false"
        return request("GET" if command == "read" else "DELETE", "/api/note", params=note)
    if command in CONTENT_COMMANDS:
        body = {**note, "content": content}
        if command == "update":
            current = request("GET", "/api/note", params=note)["data"]
            if not isinstance(current, dict) or type(current.get("ctime")) is not int or current["ctime"] < 0:
                raise Failure(4, "protocol", "Existing note has invalid creation metadata")
            if "expect_hash" in values and current.get("contentHash") != values["expect_hash"]:
                raise Failure(6, "precondition", "Content hash changed or is missing; no write performed")
            body["ctime"] = current["ctime"]
        if command in ("create", "update", "upsert"):
            body["createOnly"] = command == "create"
            body.update({key: values[key] for key in ("ctime", "mtime") if key in values})
            return request("POST", "/api/note", body=body)
        return request("POST", "/api/note/" + command, body=body)
    if command == "replace":
        body = {**note, "find": values["find"], "replace": values["replace"],
                "regex": values.get("regex", False), "all": values.get("all", False),
                "failIfNoMatch": values.get("fail_if_no_match", False)}
        return request("POST", "/api/note/replace", body=body)
    if command == "frontmatter":
        body = {**note, **{key: values[key] for key in ("updates", "remove") if key in values}}
        return request("PATCH", "/api/note/frontmatter", body=body)
    if command == "rename":
        body = {**note, "oldPath": values["old_path"]}
        if values.get("old_path_hash"):
            body["oldPathHash"] = values["old_path_hash"]
        return request("POST", "/api/note/rename", body=body)
    if command == "restore":
        return request("PUT", "/api/note/restore", body=note)
    if command == "recycle-clear":
        body = {"vault": client.vault} if values.get("all") else note
        return request("DELETE", "/api/note/recycle-clear", body=body)
    usage_error("Unsupported command dispatch")


def emit(value):
    # ASCII JSON works even with a non-UTF-8 stdout locale, without changing data.
    print(json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":")))


def main(argv=None):
    client = None
    try:
        command, values = parse(list(sys.argv[1:] if argv is None else argv))
        if command is None:
            print(HELP, end="")
            return 0
        environment = dict(os.environ)
        if command == "recycle-clear":
            clear_guard(values, values.get("vault", environment.get("FNS_VAULT", "")))
        content = (attachment_input(values) if command == "file-upload" else
                   content_input(values) if command in CONTENT_COMMANDS else None)
        client = Client(environment, command, values)
        result = execute(client, command, values, content)
        # Only redact payload keys/values; never rename the client-owned envelope.
        result["data"] = client.redactor.redact(result["data"])
        emit(result)
        return 0
    except Failure as error:
        emit({"success": False, "error": error.error})
        return error.exit_code
    except KeyboardInterrupt:
        emit({"success": False, "error": {"type": "transport", "message": "Interrupted; write outcome may be unknown. Do not automatically retry."}})
        return 3
    except Exception:
        # No raw exception, request URL, credentials, or server body in reports.
        emit({"success": False, "error": {"type": "protocol", "message": "Unable to complete the request safely"}})
        return 4
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    sys.exit(main())
