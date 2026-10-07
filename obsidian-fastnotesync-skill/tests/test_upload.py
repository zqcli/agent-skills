"""Offline attachment contract regressions; no live services or real credentials.

Run only this module, from the skill root:
    .venv/bin/python -B -m unittest discover -s tests -p test_upload.py -v

Import the existing fixtures as a module: importing its TestCase classes into
this module would also make unittest execute the pre-existing test suite.
"""

import contextlib
import errno
from email import policy
from email.parser import BytesParser
import importlib.util
import inspect
import io
import json
import logging
import os
from pathlib import Path
import signal
import subprocess
import sys
import unittest
from unittest import mock
from urllib.parse import unquote, urlsplit

if __package__:
    from . import test_fns as support
else:
    import test_fns as support


MAX_BYTES = 10 * 1024 * 1024
REMOTE = "附件/中文 空格😀.bin"
BINARY = bytes(range(256)) + b"\x00\xff\xc0\xaf\xed\xa0\x80\r\nlast\r\n"
CTIME = 1700000000123
MTIME = 1700000000456
USERNAME = "synthetic-upload-user-ONLY-FOR-TESTS"
PASSWORD = "synthetic-upload-password-ONLY-FOR-TESTS"
FIELDS = ("id", "path", "pathHash", "size", "contentHash", "ctime", "mtime")
POST_FIELDS = FIELDS[:5]


def rolling_hash(units):
    """Independent contract oracle: multiply by 31, wrap to signed int32."""
    result = 0
    for unit in units:
        result = (31 * result + unit) & 0xffffffff
    return str(result if result < 0x80000000 else result - 0x100000000)


def path_hash(path):
    raw = path.encode("utf-16-be")
    return rolling_hash(int.from_bytes(raw[i:i + 2], "big")
                        for i in range(0, len(raw), 2))


def metadata(path=REMOTE, payload=BINARY, **changes):
    value = {"id": 23, "path": path, "pathHash": path_hash(path),
             "size": len(payload), "contentHash": rolling_hash(payload),
             "ctime": CTIME, "mtime": MTIME}
    return {**value, **changes}


def missing():
    return support.envelope(None, code=0, message="Synthetic file-info failure",
                            details="record not found")


def invalid_metadata():
    """Schema errors applicable to info, prechecks, and final readback."""
    valid = metadata()
    for field in FIELDS:
        value = valid.copy()
        del value[field]
        yield "missing_" + field, value
    for field, invalid in (
        ("id", (True, False, 0, -1, "23", 23.0, None)),
        ("path", (None, False, "", REMOTE + "x")),
        ("pathHash", (None, True, int(valid["pathHash"]), "", "0",
                      "+" + valid["pathHash"], "2147483648")),
        ("size", (True, False, -1, "0", 0.0, None)),
        ("contentHash", (None, True, 0, "", "not-a-hash", "+0", "00",
                         "-0", "2147483648", "-2147483649")),
        ("ctime", (True, False, -1, "0", 0.0, None)),
        ("mtime", (True, False, -1, "0", 0.0, None)),
    ):
        for value in invalid:
            yield "{}_{}_{}".format(field, type(value).__name__, repr(value)), {
                **valid, field: value}
    for value in (None, [], "metadata", True, 23):
        yield "data_" + repr(value), value


class ReadProbe:
    """Observe real binary reads without depending on upload helper names."""

    def __init__(self, stream, observed):
        self.stream = stream
        self.observed = observed

    def __getattr__(self, name):
        return getattr(self.stream, name)

    def __enter__(self):
        self.stream.__enter__()
        return self

    def __exit__(self, *args):
        return self.stream.__exit__(*args)

    def read(self, *args):
        data = self.stream.read(*args)
        self.observed(data)
        return data

    def read1(self, *args):
        data = self.stream.read1(*args)
        self.observed(data)
        return data

    def readall(self):
        data = self.stream.readall()
        self.observed(data)
        return data

    def readinto(self, buffer):
        count = self.stream.readinto(buffer)
        if count:
            self.observed(bytes(memoryview(buffer)[:count]))
        return count


