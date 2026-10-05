"""Local CLI contract tests for Fast Note Sync Service 3.6.1 (7a6c787).

Run with: python3 -B -m unittest discover -s obsidian-fastnotesync-skill/tests -v
Only loopback HTTP and a fake curl are used. No credentials or proxy settings
are inherited from the user's environment. The mock records requests and emits
scripted replies; it deliberately does not implement note-server semantics.
A missing scripts/fns.sh fails the suite rather than producing skipped success.
"""

import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "fns.sh"
TOKEN = "synthetic-fns-token-ONLY-FOR-TESTS"
LOGIN_TOKEN = "synthetic-login-token-ONLY-FOR-TESTS"
VAULT = "synthetic-vault"
UNSET = object()
INVALID_UTF8_CASES = (
    ("invalid_single_byte", b'#title\ncaf\xe9\n'),
    ("above_unicode_maximum", b'#title\n\xf4\x90\x80\x80\n'),
    ("overlong_nul", b'#title\n\xc0\x80\n'),
    ("overlong_ascii", b'#title\n\xc1\xbf\n'),
    ("surrogate", b'#title\n\xed\xa0\x80\n'),
    ("truncated_three_byte", b'#title\n\xe2\x82'),
    ("truncated_four_byte", b'#title\n\xf0\x9f\x92'),
)


def envelope(data=None, code=1, **extra):
    return {"code": code, "status": code == 1, "data": data, **extra}


@dataclass
class Reply:
    body: bytes = b""
    status: int = 200
    headers: dict = field(default_factory=dict)
    disconnect: bool = False
    advertised_length: int = None

    @classmethod
    def json(cls, value, status=200, headers=None):
        return cls(json.dumps(value, ensure_ascii=False).encode("utf-8"),
                   status, headers or {})


@dataclass
class Request:
    method: str
    target: str
    headers: dict
    body: bytes

    @property
    def path(self):
        return urlsplit(self.target).path

    @property
    def query(self):
        return parse_qs(urlsplit(self.target).query, keep_blank_values=True)

    def json(self):
        return json.loads(self.body.decode("utf-8"))


