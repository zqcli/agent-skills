#!/usr/bin/env python3
"""Opt-in upload checks against a NEW, disposable FNS 3.6.1 instance.

Only explicit --base-url http://127.0.0.1:PORT (or http://[::1]:PORT) and
--confirm-isolated-instance authorize network access. A loopback SSH tunnel is
NOT proof of isolation: its owner must ensure a fresh container, independent
SQLite/config/storage, no production mounts, and no concurrent writers. The
runner does not start Docker, SSH, or read any inherited FNS/BW credentials.

Bootstrap requests were checked against official tag 3.6.1, commit
7a6c78792c631f999c8a5f725bba5dd7235d6688:
  internal/routers/router_api.go
  internal/dto/{user,vault,token,file,admin}_dto.go
  internal/service/{user,token}_service.go
  internal/middleware/user_auth_token.go
  pkg/app/permission.go, pkg/util/{validator,hash}.go

Registration: POST /api/user/register, email/username/password/confirmPassword.
Login: POST /api/user/login, credentials/password; data.token is the session.
Both use x-client:webgui. POST /api/vault takes {vault}, with no update id.
POST /api/token takes clientType/protocol/client/function/expiredDays/userAgent/
vaults; data.token is the manual JWT. Explicit dimensions produce
p:rest c:obsidian-fastnotesync-skill f:file_rw, restricted to our unique vault.
The webgui session inspects notes because file_rw must NOT grant note reads.

For main's bootstrap: an empty default installation auto-generates the embedded
config/config.yaml (cmd/run.go, main.go); it enables registration and SQLite.
A custom minimal YAML MUST set user.register-is-enable:true: UserConfig has no
true default. admin-uid:0 allows only the first registration, then refuses more
users. Default DB paths and per-user SQLite files all need disposable storage.
REST uploads use storage/temp then storage/vault/u_<uid>/file/f_<id>/file.dat;
they do not require enabling the separate storage.local-fs backend. Put temp and
final files on the same filesystem (file_repository.go uses os.Rename).

Only synthetic fixture bytes are written locally, under TemporaryDirectory.
Passwords/tokens exist only in memory and the clean CLI child environment; no
credential file/cache is created. FNS's own temporary DB necessarily stores its
user/token records; main destroys that instance. No vault/file/history deletion
is performed. Writes are never retried, even after uncertain outcomes.

The CLI still runs against the real instance. A child-only Requests observer
checks GET /api/file/info -> multipart POST /api/file -> GET /api/file/info,
and a socket audit guard disallows other destinations. Local-rejection cases
block all child networking and require an empty request trace. This observer
assumes the existing CLI's Session.request transport; it does not mock replies.
Only JSON summaries are reported, never child stdout/stderr or exception text.
--help and AST compilation are offline; neither proves live compatibility.
"""

import argparse
import base64
from contextlib import contextmanager
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import sys
import tempfile
from urllib.parse import urlencode, urlsplit
import uuid


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "fns.py"
CLIENT = "obsidian-fastnotesync-skill"
USER_AGENT = CLIENT + "/2.0.1"
LIMIT = 10 * 1024 * 1024
# A remote loopback SSH tunnel can spend most of a 30-second deadline sending
# the 10 MiB boundary fixture. Production CLI defaults remain unchanged.
HTTP_TIMEOUT = 120
CLI_TIMEOUT = 240
INFO = "GET /api/file/info"
UPLOAD = "POST /api/file"
META_FIELDS = ("id", "path", "pathHash", "contentHash", "size", "ctime", "mtime")
TRACE_PREFIX = b"FNS_UPLOAD_TRACE="
FAIL_REASON = "Operation or assertion failed; no retry or remote cleanup attempted."