class UploadBase(support.CLIBase):
    def setUp(self):
        super().setUp()
        self.server = support.MockHTTP()
        self.addCleanup(self.server.close)
        self.env.update(FNS_BASE_URL=self.server.url,
                        FNS_USERNAME=USERNAME, FNS_PASSWORD=PASSWORD)
        self.source = self.root / "local-source-not-the-target-name.dat"
        self.source.write_bytes(BINARY)
        self.calls = []
        self.module = None

    def arguments(self, *extra, path=REMOTE, source=None, explicit_times=True):
        args = ["file-upload", "--file", str(source or self.source), "--path", path]
        if explicit_times:
            args += ["--ctime", str(CTIME), "--mtime", str(MTIME)]
        return [*args, *extra]

    def replies(self, *values, server=None):
        (server or self.server).script(*(value if isinstance(value, support.Reply)
                                        else support.Reply.json(value)
                                        for value in values))

    def upload_replies(self, current=None, final=None, server=None):
        final = metadata() if final is None else final
        self.replies(missing() if current is None else support.envelope(current),
                     support.envelope({key: final[key] for key in POST_FIELDS}),
                     support.envelope(final), server=server)
        return final

    def assert_private_output(self, result, env=None, logs=""):
        environment = {**self.env, **(env or {})}
        secrets = [support.TOKEN, support.LOGIN_TOKEN, USERNAME, PASSWORD]
        for key in ("FNS_TOKEN", "FNS_USERNAME", "FNS_PASSWORD", "FNS_PROXY"):
            if environment.get(key):
                secrets.append(str(environment[key]))
        if environment.get("FNS_PROXY"):
            proxy = urlsplit(environment["FNS_PROXY"])
            for part in (proxy.username, proxy.password):
                if part:
                    secrets.extend((part, unquote(part)))
        self.assert_no_secret_output(result, secrets)
        for secret in secrets:
            self.assertNotIn(secret, logs)

    def run_cli(self, *args, env=None, stdin=None):
        result = super().run_cli(*args, env=env, stdin=stdin)
        self.assert_private_output(result, env)
        return result

    def multipart(self, request, *, path=REMOTE, payload=BINARY,
                  vault=support.VAULT, ctime=CTIME, mtime=MTIME):
        self.assertEqual(request.query, {}, "Multipart must not put vault in the URL")
        content_type = request.headers.get("content-type", "")
        self.assertTrue(content_type.startswith("multipart/form-data;"), content_type)
        # Parse the actual MIME body, including binary parts and encoded filenames.
        message = BytesParser(policy=policy.default).parsebytes(
            b"Content-Type: " + content_type.encode("ascii") + b"\r\n\r\n" + request.body)
        self.assertTrue(message.is_multipart())
        self.assertEqual(message.defects, [])
        parts = {}
        for part in message.iter_parts():
            self.assertEqual(part.get_content_disposition(), "form-data")
            self.assertEqual(part.defects, [])
            name = part.get_param("name", header="content-disposition")
            self.assertNotIn(name, parts, "Duplicate multipart field")
            parts[name] = part
        self.assertEqual(set(parts), {"vault", "path", "file", "ctime", "mtime"})
        # get_filename() strips surrounding whitespace by design; the MIME
        # parameter preserves the actual filename sent over HTTP.
        self.assertEqual(parts["file"].get_param("filename", header="content-disposition"),
                         path.rsplit("/", 1)[-1])
        self.assertEqual(parts["file"].get_content_type(), "application/octet-stream")
        self.assertEqual(parts["file"].get_payload(decode=True), payload)
        fields = {}
        for name in ("vault", "path", "ctime", "mtime"):
            self.assertIsNone(parts[name].get_filename())
            fields[name] = parts[name].get_payload(decode=True).decode("utf-8", "strict")
        self.assertEqual(fields["vault"], vault)
        self.assertEqual(fields["path"], path)
        for name, expected in (("ctime", ctime), ("mtime", mtime)):
            self.assertRegex(fields[name], r"^(?:0|[1-9][0-9]*)$")
            if expected is not None:
                self.assertEqual(fields[name], str(expected))
        return fields

    def assert_sequence(self, requests, expected):
        self.assertEqual([(r.method, r.path) for r in requests], expected)

    def assert_upload_sequence(self, requests, *, path=REMOTE, payload=BINARY,
                               vault=support.VAULT, ctime=CTIME, mtime=MTIME):
        self.assert_sequence(requests, [("GET", "/api/file/info"),
                                       ("POST", "/api/file"),
                                       ("GET", "/api/file/info")])
        for index in (0, 2):
            self.assertEqual(requests[index].query, {"vault": [vault], "path": [path]})
            self.assertEqual(requests[index].body, b"")
        return self.multipart(requests[1], path=path, payload=payload, vault=vault,
                              ctime=ctime, mtime=mtime)

    def import_main(self):
        if self.module is None:
            spec = importlib.util.spec_from_file_location("fns_upload_contract", support.SCRIPT)
            self.module = importlib.util.module_from_spec(spec)
            module_patch = mock.patch.dict(sys.modules, {spec.name: self.module})
            module_patch.start()
            self.addCleanup(module_patch.stop)
            output, errors = io.StringIO(), io.StringIO()
            with mock.patch.dict(os.environ, self.env, clear=True), \
                    mock.patch.object(sys, "dont_write_bytecode", True), \
                    mock.patch("socket.getaddrinfo", side_effect=AssertionError("No DNS on import")), \
                    mock.patch("socket.create_connection", side_effect=AssertionError("No sockets on import")), \
                    contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                spec.loader.exec_module(self.module)
            self.assertEqual(output.getvalue(), "")
            self.assertEqual(errors.getvalue(), "")
        return self.module

    def run_main(self, *args, replies=(), env=None, on_request=None):
        """Patch the existing Requests seam, never contact even the local fixture."""
        import requests
        module = self.import_main()
        environment = self.env.copy()
        for key, value in (env or {}).items():
            if value is None:
                environment.pop(key, None)
            else:
                environment[key] = str(value)
        queue = list(replies)
        self.calls.clear()
        output, errors, logs = io.StringIO(), io.StringIO(), io.StringIO()
        owner = self

        def request(session, method, url, **kwargs):
            prepared = session.prepare_request(requests.Request(
                method=method, url=url, headers=kwargs.get("headers"), params=kwargs.get("params"),
                json=kwargs.get("json"), data=kwargs.get("data"), files=kwargs.get("files")))
            body = prepared.body or b""
            if isinstance(body, str):
                body = body.encode("utf-8")
            captured = support.Request(method, prepared.url,
                {key.lower(): value for key, value in prepared.headers.items()}, body)
            owner.calls.append({"request": captured, "kwargs": kwargs,
                "session": session, "trust_env": session.trust_env,
                "timer": signal.getitimer(signal.ITIMER_REAL)[0],
                "retries": {scheme: session.get_adapter(scheme + "://").max_retries.total
                            for scheme in ("http", "https")}})
            if on_request:
                on_request(captured)
            if not queue:
                raise AssertionError("Unscripted upload request")
            value = queue.pop(0)
            if callable(value):
                value = value(captured)
            if isinstance(value, BaseException):
                raise value
            reply = value if isinstance(value, support.Reply) else support.Reply.json(value)
            response = requests.Response()
            response.status_code = reply.status
            response._content = reply.body
            response._content_consumed = True
            response.url = prepared.url
            response.request = prepared
            return response

        handler = logging.StreamHandler(logs)
        logger = logging.getLogger()
        level = logger.level
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        try:
            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.dict(os.environ, environment, clear=True))
                stack.enter_context(contextlib.redirect_stdout(output))
                stack.enter_context(contextlib.redirect_stderr(errors))
                stack.enter_context(mock.patch.object(requests.Session, "request", request))
                stack.enter_context(mock.patch.object(requests.Session, "send", side_effect=AssertionError(
                    "Must reuse Client.request, not open another transport")))
                for name in ("socket.getaddrinfo", "socket.create_connection", "socket.socket.connect",
                             "subprocess.run", "subprocess.Popen", "tempfile.mkstemp",
                             "tempfile.mkdtemp", "tempfile.NamedTemporaryFile"):
                    stack.enter_context(mock.patch(name, side_effect=AssertionError(
                        "Offline upload must not open another transport or persist credentials")))
                code = module.main(list(args))
        finally:
            logger.removeHandler(handler)
            logger.setLevel(level)
        result = subprocess.CompletedProcess(list(args), code, output.getvalue(), errors.getvalue())
        self.assert_private_output(result, environment, logs.getvalue())
        self.assertEqual(list(self.tmp.iterdir()), [])
        self.assertEqual(list(self.home.iterdir()), [], "No persistent authentication cache")
        return result

    def captured(self):
        return [call["request"] for call in self.calls]

    @contextlib.contextmanager
    def observe_reads(self, observed):
        original_stat = self.source.stat()
        original_io_open, original_open, original_os_read = io.open, open, os.read

        def is_source(value):
            try:
                if isinstance(value, int):
                    current = os.fstat(value)
                    return (current.st_dev, current.st_ino) == (
                        original_stat.st_dev, original_stat.st_ino)
                return Path(value) == self.source
            except (TypeError, ValueError, OSError):
                return False

        def wrap(original):
            def open_file(file, *args, **kwargs):
                stream = original(file, *args, **kwargs)
                mode = kwargs.get("mode", args[0] if args else "r")
                if is_source(file) and "b" in mode and not any(c in mode for c in "wax+"):
                    return ReadProbe(stream, observed)
                return stream
            return open_file

        def read(fd, count):
            source = is_source(fd)
            data = original_os_read(fd, count)
            if source:
                observed(data)
            return data

        with mock.patch("io.open", wrap(original_io_open)), \
                mock.patch("builtins.open", wrap(original_open)), mock.patch("os.read", read):
            yield