class MockHTTP:
    """A response queue, not an implementation of the upstream application."""

    def __init__(self):
        self.requests = []
        self.replies = []
        self.lock = threading.Lock()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_):
                pass

            def handle_request(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                request = Request(self.command, self.path,
                                  {k.lower(): v for k, v in self.headers.items()}, body)
                with owner.lock:
                    owner.requests.append(request)
                    reply = (owner.replies.pop(0) if owner.replies else
                             Reply.json({"message": "Unscripted mock request"}, 500))
                if reply.disconnect:
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    self.close_connection = True
                    return
                self.send_response(reply.status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(
                    len(reply.body) if reply.advertised_length is None
                    else reply.advertised_length))
                self.send_header("Connection", "close")
                for key, value in reply.headers.items():
                    self.send_header(key, value)
                self.end_headers()
                try:
                    self.wfile.write(reply.body)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                self.close_connection = True

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = handle_request

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(
            target=lambda: self.server.serve_forever(poll_interval=0.02), daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:{}".format(self.server.server_port)

    def script(self, *replies):
        with self.lock:
            self.requests.clear()
            self.replies[:] = replies

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


# This subprocess never opens a socket. It captures argv and curl config while
# they still exist, then implements only curl's output/write-out plumbing.
FAKE_CURL = r'''import json, os, re, stat, sys
from pathlib import Path

argv = sys.argv[1:]
options = []
configs = []
files = []

aliases = {"-K": "config", "-o": "output", "-w": "write-out",
           "-X": "request", "-H": "header", "-A": "user-agent",
           "-d": "data", "-x": "proxy", "-m": "max-time", "-G": "get",
           "-g": "globoff"}
value_options = {"config", "output", "write-out", "request", "header",
                 "user-agent", "data", "data-raw", "data-binary", "data-urlencode",
                 "proxy", "proxy-user", "noproxy", "url", "max-time", "connect-timeout",
                 "socks5", "socks5-hostname", "retry", "proto", "proto-redir"}

def unquote(value):
    value = value.strip()
    if value.startswith('"') and value.endswith('"'):
        value = value[1:-1]
        escapes = {"n": "\n", "r": "\r", "t": "\t", "v": "\v"}
        value = re.sub(r"\\(.)", lambda m: escapes.get(m[1], m[1]), value)
    return value

def config(path):
    p = Path(path)
    raw = p.read_text(encoding="utf-8")
    parsed = []
    configs.append({"path": str(p), "mode": stat.S_IMODE(p.stat().st_mode),
                    "parentMode": stat.S_IMODE(p.parent.stat().st_mode),
                    "text": raw, "options": parsed})
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.match(r"(?:--)?([a-zA-Z0-9-]+)(?:\s*=\s*|\s+)?(.*)$", line)
        if not match:
            raise SystemExit("Unsupported fake-curl config syntax")
        key, value = match.groups()
        option = [key, unquote(value)]
        options.append(option)
        parsed.append(option)

index = 0
while index < len(argv):
    arg = argv[index]
    key = aliases.get(arg, arg[2:] if arg.startswith("--") else arg)
    value = ""
    if key in value_options:
        index += 1
        value = argv[index]
    elif arg.startswith("--") and "=" in arg:
        key, value = arg[2:].split("=", 1)
    options.append([key, value])
    if key == "config":
        config(value)
    index += 1

for key, value in options:
    if key in {"data", "data-binary", "data-urlencode"} and value.startswith("@"):
        p = Path(value[1:])
        files.append({"path": str(p), "mode": stat.S_IMODE(p.stat().st_mode),
                      "parentMode": stat.S_IMODE(p.parent.stat().st_mode),
                      "text": p.read_text(encoding="utf-8")})

log = Path(os.environ["FAKE_CURL_LOG"])
previous = log.read_text().splitlines() if log.exists() else []
record = {"argv": argv, "options": options, "configs": configs, "files": files}
with log.open("a", encoding="utf-8") as stream:
    stream.write(json.dumps(record) + "\n")
replies = json.loads(Path(os.environ["FAKE_CURL_REPLIES"]).read_text())
reply = replies[len(previous)] if len(previous) < len(replies) else {
    "status": 500, "body": '{"message":"Unscripted fake-curl request"}'}
body = reply.get("body", "")
status = str(reply.get("status", 200)).zfill(3)
output = next((v for k, v in reversed(options) if k == "output"), None)
if output and output != "-":
    Path(output).write_bytes(body.encode("utf-8"))
else:
    sys.stdout.write(body)
writeout = next((v for k, v in reversed(options) if k == "write-out"), "")
writeout = writeout.replace("%{http_code}", status).replace("%{response_code}", status)
writeout = writeout.replace("%{num_redirects}", "0")
writeout = writeout.replace("%{size_download}", str(len(body.encode("utf-8"))))
sys.stdout.write(writeout)
sys.stderr.write(reply.get("stderr", ""))
sys.exit(reply.get("exit", 0))
'''


class CLIBase(unittest.TestCase):
    def setUp(self):
        self.assertTrue(SCRIPT.is_file(), "Required CLI script is missing: {}".format(SCRIPT))
        self.temp = tempfile.TemporaryDirectory(prefix="fns-contract-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.tmp = self.root / "tmp"
        self.home.mkdir(mode=0o700)
        self.tmp.mkdir(mode=0o700)
        self.addCleanup(lambda: self.assertEqual(
            list(self.tmp.iterdir()), [], "Temporary request files must be cleaned on every exit"))
        # Construct, do not copy os.environ: no real token, BASH_ENV, curlrc,
        # netrc, username, password, or host proxy can reach the child.
        tools = [shutil.which(name) for name in ("bash", "curl", "jq", "python3")]
        directories = [str(Path(tool).parent) for tool in tools if tool]
        path = os.pathsep.join(dict.fromkeys(directories + ["/usr/bin", "/bin"]))
        self.env = {"PATH": path, "HOME": str(self.home), "TMPDIR": str(self.tmp),
                    "LC_ALL": "C", "LANG": "C", "FNS_TOKEN": TOKEN,
                    "FNS_VAULT": VAULT}

    def run_cli(self, *args, env=None, stdin=None):
        child_env = self.env.copy()
        for key, value in (env or {}).items():
            if value is None:
                child_env.pop(key, None)
            else:
                child_env[key] = str(value)
        return subprocess.run(
            ["/bin/bash", str(SCRIPT), *args], env=child_env, cwd=self.root,
            input=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            encoding="utf-8", timeout=12, check=False)

    def output(self, result):
        try:
            value = json.loads(result.stdout)
        except (ValueError, TypeError) as error:
            self.fail("Expected exactly one JSON output, exit={} stdout={!r} stderr={!r}: {}"
                      .format(result.returncode, result.stdout, result.stderr, error))
        self.assertIsInstance(value, dict)
        return value

    def assert_success(self, result, data=UNSET, code=1):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        value = self.output(result)
        self.assertEqual(set(value), {"success", "code", "data"})
        self.assertIs(value["success"], True)
        self.assertIs(type(value["code"]), int)
        self.assertEqual(value["code"], code)
        if data is not UNSET:
            self.assertEqual(value["data"], data)
        return value

    def assert_failure(self, result, exit_code, code=UNSET, http_status=UNSET,
                       secrets=()):
        self.assertEqual(result.returncode, exit_code, result.stdout + result.stderr)
        value = self.output(result)
        self.assertEqual(set(value), {"success", "error"})
        self.assertIs(value["success"], False)
        error = value["error"]
        self.assertIsInstance(error, dict)
        self.assertTrue({"type", "message"}.issubset(error))
        self.assertLessEqual(set(error), {"type", "message", "code", "httpStatus"})
        self.assertIsInstance(error["type"], str)
        self.assertTrue(error["type"])
        self.assertIsInstance(error["message"], str)
        if code is not UNSET:
            self.assertIs(type(error.get("code")), int)
            self.assertEqual(error.get("code"), code)
        if http_status is not UNSET:
            self.assertEqual(error.get("httpStatus"), http_status)
        for secret in (TOKEN, LOGIN_TOKEN, *secrets):
            if secret:
                self.assertNotIn(secret, result.stdout + result.stderr + error["message"])
        return value

    def assert_no_secret_output(self, result, secrets):
        for secret in secrets:
            self.assertNotIn(secret, result.stdout + result.stderr)

        def inspect(value):
            if isinstance(value, str):
                for secret in secrets:
                    self.assertNotIn(secret, value)
            elif isinstance(value, list):
                for item in value:
                    inspect(item)
            elif isinstance(value, dict):
                for key, item in value.items():
                    inspect(key)
                    inspect(item)

        # Inspect decoded keys and values as well: JSON escaping can conceal a
        # quote/backslash credential from a raw stdout substring assertion.
        inspect(self.output(result))


class HTTPContractTests(CLIBase):
    def setUp(self):
        super().setUp()
        self.mock = MockHTTP()
        self.addCleanup(self.mock.close)
        self.env["FNS_BASE_URL"] = self.mock.url

    def reply(self, *values):
        self.mock.script(*(Reply.json(value) for value in values))

    def request(self, method, path, index=0, authenticated=True, client=None,
                token=TOKEN, user_agent="obsidian-fastnotesync-skill/2.0.0"):
        self.assertGreater(len(self.mock.requests), index)
        request = self.mock.requests[index]
        self.assertEqual(request.method, method)
        self.assertEqual(request.path, path)
        self.assertEqual(request.headers.get("user-agent"), user_agent)
        if authenticated:
            self.assertEqual(request.headers.get("authorization"), "Bearer " + token)
            self.assertEqual(request.headers.get("x-client"),
                             client or "obsidian-fastnotesync-skill")
        else:
            self.assertNotIn("authorization", request.headers)
        return request

    def query(self, request, expected, optional=None):
        actual = request.query
        for key, value in expected.items():
            self.assertEqual(actual.get(key), [str(value)], (key, actual))
        self.assertLessEqual(set(actual), set(expected) | set(optional or {}))
        for key, value in (optional or {}).items():
            if key in actual:
                self.assertEqual(actual[key], [str(value)])

    def body(self, request, expected, optional=()):
        self.assertEqual(request.query, {})
        self.assertTrue(request.headers.get("content-type", "").startswith(
            "application/json"))
        actual = request.json()
        self.assertIsInstance(actual, dict)
        for key, value in expected.items():
            self.assertIn(key, actual)
            self.assertEqual(actual[key], value, (key, actual))
            if isinstance(value, bool):
                self.assertIs(actual[key], value)
        self.assertLessEqual(set(actual), set(expected) | set(optional))
        return actual

    def test_public_health_and_version_without_credentials(self):
        for command in ("health", "version"):
            with self.subTest(command=command):
                self.reply(envelope({"marker": command}))
                result = self.run_cli(command, env={"FNS_TOKEN": None,
                    "FNS_VAULT": None, "FNS_AUTH_MODE": "password"})
                self.assert_success(result)
                self.assertEqual(len(self.mock.requests), 1)
                request = self.request("GET", "/api/" + command, authenticated=False)
                self.query(request, {})
                self.assertEqual(request.body, b"")

    def test_doctor_combines_public_requests_without_login(self):
        self.reply(envelope({"marker": "health-marker"}),
                   envelope({"marker": "version-marker"}))
        result = self.run_cli("doctor", env={"FNS_TOKEN": None, "FNS_VAULT": None})
        value = self.assert_success(result)
        self.assertEqual(len(self.mock.requests), 2)
        self.assertEqual(sorted(request.path for request in self.mock.requests),
                         ["/api/health", "/api/version"])
        for index, request in enumerate(self.mock.requests):
            self.request("GET", request.path, index, authenticated=False)
            self.query(request, {})
            self.assertEqual(request.body, b"")
        # Do not invent a doctor aggregation key layout.
        serialized = json.dumps(value["data"])
        self.assertIn("health-marker", serialized)
        self.assertIn("version-marker", serialized)

    def test_curlrc_is_ignored(self):
        (self.home / ".curlrc").write_text(
            'header = "X-Curlrc-Leak: must-not-be-sent"\n', encoding="utf-8")
        self.reply(envelope({}))
        self.assert_success(self.run_cli("health"))
        self.assertNotIn("x-curlrc-leak", self.mock.requests[0].headers)

    def test_base_url_port_glob_is_rejected_before_any_requests(self):
        other = MockHTTP()
        self.addCleanup(other.close)
        self.reply(envelope({"token": LOGIN_TOKEN}), envelope({}))
        other.script(Reply.json(envelope({"token": LOGIN_TOKEN})), Reply.json(envelope({})))
        base = "http://127.0.0.1:{{{},{}}}".format(
            self.mock.server.server_port, other.server.server_port)
        result = self.run_cli("read", "--path", "x.md", env={
            "FNS_BASE_URL": base, "FNS_AUTH_MODE": "password",
            "FNS_USERNAME": "synthetic-glob-user", "FNS_PASSWORD": "synthetic-glob-password"})
        self.assertEqual(self.mock.requests, [], "URL globbing must not send even one request")
        self.assertEqual(other.requests, [], "URL globbing must not contact a second server")
        self.assert_failure(result, 2, secrets=("synthetic-glob-password",))

    def test_explicit_http_proxy_overrides_both_no_proxy_environment_variables(self):
        proxy = MockHTTP()
        self.addCleanup(proxy.close)
        bypasses = ({"NO_PROXY": "*"}, {"no_proxy": "*"},
                    {"NO_PROXY": "*", "no_proxy": "*"})
        for bypass in bypasses:
            with self.subTest(bypass=bypass):
                self.reply(envelope({"token": LOGIN_TOKEN}), envelope({"via": "target"}))
                proxy.script(Reply.json(envelope({"token": LOGIN_TOKEN})),
                             Reply.json(envelope({"via": "proxy"})))
                result = self.run_cli("read", "--path", "x.md", env={
                    **bypass, "FNS_PROXY": proxy.url, "FNS_AUTH_MODE": "password",
                    "FNS_USERNAME": "synthetic-proxy-login-user",
                    "FNS_PASSWORD": "synthetic-proxy-login-password"})
                self.assertEqual(self.mock.requests, [],
                                 "An explicit FNS_PROXY must override inherited proxy bypasses")
                self.assert_success(result, {"via": "proxy"})
                self.assertEqual(len(proxy.requests), 2)
                login, business = proxy.requests
                self.assertEqual((login.method, login.path), ("POST", "/api/user/login"))
                self.assertEqual((business.method, business.path), ("GET", "/api/note"))
                for request in proxy.requests:
                    self.assertTrue(request.target.startswith(self.mock.url + "/api/"),
                                    "The HTTP proxy must receive the absolute target URL")
                    self.assertEqual(request.headers.get("x-client"), "webgui")
                    self.assertEqual(request.headers.get("user-agent"),
                                     "obsidian-fastnotesync-skill/2.0.0")
                self.assertNotIn("authorization", login.headers)
                self.assertEqual(business.headers.get("authorization"), "Bearer " + LOGIN_TOKEN)

    def test_vaults_is_authenticated_but_needs_no_vault(self):
        data = [{"name": "one"}, {"name": "two"}]
        self.reply(envelope(data))
        self.assert_success(self.run_cli("vaults", env={"FNS_VAULT": None}), data)
        self.assertEqual(len(self.mock.requests), 1)
        self.query(self.request("GET", "/api/vault"), {})

    def test_global_vault_anywhere_overrides_environment(self):
        vault = "柜/space &+?#%=仓"
        variants = [("--vault", vault, "read", "--path", "x.md"),
                    ("read", "--vault", vault, "--path", "x.md"),
                    ("read", "--path", "x.md", "--vault", vault)]
        for args in variants:
            with self.subTest(args=args):
                self.reply(envelope({}))
                self.assert_success(self.run_cli(*args))
                self.assertEqual(len(self.mock.requests), 1)
                self.query(self.request("GET", "/api/note"),
                           {"vault": vault, "path": "x.md"}, {"isRecycle": "false"})

    def test_list_and_search_query_contract(self):
        keyword = '会议 & +#?%= "\\"\n另行'
        for command in ("list", "search"):
            for mode in ("path", "content"):
                with self.subTest(command=command, mode=mode):
                    self.reply(envelope({"list": [], "total": 0}))
                    self.assert_success(self.run_cli(
                        command, "--keyword", keyword, "--search-mode", mode,
                        "--page", "2", "--page-size", "100", "--sort-by", "ctime",
                        "--sort-order", "asc", "--recycle"))
                    self.assertEqual(len(self.mock.requests), 1)
                    request = self.request("GET", "/api/notes")
                    self.query(request, {"vault": VAULT, "keyword": keyword,
                        "searchMode": mode, "page": "2", "pageSize": "100",
                        "sortBy": "ctime", "sortOrder": "asc", "isRecycle": "true"})
                    self.assertNotIn("searchContent", request.query)
                    self.assertEqual(request.body, b"")

    def test_list_defaults_to_path_search(self):
        self.reply(envelope({"list": []}))
        self.assert_success(self.run_cli("list", "--keyword", "todo"))
        request = self.request("GET", "/api/notes")
        self.assertEqual(request.query.get("searchMode"), ["path"])
        self.assertNotIn("searchContent", request.query)

    def test_search_defaults_to_content_mode_not_search_content(self):
        self.reply(envelope({"list": []}))
        self.assert_success(self.run_cli("search", "--keyword", "todo"))
        request = self.request("GET", "/api/notes")
        self.assertEqual(request.query.get("searchMode"), ["content"])
        self.assertNotIn("searchContent", request.query)
        self.assertEqual(request.query.get("keyword"), ["todo"])

    def test_list_pagination_boundaries_and_sort_fields(self):
        for size in (1, 100):
            for sort_by in ("mtime", "ctime", "path"):
                for order in ("asc", "desc"):
                    with self.subTest(size=size, sort_by=sort_by, order=order):
                        self.reply(envelope({"list": []}))
                        self.assert_success(self.run_cli("list", "--page-size", str(size),
                            "--sort-by", sort_by, "--sort-order", order))
                        query = self.request("GET", "/api/notes").query
                        self.assertEqual(query["pageSize"], [str(size)])
                        self.assertEqual(query["sortBy"], [sort_by])
                        self.assertEqual(query["sortOrder"], [order])

    def test_list_and_search_normalize_null_lists_only_with_zero_pager_count(self):
        for command in ("list", "search"):
            for recycle in (False, True):
                for code in (1, 6):
                    with self.subTest(command=command, recycle=recycle, code=code):
                        data = {"list": None,
                                "pager": {"page": 1, "pageSize": 10, "totalRows": 0},
                                "extra": {"list": None, "values": [None]}}
                        self.reply({"code": code, "status": True, "data": data})
                        arguments = [command, "--keyword", "synthetic-no-match"]
                        if recycle:
                            arguments.append("--recycle")
                        expected = {**data, "list": []}
                        self.assert_success(self.run_cli(*arguments), expected, code=code)
                        self.assertEqual(len(self.mock.requests), 1)
                        request = self.request("GET", "/api/notes")
                        self.assertEqual(request.query["searchMode"],
                                         ["content" if command == "search" else "path"])
                        self.assertEqual(request.query["isRecycle"], [str(recycle).lower()])

    def test_list_and_search_leave_array_payloads_and_metadata_unchanged(self):
        rows = [{"path": "目录/space + & %.md", "ctime": 0, "mtime": 123, "extra": None},
                {"path": "other.md", "nested": {"list": None}, "flags": [False, None]}]
        payloads = [{"list": []},
                    {"list": [], "pager": {"page": 1, "pageSize": 10, "totalRows": 0}},
                    {"list": rows, "pager": {"page": 1, "pageSize": 10, "totalRows": 2},
                     "extra": {"list": None, "values": [None]}}]
        for command in ("list", "search"):
            for data in payloads:
                with self.subTest(command=command, data=data):
                    self.reply(envelope(data))
                    self.assert_success(self.run_cli(command, "--keyword", "synthetic"), data)
                    self.assertEqual(len(self.mock.requests), 1)
                    self.request("GET", "/api/notes")

    def test_list_and_search_reject_malformed_data_or_list_shapes(self):
        payloads = [UNSET, None, False, True, 0, "string", [], [{"list": []}], {},
                    {"pager": {"totalRows": 0}}, {"notes": [], "pager": {"totalRows": 0}}]
        payloads += [{"list": value, "pager": {"totalRows": 0}}
                     for value in (False, True, 0, 1, "", "[]", {}, {"path": "x.md"})]
        for command in ("list", "search"):
            for data in payloads:
                with self.subTest(command=command, data=data):
                    response = {"code": 1, "status": True} if data is UNSET else envelope(data)
                    self.reply(response)
                    self.assert_failure(self.run_cli(command, "--keyword", "synthetic"), 4)
                    self.assertEqual(len(self.mock.requests), 1)
                    self.request("GET", "/api/notes")

    def test_list_and_search_reject_null_lists_without_explicit_zero_pager_count(self):
        pagers = [UNSET, None, False, True, 0, "string", [], {},
                  {"page": 1, "pageSize": 10}, {"total": 0}]
        pagers += [{"totalRows": value} for value in (None, False, True, "0", -1, 1, 100, [], {})]
        payloads = [{"list": None, "total": 0, "totalRows": 0} if pager is UNSET
                    else {"list": None, "pager": pager} for pager in pagers]
        for command in ("list", "search"):
            for data in payloads:
                with self.subTest(command=command, data=data):
                    self.reply(envelope(data))
                    self.assert_failure(self.run_cli(command, "--keyword", "synthetic"), 4)
                    self.assertEqual(len(self.mock.requests), 1)
                    self.request("GET", "/api/notes")

    def test_read_and_delete_escape_path_and_hash(self):
        path = '目录/space + &?#%= "quoted"\\.md'
        path_hash = "hash+&?#%=值"
        for command, method in (("read", "GET"), ("delete", "DELETE")):
            with self.subTest(command=command):
                self.reply(envelope({"path": path, "content": "body"}))
                args = [command, "--path", path, "--path-hash", path_hash]
                if command == "read":
                    args.append("--recycle")
                self.assert_success(self.run_cli(*args))
                self.assertEqual(len(self.mock.requests), 1)
                expected = {"vault": VAULT, "path": path, "pathHash": path_hash}
                if command == "read":
                    expected["isRecycle"] = "true"
                request = self.request(method, "/api/note")
                self.query(request, expected)
                self.assertEqual(request.body, b"")

    def test_restore_sends_put_json(self):
        self.reply(envelope({"path": "old.md"}))
        self.assert_success(self.run_cli("restore", "--path", "old.md"))
        self.assertEqual(len(self.mock.requests), 1)
        self.body(self.request("PUT", "/api/note/restore"),
                  {"vault": VAULT, "path": "old.md"})

    def test_create_and_upsert_are_distinct_and_do_not_preread(self):
        for command, create_only in (("create", True), ("upsert", False)):
            with self.subTest(command=command):
                self.reply(envelope({"path": "new.md"}))
                self.assert_success(self.run_cli(command, "--path", "new.md",
                    "--content", "Unicode 文\n\"\\", "--ctime", "1234567890",
                    "--mtime", "1234567999", "--path-hash", "hash-new"))
                self.assertEqual(len(self.mock.requests), 1)
                self.body(self.request("POST", "/api/note"),
                    {"vault": VAULT, "path": "new.md", "content": "Unicode 文\n\"\\",
                     "ctime": 1234567890, "mtime": 1234567999,
                     "pathHash": "hash-new", "createOnly": create_only})

    def test_content_sources_preserve_final_newlines_and_empty_content(self):
        for command in ("create", "upsert", "update", "append", "prepend"):
            for source in ("--content", "--content-file", "--stdin"):
                for content in ("", '文\r\nline "\\\n\n'):
                    with self.subTest(command=command, source=source, content=content):
                        if command == "update":
                            self.reply(envelope({"ctime": 123, "mtime": 456,
                                                "contentHash": "existing"}), envelope({}))
                        else:
                            self.reply(envelope({}))
                        args = [command, "--path", "content.md", source]
                        stdin = None
                        if source == "--content":
                            args.append(content)
                        elif source == "--content-file":
                            file = self.root / "content with spaces.md"
                            file.write_bytes(content.encode("utf-8"))
                            args.append(str(file))
                        else:
                            stdin = content
                        self.assert_success(self.run_cli(*args, stdin=stdin))
                        index = 1 if command == "update" else 0
                        self.assertEqual(len(self.mock.requests), index + 1)
                        endpoint = "/api/note" if command in ("create", "upsert", "update") else (
                            "/api/note/" + command)
                        expected = {"vault": VAULT, "path": "content.md", "content": content}
                        if command in ("create", "upsert", "update"):
                            expected["createOnly"] = command == "create"
                        if command == "update":
                            expected["ctime"] = 123
                            self.query(self.request("GET", "/api/note"),
                                       {"vault": VAULT, "path": "content.md"},
                                       {"isRecycle": "false"})
                        self.body(self.request("POST", endpoint, index), expected)

    def test_update_prereads_existing_and_preserves_creation_time(self):
        for override in (False, True):
            with self.subTest(override=override):
                self.reply(envelope({"path": "x.md", "ctime": 1234567890,
                    "mtime": 9876543210, "contentHash": "old-hash", "content": "old"}),
                    envelope({"content": "new"}))
                args = ["update", "--path", "x.md", "--path-hash", "path-hash",
                        "--content", "new", "--mtime", "9876543222",
                        "--expect-hash", "old-hash"]
                if override:
                    args += ["--ctime", "1111111111"]
                self.assert_success(self.run_cli(*args))
                self.assertEqual(len(self.mock.requests), 2)
                self.query(self.request("GET", "/api/note", 0),
                    {"vault": VAULT, "path": "x.md", "pathHash": "path-hash"},
                    {"isRecycle": "false"})
                self.body(self.request("POST", "/api/note", 1),
                    {"vault": VAULT, "path": "x.md", "pathHash": "path-hash",
                     "content": "new", "ctime": 1111111111 if override else 1234567890,
                     "mtime": 9876543222, "createOnly": False})

    def test_literal_content_may_begin_with_yaml_frontmatter(self):
        content = '---\ntitle: "文"\n---\n# Body\n\n'
        self.reply(envelope({}))
        self.assert_success(self.run_cli("create", "--path", "yaml.md", "--content", content))
        self.assertEqual(len(self.mock.requests), 1)
        self.body(self.request("POST", "/api/note"),
                  {"vault": VAULT, "path": "yaml.md", "content": content, "createOnly": True})

    def test_invalid_utf8_content_file_is_rejected_before_login_or_network(self):
        file = self.root / "invalid-utf8.md"
        for command in ("create", "upsert", "update", "append", "prepend"):
            for encoding, payload in INVALID_UTF8_CASES:
                with self.subTest(command=command, encoding=encoding):
                    file.write_bytes(payload)
                    self.reply(envelope({"token": LOGIN_TOKEN}), envelope({}))
                    result = self.run_cli(command, "--path", "x.md", "--content-file", str(file), env={
                        "FNS_AUTH_MODE": "password", "FNS_USERNAME": "synthetic-utf8-user",
                        "FNS_PASSWORD": "synthetic-utf8-password"})
                    self.assertFalse(bool(self.mock.requests),
                                     "Invalid UTF-8 must be rejected before any login or note request")
                    self.assert_failure(result, 2, secrets=("synthetic-utf8-password",))

    def test_invalid_utf8_stdin_is_rejected_before_login_or_network(self):
        child_env = self.env.copy()
        child_env.update({"FNS_AUTH_MODE": "password", "FNS_USERNAME": "synthetic-utf8-user",
                          "FNS_PASSWORD": "synthetic-utf8-password"})
        for command in ("create", "upsert", "update", "append", "prepend"):
            for encoding, payload in INVALID_UTF8_CASES:
                with self.subTest(command=command, encoding=encoding):
                    self.reply(envelope({"token": LOGIN_TOKEN}), envelope({}))
                    # Deliberately bypass text-mode run_cli so Python cannot decode
                    # or normalize the malformed bytes before the CLI sees them.
                    raw = subprocess.run(
                        ["/bin/bash", str(SCRIPT), command, "--path", "x.md", "--stdin"],
                        env=child_env, cwd=self.root, input=payload,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=12, check=False)
                    self.assertFalse(bool(self.mock.requests),
                                     "Invalid UTF-8 must be rejected before any login or note request")
                    try:
                        stdout, stderr = raw.stdout.decode("utf-8"), raw.stderr.decode("utf-8")
                    except UnicodeDecodeError:
                        self.fail("Invalid-input diagnostics must themselves be valid UTF-8")
                    result = subprocess.CompletedProcess(raw.args, raw.returncode, stdout, stderr)
                    self.assert_failure(result, 2, secrets=("synthetic-utf8-password",))

    def test_valid_unicode_replacement_and_maximum_scalars_are_preserved(self):
        for command in ("create", "upsert", "update", "append", "prepend"):
            for source in ("--content", "--content-file", "--stdin"):
                for name, character in (("U+FFFD", "\ufffd"), ("U+10FFFF", "\U0010ffff")):
                    with self.subTest(command=command, source=source, scalar=name):
                        content = "# Valid UTF-8 scalar\n" + character + "\n\n"
                        if command == "update":
                            self.reply(envelope({"ctime": 123, "contentHash": "existing"}),
                                       envelope({}))
                        else:
                            self.reply(envelope({}))
                        arguments = [command, "--path", "scalar.md", source]
                        stdin = None
                        if source == "--content":
                            arguments.append(content)
                        elif source == "--content-file":
                            file = self.root / "valid-utf8.md"
                            file.write_bytes(content.encode("utf-8"))
                            arguments.append(str(file))
                        else:
                            stdin = content
                        self.assert_success(self.run_cli(*arguments, stdin=stdin))
                        index = 1 if command == "update" else 0
                        self.assertEqual(len(self.mock.requests), index + 1)
                        expected = {"vault": VAULT, "path": "scalar.md", "content": content}
                        endpoint = "/api/note/" + command
                        if command in ("create", "upsert", "update"):
                            endpoint = "/api/note"
                            expected["createOnly"] = command == "create"
                        if command == "update":
                            expected["ctime"] = 123
                            self.query(self.request("GET", "/api/note"),
                                       {"vault": VAULT, "path": "scalar.md"},
                                       {"isRecycle": "false"})
                        actual = self.body(self.request("POST", endpoint, index), expected)
                        self.assertEqual(actual["content"].encode("utf-8"), content.encode("utf-8"),
                                         "Valid scalars and trailing newlines must round-trip exactly")

    def test_reserved_credential_names_preserve_success_envelope_and_redact_payload(self):
        safe = {"text": "ordinary 文", "flag": False, "count": 0, "none": None}
        for username in ("data", "success", "code"):
            with self.subTest(username=username):
                data = {username: username, "safe": safe,
                        "nested": [{username: "prefix:" + username + ":suffix"},
                                   {"value": TOKEN + " " + username}]}
                expected = {"[REDACTED]": "[REDACTED]", "safe": safe,
                            "nested": [{"[REDACTED]": "prefix:[REDACTED]:suffix"},
                                       {"value": "[REDACTED] [REDACTED]"}]}
                self.reply(envelope(data))
                result = self.run_cli("read", "--path", "x.md", env={
                    "FNS_AUTH_MODE": "token", "FNS_USERNAME": username})
                # Reserved top-level fields are client-owned, not payload keys:
                # do not run a whole-envelope secret-substring assertion here.
                self.assert_success(result, expected)
                self.assertNotIn(TOKEN, result.stdout + result.stderr)
                self.assertEqual(len(self.mock.requests), 1)
                self.request("GET", "/api/note")

    def test_reserved_credential_names_preserve_generic_error_envelopes(self):
        for username in ("data", "success", "code", "error", "type", "message"):
            for status, exit_code in ((200, 5), (500, 3)):
                with self.subTest(username=username, status=status):
                    echoed = "synthetic-upstream-echo-" + username
                    self.mock.script(Reply.json(envelope(
                        None, code=305, message=echoed + " " + TOKEN), status=status))
                    result = self.run_cli("read", "--path", "x.md", env={
                        "FNS_AUTH_MODE": "token", "FNS_USERNAME": username})
                    # Generic owned diagnostics/field names may coincide with a
                    # credential. Only the upstream echo must be suppressed.
                    self.assert_failure(result, exit_code,
                                        code=305 if status == 200 else UNSET,
                                        http_status=status)
                    self.assertNotIn(echoed, result.stdout + result.stderr)
                    self.assertEqual(len(self.mock.requests), 1)
                    self.request("GET", "/api/note")

    def test_empty_append_and_prepend_forward_explicit_content_and_business_error(self):
        for command in ("append", "prepend"):
            with self.subTest(command=command):
                self.reply(envelope(None, code=305, message="content is required"))
                result = self.run_cli(command, "--path", "x.md", "--content", "")
                self.assert_failure(result, 5, code=305)
                self.assertEqual(len(self.mock.requests), 1)
                self.body(self.request("POST", "/api/note/" + command),
                          {"vault": VAULT, "path": "x.md", "content": ""})

    def test_update_missing_or_hash_mismatch_never_writes(self):
        cases = [(envelope(None, code=430, message="not found"), 5, 430),
                 (envelope({"ctime": 123, "contentHash": "other"}), 6, UNSET),
                 (envelope({"ctime": 123}), 6, UNSET)]
        for response, exit_code, code in cases:
            with self.subTest(response=response):
                self.reply(response, envelope({}))
                result = self.run_cli("update", "--path", "x.md", "--content", "new",
                                      "--expect-hash", "expected")
                self.assert_failure(result, exit_code, code=code)
                self.assertEqual(len(self.mock.requests), 1)
                self.request("GET", "/api/note")

    def test_update_requires_existing_note_even_without_hash_option(self):
        self.reply(envelope(None, code=430, message="missing"), envelope({}))
        self.assert_failure(self.run_cli("update", "--path", "x.md", "--content", ""),
                            5, code=430)
        self.assertEqual(len(self.mock.requests), 1)
        self.request("GET", "/api/note")

    def test_update_rejects_invalid_existing_note_metadata_before_write(self):
        for data in (None, [], "not-a-note", {}, {"ctime": None}, {"ctime": "bad"}):
            with self.subTest(data=data):
                self.reply(envelope(data), envelope({}))
                self.assert_failure(self.run_cli("update", "--path", "x.md",
                                                "--content", "new"), 4)
                self.assertEqual(len(self.mock.requests), 1)
                self.request("GET", "/api/note")

    def test_update_preread_failure_stops_write(self):
        cases = [(Reply.json(envelope({}), status=503), 3),
                 (Reply(b"not json"), 4), (Reply(disconnect=True), 3)]
        for response, exit_code in cases:
            with self.subTest(exit_code=exit_code, response=response):
                self.mock.script(response, Reply.json(envelope({})))
                self.assert_failure(self.run_cli("update", "--path", "x.md",
                                                "--content", "new"), exit_code)
                self.assertEqual(len(self.mock.requests), 1)

    def test_replace_flags_and_explicit_empty_replacement(self):
        for flags in ([], ["--regex", "--all", "--fail-if-no-match"]):
            with self.subTest(flags=flags):
                self.reply(envelope({"replacementCount": 1}))
                self.assert_success(self.run_cli("replace", "--path", "x.md",
                    "--find", r"a\s+文", "--replace", "", *flags))
                expected = {"vault": VAULT, "path": "x.md", "find": r"a\s+文", "replace": ""}
                expected.update({"regex": True, "all": True, "failIfNoMatch": True}
                                if flags else {})
                actual = self.body(self.request("POST", "/api/note/replace"), expected,
                                   optional={"regex", "all", "failIfNoMatch"})
                if not flags:
                    for key in ("regex", "all", "failIfNoMatch"):
                        if key in actual:
                            self.assertIs(actual[key], False)

    def test_frontmatter_patch_preserves_json_types(self):
        updates = {"title": '文 "\\\n', "done": True, "count": 3,
                   "tags": ["a", "b"], "nested": {"null": None}}
        remove = ["draft", "旧字段"]
        variants = [("--updates", json.dumps(updates), "--remove", json.dumps(remove)),
                    ("--updates", "{}"), ("--remove", "[]")]
        for options in variants:
            with self.subTest(options=options):
                self.reply(envelope({}))
                self.assert_success(self.run_cli("frontmatter", "--path", "x.md", *options))
                expected = {"vault": VAULT, "path": "x.md"}
                if "--updates" in options:
                    expected["updates"] = json.loads(options[options.index("--updates") + 1])
                if "--remove" in options:
                    expected["remove"] = json.loads(options[options.index("--remove") + 1])
                self.body(self.request("PATCH", "/api/note/frontmatter"), expected)

    def test_find_and_replace_values_may_begin_with_double_hyphens(self):
        for find, replacement in (("--needle", "--replacement"), ("--all", "--regex")):
            with self.subTest(find=find, replacement=replacement):
                self.reply(envelope({}))
                self.assert_success(self.run_cli("replace", "--path", "x.md",
                                                "--find", find, "--replace", replacement))
                self.assertEqual(len(self.mock.requests), 1)
                actual = self.body(self.request("POST", "/api/note/replace"),
                    {"vault": VAULT, "path": "x.md", "find": find, "replace": replacement},
                    optional={"regex", "all", "failIfNoMatch"})
                for key in ("regex", "all", "failIfNoMatch"):
                    if key in actual:
                        self.assertIs(actual[key], False, "A literal value must not enable an option")

    def test_rename_uses_old_and_new_path_hashes(self):
        self.reply(envelope({}))
        self.assert_success(self.run_cli("rename", "--old-path", "旧/old.md",
            "--path", "新/new.md", "--old-path-hash", "old-h", "--path-hash", "new-h"))
        self.body(self.request("POST", "/api/note/rename"),
            {"vault": VAULT, "oldPath": "旧/old.md", "path": "新/new.md",
             "oldPathHash": "old-h", "pathHash": "new-h"})

    def test_recycle_clear_single_and_all_have_separate_contracts(self):
        cases = [(('--path', 'x.md', '--confirm'),
                  {"vault": VAULT, "path": "x.md"}),
                 (('--all', '--confirm-vault', VAULT), {"vault": VAULT})]
        for options, expected in cases:
            with self.subTest(options=options):
                self.reply(envelope({}))
                self.assert_success(self.run_cli("recycle-clear", *options))
                self.assertEqual(len(self.mock.requests), 1)
                actual = self.body(self.request("DELETE", "/api/note/recycle-clear"),
                                   expected, optional={"path", "pathHash"})
                if "path" not in expected:
                    self.assertEqual(actual.get("path", ""), "")
                    self.assertEqual(actual.get("pathHash", ""), "")

    def test_recycle_clear_confirms_effective_overridden_vault(self):
        self.reply(envelope({}))
        self.assert_success(self.run_cli("recycle-clear", "--all", "--vault", "override",
                                        "--confirm-vault", "override"))
        actual = self.body(self.request("DELETE", "/api/note/recycle-clear"),
                           {"vault": "override"}, optional={"path", "pathHash"})
        self.assertEqual(actual.get("path", ""), "")
        self.assertEqual(actual.get("pathHash", ""), "")

    def test_password_mode_login_quoting_and_in_memory_token(self):
        marker = self.root / "must-not-exist"
        username = 'synthetic-user "\\\n文'
        password = 'synthetic-password "\\\n$(touch {})'.format(marker)
        self.reply(envelope({"token": LOGIN_TOKEN}), envelope({"content": "note"}))
        result = self.run_cli("read", "--path", "x.md", env={
            "FNS_AUTH_MODE": "password", "FNS_USERNAME": username,
            "FNS_PASSWORD": password, "FNS_CLIENT": "custom-token-client",
            "FNS_USER_AGENT": "contract-suite/7"})
        self.assert_success(result, {"content": "note"})
        self.assertEqual(len(self.mock.requests), 2)
        login = self.request("POST", "/api/user/login", 0, authenticated=False,
                             user_agent="contract-suite/7")
        self.assertEqual(login.headers.get("x-client"), "webgui")
        self.body(login, {"credentials": username, "password": password})
        self.request("GET", "/api/note", 1, client="webgui", token=LOGIN_TOKEN,
                     user_agent="contract-suite/7")
        self.assertFalse(marker.exists())
        for secret in (password, TOKEN, LOGIN_TOKEN):
            self.assertNotIn(secret, result.stdout + result.stderr)
        self.assertEqual(list(self.tmp.iterdir()), [], "Authentication artifacts must be removed")
        self.assertEqual(list(self.home.iterdir()), [], "Login must not persist a token cache")

    def test_token_mode_custom_identity_does_not_login(self):
        self.reply(envelope({}))
        self.assert_success(self.run_cli("read", "--path", "x.md", env={
            "FNS_CLIENT": "custom-client", "FNS_USER_AGENT": "custom-agent/3",
            "FNS_USERNAME": "must-not-login", "FNS_PASSWORD": "must-not-login"}))
        self.assertEqual(len(self.mock.requests), 1)
        self.request("GET", "/api/note", client="custom-client", user_agent="custom-agent/3")

    def test_login_requires_a_nonempty_string_token(self):
        for data in ({}, None, {"token": None}, {"token": ""},
                     {"token": False}, {"token": 42}, {"token": {"value": "bad"}}):
            with self.subTest(data=data):
                self.reply(envelope(data), envelope({}))
                result = self.run_cli("read", "--path", "x.md", env={
                    "FNS_AUTH_MODE": "password", "FNS_USERNAME": "user",
                    "FNS_PASSWORD": "synthetic-password"})
                self.assert_failure(result, 4, secrets=("synthetic-password",))
                self.assertEqual(len(self.mock.requests), 1)
                self.request("POST", "/api/user/login", authenticated=False)

    def test_login_failures_do_not_send_business_requests(self):
        cases = [(Reply.json(envelope(None, code=308, message="rejected")), 5),
                 (Reply.json(envelope({"token": LOGIN_TOKEN}), status=401), 3),
                 (Reply(b"not JSON"), 4), (Reply(disconnect=True), 3)]
        for response, exit_code in cases:
            with self.subTest(response=response):
                self.mock.script(response, Reply.json(envelope({})))
                result = self.run_cli("create", "--path", "x.md", "--content", "x", env={
                    "FNS_AUTH_MODE": "password", "FNS_USERNAME": "user",
                    "FNS_PASSWORD": "synthetic-password"})
                self.assert_failure(result, exit_code, secrets=("synthetic-password",))
                self.assertEqual(len(self.mock.requests), 1)
                self.request("POST", "/api/user/login", authenticated=False)

    def test_no_automatic_refresh_after_expiry_in_either_auth_mode(self):
        for mode in ("token", "password"):
            with self.subTest(mode=mode):
                responses = ([envelope({"token": LOGIN_TOKEN})] if mode == "password" else [])
                responses += [envelope(None, code=310, message="expired"), envelope({})]
                self.reply(*responses)
                result = self.run_cli("create", "--path", "x.md", "--content", "new",
                    env={"FNS_AUTH_MODE": mode, "FNS_USERNAME": "user",
                         "FNS_PASSWORD": "synthetic-password"})
                self.assert_failure(result, 5, code=310)
                self.assertEqual(len(self.mock.requests), 2 if mode == "password" else 1)
                self.request("POST", "/api/note", 1 if mode == "password" else 0,
                             client="webgui" if mode == "password" else None,
                             token=LOGIN_TOKEN if mode == "password" else TOKEN)

    def test_all_six_pinned_success_codes_are_preserved(self):
        for code in range(1, 7):
            with self.subTest(code=code):
                self.reply({"code": code, "status": True, "data": {"marker": code}})
                self.assert_success(self.run_cli("read", "--path", "x.md"),
                                    {"marker": code}, code=code)

    def test_success_envelope_optional_status_and_data_and_falsey_payloads(self):
        for response, expected in [({"code": 1}, None), ({"code": 1, "status": True}, None),
                                   ({"code": 1, "data": {"x": 1}}, {"x": 1})] + [
                ({"code": 1, "data": value}, value) for value in (None, False, 0, "", [])]:
            with self.subTest(response=response):
                self.reply(response)
                self.assert_success(self.run_cli("read", "--path", "x.md"), expected)

    def test_business_codes_are_not_success_even_on_http_200(self):
        for code in (0, 305, 307, 308, 310, 312, 313, 314, 315, 414, 420, 430, 431,
                     442, 443, 444):
            for extra in ({}, {"status": False}, {"status": True, "data": {}}):
                with self.subTest(code=code, extra=extra):
                    self.reply({"code": code, "message": "business failed", **extra})
                    self.assert_failure(self.run_cli("read", "--path", "x.md"), 5, code=code)
                    self.assertEqual(len(self.mock.requests), 1)

    def test_status_false_is_never_reported_as_success(self):
        self.reply({"code": 1, "status": False, "data": {}, "message": "failed"})
        self.assert_failure(self.run_cli("read", "--path", "x.md"), 5, code=1)

    def test_protocol_errors_reject_empty_invalid_truncated_and_multiple_json(self):
        bodies = [b"", b" \n\t", b"<html>bad gateway</html>", b'{"code":1,"data":',
                  b'{"code":1}{"code":1}', b'{"code":1}\n{"code":1}', b"null",
                  b"[]", b'"string"', b"true", b"123", b'{"data":{}}',
                  b'{"code":999999,"status":true}', b'{"code":"1"}',
                  b'{"code":true}', b'{"code":null}', b'{"code":1,"status":"true"}',
                  b'{"code":428}', b'{"code":507}', b'{"code":508}']
        for body in bodies:
            with self.subTest(body=body):
                self.mock.script(Reply(body))
                self.assert_failure(self.run_cli("read", "--path", "x.md"), 4)
                self.assertEqual(len(self.mock.requests), 1)

    def test_non_2xx_http_is_transport_failure_even_with_success_json(self):
        for status in (400, 401, 403, 404, 429, 500, 503):
            with self.subTest(status=status):
                self.mock.script(Reply.json(envelope({}), status), Reply.json(envelope({})))
                self.assert_failure(self.run_cli("create", "--path", "x.md", "--content", "x"),
                                    3, http_status=status)
                self.assertEqual(len(self.mock.requests), 1, "Writes must never be retried")

    def test_redirects_are_not_followed_and_writes_are_not_retried(self):
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status):
                self.mock.script(Reply.json(envelope({}), status,
                    {"Location": self.mock.url + "/must-not-follow"}), Reply.json(envelope({})))
                self.assert_failure(self.run_cli("create", "--path", "x.md", "--content", "x"),
                                    3, http_status=status)
                self.assertEqual(len(self.mock.requests), 1)
                self.request("POST", "/api/note")

    def test_network_disconnect_and_short_http_body_are_not_retried(self):
        for response in (Reply(disconnect=True), Reply(b'{"code":1}', advertised_length=100)):
            with self.subTest(response=response):
                self.mock.script(response, Reply.json(envelope({})))
                self.assert_failure(self.run_cli("upsert", "--path", "x.md", "--content", "x"), 3)
                self.assertEqual(len(self.mock.requests), 1)

    def test_connection_refused_is_transport_failure(self):
        with socket.socket() as reserved:
            reserved.bind(("127.0.0.1", 0))  # Reserved, deliberately not listening.
            result = self.run_cli("read", "--path", "x.md", env={
                "FNS_BASE_URL": "http://127.0.0.1:{}".format(reserved.getsockname()[1]),
                "FNS_TIMEOUT": "1", "FNS_CONNECT_TIMEOUT": "1"})
        self.assert_failure(result, 3)
        self.assertEqual(self.mock.requests, [])

    def test_server_errors_are_redacted(self):
        password = "synthetic-secret-password"
        message = "leaked " + " ".join((TOKEN, password))
        for status, exit_code in ((200, 5), (500, 3)):
            with self.subTest(status=status):
                self.mock.script(Reply.json(envelope(None, code=305, message=message), status))
                result = self.run_cli("read", "--path", "x.md", env={"FNS_PASSWORD": password})
                self.assert_failure(result, exit_code, secrets=(password,))
                self.assertNotIn(TOKEN, self.output(result)["error"]["message"])

    def test_help_is_text_and_needs_no_configuration(self):
        result = self.run_cli("--help", env={"FNS_BASE_URL": None, "FNS_TOKEN": None,
                                            "FNS_VAULT": None})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.strip())
        self.assertIn("--vault", result.stdout)
        self.assertIn("recycle-clear", result.stdout)
        self.assertEqual(self.mock.requests, [])

    def assert_live_runner_guard_refusal(self, arguments, environment, stage):
        runner = Path(__file__).with_name("run_live.py")
        self.assertTrue(runner.is_file(), "The live runner must exist for offline guard checks")
        result = subprocess.run(
            [sys.executable, "-B", str(runner), *arguments], env=environment, cwd=self.root,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8", timeout=5,
            check=False)
        self.assertEqual(self.mock.requests, [], "Guard refusal must occur before any network call")
        self.assertEqual(result.returncode, 2, "The live runner must refuse missing authorization/configuration")
        try:
            summary = json.loads(result.stdout)
        except (ValueError, TypeError):
            self.fail("Guard refusal must return one JSON summary, not raw diagnostics")
        self.assertIsInstance(summary, dict)
        self.assertIs(summary.get("success"), False)
        self.assertEqual((summary.get("pass"), summary.get("fail"), summary.get("skip")), (0, 1, 0))
        self.assertEqual(len(summary.get("tests", [])), 1)
        self.assertEqual(summary["tests"][0].get("name"), stage)
        self.assertEqual(summary["tests"][0].get("status"), "FAIL")
        self.assertNotIn("runPrefix", summary)
        for secret in (TOKEN, LOGIN_TOKEN, "synthetic-live-guard-user", "synthetic-live-guard-password"):
            self.assertFalse(secret in result.stdout + result.stderr,
                             "The live runner must not echo environment credentials")
        self.assertFalse("Traceback" in result.stderr, "Guard refusal must not dump an exception")

    def test_live_runner_missing_confirmation_refuses_before_network(self):
        # Copy the synthetic fixture environment, never the host's os.environ.
        environment = {**self.env, "FNS_VAULT": "fns-skill-test-offline-guard",
                       "FNS_AUTH_MODE": "token", "FNS_USERNAME": "synthetic-live-guard-user",
                       "FNS_PASSWORD": "synthetic-live-guard-password"}
        for arguments in ([], ["--allow-clear"]):
            with self.subTest(arguments=arguments):
                self.mock.script()
                self.assert_live_runner_guard_refusal(arguments, environment, "arguments")

    def test_live_runner_missing_required_environment_refuses_before_network(self):
        baseline = {**self.env, "FNS_VAULT": "fns-skill-test-offline-guard",
                    "FNS_AUTH_MODE": "token", "FNS_USERNAME": "synthetic-live-guard-user",
                    "FNS_PASSWORD": "synthetic-live-guard-password"}
        for key in ("FNS_BASE_URL", "FNS_VAULT", "FNS_AUTH_MODE", "FNS_TOKEN"):
            for empty in (False, True):
                with self.subTest(key=key, empty=empty):
                    environment = baseline.copy()
                    if empty:
                        environment[key] = ""
                    else:
                        environment.pop(key)
                    self.mock.script()
                    self.assert_live_runner_guard_refusal(
                        ["--confirm-vault", baseline["FNS_VAULT"], "--allow-clear"],
                        environment, "scope_guard")

    def test_usage_validation_happens_before_auth_or_network(self):
        cases = [(), ("unknown-command",), ("--path", "x.md", "read"),
                 ("read",), ("read", "--path"), ("read", "--path", ""),
                 ("read", "--path-hash", "h"), ("read", "--path", "x.md", "stray"),
                 ("read", "--path", "x.md", "--unknown"),
                 ("read", "--path", "x.md", "--keyword", "x"),
                 ("delete", "--path", "x.md", "--recycle"),
                 ("read", "--path", "x.md", "--vault"),
                 ("read", "--path", "--vault", VAULT),
                 ("read", "--path", "x.md", "--path", "y.md"),
                 ("--vault", "a", "read", "--path", "x.md", "--vault", "b"),
                 ("read", "--path", "x.md", "--recycle", "--recycle"),
                 ("search",), ("search", "--keyword", ""),
                 ("list", "--search-mode", "regex"), ("list", "--sort-by", "size"),
                 ("list", "--sort-order", "up"), ("list", "--page-size", "0"),
                 ("list", "--page-size", "101"), ("list", "--page", "0"),
                 ("list", "--page", "-1"), ("list", "--page", "1.5"),
                 ("list", "--page-size", "abc"),
                 ("list", "--page", "1", "--page", "2"),
                 ("replace", "--path", "x.md", "--find", "", "--replace", "x"),
                 ("replace", "--path", "x.md", "--find", "a"),
                 ("replace", "--path", "x.md", "--replace", "b"),
                 ("frontmatter", "--path", "x.md"),
                 ("rename", "--path", "new.md"), ("restore",),
                 ("upsert", "--path", "x.md", "--content", "x", "--expect-hash", "h")]
        for args in cases:
            with self.subTest(args=args):
                self.mock.script(Reply.json(envelope({"token": LOGIN_TOKEN})))
                self.assert_failure(self.run_cli(*args, env={"FNS_AUTH_MODE": "password",
                    "FNS_USERNAME": "user", "FNS_PASSWORD": "synthetic-password"}), 2)
                self.assertEqual(self.mock.requests, [])

    def test_writes_require_exactly_one_explicit_content_source(self):
        file = self.root / "content.md"
        file.write_text("body\n", encoding="utf-8")
        invalid = [[], ["--content", "x", "--stdin"],
                   ["--content", "x", "--content-file", str(file)],
                   ["--content-file", str(file), "--stdin"],
                   ["--content", "a", "--content", "b"],
                   ["--stdin", "--stdin"], ["--content-file", str(self.root / "missing")]]
        for command in ("create", "upsert", "update", "append", "prepend"):
            for options in invalid:
                with self.subTest(command=command, options=options):
                    self.mock.script()
                    self.assert_failure(self.run_cli(command, "--path", "x.md", *options,
                                                     stdin="unselected body"), 2)
                    self.assertEqual(self.mock.requests, [])

    def test_options_are_rejected_outside_their_command_scope(self):
        cases = [(('health',), ('--path', 'x.md')),
                 (('version',), ('--keyword', 'todo')),
                 (('doctor',), ('--content', 'body')),
                 (('vaults',), ('--recycle',)),
                 (('list',), ('--path', 'x.md')),
                 (('read', '--path', 'x.md'), ('--content', 'body')),
                 (('create', '--path', 'x.md', '--content', 'body'), ('--recycle',)),
                 (('upsert', '--path', 'x.md', '--content', 'body'), ('--all',)),
                 (('update', '--path', 'x.md', '--content', 'body'), ('--keyword', 'todo')),
                 (('append', '--path', 'x.md', '--content', 'body'), ('--expect-hash', 'h')),
                 (('prepend', '--path', 'x.md', '--content', 'body'), ('--regex',)),
                 (('replace', '--path', 'x.md', '--find', 'a', '--replace', ''),
                  ('--updates', '{}')),
                 (('frontmatter', '--path', 'x.md', '--updates', '{}'), ('--find', 'a')),
                 (('rename', '--old-path', 'old.md', '--path', 'new.md'), ('--confirm',)),
                 (('delete', '--path', 'x.md'), ('--all',)),
                 (('restore', '--path', 'x.md'), ('--content', 'body')),
                 (('recycle-clear', '--path', 'x.md', '--confirm'), ('--recycle',))]
        for args, misplaced in cases:
            with self.subTest(args=args, misplaced=misplaced):
                self.mock.script()
                self.assert_failure(self.run_cli(*args, *misplaced), 2)
                self.assertEqual(self.mock.requests, [])

    def test_duplicate_options_are_rejected_even_when_values_match(self):
        cases = [(('read', '--path', 'x.md'), ('--path', 'x.md')),
                 (('read', '--path', 'x.md', '--vault', VAULT), ('--vault', VAULT)),
                 (('read', '--path', 'x.md', '--path-hash', 'h'), ('--path-hash', 'h')),
                 (('list', '--keyword', 'todo'), ('--keyword', 'todo')),
                 (('list', '--search-mode', 'path'), ('--search-mode', 'path')),
                 (('list', '--page-size', '10'), ('--page-size', '10')),
                 (('create', '--path', 'x.md', '--content', 'body', '--ctime', '123'),
                  ('--ctime', '123')),
                 (('update', '--path', 'x.md', '--content', 'body', '--expect-hash', 'h'),
                  ('--expect-hash', 'h')),
                 (('replace', '--path', 'x.md', '--find', 'a', '--replace', '', '--all'),
                  ('--all',)),
                 (('frontmatter', '--path', 'x.md', '--updates', '{}'), ('--updates', '{}')),
                 (('frontmatter', '--path', 'x.md', '--remove', '[]'), ('--remove', '[]')),
                 (('rename', '--old-path', 'old.md', '--path', 'new.md'),
                  ('--old-path', 'old.md')),
                 (('recycle-clear', '--path', 'x.md', '--confirm'), ('--confirm',)),
                 (('recycle-clear', '--all', '--confirm-vault', VAULT), ('--all',)),
                 (('recycle-clear', '--all', '--confirm-vault', VAULT),
                  ('--confirm-vault', VAULT))]
        for args, duplicate in cases:
            with self.subTest(args=args, duplicate=duplicate):
                self.mock.script()
                self.assert_failure(self.run_cli(*args, *duplicate), 2)
                self.assertEqual(self.mock.requests, [])

    def test_frontmatter_rejects_wrong_json_shapes_before_network(self):
        for option, values in (("--updates", ["[]", "null", '"string"', "3", "{", "{}{}"]),
                               ("--remove", ["{}", "null", '[1]', '["x",null]', '[{}]', "["])):
            for value in values:
                with self.subTest(option=option, value=value):
                    self.mock.script()
                    self.assert_failure(self.run_cli("frontmatter", "--path", "x.md",
                                                     option, value), 2)
                    self.assertEqual(self.mock.requests, [])

    def test_write_metadata_rejects_non_integer_timestamps(self):
        for option in ("--ctime", "--mtime"):
            for value in ("", "-1", "1.2", "abc", '1\nheader = "injected"'):
                with self.subTest(option=option, value=value):
                    self.mock.script()
                    self.assert_failure(self.run_cli("create", "--path", "x.md",
                        "--content", "x", option, value), 2)
                    self.assertEqual(self.mock.requests, [])

    def test_recycle_clear_rejects_unsafe_or_mixed_selection_before_network(self):
        options = [[], ["--confirm"], ["--path", "x.md"], ["--all"],
                   ["--all", "--confirm"], ["--confirm-vault", VAULT],
                   ["--all", "--confirm-vault", "wrong"],
                   ["--all", "--confirm-vault", VAULT, "--path", "x.md"],
                   ["--all", "--confirm-vault", VAULT, "--path-hash", "h"],
                   ["--path-hash", "h", "--confirm"],
                   ["--path", "x.md", "--confirm-vault", VAULT],
                   ["--path", "", "--confirm"],
                   ["--all", "--vault", "override", "--confirm-vault", VAULT]]
        for args in options:
            with self.subTest(args=args):
                self.mock.script()
                self.assert_failure(self.run_cli("recycle-clear", *args), 2)
                self.assertEqual(self.mock.requests, [])

    def test_missing_or_invalid_configuration_never_makes_requests(self):
        cases = [{"FNS_BASE_URL": None}, {"FNS_BASE_URL": ""}, {"FNS_TOKEN": None},
                 {"FNS_TOKEN": ""}, {"FNS_VAULT": None}, {"FNS_VAULT": ""},
                 {"FNS_AUTH_MODE": "automatic"},
                 {"FNS_TOKEN": None, "FNS_USERNAME": "user", "FNS_PASSWORD": "password"},
                 {"FNS_AUTH_MODE": "password", "FNS_USERNAME": None, "FNS_PASSWORD": "p"},
                 {"FNS_AUTH_MODE": "password", "FNS_USERNAME": "u", "FNS_PASSWORD": None},
                 {"FNS_BASE_URL": "ftp://127.0.0.1"}, {"FNS_BASE_URL": "not-a-url"},
                 {"FNS_TIMEOUT": "0"}, {"FNS_TIMEOUT": "-1"}, {"FNS_TIMEOUT": "abc"},
                 {"FNS_CONNECT_TIMEOUT": "0"}, {"FNS_CONNECT_TIMEOUT": "abc"}]
        for env in cases:
            with self.subTest(env=env):
                self.mock.script()
                self.assert_failure(self.run_cli("read", "--path", "x.md", env=env), 2)
                self.assertEqual(self.mock.requests, [])


class CurlSecurityContractTests(CLIBase):
    def setUp(self):
        super().setUp()
        self.bin = self.root / "bin"
        self.bin.mkdir(mode=0o700)
        curl = self.bin / "curl"
        curl.write_text("#!{}\n{}".format(sys.executable, FAKE_CURL), encoding="utf-8")
        curl.chmod(0o700)
        self.jq_log = self.root / "jq-argv.jsonl"
        real_jq = shutil.which("jq")
        if real_jq is None:
            self.fail("The CLI requires jq; install it before running the contract suite")
        jq = self.bin / "jq"
        jq.write_text(
            "#!{}\nimport json, os, sys\n"
            "with open(os.environ['JQ_ARGV_LOG'], 'a', encoding='utf-8') as log:\n"
            "    log.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "os.execv({!r}, [{!r}] + sys.argv[1:])\n".format(
                sys.executable, real_jq, real_jq), encoding="utf-8")
        jq.chmod(0o700)
        self.log = self.root / "curl-log.jsonl"
        self.responses = self.root / "curl-responses.json"
        self.env.update({"PATH": str(self.bin) + os.pathsep + self.env["PATH"],
                         "FNS_BASE_URL": "https://contract.invalid",
                         "FAKE_CURL_LOG": str(self.log),
                         "JQ_ARGV_LOG": str(self.jq_log),
                         "FAKE_CURL_REPLIES": str(self.responses)})
        self.fake_replies(envelope({}))

    def fake_replies(self, *values, status=200, exit_code=0, stderr=""):
        if self.log.exists():
            self.log.unlink()
        if self.jq_log.exists():
            self.jq_log.unlink()
        self.responses.write_text(json.dumps([
            {"status": status, "exit": exit_code, "stderr": stderr,
             "body": json.dumps(value, ensure_ascii=False)} for value in values
        ]), encoding="utf-8")

    def records(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    def assert_safe_curl(self, records, secrets=()):
        self.assertTrue(records)
        if self.jq_log.exists():
            for line in self.jq_log.read_text(encoding="utf-8").splitlines():
                arguments = "\n".join(json.loads(line))
                for secret in (TOKEN, LOGIN_TOKEN, *secrets):
                    if secret:
                        self.assertNotIn(secret, arguments,
                                         "Secrets must not appear in jq process argv")
        forbidden = {"insecure", "location", "location-trusted", "retry", "no-globoff",
                     "retry-all-errors", "retry-connrefused", "retry-max-time"}
        for record in records:
            self.assertEqual(record["argv"][0], "-q", "curl -q must be the first argument")
            argv = "\n".join(record["argv"])
            for secret in (TOKEN, LOGIN_TOKEN, *secrets):
                if secret:
                    self.assertNotIn(secret, argv, "Secrets must not appear in process argv")
            for arg in record["argv"]:
                if arg.startswith("-") and not arg.startswith("--"):
                    self.assertNotIn("k", arg)
                    self.assertNotIn("L", arg)
            self.assertFalse(forbidden.intersection(k for k, _ in record["options"]))
            self.assertTrue(any(k == "globoff" and v in ("", "true")
                                for k, v in record["options"]),
                            "Every curl request must explicitly disable URL globbing")
            self.assertTrue(record["configs"], "curl must use a private configuration file")
            if any(k == "proxy" for k, _ in record["options"]):
                self.assertTrue(any(["noproxy", ""] in config["options"]
                                    for config in record["configs"]),
                                'Explicit FNS_PROXY requires noproxy = "" in curl config')
            for file in record["configs"] + record["files"]:
                self.assertEqual(file["mode"], 0o600, file["path"])
                self.assertEqual(file["parentMode"], 0o700, file["path"])
                self.assertFalse(Path(file["path"]).exists(), "Temporary secret file was not removed")
        self.assertEqual(list(self.tmp.iterdir()), [], "Temporary request directories were not removed")

    def test_https_base_url_host_globs_are_rejected_before_curl(self):
        for base in ("https://{one,two}.contract.invalid",
                     "https://contract{1,2}.invalid"):
            with self.subTest(base=base):
                self.fake_replies(envelope({"token": LOGIN_TOKEN}), envelope({}))
                result = self.run_cli("read", "--path", "x.md", env={
                    "FNS_BASE_URL": base, "FNS_AUTH_MODE": "password",
                    "FNS_USERNAME": "synthetic-glob-user", "FNS_PASSWORD": "synthetic-glob-password"})
                self.assertEqual(self.records(), [], "Host globs must be rejected before curl runs")
                self.assert_failure(result, 2, secrets=("synthetic-glob-password",))

    def test_success_redacts_credentials_in_string_values_and_object_keys(self):
        username = "synthetic-service-user+@local"
        password = 'synthetic-service-password"\\[.]^$'
        proxy_user = "synthetic-success-proxy-user%40local"
        proxy_password = "synthetic-success-proxy-password%3A%22%5C"
        proxy = "http://" + proxy_user + ":" + proxy_password + "@proxy.invalid:8080"
        configured_secrets = (TOKEN, username, password, proxy, proxy_user, proxy_password,
                              unquote(proxy_user), unquote(proxy_password))
        safe = {"text": "ordinary Unicode 文", "count": 3, "flag": False, "none": None}
        for mode in ("token", "password"):
            secrets = configured_secrets + ((LOGIN_TOKEN,) if mode == "password" else ())
            for location in ("values", "keys"):
                with self.subTest(mode=mode, location=location):
                    if location == "values":
                        data = {"safe": safe, "nested": [
                            {"value": "prefix:" + secret + ":suffix"} for secret in secrets]}
                    else:
                        data = {"safe": safe, TOKEN: "ordinary token-key value", "nested": [
                            {"prefix:" + secret + ":suffix": {"marker": "ordinary value"}}
                            for secret in secrets]}
                    responses = ([envelope({"token": LOGIN_TOKEN})] if mode == "password" else [])
                    self.fake_replies(*responses, envelope(data))
                    result = self.run_cli("read", "--path", "x.md", env={
                        "FNS_AUTH_MODE": mode, "FNS_USERNAME": username,
                        "FNS_PASSWORD": password, "FNS_PROXY": proxy})
                    value = self.assert_success(result)
                    self.assertEqual(value["data"]["safe"], safe,
                                     "Redaction must preserve nonsecret data and JSON types")
                    self.assert_no_secret_output(result, secrets)
                    records = self.records()
                    self.assertEqual(len(records), 2 if mode == "password" else 1)
                    self.assert_safe_curl(records, secrets)

    def test_proxy_schemes_and_credentials_apply_to_every_password_request(self):
        proxy_user = "synthetic-proxy-user"
        proxy_password = "synthetic-proxy-password"
        for scheme in ("socks5h", "socks5", "http", "https"):
            for authenticated_proxy in (False, True):
                with self.subTest(scheme=scheme, authenticated_proxy=authenticated_proxy):
                    userinfo = (proxy_user + ":" + proxy_password + "@"
                                if authenticated_proxy else "")
                    proxy = scheme + "://" + userinfo + "proxy.invalid:1080"
                    password = 'synthetic-login-password "\\\n'
                    username = 'synthetic-login-user "\\'
                    self.fake_replies(envelope({"token": LOGIN_TOKEN}), envelope({"note": "x"}))
                    result = self.run_cli("upsert", "--path", "x.md", "--content", "body", env={
                        "FNS_PROXY": proxy, "FNS_AUTH_MODE": "password",
                        "FNS_USERNAME": username, "FNS_PASSWORD": password})
                    self.assert_success(result, {"note": "x"})
                    records = self.records()
                    self.assertEqual(len(records), 2)
                    secrets = (proxy, proxy_user, proxy_password, password, username)
                    self.assert_safe_curl(records, secrets)
                    for record in records:
                        self.assertIn(["proxy", proxy], record["options"],
                                      "Preserve the native proxy URL, including credentials and scheme")
                        headers = {v.partition(":")[0].strip().lower(): v.partition(":")[2].strip()
                                   for k, v in record["options"] if k == "header"}
                        self.assertEqual(headers.get("x-client"), "webgui")
                    for secret in (TOKEN, LOGIN_TOKEN, *secrets):
                        self.assertNotIn(secret, result.stdout + result.stderr)

    def test_proxy_applies_to_public_doctor_and_each_update_request(self):
        proxy_user = "synthetic-public-proxy-user"
        proxy_password = "synthetic-public-proxy-password"
        proxy = "socks5h://" + proxy_user + ":" + proxy_password + "@proxy.invalid:1080"
        cases = [(('doctor',), [envelope({"health": True}), envelope({"version": "3.6.1"})]),
                 (('update', '--path', 'x.md', '--content', 'new'),
                  [envelope({"ctime": 123, "contentHash": "h"}), envelope({})])]
        for args, responses in cases:
            with self.subTest(args=args):
                self.fake_replies(*responses)
                result = self.run_cli(*args, env={"FNS_PROXY": proxy})
                self.assert_success(result)
                for secret in (proxy, proxy_user, proxy_password):
                    self.assertNotIn(secret, result.stdout + result.stderr)
                records = self.records()
                self.assertEqual(len(records), 2)
                self.assert_safe_curl(records, (proxy, proxy_user, proxy_password))
                for record in records:
                    self.assertIn(["proxy", proxy], record["options"])

    def test_percent_encoded_proxy_credentials_stay_in_config(self):
        proxy_user = "synthetic-escaped-proxy-user%40local"
        proxy_password = "synthetic-escaped-proxy-password%3A%22%5C"
        proxy = "https://" + proxy_user + ":" + proxy_password + "@proxy.invalid:8443"
        secrets = (proxy, proxy_user, proxy_password, unquote(proxy_user), unquote(proxy_password))
        self.fake_replies(envelope({}))
        result = self.run_cli("read", "--path", "x.md", env={"FNS_PROXY": proxy})
        self.assert_success(result)
        for secret in secrets:
            self.assertNotIn(secret, result.stdout + result.stderr)
        records = self.records()
        self.assertEqual(len(records), 1)
        self.assert_safe_curl(records, secrets)
        self.assertIn(["proxy", proxy], records[0]["options"])

    def test_encoded_and_decoded_proxy_credentials_are_redacted_from_errors(self):
        proxy_user = "synthetic-redacted-proxy-user%40local"
        proxy_password = "synthetic-redacted-proxy-password%3Asecret"
        proxy = "http://" + proxy_user + ":" + proxy_password + "@proxy.invalid:8080"
        secrets = (proxy, proxy_user, proxy_password, unquote(proxy_user), unquote(proxy_password))
        message = "server echoed " + " ".join(secrets)
        for status, exit_code in ((200, 5), (500, 3)):
            with self.subTest(status=status):
                self.fake_replies(envelope(None, code=305, message=message), status=status)
                result = self.run_cli("read", "--path", "x.md", env={"FNS_PROXY": proxy})
                self.assert_failure(result, exit_code, secrets=secrets)
                records = self.records()
                self.assertEqual(len(records), 1)
                self.assert_safe_curl(records, secrets)

    def test_curl_timeout_defaults_and_overrides(self):
        for env, timeout, connect in (({}, "30", "10"),
                                     ({"FNS_TIMEOUT": "5", "FNS_CONNECT_TIMEOUT": "2"}, "5", "2")):
            with self.subTest(env=env):
                self.fake_replies(envelope({}))
                self.assert_success(self.run_cli("read", "--path", "x.md", env=env))
                records = self.records()
                self.assertEqual(len(records), 1)
                self.assert_safe_curl(records)
                self.assertIn(["max-time", timeout], records[0]["options"])
                self.assertIn(["connect-timeout", connect], records[0]["options"])

    def test_nonloopback_http_requires_explicit_allowance(self):
        for allowance in (None, "0", "1"):
            with self.subTest(allowance=allowance):
                self.fake_replies(envelope({}))
                result = self.run_cli("read", "--path", "x.md", env={
                    "FNS_BASE_URL": "http://contract.invalid", "FNS_ALLOW_HTTP": allowance})
                if allowance == "1":
                    self.assert_success(result)
                    self.assertEqual(len(self.records()), 1)
                    self.assert_safe_curl(self.records())
                else:
                    self.assert_failure(result, 2)
                    self.assertEqual(self.records(), [])

    def test_config_newline_injection_is_rejected_before_curl(self):
        for name in ("FNS_BASE_URL", "FNS_TOKEN", "FNS_CLIENT", "FNS_USER_AGENT", "FNS_PROXY"):
            for separator in ("\n", "\r\n"):
                with self.subTest(name=name, separator=repr(separator)):
                    self.fake_replies(envelope({}))
                    base = {"FNS_BASE_URL": "https://contract.invalid", "FNS_TOKEN": TOKEN,
                            "FNS_CLIENT": "client", "FNS_USER_AGENT": "agent",
                            "FNS_PROXY": "http://proxy.invalid:1080"}[name]
                    malicious = base + separator + 'header = "X-Injected: yes"'
                    self.assert_failure(self.run_cli("read", "--path", "x.md", env={name: malicious}), 2)
                    self.assertEqual(self.records(), [])

    def test_malformed_proxy_credentials_are_not_echoed(self):
        # Credentials are supported, but a newline must never become a curl option.
        password = "synthetic-invalid-proxy-password"
        proxy = "http://synthetic-user:" + password + '@proxy.invalid:8080\nheader = "X-Injection: yes"'
        self.fake_replies(envelope({}))
        result = self.run_cli("read", "--path", "x.md", env={"FNS_PROXY": proxy})
        self.assert_failure(result, 2, secrets=(password, proxy))
        self.assertEqual(self.records(), [])

    def test_quotes_and_backslashes_are_safe_in_curl_config(self):
        token = 'synthetic-quoted-token-"\\-end'
        client = 'synthetic-client-"\\-end'
        agent = 'synthetic-agent-"\\-end'
        self.fake_replies(envelope({}))
        self.assert_success(self.run_cli("read", "--path", "x.md", env={
            "FNS_TOKEN": token, "FNS_CLIENT": client, "FNS_USER_AGENT": agent}))
        records = self.records()
        self.assertEqual(len(records), 1)
        self.assert_safe_curl(records, (token,))
        options = records[0]["options"]
        headers = {v.partition(":")[0].strip().lower(): v.partition(":")[2].strip()
                   for k, v in options if k == "header"}
        self.assertEqual(headers.get("authorization"), "Bearer " + token)
        self.assertEqual(headers.get("x-client"), client)
        self.assertTrue(["user-agent", agent] in options or headers.get("user-agent") == agent)
        self.assertFalse(any("x-injected" in v.lower() for _, v in options))

    def test_transport_errors_redact_credentials_and_do_not_retry(self):
        password = "synthetic-login-password-sensitive"
        proxy_user = "synthetic-error-proxy-user"
        proxy_password = "synthetic-error-proxy-password"
        proxy = "http://" + proxy_user + ":" + proxy_password + "@proxy.invalid:8080"
        secrets = (password, proxy, proxy_user, proxy_password)
        message = "failure " + " ".join((TOKEN, *secrets))
        for exit_code in (5, 7, 18, 28, 35, 60):
            with self.subTest(curl_exit=exit_code):
                self.fake_replies(envelope({}), envelope({}), status=0,
                                  exit_code=exit_code, stderr=message)
                result = self.run_cli("create", "--path", "x.md", "--content", "x", env={
                    "FNS_PROXY": proxy, "FNS_PASSWORD": password})
                self.assert_failure(result, 3, secrets=secrets)
                records = self.records()
                self.assertEqual(len(records), 1)
                self.assert_safe_curl(records, secrets)

    def test_business_and_http_messages_redact_all_known_secrets(self):
        password = "synthetic-login-password-sensitive"
        proxy_user = "synthetic-error-proxy-user"
        proxy_password = "synthetic-error-proxy-password"
        proxy = "http://" + proxy_user + ":" + proxy_password + "@proxy.invalid:8080"
        secrets = (password, proxy, proxy_user, proxy_password)
        message = "failure " + " ".join((TOKEN, *secrets))
        for status, exit_code in ((200, 5), (500, 3)):
            with self.subTest(status=status):
                self.fake_replies(envelope(None, code=305, message=message), status=status)
                result = self.run_cli("read", "--path", "x.md", env={
                    "FNS_PROXY": proxy, "FNS_PASSWORD": password})
                self.assert_failure(result, exit_code, secrets=secrets)
                self.assertEqual(len(self.records()), 1)
                self.assert_safe_curl(self.records(), secrets)


if __name__ == "__main__":
    unittest.main()