# No secrets in this program or argv. Its only environment is built below, not
# copied from os.environ. Observe real requests and fail closed on any diversion.
CHILD_PROGRAM = r'''
import json, os, runpy, sys
from urllib.parse import urlsplit
import requests
base = urlsplit(os.environ["FNS_BASE_URL"])
expected_vault = os.environ["UPLOAD_EXPECTED_VAULT"]
local_only = os.environ["UPLOAD_NO_NETWORK"] == "1"
trace, blocked = [], [False]
original = requests.sessions.Session.request

def deny():
    blocked[0] = True
    raise SystemExit(91)

def audit(event, args):
    if event == "socket.getaddrinfo":
        if local_only or args[0] != base.hostname or str(args[1]) != str(base.port):
            deny()
    elif event == "socket.connect":
        address = args[1]
        if (local_only or not isinstance(address, tuple) or len(address) < 2
                or address[0] != base.hostname or address[1] != base.port):
            deny()

def observed(session, method, url, **kwargs):
    target = urlsplit(url)
    action = method.upper() + " " + target.path
    if (local_only or target.scheme != "http" or target.netloc != base.netloc
            or target.query or target.fragment
            or action not in ("GET /api/file/info", "POST /api/file")
            or kwargs.get("allow_redirects") is not False or session.trust_env
            or any(kwargs.get("proxies", {}).values())):
        deny()
    headers = kwargs.get("headers", {})
    if (headers.get("x-client") != os.environ["FNS_CLIENT"]
            or headers.get("User-Agent") != os.environ["FNS_USER_AGENT"]
            or headers.get("Authorization") != "Bearer " + os.environ["FNS_TOKEN"]):
        deny()
    fields = kwargs.get("params") if method.upper() == "GET" else kwargs.get("data")
    if (not isinstance(fields, dict) or fields.get("vault") != expected_vault
            or "token" in fields or "password" in fields):
        deny()
    if method.upper() == "POST" and not kwargs.get("files"):
        deny()
    trace.append(action)
    try:
        return original(session, method, url, **kwargs)
    except Exception as failure:
        # Exception class names are safe; messages/URLs/headers are not.
        classes, pending = [], [failure]
        while pending and len(classes) < 8:
            error = pending.pop()
            if isinstance(error, BaseException):
                classes.append(type(error).__name__)
                pending.extend(arg for arg in error.args if isinstance(arg, BaseException))
        sys.stderr.write("FNS_UPLOAD_TRANSPORT_TYPES=" + json.dumps(classes) + "\\n")
        raise

requests.sessions.Session.request = observed
sys.addaudithook(audit)
sys.argv = sys.argv[1:]
try:
    runpy.run_path(sys.argv[0], run_name="__main__")
finally:
    sys.stderr.write("FNS_UPLOAD_TRACE=" + json.dumps(
        {"requests": trace, "blocked": blocked[0]}, separators=(",", ":")) + "\n")
'''


class CheckFailure(Exception):
    """Only numeric, non-secret diagnostics may reach the summary."""

    def __init__(self, code=None, http_status=None, stage=None, cli_exit=None):
        super().__init__()
        self.diagnostics = {}
        if type(code) is int:
            self.diagnostics["code"] = code
        if type(http_status) is int:
            self.diagnostics["http_status"] = http_status
        if stage in ("cli_request_trace", "cli_exit"):
            self.diagnostics["stage"] = stage
        if type(cli_exit) is int:
            self.diagnostics["cli_exit"] = cli_exit


def require(condition):
    if not condition:
        raise CheckFailure()


def report(checks, fixture_count=0, sha256_match=None, notes_untouched=None):
    success = bool(checks) and all(check["status"] == "pass" for check in checks)
    print(json.dumps({"success": success, "fixture_count": fixture_count,
                      "checks": checks, "sha256_match": sha256_match,
                      "notes_untouched": notes_untouched},
                     ensure_ascii=True, allow_nan=False, separators=(",", ":")))
    return 0 if success else 1


class SafeParser(argparse.ArgumentParser):
    def error(self, _message):
        report([{"name": "arguments", "status": "fail",
                 "reason": "Invalid arguments; use --help. No live calls made."}])
        self.exit(2)


def parser():
    result = SafeParser(
        description="Real FNS 3.6.1 file-upload/file-info checks on a NEW disposable instance.",
        epilog="Never point a tunnel at production. No environment credentials, external proxies, "
               "redirects, retries, credential files, or remote cleanup are used. "
               "Main owns container/tunnel teardown. Run with the CLI's Python dependencies installed.",
        allow_abbrev=False)
    result.add_argument("--base-url", required=True, metavar="LOOPBACK_HTTP",
                        help="Explicit HTTP origin with port: 127.0.0.1 or [::1] only; no DNS names or path.")
    result.add_argument("--confirm-isolated-instance", required=True, action="store_true",
                        help="Confirm a fresh, short-lived instance with independent DB/config/storage "
                             "and no production mounts or concurrent writers.")
    return result