class UploadHTTPTests(UploadBase):
    def test_binary_multipart_chinese_path_vault_override_and_explicit_times(self):
        self.upload_replies()
        result = self.run_cli(*self.arguments("--vault", "synthetic-override 文"))
        self.assert_success(result)
        self.assert_upload_sequence(self.server.requests, vault="synthetic-override 文")
        for request in self.server.requests:
            self.assertEqual(request.headers.get("authorization"), "Bearer " + support.TOKEN)
            self.assertEqual(request.headers.get("user-agent"), support.DEFAULT_AGENT)
            self.assertEqual(request.headers.get("x-client"), "obsidian-fastnotesync-skill")

    def test_zero_byte_file_and_zero_timestamps_are_allowed(self):
        self.source.write_bytes(b"")
        self.upload_replies(final=metadata(payload=b"", ctime=0, mtime=0))
        result = self.run_cli(*self.arguments(explicit_times=False), "--ctime", "0", "--mtime", "0")
        self.assert_success(result)
        self.assert_upload_sequence(self.server.requests, payload=b"", ctime=0, mtime=0)

    def test_exact_ten_mib_is_accepted(self):
        payload = b"\x00" * MAX_BYTES
        self.source.write_bytes(payload)
        # Every rolling-hash step on zero bytes is zero, independently of size.
        self.upload_replies(final={**metadata(payload=b""), "size": MAX_BYTES})
        self.assert_success(self.run_cli(*self.arguments()))
        self.assert_upload_sequence(self.server.requests, payload=payload)

    def test_token_and_password_use_stable_identity_on_all_upload_requests(self):
        for mode in ("token", "password"):
            with self.subTest(mode=mode):
                final = metadata()
                replies = ([support.envelope({"token": support.LOGIN_TOKEN})]
                           if mode == "password" else [])
                self.replies(*replies, missing(), support.envelope(final), support.envelope(final))
                env = {"FNS_AUTH_MODE": mode, "FNS_CLIENT": "synthetic-custom-client",
                       "FNS_USER_AGENT": "synthetic-upload-agent/9"}
                self.assert_success(self.run_cli(*self.arguments(), env=env))
                requests = self.server.requests
                if mode == "password":
                    login, requests = requests[0], requests[1:]
                    self.assertEqual((login.method, login.path), ("POST", "/api/user/login"))
                    self.assertEqual(login.query, {})
                    self.assertTrue(login.headers["content-type"].startswith("application/json"))
                    self.assertEqual(login.json(), {"credentials": USERNAME, "password": PASSWORD})
                    self.assertNotIn("authorization", login.headers)
                    self.assertEqual(login.headers["x-client"], "webgui")
                    self.assertEqual(login.headers["user-agent"], env["FNS_USER_AGENT"])
                self.assert_upload_sequence(requests)
                for request in requests:
                    self.assertEqual(request.headers["x-client"],
                                     "webgui" if mode == "password" else env["FNS_CLIENT"])
                    self.assertEqual(request.headers["user-agent"], env["FNS_USER_AGENT"])
                    self.assertEqual(request.headers["authorization"], "Bearer " + (
                        support.LOGIN_TOKEN if mode == "password" else support.TOKEN))

    def tls_fixture(self):
        cert, key = self.root / "synthetic-ca.pem", self.root / "synthetic-key.pem"
        cert.write_text(support.TLS_CERT, encoding="ascii")
        key.write_text(support.TLS_KEY, encoding="ascii")
        key.chmod(0o600)
        server = support.MockHTTP(tls_files=(cert, key))
        self.addCleanup(server.close)
        return cert, server

    def test_tls_origin_verification_and_ca_bundle_configuration_are_retained(self):
        cert, origin = self.tls_fixture()
        for ca in ({}, {"REQUESTS_CA_BUNDLE": str(cert)}, {"CURL_CA_BUNDLE": str(cert)}):
            with self.subTest(ca=ca):
                self.upload_replies(server=origin)
                env = {**ca, "FNS_BASE_URL": origin.url, "FNS_TIMEOUT": "3"}
                result = self.run_cli(*self.arguments(), env=env)
                if ca:
                    self.assert_success(result)
                    self.assert_upload_sequence(origin.requests)
                else:
                    self.assert_failure(result, 3)
                    self.assertEqual(origin.requests, [], "Reject TLS before Bearer or upload")
                self.assertEqual(self.server.requests, [])

    def test_https_proxy_verifies_tls_even_for_http_target_and_never_bypasses(self):
        cert, proxy = self.tls_fixture()
        proxy_url = "https://synthetic-proxy-user%40local:synthetic-proxy-password%3Asecret@" + urlsplit(proxy.url).netloc
        import base64
        basic = "Basic " + base64.b64encode(
            b"synthetic-proxy-user@local:synthetic-proxy-password:secret").decode("ascii")
        for ca in ({}, {"REQUESTS_CA_BUNDLE": str(cert)}, {"CURL_CA_BUNDLE": str(cert)}):
            with self.subTest(ca=ca):
                self.upload_replies(server=proxy)
                self.replies()
                env = {**ca, "FNS_PROXY": proxy_url, "NO_PROXY": "*", "FNS_TIMEOUT": "3"}
                result = self.run_cli(*self.arguments(), env=env)
                if ca:
                    self.assert_success(result)
                    self.assert_upload_sequence(proxy.requests)
                    for request in proxy.requests:
                        self.assertTrue(request.target.startswith(self.server.url + "/api/file"))
                        self.assertEqual(request.headers.get("proxy-authorization"), basic)
                        self.assertEqual(request.headers.get("authorization"), "Bearer " + support.TOKEN)
                else:
                    self.assert_failure(result, 3)
                    self.assertEqual(proxy.requests, [], "Untrusted proxy must not receive HTTP/auth")
                self.assertEqual(self.server.requests, [], "Explicit proxy overrides NO_PROXY")