def checked_origin(value):
    require(isinstance(value, str) and
            re.fullmatch(r"http://(?:127\.0\.0\.1|\[::1\]):[0-9]{1,5}/?", value) is not None)
    parsed = urlsplit(value)
    require(parsed.hostname in ("127.0.0.1", "::1") and 1 <= parsed.port <= 65535)
    return value.rstrip("/"), parsed.hostname, parsed.port


def checked_token(data):
    require(isinstance(data, dict))
    token = data.get("token")
    require(isinstance(token, str) and bool(token) and
            re.fullmatch(r"[A-Za-z0-9_.-]+", token) is not None)
    return token


@contextmanager
def deadline():
    def expired(_signum, _frame):
        raise CheckFailure()

    previous = signal.signal(signal.SIGALRM, expired)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, HTTP_TIMEOUT)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
        signal.setitimer(signal.ITIMER_REAL, *previous_timer)


class IsolatedChecks:
    def __init__(self, origin, host, port):
        self.origin, self.host, self.port = origin, host, port
        self.vault = "fns-skill-test-" + str(uuid.uuid4())
        self.prefix = "fns-upload-" + uuid.uuid4().hex + "/"
        self.session_token = None
        self.manual_token = None
        self.readonly_token = None
        self.fixture_count = 0
        self.sha256_match = None
        self.notes_untouched = None
        self.checks = []
        self.payloads = {}
        self.metadata = {}
        self.directory = None

    def raw(self, method, endpoint, *, token=None, client="webgui", params=None,
            body=None, binary=False):
        allowed = {("GET", "/api/webgui/config"), ("POST", "/api/user/register"),
                   ("POST", "/api/user/login"), ("GET", "/api/vault"),
                   ("POST", "/api/vault"), ("POST", "/api/token"),
                   ("GET", "/api/file/info"), ("GET", "/api/file"),
                   ("GET", "/api/files"), ("GET", "/api/notes")}
        require((method, endpoint) in allowed)
        # All data inspection is restricted to exactly the newly created vault.
        if endpoint in ("/api/file/info", "/api/file", "/api/files", "/api/notes"):
            require(isinstance(params, dict) and params.get("vault") == self.vault)
        if endpoint == "/api/vault" and method == "POST":
            require(body == {"vault": self.vault})
        if endpoint == "/api/token":
            require(body.get("vaults") == self.vault)
        headers = {"x-client": client, "User-Agent": USER_AGENT, "Connection": "close"}
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        request_body = None
        if body is not None:
            request_body = json.dumps(body, ensure_ascii=True, allow_nan=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        target = endpoint + ("?" + urlencode(params) if params else "")
        # HTTPConnection connects directly to the fixed numeric loopback host;
        # no urllib/Requests environment proxy discovery, cookies, or redirects.
        with deadline():
            connection = http.client.HTTPConnection(self.host, self.port, timeout=HTTP_TIMEOUT)
            try:
                connection.request(method, target, body=request_body, headers=headers)
                response = connection.getresponse()
                if response.status != 200:
                    raise CheckFailure(http_status=response.status)
                content = response.read((LIMIT if binary else 1024 * 1024) + 1)
                require(len(content) <= (LIMIT if binary else 1024 * 1024))
            finally:
                connection.close()
        if binary:
            return content
        value = json.loads(content.decode("utf-8", "strict"))
        require(isinstance(value, dict) and type(value.get("code")) is int)
        require("status" not in value or type(value["status"]) is bool)
        return value

    @staticmethod
    def success(value):
        require(isinstance(value, dict))
        if value.get("code") not in range(1, 7) or value.get("status") is False:
            raise CheckFailure(code=value.get("code"), http_status=200)
        return value.get("data")

    def bootstrap(self):
        configuration = self.success(self.raw("GET", "/api/webgui/config"))
        require(isinstance(configuration, dict) and configuration.get("registerIsEnable") is True)
        username = "fns_" + secrets.token_hex(8)  # Official validator: 3..20 ASCII letters/digits/_ .
        password = secrets.token_urlsafe(32)  # Below bcrypt's 72-byte maximum.
        user = self.success(self.raw("POST", "/api/user/register", body={
            "email": username + "@example.invalid", "username": username,
            "password": password, "confirmPassword": password}))
        require(isinstance(user, dict) and type(user.get("uid")) is int and user["uid"] > 0)
        uid = user["uid"]
        del user  # Registration also returns a token; do not persist/use it.
        login = self.success(self.raw("POST", "/api/user/login",
                                     body={"credentials": username, "password": password}))
        require(isinstance(login, dict) and login.get("uid") == uid)
        self.session_token = checked_token(login)
        del login, username, password
        vaults = self.success(self.raw("GET", "/api/vault", token=self.session_token))
        require(vaults is None or vaults == [])  # Only the newly registered user's account.
        vault = self.success(self.raw("POST", "/api/vault", token=self.session_token,
                                      body={"vault": self.vault}))
        require(isinstance(vault, dict) and vault.get("vault") == self.vault)
        require(type(vault.get("id")) is int and vault["id"] > 0)
        self.manual_token = self.issue_token("file_rw")

    def issue_token(self, function):
        require(function in ("file_rw", "file_r"))
        issued = self.success(self.raw("POST", "/api/token", token=self.session_token, body={
            "clientType": CLIENT, "protocol": "rest", "client": CLIENT,
            "function": function, "expiredDays": 1, "userAgent": USER_AGENT,
            "vaults": self.vault}))
        token = checked_token(issued)
        require(issued.get("scope") == "p:rest c:" + CLIENT + " f:" + function)
        require(issued.get("issueType") == 2 and issued.get("clientType") == CLIENT)
        require(issued.get("vaults") == self.vault and issued.get("userAgent") == USER_AGENT)
        return token

    def environment(self, token, vault, no_network):
        # Deliberate allowlist: never even read os.environ, HOME, BW_SESSION,
        # FNS_USERNAME/PASSWORD, proxy credentials, Python startup hooks, or CA overrides.
        return {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C",
                "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1",
                "NO_PROXY": "*", "no_proxy": "*", "FNS_BASE_URL": self.origin,
                "FNS_AUTH_MODE": "token", "FNS_TOKEN": token, "FNS_VAULT": self.vault,
                "FNS_CLIENT": CLIENT, "FNS_USER_AGENT": USER_AGENT,
                "FNS_TIMEOUT": str(HTTP_TIMEOUT), "FNS_CONNECT_TIMEOUT": "60",
                "UPLOAD_EXPECTED_VAULT": vault, "UPLOAD_NO_NETWORK": "1" if no_network else "0"}

    @staticmethod
    def stop_child(process):
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate(timeout=3)

    def cli(self, command, *arguments, token=None, vault=None, exit_code=0,
            error_code=None, no_network=False, trace=None):
        require(command in ("file-upload", "file-info"))
        target_vault = self.vault if vault is None else vault
        require(target_vault in (self.vault, self.vault + "-denied"))
        process = subprocess.Popen(
            [sys.executable, "-B", "-c", CHILD_PROGRAM, str(SCRIPT), command,
             *arguments, "--vault", target_vault],
            env=self.environment(self.manual_token if token is None else token,
                                 target_vault, no_network),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=SCRIPT.parent.parent, start_new_session=True)
        try:
            output, diagnostics = process.communicate(timeout=CLI_TIMEOUT)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            self.stop_child(process)
            raise CheckFailure() from None
        value = json.loads(output.decode("utf-8", "strict"))
        safe_code = value.get("error", {}).get("code") if isinstance(value, dict) else None
        lines = [line[len(TRACE_PREFIX):] for line in diagnostics.splitlines()
                 if line.startswith(TRACE_PREFIX)]
        require(len(lines) == 1)
        observed = json.loads(lines[0])
        if observed.get("blocked") is not False or observed.get("requests") != trace:
            failure = CheckFailure(code=safe_code, stage="cli_request_trace", cli_exit=process.returncode)
            types = [line.split(b"=", 1)[1] for line in diagnostics.splitlines()
                     if line.startswith(b"FNS_UPLOAD_TRANSPORT_TYPES=")]
            if types:
                failure.diagnostics["transport_types"] = json.loads(types[-1])
            raise failure
        if process.returncode != exit_code:
            raise CheckFailure(code=safe_code, stage="cli_exit", cli_exit=process.returncode)
        require(isinstance(value, dict))
        if exit_code == 0:
            require(set(value) == {"success", "code", "data"} and value["success"] is True)
            require(type(value["code"]) is int and value["code"] in range(1, 7))
            return value["data"]
        require(set(value) == {"success", "error"} and value["success"] is False)
        error = value["error"]
        require(isinstance(error, dict) and isinstance(error.get("message"), str))
        if error_code is not None:
            require(type(error.get("code")) is int and error["code"] == error_code)
        if exit_code == 2:
            require(error.get("type") == "usage")
        elif exit_code == 6:
            require(error.get("type") == "precondition")
        elif exit_code == 5:
            require(error.get("type") == "business")
        return None

    def missing(self, path):
        result = self.raw("GET", "/api/file/info", token=self.manual_token, client=CLIENT,
                          params={"vault": self.vault, "path": path})
        require(result["code"] == 0 and result.get("status") is False)
        require(result.get("details") == "record not found")

    def metadata_for(self, path):
        data = self.cli("file-info", "--path", path, trace=[INFO])
        require(isinstance(data, dict) and all(field in data for field in META_FIELDS))
        require(data["path"] == path)
        require(type(data["id"]) is int and data["id"] > 0)
        require(type(data["size"]) is int and data["size"] >= 0)
        for field in ("ctime", "mtime"):
            require(type(data[field]) is int and data[field] > 0)
        for field in ("pathHash", "contentHash"):
            require(isinstance(data[field], str) and re.fullmatch(r"-?[0-9]+", data[field]) is not None)
        return {field: data[field] for field in META_FIELDS}

    def compare_download(self, path, expected):
        require(path in self.payloads and path.startswith(self.prefix))
        actual = self.raw("GET", "/api/file", token=self.manual_token, client=CLIENT,
                          params={"vault": self.vault, "path": path}, binary=True)
        equal = len(actual) == len(expected) and hashlib.sha256(actual).digest() == hashlib.sha256(expected).digest()
        if not equal:
            self.sha256_match = False
        require(equal)

    def list_rows(self, endpoint, *, recycle=False):
        require(endpoint in ("/api/files", "/api/notes"))
        # There are at most five fixtures; requesting 100 still checks the full
        # total, not a filtered prefix or an assumed first-page-only snapshot.
        token, client = ((self.manual_token, CLIENT) if endpoint == "/api/files"
                         else (self.session_token, "webgui"))
        data = self.success(self.raw("GET", endpoint, token=token, client=client, params={
            "vault": self.vault, "page": 1, "pageSize": 100, "sortBy": "path",
            "sortOrder": "asc", "isRecycle": "true" if recycle else "false"}))
        require(isinstance(data, dict) and isinstance(data.get("pager"), dict))
        pager, rows = data["pager"], data.get("list")
        require(type(pager.get("totalRows")) is int and 0 <= pager["totalRows"] <= 100)
        require(pager.get("page") == 1 and pager.get("pageSize") == 100)
        if rows is None:
            require(pager["totalRows"] == 0)
            rows = []
        require(isinstance(rows, list) and len(rows) == pager["totalRows"])
        require(all(isinstance(row, dict) and isinstance(row.get("path"), str) for row in rows))
        require(len({row["path"] for row in rows}) == len(rows))
        return rows

    def initially_empty(self):
        require(self.list_rows("/api/files") == [])
        require(self.list_rows("/api/files", recycle=True) == [])
        require(self.list_rows("/api/notes") == [])
        require(self.list_rows("/api/notes", recycle=True) == [])

    def upload(self, label, payload, *, timestamps=False):
        path = self.prefix + label
        require(path not in self.payloads)
        self.missing(path)  # Pin the real code-0/details absence shape first.
        self.cli("file-info", "--path", path, exit_code=5, error_code=0, trace=[INFO])
        local = self.directory / ("fixture-" + str(self.fixture_count))
        local.write_bytes(payload)
        arguments = ["--file", str(local), "--path", path]
        if timestamps:
            arguments.extend(("--ctime", "1600000000123", "--mtime", "1600000000456"))
        ack = self.cli("file-upload", *arguments, trace=[INFO, UPLOAD, INFO])
        require(isinstance(ack, dict) and all(field in ack for field in META_FIELDS))
        info = self.metadata_for(path)
        require({field: ack[field] for field in META_FIELDS} == info and info["size"] == len(payload))
        if timestamps:
            require(info["ctime"] == 1600000000123 and info["mtime"] == 1600000000456)
        self.payloads[path], self.metadata[path] = payload, info
        self.fixture_count += 1
        self.compare_download(path, payload)

    def binary(self):
        self.upload("binary.bin", bytes(range(256)) * 17 + secrets.token_bytes(73), timestamps=True)

    def png(self):
        self.upload("pixel.png", base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR4nGNgAAIAAAUAAXpeqz8AAAAASUVORK5CYII=",
            validate=True))

    def unicode(self):
        self.upload("中文 空格/附件 😀.bin", "附件内容\x00\r\n😀".encode("utf-8") + b"\xff\xfe")

    def empty(self):
        self.upload("empty.bin", b"")

    def boundary(self):
        self.upload("exactly-10MiB.bin", bytes(range(256)) * (LIMIT // 256))

    def duplicate(self):
        path = self.prefix + "binary.bin"
        replacement = self.directory / "replacement.bin"
        replacement.write_bytes(b"replacement\x00\xff" + secrets.token_bytes(128))
        self.cli("file-upload", "--file", str(replacement), "--path", path,
                 exit_code=6, trace=[INFO])
        require(self.metadata_for(path) == self.metadata[path])
        self.compare_download(path, self.payloads[path])

    def overwrite(self):
        path = self.prefix + "binary.bin"
        payload = (self.directory / "replacement.bin").read_bytes()
        ack = self.cli("file-upload", "--file", str(self.directory / "replacement.bin"),
                       "--path", path, "--overwrite", "--ctime", "1600000000789",
                       "--mtime", "1600000000999", trace=[INFO, UPLOAD, INFO])
        info = self.metadata_for(path)
        require(isinstance(ack, dict) and all(field in ack for field in META_FIELDS))
        require({field: ack[field] for field in META_FIELDS} == info)
        require(info["id"] == self.metadata[path]["id"] and info["pathHash"] == self.metadata[path]["pathHash"])
        require(info["size"] == len(payload) and info["ctime"] == 1600000000789 and info["mtime"] == 1600000000999)
        self.payloads[path], self.metadata[path] = payload, info
        self.compare_download(path, payload)

    def oversize(self):
        local = self.directory / "over-limit.bin"
        with local.open("wb") as fixture:
            fixture.truncate(LIMIT + 1)
        path = self.prefix + "over-limit.bin"
        self.cli("file-upload", "--file", str(local), "--path", path,
                 exit_code=2, no_network=True, trace=[])
        self.missing(path)

    def invalid_paths(self):
        local = self.directory / "fixture-0"
        for path in ("", "/absolute.bin", "../escape.bin", "a/../escape.bin",
                     "a//empty-component.bin", "./dot.bin", "a\\windows.bin", "bare %.bin"):
            self.cli("file-upload", "--file", str(local), "--path", path,
                     exit_code=2, no_network=True, trace=[])
            self.cli("file-info", "--path", path, exit_code=2, no_network=True, trace=[])

    def readonly(self):
        self.readonly_token = self.issue_token("file_r")
        path = self.prefix + "readonly-denied.bin"
        self.cli("file-upload", "--file", str(self.directory / "fixture-0"), "--path", path,
                 token=self.readonly_token, exit_code=5, error_code=315, trace=[INFO, UPLOAD])
        self.missing(path)
        existing = self.prefix + "binary.bin"
        self.cli("file-upload", "--file", str(self.directory / "fixture-0"), "--path", existing,
                 "--overwrite", token=self.readonly_token,
                 exit_code=5, error_code=315, trace=[INFO, UPLOAD])
        require(self.metadata_for(existing) == self.metadata[existing])
        self.compare_download(existing, self.payloads[existing])

    def wrong_vault(self):
        # No second vault is created. 315 must happen at the GET precheck,
        # including --overwrite. The token cannot write outside our own vault.
        path = self.prefix + "wrong-vault-denied.bin"
        self.cli("file-info", "--path", path, vault=self.vault + "-denied",
                 exit_code=5, error_code=315, trace=[INFO])
        self.cli("file-upload", "--file", str(self.directory / "fixture-0"), "--path", path,
                 "--overwrite", vault=self.vault + "-denied",
                 exit_code=5, error_code=315, trace=[INFO])
        self.missing(path)

    def files_only(self):
        rows = self.list_rows("/api/files")
        require({row["path"] for row in rows} == set(self.payloads))
        require(self.list_rows("/api/files", recycle=True) == [])
        for row in rows:
            require({field: row[field] for field in META_FIELDS} == self.metadata[row["path"]])

    def retained_hashes(self):
        require(self.fixture_count == 5)
        for path, payload in self.payloads.items():
            self.compare_download(path, payload)
        self.sha256_match = True

    def notes_empty(self):
        # Positive inspection uses webgui, not a falsely broadened manual token.
        active, recycled = self.list_rows("/api/notes"), self.list_rows("/api/notes", recycle=True)
        self.notes_untouched = active == [] and recycled == []
        require(self.notes_untouched)

    def run(self):
        cases = [("register_login_create_unique_vault_manual_file_rw", self.bootstrap),
                 ("new_vault_active_and_recycle_empty", self.initially_empty),
                 ("binary_ack_timestamps_get_post_get_sha256", self.binary),
                 ("png_ack_get_post_get_sha256", self.png),
                 ("chinese_spaces_emoji_path_and_bytes", self.unicode),
                 ("empty_file_ack_and_download", self.empty),
                 ("exact_10MiB_accepted", self.boundary),
                 ("duplicate_exit6_original_unchanged", self.duplicate),
                 ("overwrite_precheck_ack_sha256", self.overwrite),
                 ("over_10MiB_local_rejection_no_network", self.oversize),
                 ("invalid_paths_local_rejection_no_network", self.invalid_paths),
                 ("readonly_token315_no_creation_or_overwrite", self.readonly),
                 ("wrong_vault315_even_overwrite_precheck_only", self.wrong_vault),
                 ("own_vault_files_list_exact_fixtures", self.files_only),
                 ("all_retained_downloads_full_sha256", self.retained_hashes),
                 ("own_vault_notes_active_recycle_untouched", self.notes_empty)]
        aborted = False
        try:
            with tempfile.TemporaryDirectory(prefix="fns-upload-fixtures-") as directory:
                self.directory = Path(directory)
                for name, check in cases:
                    if aborted:
                        self.checks.append({"name": name, "status": "skip"})
                        continue
                    try:
                        check()
                    except (Exception, KeyboardInterrupt) as failure:
                        entry = {"name": name, "status": "fail", "reason": FAIL_REASON}
                        if isinstance(failure, CheckFailure):
                            entry.update(failure.diagnostics)
                        self.checks.append(entry)
                        aborted = True
                    else:
                        self.checks.append({"name": name, "status": "pass"})
        except (Exception, KeyboardInterrupt):
            self.checks.append({"name": "local_fixture_lifecycle", "status": "fail", "reason": FAIL_REASON})
        finally:
            # No credentials are returned or saved. Instance destruction is main's job.
            self.session_token = self.manual_token = self.readonly_token = None
        return report(self.checks, self.fixture_count, self.sha256_match, self.notes_untouched)


def main(argv=None):
    try:
        arguments = parser().parse_args(argv)
        require(arguments.confirm_isolated_instance)
        origin, host, port = checked_origin(arguments.base_url)
        require(sys.version_info >= (3, 10) and os.name == "posix" and SCRIPT.is_file())
        # Dependency checks are after --help and all guards, before any live request.
        import requests
        import socks
        import urllib3
        require((2, 34, 2) <= tuple(int(part) for part in requests.__version__.split(".")[:3]) < (3, 0, 0))
        require((2, 8, 0) <= tuple(int(part) for part in urllib3.__version__.split(".")[:3]) < (3, 0, 0))
        require(callable(socks.socksocket))
        runner = IsolatedChecks(origin, host, port)
    except (Exception, KeyboardInterrupt):
        report([{"name": "isolation_and_prerequisites", "status": "fail",
                 "reason": "Require explicit numeric-loopback HTTP origin, isolation confirmation, "
                           "POSIX Python 3.10+, and CLI dependencies. No live calls made."}])
        return 2
    try:
        return runner.run()
    except (Exception, KeyboardInterrupt):
        runner.checks.append({"name": "runner", "status": "fail", "reason": FAIL_REASON})
        return report(runner.checks, runner.fixture_count, runner.sha256_match, runner.notes_untouched)


if __name__ == "__main__":
    sys.exit(main())