class UploadOfflineTests(UploadBase):
    def test_full_ten_mib_hash_includes_the_final_nonzero_byte(self):
        payload = b"\x00" * (MAX_BYTES - 1) + b"\xff"
        self.source.write_bytes(payload)
        final = {**metadata(payload=b""), "size": MAX_BYTES, "contentHash": "255"}
        result = self.run_main(*self.arguments(), replies=[missing(),
            support.envelope({key: final[key] for key in POST_FIELDS}), support.envelope(final)])
        self.assert_success(result)
        self.assert_upload_sequence(self.captured(), payload=payload)

    def test_upload_uses_utf16_signed_path_hash_and_unsigned_original_byte_hash(self):
        # Golden vectors exercise surrogate pairs and negative signed int32 output;
        # high bytes deliberately cannot be UTF-8-decoded or signed-byte hashed.
        for path, expected_path_hash in (("😀", "1772899"),
                                         ("polygenelubricants", "-2147483648")):
            with self.subTest(path=path):
                self.source.write_bytes(b"\x80\xff")
                final = {**metadata(path=path, payload=b"\x80\xff"),
                         "pathHash": expected_path_hash, "contentHash": "4223"}
                result = self.run_main(*self.arguments(path=path), replies=[missing(),
                    support.envelope({key: final[key] for key in POST_FIELDS}), support.envelope(final)])
                self.assert_success(result)
                self.assert_upload_sequence(self.captured(), path=path, payload=b"\x80\xff")

    def test_hash_oracle_covers_utf16_surrogates_int32_overflow_and_raw_bytes(self):
        self.assertEqual(path_hash("hello"), "99162322")
        self.assertEqual(path_hash("😀"), "1772899")
        self.assertEqual(path_hash("polygenelubricants"), "-2147483648")
        self.assertEqual(rolling_hash(b"\x80\xff"), "4223")
        self.assertEqual(rolling_hash(b""), "0")
        self.assertNotEqual(path_hash(REMOTE), rolling_hash(REMOTE.encode("utf-8")))

    def test_version_and_client_request_keyword_extension_preserve_identity(self):
        module = self.import_main()
        self.assertEqual(module.VERSION, "2.0.1")
        params = inspect.signature(module.Client.request).parameters
        additions = {"form", "files", "missing_file_ok"}
        self.assertTrue(additions.issubset(params),
                        "Client.request is missing keyword parameters: " + repr(additions - params.keys()))
        for name in additions:
            with self.subTest(parameter=name):
                self.assertIn(params[name].kind, (inspect.Parameter.KEYWORD_ONLY,
                                                 inspect.Parameter.POSITIONAL_OR_KEYWORD))
        self.assertIs(params["missing_file_ok"].default, False)

    def test_multipart_fixture_parses_requests_binary_and_unicode_filename(self):
        import requests
        prepared = requests.Request("POST", self.server.url + "/api/file",
            data={"vault": support.VAULT, "path": REMOTE,
                  "ctime": str(CTIME), "mtime": str(MTIME)},
            files={"file": (REMOTE.rsplit("/", 1)[-1], BINARY, "application/octet-stream")}).prepare()
        request = support.Request("POST", prepared.url,
            {key.lower(): value for key, value in prepared.headers.items()}, prepared.body)
        self.multipart(request)

    def test_read_probe_observes_real_path_and_descriptor_binary_reads(self):
        for api in ("path", "descriptor"):
            with self.subTest(api=api):
                observed = []
                with self.observe_reads(observed.append):
                    if api == "path":
                        payload = self.source.read_bytes()
                    else:
                        fd = os.open(self.source, os.O_RDONLY)
                        try:
                            payload = os.read(fd, len(BINARY) + 1)
                        finally:
                            os.close(fd)
                self.assertEqual(payload, BINARY)
                self.assertEqual(b"".join(observed), BINARY)

    def test_file_info_gets_only_exact_vault_and_path_query(self):
        value = metadata()
        result = self.run_main("--vault", "synthetic-override 文", "file-info", "--path", REMOTE,
                               replies=[support.envelope(value)])
        self.assert_success(result, value)
        self.assert_sequence(self.captured(), [("GET", "/api/file/info")])
        self.assertEqual(self.captured()[0].query,
                         {"vault": ["synthetic-override 文"], "path": [REMOTE]})
        self.assertEqual(self.captured()[0].body, b"")

    def test_file_info_missing_is_not_a_success_or_a_delete(self):
        result = self.run_main("file-info", "--path", REMOTE, replies=[missing()])
        self.assert_failure(result, 5, code=0, http_status=200)
        self.assert_sequence(self.captured(), [("GET", "/api/file/info")])

    def test_file_info_rejects_every_invalid_metadata_field(self):
        for label, value in invalid_metadata():
            with self.subTest(case=label):
                result = self.run_main("file-info", "--path", REMOTE,
                                       replies=[support.envelope(value)])
                self.assert_failure(result, 4)
                self.assert_sequence(self.captured(), [("GET", "/api/file/info")])

    def test_existing_default_refuses_even_identical_content_without_post(self):
        for payload in (BINARY, b"old existing bytes"):
            with self.subTest(identical=payload == BINARY):
                result = self.run_main(*self.arguments(), replies=[support.envelope(metadata(payload=payload))])
                self.assert_failure(result, 6)
                self.assert_sequence(self.captured(), [("GET", "/api/file/info")])

    def test_overwrite_preserves_existing_ctime_unless_explicitly_overridden(self):
        old = metadata(payload=b"different existing binary\xff", ctime=1234, mtime=2345)
        for override in (None, 0, CTIME):
            with self.subTest(ctime=override):
                ctime = old["ctime"] if override is None else override
                final = metadata(ctime=ctime)
                args = self.arguments("--overwrite", "--mtime", str(MTIME), explicit_times=False)
                if override is not None:
                    args += ["--ctime", str(override)]
                result = self.run_main(*args, replies=[support.envelope(old),
                    support.envelope({k: final[k] for k in POST_FIELDS}), support.envelope(final)])
                self.assert_success(result)
                self.assert_upload_sequence(self.captured(), ctime=ctime)

    def test_overwrite_validates_all_precheck_metadata_before_post(self):
        for label, value in invalid_metadata():
            with self.subTest(case=label):
                result = self.run_main(*self.arguments("--overwrite"), replies=[support.envelope(value)])
                self.assert_failure(result, 4)
                self.assert_sequence(self.captured(), [("GET", "/api/file/info")])

    def test_path_hash_collision_and_path_mismatch_never_authorize_overwrite(self):
        target, other = "Aa.bin", "BB.bin"
        self.assertNotEqual(target, other)
        self.assertEqual(path_hash(target), path_hash(other))
        for args in (["file-info", "--path", target], self.arguments(path=target),
                     self.arguments("--overwrite", path=target)):
            with self.subTest(args=args):
                result = self.run_main(*args, replies=[support.envelope(metadata(path=other))])
                self.assert_failure(result, 4)
                self.assert_sequence(self.captured(), [("GET", "/api/file/info")])

    def test_default_times_are_sent_and_verified_without_prescribing_their_source(self):
        final = metadata()

        def posted(request):
            fields = self.multipart(request, ctime=None, mtime=None)
            final.update(ctime=int(fields["ctime"]), mtime=int(fields["mtime"]))
            return support.envelope({k: final[k] for k in POST_FIELDS})

        result = self.run_main(*self.arguments(explicit_times=False),
            replies=[missing(), posted, lambda _: support.envelope(final)])
        self.assert_success(result)
        self.assert_upload_sequence(self.captured(), ctime=final["ctime"], mtime=final["mtime"])

    def test_allowed_relative_paths_preserve_unicode_spaces_and_dot_filenames(self):
        for path in (REMOTE, "相册/ 图像 😀.bin", "dir/.hidden", "one..two.bin"):
            with self.subTest(path=path):
                final = metadata(path=path)
                result = self.run_main(*self.arguments(path=path),
                    replies=[missing(), support.envelope(final), support.envelope(final)])
                self.assert_success(result)
                self.assert_upload_sequence(self.captured(), path=path)

    def test_unsafe_paths_are_rejected_locally_before_password_login(self):
        paths = ("", "/", "/absolute.bin", ".", "..", "./x", "../x", "a/./x",
                 "a/../x", "a/.", "a/..", "a//x", "a/", "//host/x", "a\\x",
                 "a%20x", "a%", "C:/x", "a:x", "a*x", "a?x", 'a"x', "a<x",
                 "a>x", "a|x", "a\x00x", "a\x01x", "a\x1fx", "a\x7fx", "a\x85x",
                 "a\x9fx", "a\nx", "a\rx", "a\tx")
        for command in ("file-info", "file-upload"):
            for path in paths:
                with self.subTest(command=command, path=repr(path)):
                    args = ([command, "--path", path] if command == "file-info"
                            else self.arguments(path=path))
                    result = self.run_main(*args, env={"FNS_AUTH_MODE": "password"})
                    self.assert_failure(result, 2)
                    self.assertEqual(self.calls, [], "Unsafe paths must not log in or send HTTP")

    def test_missing_directory_fifo_and_oversized_sources_never_log_in_or_send_http(self):
        fifo = self.root / "synthetic-fifo"
        os.mkfifo(fifo)
        large = self.root / "one-byte-too-large.bin"
        with large.open("wb") as stream:
            stream.truncate(MAX_BYTES + 1)
        for source in (self.root / "nonexistent", self.root, fifo, large):
            for mode in ("token", "password"):
                with self.subTest(source=source.name, mode=mode):
                    if source == fifo:
                        # A buggy blocking open must not hang the unittest process;
                        # CLIBase's bounded subprocess kills it on timeout.
                        self.replies()
                        try:
                            result = self.run_cli(*self.arguments(source=source),
                                                  env={"FNS_AUTH_MODE": mode})
                        except subprocess.TimeoutExpired:
                            self.fail("FIFO upload blocked instead of rejecting a non-regular source")
                        self.assert_failure(result, 2)
                        self.assertEqual(self.server.requests, [])
                    else:
                        result = self.run_main(*self.arguments(source=source), env={"FNS_AUTH_MODE": mode})
                        self.assert_failure(result, 2)
                        self.assertEqual(self.calls, [])

    def test_unreadable_source_is_rejected_before_password_login(self):
        original_io_open, original_open, original_os_open = io.open, open, os.open
        attempted = []

        def wrap(original):
            def denied(path, *args, **kwargs):
                if not isinstance(path, int) and Path(path) == self.source:
                    attempted.append(path)
                    raise PermissionError("Synthetic unreadable source")
                return original(path, *args, **kwargs)
            return denied

        with mock.patch("io.open", wrap(original_io_open)), \
                mock.patch("builtins.open", wrap(original_open)), \
                mock.patch("os.open", wrap(original_os_open)):
            result = self.run_main(*self.arguments(), env={"FNS_AUTH_MODE": "password"})
        self.assertTrue(attempted, "The valid CLI must reach binary snapshot loading")
        self.assert_failure(result, 2)
        self.assertEqual(self.calls, [])

    def test_source_changes_during_snapshot_fail_before_any_login_or_http(self):
        for change in ("grow", "shrink", "same-size-edit", "mtime-only", "replace-inode"):
            with self.subTest(change=change):
                self.source.write_bytes(BINARY)
                before = self.source.stat()
                changed = []

                def mutate(_data):
                    if changed:
                        return
                    changed.append(change)
                    if change == "grow":
                        self.source.write_bytes(BINARY + b"x")
                    elif change == "shrink":
                        self.source.write_bytes(BINARY[:-1])
                    elif change == "same-size-edit":
                        self.source.write_bytes(b"x" + BINARY[1:])
                        os.utime(self.source, ns=(before.st_atime_ns, before.st_mtime_ns + 1000000))
                    elif change == "mtime-only":
                        os.utime(self.source, ns=(before.st_atime_ns, before.st_mtime_ns + 1000000))
                    else:
                        replacement = self.root / "replacement.bin"
                        replacement.write_bytes(BINARY)
                        os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
                        os.replace(replacement, self.source)

                with self.observe_reads(mutate):
                    result = self.run_main(*self.arguments(), env={"FNS_AUTH_MODE": "password"})
                self.assertTrue(changed, "Must actually exercise the source's binary read")
                self.assert_failure(result, 2)
                self.assertEqual(self.calls, [], "A changing snapshot must fail before login")

    def test_snapshot_precedes_login_and_http_and_is_not_reread_afterwards(self):
        for mode in ("token", "password"):
            with self.subTest(mode=mode):
                self.source.write_bytes(BINARY)
                events = []
                altered = []

                def observed(data):
                    events.append(("read", data))

                def network(request):
                    events.append(("http", request.path))
                    if not altered:
                        altered.append(True)
                        self.source.unlink()

                final = metadata()
                replies = ([support.envelope({"token": support.LOGIN_TOKEN})]
                           if mode == "password" else [])
                with self.observe_reads(observed):
                    result = self.run_main(*self.arguments(), env={"FNS_AUTH_MODE": mode},
                        replies=[*replies, missing(), support.envelope(final), support.envelope(final)],
                        on_request=network)
                self.assert_success(result)
                self.assertEqual(events[0][0], "read", "Snapshot must precede even password login")
                first_http = next(i for i, event in enumerate(events) if event[0] == "http")
                self.assertEqual(b"".join(data for _, data in events[:first_http]), BINARY)
                self.assertFalse(any(kind == "read" for kind, _ in events[first_http:]))
                self.assert_upload_sequence(self.captured()[1:] if mode == "password" else self.captured())

    def test_snapshot_descriptors_close_before_http_and_on_each_exception_path(self):
        self.import_main()
        original_open, original_fdopen = os.open, os.fdopen
        original_fstat, original_stat = os.fstat, Path.stat
        final = metadata()
        for phase in ("success", "fdopen-error", "fdopen-interrupt", "fstat-error",
                      "read-error", "read-interrupt", "restat-error", "fifo-swap-before-open"):
            with self.subTest(phase=phase):
                self.source.unlink(missing_ok=True)
                self.source.write_bytes(BINARY)
                descriptors, streams, restats = [], [], []
                swapped = []

                def open_source(path, flags, *args, **kwargs):
                    if Path(path) == self.source:
                        if phase == "fifo-swap-before-open":
                            self.source.unlink()
                            os.mkfifo(self.source)
                            swapped.append(True)
                            self.assertTrue(flags & os.O_NONBLOCK,
                                            "A regular-file-to-FIFO race must not block")
                        fd = original_open(path, flags, *args, **kwargs)
                        descriptors.append(fd)
                        return fd
                    return original_open(path, flags, *args, **kwargs)

                def fdopen(fd, *args, **kwargs):
                    if fd not in descriptors:
                        return original_fdopen(fd, *args, **kwargs)
                    if phase == "fdopen-error":
                        raise OSError("Synthetic fdopen failure")
                    if phase == "fdopen-interrupt":
                        raise KeyboardInterrupt()
                    stream = original_fdopen(fd, *args, **kwargs)
                    streams.append(stream)
                    if phase.startswith("read-"):
                        def read_error(_data):
                            if phase == "read-interrupt":
                                raise KeyboardInterrupt()
                            raise OSError("Synthetic binary read failure")
                        return ReadProbe(stream, read_error)
                    return stream

                def fstat(fd):
                    if fd in descriptors and phase == "fstat-error":
                        raise OSError("Synthetic fstat failure")
                    return original_fstat(fd)

                def stat_path(path, *args, **kwargs):
                    if path == self.source:
                        restats.append(True)
                        if phase == "restat-error" and len(restats) == 2:
                            raise FileNotFoundError("Synthetic post-read stat failure")
                    return original_stat(path, *args, **kwargs)

                def closed_descriptors(_request=None):
                    self.assertTrue(descriptors, "Must reach the source descriptor open")
                    for stream in streams:
                        self.assertTrue(stream.closed, "Snapshot stream leaked across HTTP/exception")
                    for fd in descriptors:
                        with self.assertRaises(OSError) as error:
                            original_fstat(fd)
                        self.assertEqual(error.exception.errno, errno.EBADF)

                with mock.patch("os.open", open_source), mock.patch("os.fdopen", fdopen), \
                        mock.patch("os.fstat", fstat), mock.patch.object(Path, "stat", stat_path):
                    result = self.run_main(*self.arguments(), env={"FNS_AUTH_MODE": "password"},
                        replies=[support.envelope({"token": support.LOGIN_TOKEN}), missing(),
                                 support.envelope(final), support.envelope(final)],
                        on_request=closed_descriptors)
                closed_descriptors()
                if phase == "success":
                    self.assert_success(result)
                    self.assert_upload_sequence(self.captured()[1:])
                else:
                    self.assert_failure(result, 3 if "interrupt" in phase else 2)
                    self.assertEqual(self.calls, [], "Snapshot failure must precede authentication")
                if phase == "fifo-swap-before-open":
                    self.assertEqual(swapped, [True])

    def test_missing_and_inapplicable_options_and_invalid_times_fail_without_http(self):
        cases = (["file-upload", "--path", REMOTE], ["file-upload", "--file", str(self.source)],
                 ["file-info"], ["file-info", "--path", REMOTE, "--overwrite"],
                 self.arguments("--path-hash", "123"), self.arguments("--content", "not-binary"),
                 self.arguments("--overwrite", "--overwrite"),
                 ["file-info", "--path", REMOTE, "--file", str(self.source)])
        for field in ("ctime", "mtime"):
            for value in ("-1", "1.5", "true", "", "1e3"):
                cases += ([*self.arguments(explicit_times=False), "--" + field, value],)
        for args in cases:
            with self.subTest(args=args):
                result = self.run_main(*args, env={"FNS_AUTH_MODE": "password"})
                self.assert_failure(result, 2)
                self.assertEqual(self.calls, [])

    def test_only_complete_exact_missing_business_error_allows_upload(self):
        errors = [support.envelope(None, code=0),
                  {"code": 0, "details": "record not found"},
                  {"code": 0, "status": False},
                  {**missing(), "status": True}]
        for details in (None, "", "Record not found", "record not found ", " record not found",
                        "wrapped: record not found", "record not found\n", ["record not found"],
                        {"error": "record not found"}):
            errors.append({**missing(), "details": details})
        for value in errors:
            with self.subTest(value=value):
                result = self.run_main(*self.arguments(), replies=[value])
                self.assert_failure(result, 5, code=0)
                self.assert_sequence(self.captured(), [("GET", "/api/file/info")])
        for status in (0, "false", None):
            with self.subTest(invalid_status=status):
                result = self.run_main(*self.arguments(), replies=[{**missing(), "status": status}])
                self.assert_failure(result, 4)
                self.assert_sequence(self.captured(), [("GET", "/api/file/info")])

    def test_precheck_missing_envelope_allows_omitted_or_null_data(self):
        final = metadata()
        omitted_data = missing()
        del omitted_data["data"]
        # Pinned pkg/app/app.go uses json:"data,omitempty" for nil Res.Data:
        # omission is the actual contract; explicit null is an equivalent mock variant.
        for label, value in (("omitted", omitted_data), ("null", missing())):
            with self.subTest(data=label):
                result = self.run_main(*self.arguments(), replies=[value,
                    support.envelope(final), support.envelope(final)])
                self.assert_success(result)
                self.assert_upload_sequence(self.captured())

    def test_precheck_missing_envelope_rejects_all_non_null_data(self):
        final = metadata()
        for data in ({}, [], False, 0, "", "record not found", True, 1,
                     ["record not found"], {"details": "record not found"}):
            with self.subTest(data=repr(data)):
                # Leave valid write/readback replies queued to expose false success,
                # rather than an incidental unscripted-response fixture failure.
                result = self.run_main(*self.arguments(), replies=[{**missing(), "data": data},
                    support.envelope(final), support.envelope(final)])
                self.assert_failure(result, 5, code=0)
                self.assert_sequence(self.captured(), [("GET", "/api/file/info")])

    def test_precheck_malformed_code_and_optional_message_do_not_change_missing_contract(self):
        for code in (False, True, 0.0, "0", None):
            with self.subTest(code=repr(code)):
                result = self.run_main(*self.arguments(), replies=[{**missing(), "code": code}])
                self.assert_failure(result, 4)
                self.assert_sequence(self.captured(), [("GET", "/api/file/info")])
        complete = missing()
        del complete["message"]  # The standard serializer's message is optional.
        final = metadata()
        result = self.run_main(*self.arguments(), replies=[complete,
            support.envelope(final), support.envelope(final)])
        self.assert_success(result)
        self.assert_upload_sequence(self.captured())

    def test_missing_is_not_inferred_from_http_status_or_other_business_codes(self):
        for status, exit_code in ((201, 5), (202, 5), (404, 3), (403, 3), (500, 3)):
            with self.subTest(status=status):
                result = self.run_main(*self.arguments(),
                    replies=[support.Reply.json(missing(), status=status)])
                self.assert_failure(result, exit_code, http_status=status)
                self.assert_sequence(self.captured(), [("GET", "/api/file/info")])
        for code in (315, 420, 430, 444):
            with self.subTest(code=code):
                result = self.run_main(*self.arguments("--overwrite"), replies=[{**missing(), "code": code}])
                self.assert_failure(result, 5, code=code)
                self.assert_sequence(self.captured(), [("GET", "/api/file/info")])

    def test_permission_and_expiry_fail_at_every_phase_without_refresh_or_retry(self):
        final = metadata()
        for mode in ("token", "password"):
            for code in (315, 310):
                for phase in (0, 1, 2):
                    with self.subTest(mode=mode, code=code, phase=phase):
                        replies = [missing(), support.envelope(final), support.envelope(final)]
                        replies[phase] = support.envelope(None, code=code,
                            message="Synthetic rejection " + support.TOKEN + " " + PASSWORD)
                        if mode == "password":
                            replies.insert(0, support.envelope({"token": support.LOGIN_TOKEN}))
                        result = self.run_main(*self.arguments("--overwrite"), replies=replies,
                                               env={"FNS_AUTH_MODE": mode})
                        self.assert_failure(result, 5, code=code)
                        expected = [("GET", "/api/file/info"), ("POST", "/api/file"),
                                    ("GET", "/api/file/info")][:phase + 1]
                        if mode == "password":
                            expected.insert(0, ("POST", "/api/user/login"))
                        self.assert_sequence(self.captured(), expected)

    def test_post_requires_code_one_not_other_nominal_success_codes(self):
        for code in range(2, 7):
            with self.subTest(code=code):
                result = self.run_main(*self.arguments(), replies=[missing(),
                    support.envelope(metadata(), code=code, status=True)])
                self.assert_failure(result, 4)
                self.assert_sequence(self.captured(), [("GET", "/api/file/info"), ("POST", "/api/file")])

    def test_post_validates_required_id_path_size_and_hashes_before_readback(self):
        final = metadata()
        cases = [(label, value) for label, value in invalid_metadata()
                 if not label.startswith(("missing_ctime", "missing_mtime", "ctime_", "mtime_"))]
        for key in POST_FIELDS:
            value = final.copy()
            del value[key]
            cases.append(("missing_post_" + key, value))
        cases.extend((("wrong_size", {**final, "size": final["size"] + 1}),
                      ("wrong_content_hash", {**final, "contentHash": "0"}),
                      ("wrong_path_hash", {**final, "pathHash": "0"})))
        for label, value in cases:
            with self.subTest(case=label):
                result = self.run_main(*self.arguments(), replies=[missing(), support.envelope(value)])
                self.assert_failure(result, 4)
                self.assert_sequence(self.captured(), [("GET", "/api/file/info"), ("POST", "/api/file")])

    def test_post_rejects_invalid_timestamps_when_the_acknowledgement_supplies_them(self):
        for key in ("ctime", "mtime"):
            for value in (False, True, None, -1, "0", 0.0):
                with self.subTest(field=key, value=repr(value)):
                    ack = {field: metadata()[field] for field in POST_FIELDS}
                    ack[key] = value
                    result = self.run_main(*self.arguments(), replies=[missing(), support.envelope(ack)])
                    self.assert_failure(result, 4)
                    self.assert_sequence(self.captured(), [("GET", "/api/file/info"), ("POST", "/api/file")])

    def test_readback_rejects_valid_but_different_metadata_with_minimal_post_ack(self):
        final = metadata()
        ack = {key: final[key] for key in POST_FIELDS}
        alternatives = {"id": final["id"] + 1, "size": final["size"] + 1,
                        "contentHash": rolling_hash(b"different"),
                        "ctime": CTIME + 1, "mtime": MTIME + 1}
        for key, value in alternatives.items():
            with self.subTest(field=key):
                result = self.run_main(*self.arguments(), replies=[missing(), support.envelope(ack),
                    support.envelope({**final, key: value})])
                self.assert_failure(result, 4)
                self.assert_sequence(self.captured(), [("GET", "/api/file/info"),
                    ("POST", "/api/file"), ("GET", "/api/file/info")])

    def test_final_get_validates_all_metadata_and_matches_each_posted_field(self):
        final = metadata()
        cases = list(invalid_metadata())
        for key in FIELDS:
            value = final.copy()
            value[key] = value[key] + 1 if type(value[key]) is int else value[key] + "x"
            cases.append(("readback_mismatch_" + key, value))
        for label, value in cases:
            with self.subTest(case=label):
                result = self.run_main(*self.arguments(), replies=[missing(), support.envelope(final),
                                                                   support.envelope(value)])
                self.assert_failure(result, 4)
                self.assert_sequence(self.captured(), [("GET", "/api/file/info"),
                    ("POST", "/api/file"), ("GET", "/api/file/info")])

    def test_http_json_and_transport_failures_stop_each_phase_without_duplicate_writes(self):
        import requests
        final = metadata()
        cases = [(support.Reply.json(support.envelope(final), 503), 3),
                 (support.Reply.json(support.envelope(final), 302,
                    headers={"Location": self.server.url + "/must-not-follow"}), 3),
                 (support.Reply(b"not JSON"), 4),
                 (support.Reply.json(support.envelope(final, status=False)), 5),
                 (support.Reply.json(support.envelope(None, code=0)), 5),
                 (requests.exceptions.ReadTimeout("Synthetic timeout " + support.TOKEN), 3),
                 (requests.exceptions.ConnectionError("Synthetic disconnect " + PASSWORD), 3)]
        for phase in (0, 1, 2):
            for value, exit_code in cases:
                with self.subTest(phase=phase, kind=type(value).__name__, exit=exit_code):
                    replies = [missing(), support.envelope(final), support.envelope(final)]
                    replies[phase] = value
                    result = self.run_main(*self.arguments(), replies=replies)
                    self.assert_failure(result, exit_code)
                    self.assert_sequence(self.captured(), [("GET", "/api/file/info"),
                        ("POST", "/api/file"), ("GET", "/api/file/info")][:phase + 1])
        result = self.run_main(*self.arguments(), replies=[missing(), support.envelope(final), missing()])
        self.assert_failure(result, 5, code=0)
        self.assertEqual(sum(r.method == "POST" for r in self.captured()), 1)
        self.assertEqual(len(self.calls), 3, "The verification GET must not tolerate missing records")

    def test_success_and_errors_redact_synthetic_credentials_including_proxy_components(self):
        proxy = "https://synthetic-upload-proxy%40local:synthetic-upload-proxy-password%3Asecret@proxy.invalid:8443"
        secrets = (support.TOKEN, support.LOGIN_TOKEN, USERNAME, PASSWORD, proxy,
                   "synthetic-upload-proxy@local", "synthetic-upload-proxy-password:secret")
        for mode in ("token", "password"):
            with self.subTest(mode=mode):
                known = tuple(secret for secret in secrets
                              if mode == "password" or secret != support.LOGIN_TOKEN)
                final = {**metadata(), "safe": "普通 metadata", "extra": [
                    {secret: "echo:" + secret} for secret in known]}
                prefix = ([support.envelope({"token": support.LOGIN_TOKEN})]
                          if mode == "password" else [])
                env = {"FNS_AUTH_MODE": mode, "FNS_PROXY": proxy}
                result = self.run_main(*self.arguments(), env=env,
                    replies=[*prefix, missing(), support.envelope(final), support.envelope(final)])
                self.assert_success(result)
                self.assert_upload_sequence(self.captured()[len(prefix):])
                result = self.run_main(*self.arguments(), env=env,
                    replies=[*prefix, support.envelope(None, code=315,
                        message=" | ".join(known), details=" | ".join(known))])
                self.assert_failure(result, 5, code=315)

    def test_upload_reuses_session_proxy_tls_deadline_and_no_retry_policy(self):
        final = metadata()
        ca = self.root / "synthetic-policy-ca.pem"
        ca.write_text(support.TLS_CERT, encoding="ascii")
        for scheme in ("http", "https", "socks5", "socks5h"):
            with self.subTest(proxy_scheme=scheme):
                proxy = scheme + "://synthetic-proxy-user:synthetic-proxy-password@proxy.invalid:1080"
                env = {"FNS_PROXY": proxy, "FNS_AUTH_MODE": "password", "NO_PROXY": "*",
                       "REQUESTS_CA_BUNDLE": str(ca), "CURL_CA_BUNDLE": "unused-ca.pem",
                       "FNS_TIMEOUT": "5", "FNS_CONNECT_TIMEOUT": "2"}
                result = self.run_main(*self.arguments(), env=env,
                    replies=[support.envelope({"token": support.LOGIN_TOKEN}), missing(),
                             support.envelope(final), support.envelope(final)])
                self.assert_success(result)
                self.assertEqual(len(self.calls), 4)
                session = self.calls[0]["session"]
                for call in self.calls:
                    self.assertIs(call["session"], session, "Login/info/upload share one transport")
                    self.assertIs(call["trust_env"], False)
                    self.assertIs(call["kwargs"].get("allow_redirects"), False)
                    self.assertEqual(call["kwargs"].get("proxies"), {"http": proxy, "https": proxy})
                    self.assertEqual(call["kwargs"].get("verify"), str(ca))
                    self.assertEqual(call["kwargs"].get("timeout"), (2, 5))
                    self.assertEqual(call["retries"], {"http": 0, "https": 0})
                    self.assertGreater(call["timer"], 0)
                    self.assertLessEqual(call["timer"], 5)
                    self.assertEqual(call["request"].headers["x-client"], "webgui")
                    self.assertEqual(call["request"].headers["user-agent"], support.DEFAULT_AGENT)
                self.assertIsNone(self.calls[2]["kwargs"].get("json"))
                self.assertEqual(set(self.calls[2]["kwargs"].get("data", {})),
                                 {"vault", "path", "ctime", "mtime"})
                self.assertEqual(set(self.calls[2]["kwargs"].get("files", {})), {"file"})

    def test_json_requests_keep_existing_call_format_and_do_not_tolerate_code_zero(self):
        expected_kwargs = {"headers", "json", "params", "proxies", "verify", "timeout", "allow_redirects"}
        for command in ("create", "upsert"):
            with self.subTest(command=command):
                result = self.run_main(command, "--path", "synthetic-note.md", "--content", "中文\r\n",
                    replies=[support.envelope({"token": support.LOGIN_TOKEN}), support.envelope({})],
                    env={"FNS_AUTH_MODE": "password"})
                self.assert_success(result)
                self.assert_sequence(self.captured(), [("POST", "/api/user/login"), ("POST", "/api/note")])
                for call in self.calls:
                    self.assertEqual(set(call["kwargs"]), expected_kwargs)
                note = self.captured()[1]
                self.assertEqual(note.query, {})
                self.assertEqual(note.json(), {"vault": support.VAULT, "path": "synthetic-note.md",
                    "content": "中文\r\n", "createOnly": command == "create"})
        result = self.run_main("upsert", "--path", "synthetic-note.md", "--content", "x", replies=[missing()])
        self.assert_failure(result, 5, code=0)
        self.assert_sequence(self.captured(), [("POST", "/api/note")])

    def test_unknown_execute_dispatch_is_rejected_without_recycle_clear_or_any_request(self):
        module = self.import_main()
        client = mock.Mock(mode="token", vault=support.VAULT)
        try:
            with self.assertRaises(module.Failure):
                module.execute(client, "synthetic-unknown-command", {"path": REMOTE}, None)
        finally:
            client.request.assert_not_called()
            client.login.assert_not_called()

    def test_missing_token_never_falls_back_to_supplied_password_credentials(self):
        result = self.run_main(*self.arguments(), env={"FNS_TOKEN": None})
        self.assert_failure(result, 2)
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
