"""Local CLI contract tests for Fast Note Sync Service 3.6.1 (7a6c787).

Run with: python3 -B -m unittest discover -s obsidian-fastnotesync-skill/tests -v
Requires the CLI's requests[socks] runtime; tests otherwise use only stdlib.
All network calls use loopback HTTP/TLS/SOCKS fixtures or patched Requests.
Credentials, proxy settings, and CA configuration are always synthetic. Mocks
record request contracts; they deliberately do not implement server semantics.
A missing scripts/fns.py fails the suite rather than producing skipped success.
"""

import base64
import contextlib
import importlib.util
import io
import json
import logging
import os
from pathlib import Path
import select
import signal
import socket
import socketserver
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "fns.py"
WRAPPER = SCRIPT.with_suffix(".sh")
DEFAULT_AGENT = "obsidian-fastnotesync-skill/2.0.1"
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

# Synthetic localhost-only TLS fixture (2020--2040), not a service credential.
# Embedded so the tests require neither openssl nor a certificate-generation SDK.
TLS_CERT = """-----BEGIN CERTIFICATE-----
MIIDJTCCAg2gAwIBAgIUPan6VRnQQxX+f0otndUk6iWmpYQwDQYJKoZIhvcNAQEL
BQAwFDESMBAGA1UEAwwJbG9jYWxob3N0MB4XDTIwMDEwMTAwMDAwMFoXDTQwMDEw
MTAwMDAwMFowFDESMBAGA1UEAwwJbG9jYWxob3N0MIIBIjANBgkqhkiG9w0BAQEF
AAOCAQ8AMIIBCgKCAQEAq8fetUeK2Ht+KKhS9N3/ntislwOSUkGd9EmRA3qTzcE1
E1AiYp2BdBWVChyMhDMScrPfVimpBV80dUqEdYuKRXlrbOwtExhi9dHQHXUOzHN0
mdb5FU8Yk4cOHARmjXoRf1YucwJ37nOSZQLgalHkREKfv30yRXTZEjzb4aLff85U
+IqHldsHBl0ZuGacuIoLeAxr9mQ3/Jg9LtrSpqUHFAQaeSPFCpFcUyJGMRcUK77h
kjN8xZoiGJ8Yq2Gf+r69EOCibx4gld4c6sqg6vGlbg4+mlNeUqWzT7GanH2RHkYl
tFHuzdSZxSFual1VNOECa+2WiO488js2a78y07G6jwIDAQABo28wbTAdBgNVHQ4E
FgQUNDgkbd2ijE/9yKqbycJpuGcoT3owHwYDVR0jBBgwFoAUNDgkbd2ijE/9yKqb
ycJpuGcoT3owDwYDVR0TAQH/BAUwAwEB/zAaBgNVHREEEzARgglsb2NhbGhvc3SH
BH8AAAEwDQYJKoZIhvcNAQELBQADggEBAANZVmhP59/SjsyFI6Uvi3GAs+zZo2wd
v17vJ59uCg0HQFamG1mtAMct5Sr7Y+O/X9loHIm7p6yTaulEQxygVx14dPEtW9wr
GUzwAJmCG7s1f2VL3n4+GSeHVHIZFml6eNvshQysKKNI8/vL9wWuWFpA1W1dGb99
YkHnIZRvyO7zCbsg9JArTbp7jki5wZ3/pfkKMI32PX4AGna3aioT87/1kS+T5lKw
TrglVYOlK0PH3LsxwGH4/R5HMQHEy56bcJ1UxXbgUkmENI7ZUwb/nhd0/STBUZRr
oekkhVhZCSXlXj9rhAms0YUx1qvL/gGKfc20r3pB0QHLfxBCBhU5FOg=
-----END CERTIFICATE-----
"""
# Same synthetic key, trusted self-signed CA, but DNS:localhost only (no IP SAN).
TLS_DNS_ONLY_CERT = """-----BEGIN CERTIFICATE-----
MIIC/jCCAeagAwIBAgIUGF/806/FzBOz56kdL9OPMiZ0gLowDQYJKoZIhvcNAQEL
BQAwFDESMBAGA1UEAwwJbG9jYWxob3N0MB4XDTIwMDEwMTAwMDAwMFoXDTQwMDEw
MTAwMDAwMFowFDESMBAGA1UEAwwJbG9jYWxob3N0MIIBIjANBgkqhkiG9w0BAQEF
AAOCAQ8AMIIBCgKCAQEAq8fetUeK2Ht+KKhS9N3/ntislwOSUkGd9EmRA3qTzcE1
E1AiYp2BdBWVChyMhDMScrPfVimpBV80dUqEdYuKRXlrbOwtExhi9dHQHXUOzHN0
mdb5FU8Yk4cOHARmjXoRf1YucwJ37nOSZQLgalHkREKfv30yRXTZEjzb4aLff85U
+IqHldsHBl0ZuGacuIoLeAxr9mQ3/Jg9LtrSpqUHFAQaeSPFCpFcUyJGMRcUK77h
kjN8xZoiGJ8Yq2Gf+r69EOCibx4gld4c6sqg6vGlbg4+mlNeUqWzT7GanH2RHkYl
tFHuzdSZxSFual1VNOECa+2WiO488js2a78y07G6jwIDAQABo0gwRjAUBgNVHREE
DTALgglsb2NhbGhvc3QwDwYDVR0TAQH/BAUwAwEB/zAdBgNVHQ4EFgQUNDgkbd2i
jE/9yKqbycJpuGcoT3owDQYJKoZIhvcNAQELBQADggEBACm4WzqRWW/WMg1oVh5P
LS+/4kIo4ROrTZ/5aRPp5rdU1YMGHcx4KvIs3t5Z59nI49Jb43GUT8oRwQffPHiU
3FPcLXTDIP/NzkNAMwOmHkeW0+BjpwiTD5oFxoLqZ687ksbfBSXDz0pC+rMkzq6j
RokL6VfX8aJCOKN8T6tptBxy1LvJGpuBjc6n0QPoN/bb3bN1nNC0J+39oz/GmQQC
VnO08FOXrG0rDi/3TtoPrpA5dtOyIVGRawv0JBWqJnsHbYxZurPL3SDPZTa0UQMC
HLb8vsn4K2cWewO1Ulo1ysoxF1ndvh62S46qd4OCC0tN4kTvp9s5M2VekltSpTby
9/0=
-----END CERTIFICATE-----
"""
TLS_KEY = """-----BEGIN PRIVATE KEY-----
MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQCrx961R4rYe34o
qFL03f+e2KyXA5JSQZ30SZEDepPNwTUTUCJinYF0FZUKHIyEMxJys99WKakFXzR1
SoR1i4pFeWts7C0TGGL10dAddQ7Mc3SZ1vkVTxiThw4cBGaNehF/Vi5zAnfuc5Jl
AuBqUeREQp+/fTJFdNkSPNvhot9/zlT4ioeV2wcGXRm4Zpy4igt4DGv2ZDf8mD0u
2tKmpQcUBBp5I8UKkVxTIkYxFxQrvuGSM3zFmiIYnxirYZ/6vr0Q4KJvHiCV3hzq
yqDq8aVuDj6aU15SpbNPsZqcfZEeRiW0Ue7N1JnFIW5qXVU04QJr7ZaI7jzyOzZr
vzLTsbqPAgMBAAECggEAE6DYdU1R86+UuE+XfwxY1405l13cm9KMmmvHiqa4edPA
XU8URsFpl9qZd5jQg5CUZI/iDqXe/tKkm1xi602BBLQ9jqoj8mEgeac6SQtln/33
TlbOil1J2R2fApuMHlFa63ps/05CZvhEu48LFor58sTMHSTQmDgkT52toEgskp1Q
g9kzkXXLkCcKTm6o9IBtCbnEmHTZYCyALLwmGr2mRRB3n3WRa0gNHyyH88oRMX+l
Znao/lIjBHr5z7Y1lEumGfUwGKzKQrkdrES3lbX+aGrz12eM08l86RCeRn7gnk4N
HUZoN5mVRralLBDTbSWfE7PoFwMN3m4oX0G9OtLDnQKBgQDcSoxsgLnCjdW3xMe8
H5yFuVL1xfre5l7ay/TLjjAcTwti2v3JqZKJYDTiA1mhw5qPgunTpvc7HyP3H5Zw
EpETPyM6aYMYFTGyKjZ8f+g6N28E+RQADUgxo9bKroll3juEzrR+Vpo0z8XaFEv9
NMl2aspzlmQyHVLlxg1l57NQLQKBgQDHoEbWvVzaOSl5zBGgKzZ01cFUnGxcsU/i
O6Rp5WigflpPqVIRkhHv6eWk2N+MO10xJvWVDaQ9syTe039jKxbUr0DvJVEXdVy7
yyVVi6W2RW3eftxBffuxwHNfITXI23pz7nfnVFBuavE7Qg1TwyMccfwnndbPfFkv
oHF2nicvKwKBgGFkG/0EVBvtdOUP/HXxS9PbARBjfOv60UjODuXHcQGy+Ie15am4
bG6LuBWMAZ6Ayd5UtGe8U2Ux+UaSEoJt0vG9Yie66hhFlnj8LKaSLes/ArgiHnN/
a8F2e8mTrG769zlZ4XZRd0+N5BzsCERjiAXwZJ0Bij6VtEadbuL0stPpAoGBAKD9
lEOYQarIELfmKIzIbMl6ncjTwdxbLvZfbS/t0Bwm6kR2Y/ZBm++k7Qutz1MziOub
5NBJSRxtPh3p6UEbvfTWRYLX0HcfsiLNKRW9Ym4Fvh6CprS2mmZ2s1ST3uFWh4G5
Xr+T0q3J1zQYB9F2fPlyv41fkJ6SRQbQ2NB1qVozAoGAL2b6T2zUtWDopgt8zCGl
uZ1MOTHwiHrqzxr17psEx49STjhRH6OYLoZG4xtVCzx8YphLragONXxMcMxw+u6o
pp1nmYWykOLNxmUerhX5FzwGZZuhI/Ro4S8nZFk7oKZtDr/ajfZKqXAG4nIWhGHP
/qmtQNs1ugTusSmzMUGS488=
-----END PRIVATE KEY-----
"""


def envelope(data=None, code=1, **extra):
    return {"code": code, "status": code == 1, "data": data, **extra}


@dataclass
class Reply:
    body: bytes = b""
    status: int = 200
    headers: dict = field(default_factory=dict)
    disconnect: bool = False
    advertised_length: int = None
    chunk_delay: float = 0
    header_delay: float = 0

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

    def __init__(self, tls_files=None):
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
                if reply.header_delay:
                    time.sleep(reply.header_delay)
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
                    if reply.chunk_delay:
                        for byte in reply.body:
                            self.wfile.write(bytes((byte,)))
                            self.wfile.flush()
                            time.sleep(reply.chunk_delay)
                    else:
                        self.wfile.write(reply.body)
                except (BrokenPipeError, ConnectionResetError, ssl.SSLError):
                    pass
                self.close_connection = True

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_CONNECT = handle_request

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        # Expected TLS/timeout failures must not dump arbitrary request data.
        self.server.handle_error = lambda *_: None
        if tls_files:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(*tls_files)
            self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.thread = threading.Thread(
            target=lambda: self.server.serve_forever(poll_interval=0.02), daemon=True)
        self.thread.start()
        self.url = "{}://127.0.0.1:{}".format("https" if tls_files else "http", self.server.server_port)

    def script(self, *replies):
        with self.lock:
            self.requests.clear()
            self.replies[:] = replies

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class MockTLSConnectProxy:
    """TLS CONNECT relay to exactly one loopback origin; no external resolution."""

    def __init__(self, tls_files, origin_port):
        self.connects = []
        self.errors = []
        self.sockets = set()
        self.workers = set()
        self.lock = threading.Lock()
        self.stop = threading.Event()
        owner = self
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(*tls_files)
        allowed = {"localhost:{}".format(origin_port), "127.0.0.1:{}".format(origin_port)}

        class Server(ThreadingHTTPServer):
            daemon_threads = True
            block_on_close = False

            def get_request(self):
                raw, address = super().get_request()
                raw.settimeout(2)
                try:
                    connection = context.wrap_socket(raw, server_side=True)
                except BaseException:
                    raw.close()
                    raise
                with owner.lock:
                    owner.sockets.add(connection)
                return connection, address

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            rbufsize = 0  # Never strand tunneled TLS bytes in an HTTP read buffer.

            def log_message(self, *_):
                pass

            def setup(self):
                super().setup()
                with owner.lock:
                    owner.workers.add(threading.current_thread())

            def finish(self):
                try:
                    super().finish()
                finally:
                    with owner.lock:
                        owner.sockets.discard(self.connection)
                        owner.workers.discard(threading.current_thread())

            def do_CONNECT(self):
                self.close_connection = True
                with owner.lock:
                    owner.connects.append(Request("CONNECT", self.path,
                        {key.lower(): value for key, value in self.headers.items()}, b""))
                if self.path not in allowed:
                    with owner.lock:
                        owner.errors.append("CONNECT requested an unapproved fixture destination")
                    self.send_error(502)
                    return
                try:
                    # Forward inner TLS records opaquely, not decrypted HTTP.
                    # The fixed numeric address cannot escape the local fixture.
                    with socket.create_connection(("127.0.0.1", origin_port), timeout=2) as upstream:
                        with owner.lock:
                            owner.sockets.add(upstream)
                        try:
                            self.send_response(200, "Connection Established")
                            self.end_headers()
                            self.wfile.flush()
                            self.connection.settimeout(0.5)
                            upstream.settimeout(0.5)
                            expires = time.monotonic() + 5
                            while not owner.stop.is_set() and time.monotonic() < expires:
                                ready, _, _ = select.select([self.connection, upstream], [], [], 0.05)
                                # SSL can already have decrypted outer-TLS data
                                # buffered even when its fd is not readable.
                                if self.connection.pending() and self.connection not in ready:
                                    ready.append(self.connection)
                                for source in ready:
                                    chunk = source.recv(65536)
                                    if not chunk:
                                        return
                                    destination = upstream if source is self.connection else self.connection
                                    destination.sendall(chunk)
                            if not owner.stop.is_set():
                                with owner.lock:
                                    owner.errors.append("CONNECT fixture exceeded its five-second relay bound")
                        finally:
                            with owner.lock:
                                owner.sockets.discard(upstream)
                except (OSError, ValueError):
                    # EOF/TLS alerts on certificate rejection are expected.
                    pass

        self.server = Server(("127.0.0.1", 0), Handler)
        self.server.handle_error = lambda *_: None
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.02),
                                       daemon=True)
        self.thread.start()
        self.url = "https://localhost:{}".format(self.server.server_port)

    def close(self):
        self.stop.set()
        with self.lock:
            sockets, workers = list(self.sockets), list(self.workers)
        for connection in sockets:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()
        self.server.shutdown()
        self.server.server_close()
        expires = time.monotonic() + 3
        for worker in [self.thread, *workers]:
            worker.join(timeout=max(0, expires - time.monotonic()))
        if self.thread.is_alive() or any(worker.is_alive() for worker in workers):
            raise RuntimeError("CONNECT fixture did not terminate within its shutdown bound")


class MockSOCKS5:
    """Bounded local SOCKS endpoint; never resolves or forwards a destination."""

    def __init__(self, credentials=None):
        self.credentials = credentials
        self.auth_attempts = []
        self.handshakes = []
        self.requests = []
        self.replies = []
        self.errors = []
        self.lock = threading.Lock()
        owner = self

        class Handler(socketserver.StreamRequestHandler):
            def exact(self, size):
                value = self.rfile.read(size)
                if len(value) != size:
                    raise EOFError()
                return value

            def handle(self):
                self.connection.settimeout(3)
                try:
                    version, count = self.exact(2)
                    methods = self.exact(count)
                    if version != 5:
                        raise ValueError()
                    method = 2 if owner.credentials else 0
                    if method not in methods:
                        self.wfile.write(b"\x05\xff")
                        return
                    self.wfile.write(bytes((5, method)))
                    self.wfile.flush()
                    credentials = None
                    if method == 2:
                        if self.exact(1) != b"\x01":
                            raise ValueError()
                        username = self.exact(self.exact(1)[0]).decode("utf-8")
                        password = self.exact(self.exact(1)[0]).decode("utf-8")
                        credentials = (username, password)
                        with owner.lock:
                            owner.auth_attempts.append(credentials)
                        accepted = credentials == owner.credentials
                        self.wfile.write(bytes((1, 0 if accepted else 1)))
                        self.wfile.flush()
                        if not accepted:
                            return
                    version, command, reserved, address_type = self.exact(4)
                    if (version, command, reserved) != (5, 1, 0):
                        raise ValueError()
                    if address_type == 1:
                        destination = socket.inet_ntop(socket.AF_INET, self.exact(4))
                    elif address_type == 4:
                        destination = socket.inet_ntop(socket.AF_INET6, self.exact(16))
                    elif address_type == 3:
                        destination = self.exact(self.exact(1)[0]).decode("ascii")
                    else:
                        raise ValueError()
                    port = struct.unpack("!H", self.exact(2))[0]
                    with owner.lock:
                        owner.handshakes.append({"addressType": address_type, "destination": destination,
                                                 "port": port, "credentials": credentials})
                    self.wfile.write(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x00")
                    self.wfile.flush()
                    method_name, target, _ = self.rfile.readline(8192).decode("ascii").strip().split(" ")
                    headers = {}
                    for _ in range(100):
                        line = self.rfile.readline(8192)
                        if line == b"\r\n":
                            break
                        key, value = line.decode("iso-8859-1").split(":", 1)
                        headers[key.lower()] = value.strip()
                    else:
                        raise ValueError()
                    body = self.exact(int(headers.get("content-length", 0)))
                    with owner.lock:
                        owner.requests.append(Request(method_name, target, headers, body))
                        reply = owner.replies.pop(0) if owner.replies else Reply.json({}, 500)
                    response = ("HTTP/1.1 {} Mock\r\nContent-Type: application/json\r\n"
                                "Content-Length: {}\r\nConnection: close\r\n\r\n").format(
                                    reply.status, len(reply.body)).encode("ascii")
                    self.wfile.write(response + reply.body)
                    self.wfile.flush()
                except (OSError, EOFError, ValueError, UnicodeError):
                    with owner.lock:
                        owner.errors.append("SOCKS fixture handshake/request failed")

        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.server.handle_error = lambda *_: None
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.02),
                                       daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def script(self, *values):
        with self.lock:
            self.handshakes.clear()
            self.auth_attempts.clear()
            self.requests.clear()
            self.errors.clear()
            self.replies[:] = [Reply.json(value) for value in values]

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


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
        # Construct, do not copy os.environ: no real token, netrc, username,
        # password, host proxy, or CA setting can reach the child.
        path = os.pathsep.join((str(Path(sys.executable).parent), "/usr/bin", "/bin"))
        self.env = {"PATH": path, "HOME": str(self.home), "TMPDIR": str(self.tmp),
                    "LC_ALL": "C", "LANG": "C", "FNS_TOKEN": TOKEN,
                    "FNS_VAULT": VAULT, "PYTHONDONTWRITEBYTECODE": "1", "NO_PROXY": "*"}

    def run_cli(self, *args, env=None, stdin=None):
        child_env = self.env.copy()
        for key, value in (env or {}).items():
            if value is None:
                child_env.pop(key, None)
            else:
                child_env[key] = str(value)
        return subprocess.run(
            [sys.executable, "-B", str(SCRIPT), *args], env=child_env, cwd=self.root,
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
                token=TOKEN, user_agent=DEFAULT_AGENT):
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

    def test_netrc_never_overrides_the_bearer_token(self):
        netrc = self.home / ".netrc"
        netrc.write_text("machine 127.0.0.1 login synthetic-netrc-user "
                         "password synthetic-netrc-password\n", encoding="utf-8")
        netrc.chmod(0o600)
        for environment in ({}, {"NETRC": str(netrc)}):
            with self.subTest(environment=environment):
                self.reply(envelope({}))
                self.assert_success(self.run_cli("read", "--path", "x.md", env=environment))
                self.assertEqual(len(self.mock.requests), 1)
                self.request("GET", "/api/note")

    def test_environment_http_proxy_and_no_proxy_use_only_selected_local_endpoint(self):
        proxy = MockHTTP()
        self.addCleanup(proxy.close)
        for bypass in (None, "*"):
            with self.subTest(bypass=bypass):
                self.reply(envelope({"via": "target"}))
                proxy.script(Reply.json(envelope({"via": "proxy"})))
                result = self.run_cli("read", "--path", "x.md", env={
                    "http_proxy": proxy.url, "HTTP_PROXY": proxy.url, "ALL_PROXY": proxy.url,
                    "NO_PROXY": bypass, "no_proxy": bypass})
                self.assert_success(result, {"via": "target" if bypass else "proxy"})
                if bypass:
                    self.assertEqual(proxy.requests, [])
                    self.assertEqual(len(self.mock.requests), 1)
                    self.request("GET", "/api/note")
                else:
                    self.assertEqual(self.mock.requests, [])
                    self.assertEqual(len(proxy.requests), 1)
                    self.assertTrue(proxy.requests[0].target.startswith(self.mock.url + "/api/note?"))
                    self.assertEqual(proxy.requests[0].headers.get("authorization"), "Bearer " + TOKEN)

    def test_authenticated_http_proxy_decodes_credentials_without_leaking_them(self):
        proxy = MockHTTP()
        self.addCleanup(proxy.close)
        proxy_user, proxy_password = "synthetic-user%40local", "synthetic-password%3Asecret"
        proxy_url = "http://" + proxy_user + ":" + proxy_password + "@127.0.0.1:" + str(proxy.server.server_port)
        proxy.script(Reply.json(envelope({"token": LOGIN_TOKEN})), Reply.json(envelope({})))
        self.reply(envelope({}))
        result = self.run_cli("read", "--path", "x.md", env={
            "FNS_PROXY": proxy_url, "NO_PROXY": "*", "FNS_AUTH_MODE": "password",
            "FNS_USERNAME": "synthetic-service-user", "FNS_PASSWORD": "synthetic-service-password"})
        self.assert_success(result)
        self.assertEqual(self.mock.requests, [])
        self.assertEqual(len(proxy.requests), 2)
        authorization = "Basic " + base64.b64encode(
            (unquote(proxy_user) + ":" + unquote(proxy_password)).encode("utf-8")).decode("ascii")
        for request in proxy.requests:
            self.assertEqual(request.headers.get("proxy-authorization"), authorization)
            self.assertEqual(request.headers.get("x-client"), "webgui")
        self.assert_no_secret_output(result, (TOKEN, LOGIN_TOKEN, proxy_url, unquote(proxy_password),
                                               "synthetic-service-password"))
        for secret in (TOKEN, LOGIN_TOKEN, proxy_url, unquote(proxy_password)):
            self.assertNotIn(secret, "\n".join(result.args))

    def test_http_and_https_proxy_basic_auth_preserves_percent_decoded_utf8_and_raw_bytes(self):
        cert, key = self.root / "proxy-ca.pem", self.root / "proxy-key.pem"
        cert.write_text(TLS_CERT, encoding="ascii")
        key.write_text(TLS_KEY, encoding="ascii")
        key.chmod(0o600)
        plain_proxy = MockHTTP()
        tls_proxy = MockHTTP(tls_files=(cert, key))
        self.addCleanup(plain_proxy.close)
        self.addCleanup(tls_proxy.close)
        cases = [("utf8", "User%E6%B1%89", "Pass%E5%AF%86", "User汉:Pass密".encode("utf-8")),
                 ("raw_bytes", "User%FF", "Pass%FF", b"User\xff:Pass\xff")]
        for proxy in (plain_proxy, tls_proxy):
            for target_scheme in ("http", "https"):
                for encoding, username, password, expected_bytes in cases:
                    with self.subTest(proxy_scheme=urlsplit(proxy.url).scheme,
                                      target_scheme=target_scheme, encoding=encoding):
                        encoded = base64.b64encode(expected_bytes).decode("ascii")
                        authorization = "Basic " + encoded
                        address = urlsplit(proxy.url)
                        proxy_url = address.scheme + "://" + username + ":" + password + "@" + address.netloc
                        secrets = (TOKEN, proxy_url, username, password, unquote(username),
                                   unquote(password), authorization, encoded)
                        if target_scheme == "http":
                            proxy.script(Reply.json(envelope({"proxyAuth": True})))
                        else:
                            # A failing CONNECT is enough to inspect actual tunnel
                            # authentication; this fixture never forwards a socket.
                            proxy.script(Reply.json({"message": " ".join(secrets)}, 502))
                        self.reply(envelope({}))
                        target = self.mock.url.replace("http://", target_scheme + "://", 1)
                        result = self.run_cli("read", "--path", "x.md", env={
                            "FNS_BASE_URL": target, "FNS_PROXY": proxy_url,
                            "REQUESTS_CA_BUNDLE": str(cert), "NO_PROXY": "*", "FNS_TIMEOUT": "3"})
                        if target_scheme == "http":
                            self.assert_success(result, {"proxyAuth": True})
                        else:
                            self.assert_failure(result, 3, secrets=secrets)
                        self.assertEqual(self.mock.requests, [], "Only the selected proxy may receive traffic")
                        self.assertEqual(len(proxy.requests), 1, "Proxy/tunnel failures must not be retried")
                        request = proxy.requests[0]
                        self.assertEqual(request.headers.get("proxy-authorization"), authorization)
                        self.assertEqual(base64.b64decode(request.headers["proxy-authorization"][6:]),
                                         expected_bytes)
                        if target_scheme == "http":
                            self.assertEqual((request.method, request.path), ("GET", "/api/note"))
                            self.assertEqual(request.headers.get("authorization"), "Bearer " + TOKEN)
                        else:
                            self.assertEqual(request.method, "CONNECT")
                            self.assertEqual(request.target, urlsplit(target).netloc)
                            self.assertNotIn("authorization", request.headers,
                                             "Service Bearer must not enter the CONNECT handshake")
                        self.assert_no_secret_output(result, secrets)
                        for secret in secrets:
                            self.assertNotIn(secret, "\n".join(result.args))

    def test_socks5_local_dns_and_socks5h_remote_dns_real_handshakes(self):
        proxy = MockSOCKS5()
        self.addCleanup(proxy.close)
        # localhost avoids any external DNS even for a regressed client. ATYP
        # still distinguishes a locally resolved address from remote DNS.
        for scheme, hostname in (("socks5", "localhost"), ("socks5h", "localhost")):
            with self.subTest(scheme=scheme):
                proxy.script(envelope({}))
                result = self.run_cli("read", "--path", "x.md", env={
                    "FNS_BASE_URL": "http://" + hostname + ":18080", "FNS_ALLOW_HTTP": "1",
                    "FNS_PROXY": scheme + "://127.0.0.1:" + str(proxy.port),
                    "NO_PROXY": "*", "FNS_TIMEOUT": "3", "FNS_CONNECT_TIMEOUT": "2"})
                self.assert_success(result)
                self.assertEqual(proxy.errors, [])
                self.assertEqual(len(proxy.handshakes), 1)
                handshake = proxy.handshakes[0]
                self.assertEqual(handshake["port"], 18080)
                if scheme == "socks5h":
                    self.assertEqual((handshake["addressType"], handshake["destination"]), (3, hostname))
                else:
                    self.assertIn(handshake["addressType"], (1, 4))
                    self.assertIn(handshake["destination"], ("127.0.0.1", "::1"))
                self.assertEqual(len(proxy.requests), 1)
                self.assertEqual(proxy.requests[0].headers.get("authorization"), "Bearer " + TOKEN)
                self.assertEqual(proxy.requests[0].path, "/api/note")

    def test_authenticated_socks5_and_socks5h_login_and_business_requests(self):
        username, password = "synthetic-socks-user%40local", "synthetic-socks-password%3Asecret"
        credentials = (unquote(username), unquote(password))
        proxy = MockSOCKS5(credentials=credentials)
        self.addCleanup(proxy.close)
        for scheme in ("socks5", "socks5h"):
            with self.subTest(scheme=scheme):
                proxy.script(envelope({"token": LOGIN_TOKEN}), envelope({}))
                proxy_url = scheme + "://" + username + ":" + password + "@127.0.0.1:" + str(proxy.port)
                result = self.run_cli("create", "--path", "x.md", "--content", "body", env={
                    "FNS_BASE_URL": "http://localhost:18080", "FNS_PROXY": proxy_url,
                    "FNS_AUTH_MODE": "password", "FNS_USERNAME": "synthetic-login-user",
                    "FNS_PASSWORD": "synthetic-login-password", "NO_PROXY": "*", "FNS_TIMEOUT": "3"})
                self.assert_success(result)
                self.assertEqual(proxy.errors, [])
                self.assertEqual(proxy.auth_attempts, [credentials, credentials])
                self.assertEqual(len(proxy.handshakes), 2)
                self.assertEqual(len(proxy.requests), 2)
                self.assertEqual([(r.method, r.path) for r in proxy.requests],
                                 [("POST", "/api/user/login"), ("POST", "/api/note")])
                for request in proxy.requests:
                    self.assertEqual(request.headers.get("x-client"), "webgui")
                    self.assertEqual(request.headers.get("user-agent"), DEFAULT_AGENT)
                self.assertNotIn("authorization", proxy.requests[0].headers)
                self.assertEqual(proxy.requests[1].headers.get("authorization"), "Bearer " + LOGIN_TOKEN)
                self.assert_no_secret_output(result, (TOKEN, LOGIN_TOKEN, username, password,
                                                       *credentials, "synthetic-login-password"))

    def test_socks_auth_rejection_never_sends_http_or_retries(self):
        proxy = MockSOCKS5(credentials=("synthetic-user", "expected-synthetic-password"))
        self.addCleanup(proxy.close)
        proxy.script(envelope({}))
        password = "wrong-synthetic-password"
        result = self.run_cli("create", "--path", "x.md", "--content", "body", env={
            "FNS_PROXY": "socks5h://synthetic-user:" + password + "@127.0.0.1:" + str(proxy.port),
            "FNS_TIMEOUT": "3"})
        self.assert_failure(result, 3, secrets=(password,))
        self.assertEqual(proxy.auth_attempts, [("synthetic-user", password)])
        self.assertEqual(proxy.requests, [])

    def test_tls_rejects_untrusted_certificate_and_honors_both_ca_bundle_variables(self):
        cert, key = self.root / "localhost-ca.pem", self.root / "localhost-key.pem"
        cert.write_text(TLS_CERT, encoding="ascii")
        key.write_text(TLS_KEY, encoding="ascii")
        key.chmod(0o600)
        server = MockHTTP(tls_files=(cert, key))
        self.addCleanup(server.close)
        for environment in ({}, {"REQUESTS_CA_BUNDLE": str(cert)}, {"CURL_CA_BUNDLE": str(cert)}):
            with self.subTest(ca_variable=next(iter(environment), "default-certifi")):
                server.script(Reply.json(envelope({"tls": True})))
                result = self.run_cli("read", "--path", "x.md", env={
                    **environment, "FNS_BASE_URL": server.url, "FNS_TIMEOUT": "3"})
                if environment:
                    self.assert_success(result, {"tls": True})
                    self.assertEqual(len(server.requests), 1)
                    self.assertEqual(server.requests[0].headers.get("authorization"), "Bearer " + TOKEN)
                else:
                    self.assert_failure(result, 3)
                    self.assertEqual(server.requests, [], "Untrusted TLS must fail before HTTP/auth")

    def test_https_proxy_for_http_target_requires_independent_certificate_verification(self):
        cert, key = self.root / "proxy-ca.pem", self.root / "proxy-key.pem"
        cert.write_text(TLS_CERT, encoding="ascii")
        key.write_text(TLS_KEY, encoding="ascii")
        key.chmod(0o600)
        proxy = MockHTTP(tls_files=(cert, key))
        self.addCleanup(proxy.close)
        proxy_password = "synthetic-tls-proxy-password"
        proxy_url = "https://synthetic-tls-proxy-user:" + proxy_password + "@" + urlsplit(proxy.url).netloc
        for ca in ({}, {"REQUESTS_CA_BUNDLE": str(cert)}, {"CURL_CA_BUNDLE": str(cert)}):
            with self.subTest(ca_variable=next(iter(ca), "default-certifi")):
                proxy.script(Reply.json(envelope({"verifiedProxy": True})))
                self.reply(envelope({}))
                result = self.run_cli("read", "--path", "x.md", env={
                    **ca, "FNS_PROXY": proxy_url, "NO_PROXY": "*", "FNS_TIMEOUT": "3"})
                if ca:
                    self.assert_success(result, {"verifiedProxy": True})
                    self.assertEqual(len(proxy.requests), 1)
                    request = proxy.requests[0]
                    self.assertTrue(request.target.startswith(self.mock.url + "/api/note?"))
                    self.assertEqual(request.headers.get("authorization"), "Bearer " + TOKEN)
                else:
                    self.assert_failure(result, 3, secrets=(proxy_url, proxy_password))
                    self.assertEqual(proxy.requests, [],
                                     "HTTP target must not disable certificate checks for the HTTPS proxy")
                self.assertEqual(self.mock.requests, [], "Explicit proxy must not be bypassed")
                self.assert_no_secret_output(result, (TOKEN, proxy_url, proxy_password))

    def test_https_proxy_trusted_ca_still_rejects_hostname_mismatch_before_http_auth(self):
        cert, key = self.root / "proxy-ca.pem", self.root / "proxy-key.pem"
        cert.write_text(TLS_DNS_ONLY_CERT, encoding="ascii")
        key.write_text(TLS_KEY, encoding="ascii")
        key.chmod(0o600)
        # macOS need not expose 127.0.0.2. Keep both connections on 127.0.0.1;
        # only localhost matches this certificate's DNS-only SAN.
        proxy = MockHTTP(tls_files=(cert, key))
        self.addCleanup(proxy.close)
        for ca_variable in ("REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
            with self.subTest(ca_variable=ca_variable):
                proxy.script(Reply.json(envelope({"hostname": True})))
                self.reply(envelope({}))
                matching = self.run_cli("read", "--path", "x.md", env={
                    "FNS_PROXY": proxy.url.replace("127.0.0.1", "localhost"),
                    ca_variable: str(cert), "NO_PROXY": "*", "FNS_TIMEOUT": "3"})
                self.assert_success(matching, {"hostname": True})
                self.assertEqual(len(proxy.requests), 1, "Verify this CA is actually trusted before mismatch check")
                self.assertEqual(proxy.requests[0].headers.get("authorization"), "Bearer " + TOKEN)
                proxy.script(Reply.json(envelope({})))
                self.reply(envelope({}))
                result = self.run_cli("read", "--path", "x.md", env={
                    "FNS_PROXY": proxy.url, ca_variable: str(cert), "NO_PROXY": "*", "FNS_TIMEOUT": "3"})
                self.assert_failure(result, 3)
                self.assertEqual(proxy.requests, [], "Trusted CA must not bypass proxy hostname verification")
                self.assertEqual(self.mock.requests, [])

    def tls_connect_fixture(self):
        cert, key = self.root / "shared-localhost-ca.pem", self.root / "shared-localhost-key.pem"
        cert.write_text(TLS_DNS_ONLY_CERT, encoding="ascii")
        key.write_text(TLS_KEY, encoding="ascii")
        key.chmod(0o600)
        # Exactly the same static certificate/CA serves the proxy and origin.
        # localhost matches; numeric 127.0.0.1 deliberately has no matching SAN.
        origin = MockHTTP(tls_files=(cert, key))
        self.addCleanup(origin.close)
        proxy = MockTLSConnectProxy((cert, key), origin.server.server_port)
        self.addCleanup(proxy.close)  # Stop the tunnel before stopping its origin.
        user, password = "synthetic-connect-user", "synthetic-connect-password"
        proxy_url = "https://" + user + ":" + password + "@" + urlsplit(proxy.url).netloc
        authorization = "Basic " + base64.b64encode((user + ":" + password).encode("ascii")).decode("ascii")
        return cert, origin, proxy, proxy_url, authorization

    def test_https_connect_tls_in_tls_trusts_both_layers_and_sends_bearer_only_to_origin(self):
        cert, origin, proxy, proxy_url, authorization = self.tls_connect_fixture()
        origin_url = origin.url.replace("127.0.0.1", "localhost")
        for index, ca_variable in enumerate(("REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE")):
            with self.subTest(ca_variable=ca_variable):
                origin.script(Reply.json(envelope({"tlsInTls": True})))
                result = self.run_cli("read", "--path", "tls-note.md", env={
                    "FNS_BASE_URL": origin_url, "FNS_PROXY": proxy_url,
                    ca_variable: str(cert), "NO_PROXY": "*", "FNS_TIMEOUT": "3"})
                self.assert_success(result, {"tlsInTls": True})
                self.assertEqual(len(proxy.connects), index + 1, "CONNECT must be attempted once per request")
                connect = proxy.connects[index]
                self.assertEqual((connect.method, connect.target), ("CONNECT", urlsplit(origin_url).netloc))
                self.assertEqual(connect.headers.get("proxy-authorization"), authorization)
                self.assertNotIn("authorization", connect.headers, "Service Bearer must not enter CONNECT")
                self.assertEqual(len(origin.requests), 1)
                request = origin.requests[0]
                self.assertEqual((request.method, request.path), ("GET", "/api/note"))
                self.query(request, {"vault": VAULT, "path": "tls-note.md"}, {"isRecycle": "false"})
                self.assertEqual(request.headers.get("authorization"), "Bearer " + TOKEN)
                self.assertEqual(request.headers.get("user-agent"), DEFAULT_AGENT)
                self.assertNotIn("proxy-authorization", request.headers,
                                 "Proxy credentials must not enter tunneled origin HTTP")
                self.assertEqual(proxy.errors, [])
                self.assertEqual(self.mock.requests, [])
                self.assert_no_secret_output(result, (TOKEN, proxy_url, authorization,
                                                       "synthetic-connect-password"))

    def test_https_connect_tls_in_tls_rejects_origin_hostname_before_origin_bearer(self):
        cert, origin, proxy, proxy_url, authorization = self.tls_connect_fixture()
        origin.script(Reply.json(envelope({})))
        result = self.run_cli("read", "--path", "tls-note.md", env={
            "FNS_BASE_URL": origin.url, "FNS_PROXY": proxy_url,
            "REQUESTS_CA_BUNDLE": str(cert), "NO_PROXY": "*", "FNS_TIMEOUT": "3"})
        self.assert_failure(result, 3, secrets=(proxy_url, "synthetic-connect-password"))
        self.assertEqual(len(proxy.connects), 1, "Trusted outer TLS must reach CONNECT exactly once")
        connect = proxy.connects[0]
        self.assertEqual(connect.target, urlsplit(origin.url).netloc)
        self.assertEqual(connect.headers.get("proxy-authorization"), authorization)
        self.assertNotIn("authorization", connect.headers)
        self.assertEqual(origin.requests, [], "Reject inner TLS hostname before sending origin HTTP/Bearer")
        self.assertEqual(proxy.errors, [])
        self.assertEqual(self.mock.requests, [])
        self.assert_no_secret_output(result, (TOKEN, proxy_url, authorization, "synthetic-connect-password"))

    def test_https_connect_tls_in_tls_rejects_untrusted_outer_before_connect_or_auth(self):
        _, origin, proxy, proxy_url, authorization = self.tls_connect_fixture()
        origin.script(Reply.json(envelope({})))
        result = self.run_cli("read", "--path", "tls-note.md", env={
            "FNS_BASE_URL": origin.url.replace("127.0.0.1", "localhost"),
            "FNS_PROXY": proxy_url, "NO_PROXY": "*", "FNS_TIMEOUT": "3"})
        self.assert_failure(result, 3, secrets=(proxy_url, "synthetic-connect-password"))
        self.assertEqual(proxy.connects, [], "Untrusted outer TLS must reject before CONNECT/proxy auth")
        self.assertEqual(origin.requests, [], "Untrusted proxy must not receive or forward origin Bearer")
        self.assertEqual(proxy.errors, [])
        self.assertEqual(self.mock.requests, [])
        self.assert_no_secret_output(result, (TOKEN, proxy_url, authorization, "synthetic-connect-password"))

    def test_slow_trickle_response_hits_total_wall_clock_deadline_without_write_retry(self):
        body = b'{"code":1,"data":"' + b'x' * 200 + b'"}'
        self.mock.script(Reply(body, chunk_delay=0.05), Reply.json(envelope({})))
        started = time.monotonic()
        try:
            result = subprocess.run(
                [sys.executable, "-B", str(SCRIPT), "create", "--path", "x.md", "--content", "body"],
                env={**self.env, "FNS_TIMEOUT": "1", "FNS_CONNECT_TIMEOUT": "10"},
                cwd=self.root, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                encoding="utf-8", timeout=4, check=False)
        except subprocess.TimeoutExpired:
            self.fail("FNS_TIMEOUT is a total deadline, not a reset-on-each-byte read timeout")
        elapsed = time.monotonic() - started
        self.assertGreater(elapsed, 0.7)
        self.assertLess(elapsed, 2.5)
        self.assert_failure(result, 3)
        self.assertEqual(len(self.mock.requests), 1, "An uncertain write must never be replayed")

    def test_total_deadline_is_per_request_not_entire_password_invocation(self):
        replies = [Reply.json(envelope({"token": LOGIN_TOKEN})), Reply.json(envelope({}))]
        for reply in replies:
            reply.header_delay = 0.6
        self.mock.script(*replies)
        started = time.monotonic()
        result = self.run_cli("read", "--path", "x.md", env={
            "FNS_AUTH_MODE": "password", "FNS_USERNAME": "synthetic-deadline-user",
            "FNS_PASSWORD": "synthetic-deadline-password", "FNS_TIMEOUT": "1",
            "FNS_CONNECT_TIMEOUT": "1"})
        self.assert_success(result)
        self.assertGreater(time.monotonic() - started, 1)
        self.assertEqual(len(self.mock.requests), 2)
        self.request("POST", "/api/user/login", 0, authenticated=False)
        self.request("GET", "/api/note", 1, client="webgui", token=LOGIN_TOKEN)

    def test_requests_socks_runtime_matches_approved_version_range(self):
        import requests
        import socks
        import urllib3
        version = tuple(int(part) for part in requests.__version__.split(".")[:3])
        self.assertGreaterEqual(version, (2, 34, 2))
        self.assertLess(version, (3, 0, 0))
        urllib_version = tuple(int(part) for part in urllib3.__version__.split(".")[:3])
        self.assertGreaterEqual(urllib_version, (2, 8, 0))
        self.assertLess(urllib_version, (3, 0, 0))
        self.assertTrue(callable(socks.socksocket))

    def test_requirements_declare_independent_https_proxy_tls_dependency(self):
        requirements = SCRIPT.parent.parent / "requirements.txt"
        self.assertTrue(requirements.is_file())
        # Comments and the new second dependency are legitimate requirements,
        # not unexpected drift from the earlier single-package declaration.
        entries = [line.split("#", 1)[0].strip().replace(" ", "")
                   for line in requirements.read_text(encoding="utf-8").splitlines()]
        self.assertIn("requests[socks]>=2.34.2,<3", entries)
        self.assertIn("urllib3>=2.8.0,<3", entries)

    def test_python_help_needs_no_installed_requests_or_configuration(self):
        result = subprocess.run([sys.executable, "-S", "-B", str(SCRIPT), "--help"],
                                env=self.env, cwd=self.root, capture_output=True,
                                encoding="utf-8", timeout=5, check=False)
        self.assertEqual(result.returncode, 0)
        self.assertIn("--vault", result.stdout)
        self.assertEqual(self.mock.requests, [])

    def test_thin_shell_wrapper_preserves_python_entry_output_and_exit(self):
        self.assertTrue(WRAPPER.is_file(), "The compatibility wrapper must exist")
        for arguments in (("--help",), ("read", "--path", "x.md"), ("unknown-command",)):
            with self.subTest(arguments=arguments):
                self.reply(envelope({"wrapper": True}))
                direct = self.run_cli(*arguments)
                self.reply(envelope({"wrapper": True}))
                wrapped = subprocess.run(["/bin/bash", str(WRAPPER), *arguments], env=self.env,
                                         cwd=self.root, capture_output=True, encoding="utf-8",
                                         timeout=8, check=False)
                self.assertEqual(wrapped.returncode, direct.returncode)
                if arguments == ("--help",):
                    self.assertEqual(wrapped.stdout, direct.stdout)
                else:
                    self.assertEqual(self.output(wrapped), self.output(direct))
                self.assertNotIn(TOKEN, wrapped.stdout + wrapped.stderr)

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
                    self.assertEqual(request.headers.get("user-agent"), DEFAULT_AGENT)
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

    def test_list_and_search_keywords_may_be_literal_option_names(self):
        for command in ("list", "search"):
            for keyword in ("--page", "--keyword", "--recycle", "--leading-text"):
                with self.subTest(command=command, keyword=keyword):
                    self.reply(envelope({"list": []}))
                    self.assert_success(self.run_cli(command, "--keyword", keyword, "--page", "2"))
                    self.assertEqual(len(self.mock.requests), 1)
                    query = self.request("GET", "/api/notes").query
                    self.assertEqual(query.get("keyword"), [keyword])
                    self.assertEqual(query.get("page"), ["2"])
                    self.assertEqual(query.get("isRecycle"), ["false"])

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
                        [sys.executable, "-B", str(SCRIPT), command, "--path", "x.md", "--stdin"],
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

    def test_invalid_utf8_literal_posix_argv_is_rejected_before_login_or_network(self):
        environment = {**self.env, "FNS_AUTH_MODE": "password",
                       "FNS_USERNAME": "synthetic-utf8-user", "FNS_PASSWORD": "synthetic-utf8-password"}
        for command in ("create", "upsert", "update", "append", "prepend"):
            for encoding, payload in INVALID_UTF8_CASES:
                with self.subTest(command=command, encoding=encoding):
                    self.reply(envelope({"token": LOGIN_TOKEN}), envelope({}))
                    # POSIX accepts byte argv. Python's surrogateescape must not
                    # let invalid literal content become JSON surrogate escapes.
                    raw = subprocess.run(
                        [sys.executable, "-B", str(SCRIPT), command, "--path", "x.md", "--content", payload],
                        env=environment, cwd=self.root, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        timeout=12, check=False)
                    self.assertFalse(bool(self.mock.requests), "Invalid literal UTF-8 must precede login")
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
                  b'{"code":1,"status":null}', b'{"code":1,"status":0}',
                  b'{"code":1,"status":[]}', b'{"code":1,"status":{}}',
                  b'{"code":428}', b'{"code":507}', b'{"code":508}']
        for body in bodies:
            with self.subTest(body=body):
                self.mock.script(Reply(body))
                self.assert_failure(self.run_cli("read", "--path", "x.md"), 4)
                self.assertEqual(len(self.mock.requests), 1)

    def test_strict_response_utf8_json_rejects_duplicates_nonfinite_and_surrogates(self):
        bodies = [b'{"code":1,"code":1}', b'{"code":1,"status":true,"status":false}',
                  b'{"code":1,"data":{"x":1,"x":2}}',
                  b'{"code":1,"data":{"nested":[{"x":1,"x":1}]}}',
                  b'{"code":1.0}', b'{"code":1e0}', b'{"code":1,"data":NaN}',
                  b'{"code":1,"data":Infinity}', b'{"code":1,"data":-Infinity}',
                  b'{"code":1,"data":1e999}',
                  b'{"code":1,"data":"\\ud800"}', b'{"code":1,"data":"\\udfff"}',
                  b'{"code":1,"data":{"\\ud800":"safe"}}',
                  b'{"code":1,"data":[{"x":"\\udfff"}]}']
        bodies += [b'{"code":1,"data":"' + raw + b'"}' for raw in
                   (b'\xe9', b'\xf4\x90\x80\x80', b'\xc0\x80', b'\xc1\xbf',
                    b'\xed\xa0\x80', b'\xe2\x82', b'\xf0\x9f\x92')]
        for body in bodies:
            with self.subTest(body=body):
                self.mock.script(Reply(body))
                self.assert_failure(self.run_cli("read", "--path", "x.md"), 4)
                self.assertEqual(len(self.mock.requests), 1)

    def test_strict_response_json_accepts_valid_replacement_and_maximum_scalars(self):
        body = b'{"code":1,"status":true,"data":{"\\udbff\\udfff":["\\ufffd","\\udbff\\udfff"]}}'
        self.mock.script(Reply(body))
        self.assert_success(self.run_cli("read", "--path", "x.md"),
                            {"\U0010ffff": ["\ufffd", "\U0010ffff"]})
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

    def test_live_runner_rejects_unsupported_urllib3_before_client_execution(self):
        import requests
        import urllib3
        runner_path = Path(__file__).with_name("run_live.py")
        spec = importlib.util.spec_from_file_location("fns_live_dependency_guard", runner_path)
        runner = importlib.util.module_from_spec(spec)
        environment = {**self.env, "FNS_VAULT": "fns-skill-test-offline-guard", "FNS_AUTH_MODE": "token"}
        with mock.patch.dict(os.environ, environment, clear=True), \
                mock.patch.object(sys, "dont_write_bytecode", True):
            spec.loader.exec_module(runner)
        for version in ("2.7.0", "3.0.0"):
            with self.subTest(urllib3_version=version):
                output, errors = io.StringIO(), io.StringIO()
                with mock.patch.dict(os.environ, environment, clear=True), \
                        mock.patch.object(urllib3, "__version__", version), \
                        mock.patch("subprocess.Popen", side_effect=AssertionError("Guard must not launch CLI")) as launch, \
                        mock.patch.object(requests.Session, "request", side_effect=AssertionError("Guard must not use transport")) as transport, \
                        contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                    code = runner.main(["--confirm-vault", environment["FNS_VAULT"], "--allow-clear"])
                self.assertEqual(code, 2)
                launch.assert_not_called()
                transport.assert_not_called()
                summary = json.loads(output.getvalue())
                self.assertIs(summary["success"], False)
                self.assertEqual((summary["pass"], summary["fail"], summary["skip"]), (0, 1, 0))
                self.assertEqual(summary["tests"][0]["name"], "scope_guard")
                self.assertNotIn(TOKEN, output.getvalue() + errors.getvalue())
                self.assertEqual(errors.getvalue(), "")
                self.assertEqual(self.mock.requests, [])

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

    def test_frontmatter_strict_json_rejects_duplicates_nonfinite_and_surrogates_before_login(self):
        options = [("--updates", value) for value in
                   ('{"x":1,"x":2}', '{"x":{"y":1,"y":1}}', '{"x":NaN}',
                    '{"x":Infinity}', '{"x":-Infinity}', '{"x":1e999}',
                    '{"x":"\\ud800"}', '{"\\udfff":"safe"}')]
        options += [("--remove", value) for value in ('["\\ud800"]', '["\\udfff"]')]
        for option, value in options:
            with self.subTest(option=option, value=value):
                self.reply(envelope({"token": LOGIN_TOKEN}), envelope({}))
                result = self.run_cli("frontmatter", "--path", "x.md", option, value, env={
                    "FNS_AUTH_MODE": "password", "FNS_USERNAME": "synthetic-json-user",
                    "FNS_PASSWORD": "synthetic-json-password"})
                self.assertFalse(bool(self.mock.requests), "Reject strict JSON errors before login or writes")
                self.assert_failure(result, 2, secrets=("synthetic-json-password",))

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


class TransportSecurityContractTests(CLIBase):
    """Inspect public Requests contracts through main(argv), never private APIs."""

    def setUp(self):
        super().setUp()
        import requests
        self.requests_library = requests
        self.env["FNS_BASE_URL"] = "https://contract.invalid"
        spec = importlib.util.spec_from_file_location("fns_contract_client", SCRIPT)
        self.client = importlib.util.module_from_spec(spec)
        module_patch = mock.patch.dict(sys.modules, {spec.name: self.client})
        module_patch.start()
        self.addCleanup(module_patch.stop)
        imported_output, imported_errors = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.object(sys, "dont_write_bytecode", True), \
                mock.patch.object(requests.Session, "request", side_effect=AssertionError(
                    "Importing the client must not initiate transport")), \
                mock.patch("socket.getaddrinfo", side_effect=AssertionError(
                    "Importing the client must not initiate DNS")), \
                contextlib.redirect_stdout(imported_output), contextlib.redirect_stderr(imported_errors):
            spec.loader.exec_module(self.client)
        self.assertEqual(imported_output.getvalue(), "", "Client imports must not print data or credentials")
        self.assertEqual(imported_errors.getvalue(), "", "Client imports must not print data or credentials")
        self.assertTrue(callable(getattr(self.client, "main", None)), "main(argv) is the stable test entry")
        self.transport_calls = []
        self.responses = []
        self.last_environment = {}
        self.last_logs = ""
        self.reply(envelope({}))

    def reply(self, *values, status=200, error=None):
        self.transport_calls.clear()
        self.responses = []
        for value in values:
            response = self.requests_library.Response()
            response.status_code = status
            response._content = json.dumps(value, ensure_ascii=False).encode("utf-8")
            response._content_consumed = True
            response.headers["Content-Type"] = "application/json"
            self.responses.append(response)
        if error is not None:
            self.responses.insert(0, error)

    def run_main(self, *arguments, env=None):
        environment = self.env.copy()
        for key, value in (env or {}).items():
            if value is None:
                environment.pop(key, None)
            else:
                environment[key] = str(value)
        self.last_environment = environment
        output, errors, logs = io.StringIO(), io.StringIO(), io.StringIO()
        before = {str(path.relative_to(self.root)): path.read_bytes()
                  for path in self.root.rglob("*") if path.is_file()}
        owner = self

        def request(session, method, url, **kwargs):
            prepared = session.prepare_request(owner.requests_library.Request(
                method=method, url=url, headers=kwargs.get("headers"), params=kwargs.get("params"),
                data=kwargs.get("data"), json=kwargs.get("json"), auth=kwargs.get("auth"),
                cookies=kwargs.get("cookies")))
            proxies = dict(session.proxies)
            proxies.update(kwargs.get("proxies") or {})
            verify = kwargs.get("verify")
            if verify is None:
                verify = session.verify
            if isinstance(verify, os.PathLike):
                verify = os.fspath(verify)
            owner.transport_calls.append({
                "request": prepared, "trust_env": session.trust_env, "proxies": proxies,
                "verify": verify, "kwargs": kwargs,
                "retries": {scheme: session.get_adapter(scheme + "://contract.invalid").max_retries.total
                            for scheme in ("http", "https")},
                "timer": signal.getitimer(signal.ITIMER_REAL)[0]})
            if not owner.responses:
                raise AssertionError("Unscripted Requests call")
            response = owner.responses.pop(0)
            if isinstance(response, BaseException):
                raise response
            response.url = prepared.url
            response.request = prepared
            return response

        handler = logging.StreamHandler(logs)
        logger = logging.getLogger()
        previous_level = logger.level
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)

        def readonly_open(original):
            def open_file(*args, **kwargs):
                mode = kwargs.get("mode", args[1] if len(args) > 1 else "r")
                if any(flag in mode for flag in ("w", "a", "x", "+")):
                    raise AssertionError("The client must not write transport/auth to disk")
                return original(*args, **kwargs)
            return open_file

        original_os_open = os.open

        def readonly_os_open(path, flags, *args, **kwargs):
            if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
                raise AssertionError("The client must not write transport/auth to disk")
            return original_os_open(path, flags, *args, **kwargs)

        try:
            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.dict(os.environ, environment, clear=True))
                stack.enter_context(mock.patch.object(sys, "argv", [str(SCRIPT), *arguments]))
                stack.enter_context(contextlib.redirect_stdout(output))
                stack.enter_context(contextlib.redirect_stderr(errors))
                stack.enter_context(mock.patch.object(self.requests_library.Session, "request", request))
                stack.enter_context(mock.patch("socket.create_connection", side_effect=AssertionError(
                    "In-process transport tests must not open sockets")))
                stack.enter_context(mock.patch("socket.getaddrinfo", side_effect=AssertionError(
                    "In-process transport tests must not resolve real hosts")))
                stack.enter_context(mock.patch("builtins.open", new=readonly_open(open)))
                stack.enter_context(mock.patch("io.open", new=readonly_open(io.open)))
                stack.enter_context(mock.patch("os.open", new=readonly_os_open))
                # The Python runtime must not spawn credential-bearing helpers
                # or write secret curl configs/body files, even transiently.
                for name in ("subprocess.run", "subprocess.Popen", "tempfile.mkstemp",
                             "tempfile.mkdtemp", "tempfile.NamedTemporaryFile"):
                    stack.enter_context(mock.patch(name, side_effect=AssertionError(
                        "The client must keep transport/auth entirely in memory")))
                code = self.client.main(list(arguments))
        finally:
            logger.removeHandler(handler)
            logger.setLevel(previous_level)
        self.last_logs = logs.getvalue()
        after = {str(path.relative_to(self.root)): path.read_bytes()
                 for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(after, before, "Client calls must leave no disk artifacts")
        return subprocess.CompletedProcess([sys.executable, str(SCRIPT), *arguments], code,
                                           output.getvalue(), errors.getvalue())

    def assert_transport_security(self, secrets=()):
        self.assertTrue(self.transport_calls)
        expected_ca = (self.last_environment.get("REQUESTS_CA_BUNDLE") or
                       self.last_environment.get("CURL_CA_BUNDLE") or True)
        for record in self.transport_calls:
            self.assertIs(record["trust_env"], False, "Disable Requests netrc/environment overrides")
            self.assertIs(record["kwargs"].get("allow_redirects"), False)
            self.assertEqual(record["verify"], expected_ca, "TLS verification is mandatory")
            self.assertEqual(record["retries"], {"http": 0, "https": 0})
            for secret in (TOKEN, LOGIN_TOKEN, *secrets):
                if secret:
                    self.assertNotIn(secret, record["request"].url, "Service tokens cannot enter URLs")
                    self.assertNotIn(secret, self.last_logs, "Transport diagnostics must not log credentials")
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_https_base_url_host_globs_are_rejected_before_transport(self):
        for base in ("https://{one,two}.contract.invalid", "https://contract{1,2}.invalid"):
            with self.subTest(base=base):
                self.reply(envelope({"token": LOGIN_TOKEN}), envelope({}))
                result = self.run_main("read", "--path", "x.md", env={
                    "FNS_BASE_URL": base, "FNS_AUTH_MODE": "password",
                    "FNS_USERNAME": "synthetic-glob-user", "FNS_PASSWORD": "synthetic-glob-password"})
                self.assertEqual(self.transport_calls, [])
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
                    self.reply(*responses, envelope(data))
                    result = self.run_main("read", "--path", "x.md", env={
                        "FNS_AUTH_MODE": mode, "FNS_USERNAME": username,
                        "FNS_PASSWORD": password, "FNS_PROXY": proxy})
                    value = self.assert_success(result)
                    self.assertEqual(value["data"]["safe"], safe)
                    self.assert_no_secret_output(result, secrets)
                    self.assertEqual(len(self.transport_calls), 2 if mode == "password" else 1)
                    self.assert_transport_security(secrets)

    def test_proxy_schemes_and_credentials_apply_to_every_password_request(self):
        proxy_user, proxy_password = "synthetic-proxy-user", "synthetic-proxy-password"
        for scheme in ("socks5h", "socks5", "http", "https"):
            for authenticated in (False, True):
                with self.subTest(scheme=scheme, authenticated=authenticated):
                    userinfo = proxy_user + ":" + proxy_password + "@" if authenticated else ""
                    proxy = scheme + "://" + userinfo + "proxy.invalid:1080"
                    password, username = 'synthetic-login-password "\\\n', 'synthetic-login-user "\\'
                    self.reply(envelope({"token": LOGIN_TOKEN}), envelope({"note": "x"}))
                    result = self.run_main("upsert", "--path", "x.md", "--content", "body", env={
                        "FNS_PROXY": proxy, "FNS_AUTH_MODE": "password", "FNS_USERNAME": username,
                        "FNS_PASSWORD": password, "NO_PROXY": "*", "no_proxy": "*",
                        "http_proxy": "http://must-not-use.invalid", "https_proxy": "http://must-not-use.invalid"})
                    self.assert_success(result, {"note": "x"})
                    self.assertEqual(len(self.transport_calls), 2)
                    secrets = (proxy, proxy_user, proxy_password, password, username)
                    self.assert_no_secret_output(result, (TOKEN, LOGIN_TOKEN, *secrets))
                    self.assert_transport_security(secrets)
                    for index, record in enumerate(self.transport_calls):
                        self.assertEqual(record["proxies"].get("http"), proxy)
                        self.assertEqual(record["proxies"].get("https"), proxy)
                        headers = record["request"].headers
                        self.assertEqual(headers.get("x-client"), "webgui")
                        self.assertEqual(headers.get("User-Agent"), DEFAULT_AGENT)
                        if index:
                            self.assertEqual(headers.get("Authorization"), "Bearer " + LOGIN_TOKEN)
                        else:
                            self.assertNotIn("Authorization", headers)
                            self.assertEqual(json.loads(record["request"].body),
                                             {"credentials": username, "password": password})

    def test_proxy_applies_to_public_doctor_and_each_update_request(self):
        proxy = "socks5h://synthetic-public-proxy-user:synthetic-public-proxy-password@proxy.invalid:1080"
        cases = [(('doctor',), [envelope({"health": True}), envelope({"version": "3.6.1"})]),
                 (('update', '--path', 'x.md', '--content', 'new'),
                  [envelope({"ctime": 123, "contentHash": "h"}), envelope({})])]
        for arguments, responses in cases:
            with self.subTest(arguments=arguments):
                self.reply(*responses)
                result = self.run_main(*arguments, env={"FNS_PROXY": proxy})
                self.assert_success(result)
                self.assert_no_secret_output(result, (proxy, "synthetic-public-proxy-password"))
                self.assertEqual(len(self.transport_calls), 2)
                self.assert_transport_security((proxy, "synthetic-public-proxy-password"))
                for record in self.transport_calls:
                    self.assertEqual(record["proxies"].get("https"), proxy)
                    if arguments[0] == "doctor":
                        self.assertNotIn("Authorization", record["request"].headers)

    def test_percent_encoded_proxy_credentials_stay_in_memory(self):
        proxy_user, proxy_password = "synthetic-escaped-proxy-user%40local", "synthetic-escaped-proxy-password%3A%22%5C"
        proxy = "https://" + proxy_user + ":" + proxy_password + "@proxy.invalid:8443"
        secrets = (proxy, proxy_user, proxy_password, unquote(proxy_user), unquote(proxy_password))
        self.reply(envelope({}))
        result = self.run_main("read", "--path", "x.md", env={"FNS_PROXY": proxy})
        self.assert_success(result)
        self.assert_no_secret_output(result, secrets)
        self.assertEqual(len(self.transport_calls), 1)
        self.assert_transport_security(secrets)
        self.assertEqual(self.transport_calls[0]["proxies"].get("https"), proxy)

    def test_encoded_and_decoded_proxy_credentials_are_redacted_from_errors(self):
        proxy_user, proxy_password = "synthetic-redacted-proxy-user%40local", "synthetic-redacted-proxy-password%3Asecret"
        proxy = "http://" + proxy_user + ":" + proxy_password + "@proxy.invalid:8080"
        secrets = (proxy, proxy_user, proxy_password, unquote(proxy_user), unquote(proxy_password))
        for status, exit_code in ((200, 5), (500, 3)):
            with self.subTest(status=status):
                self.reply(envelope(None, code=305, message="server echoed " + " ".join(secrets)), status=status)
                result = self.run_main("read", "--path", "x.md", env={"FNS_PROXY": proxy})
                self.assert_failure(result, exit_code, secrets=secrets)
                self.assertEqual(len(self.transport_calls), 1)
                self.assert_transport_security(secrets)

    def test_total_timeout_defaults_overrides_and_connect_cap(self):
        cases = [({}, 30, 10), ({"FNS_TIMEOUT": "5", "FNS_CONNECT_TIMEOUT": "2"}, 5, 2),
                 ({"FNS_TIMEOUT": "1", "FNS_CONNECT_TIMEOUT": "10"}, 1, 1)]
        for environment, total, connect in cases:
            with self.subTest(environment=environment):
                self.reply(envelope({}))
                self.assert_success(self.run_main("read", "--path", "x.md", env=environment))
                self.assertEqual(len(self.transport_calls), 1)
                self.assert_transport_security()
                record = self.transport_calls[0]
                timeout = record["kwargs"].get("timeout")
                self.assertIsInstance(timeout, tuple)
                self.assertEqual(len(timeout), 2)
                self.assertGreater(timeout[0], 0)
                self.assertLessEqual(timeout[0], connect)
                self.assertGreater(timeout[1], 0)
                self.assertLessEqual(timeout[1], total)
                self.assertGreater(record["timer"], 0, "A total POSIX deadline must cover request execution")
                self.assertLessEqual(record["timer"], total)

    def test_nonloopback_http_requires_explicit_allowance(self):
        for allowance in (None, "0", "1"):
            with self.subTest(allowance=allowance):
                self.reply(envelope({}))
                result = self.run_main("read", "--path", "x.md", env={
                    "FNS_BASE_URL": "http://contract.invalid", "FNS_ALLOW_HTTP": allowance})
                if allowance == "1":
                    self.assert_success(result)
                    self.assertEqual(len(self.transport_calls), 1)
                    self.assert_transport_security()
                else:
                    self.assert_failure(result, 2)
                    self.assertEqual(self.transport_calls, [])

    def test_config_newline_injection_is_rejected_before_transport(self):
        for name in ("FNS_BASE_URL", "FNS_TOKEN", "FNS_CLIENT", "FNS_USER_AGENT", "FNS_PROXY"):
            for separator in ("\n", "\r\n"):
                with self.subTest(name=name, separator=repr(separator)):
                    self.reply(envelope({}))
                    base = {"FNS_BASE_URL": "https://contract.invalid", "FNS_TOKEN": TOKEN,
                            "FNS_CLIENT": "client", "FNS_USER_AGENT": "agent",
                            "FNS_PROXY": "http://proxy.invalid:1080"}[name]
                    malicious = base + separator + 'header = "X-Injected: yes"'
                    self.assert_failure(self.run_main("read", "--path", "x.md", env={name: malicious}), 2)
                    self.assertEqual(self.transport_calls, [])

    def test_malformed_proxy_credentials_are_not_echoed(self):
        password = "synthetic-invalid-proxy-password"
        proxy = "http://synthetic-user:" + password + '@proxy.invalid:8080\nheader = "X-Injection: yes"'
        self.reply(envelope({}))
        result = self.run_main("read", "--path", "x.md", env={"FNS_PROXY": proxy})
        self.assert_failure(result, 2, secrets=(password, proxy))
        self.assertEqual(self.transport_calls, [])

    def test_quotes_and_backslashes_are_safe_in_transport_headers(self):
        token, client, agent = ('synthetic-quoted-token-"\\-end', 'synthetic-client-"\\-end',
                                'synthetic-agent-"\\-end')
        self.reply(envelope({}))
        self.assert_success(self.run_main("read", "--path", "x.md", env={
            "FNS_TOKEN": token, "FNS_CLIENT": client, "FNS_USER_AGENT": agent}))
        self.assertEqual(len(self.transport_calls), 1)
        self.assert_transport_security((token,))
        headers = self.transport_calls[0]["request"].headers
        self.assertEqual(headers.get("Authorization"), "Bearer " + token)
        self.assertEqual(headers.get("x-client"), client)
        self.assertEqual(headers.get("User-Agent"), agent)
        self.assertNotIn("X-Injected", headers)

    def test_transport_errors_redact_credentials_and_do_not_retry(self):
        password = "synthetic-login-password-sensitive"
        proxy_user, proxy_password = "synthetic-error-proxy-user", "synthetic-error-proxy-password"
        proxy = "http://" + proxy_user + ":" + proxy_password + "@proxy.invalid:8080"
        secrets = (password, proxy, proxy_user, proxy_password)
        exceptions = self.requests_library.exceptions
        for kind in (exceptions.ProxyError, exceptions.ConnectionError, exceptions.ChunkedEncodingError,
                     exceptions.ReadTimeout, exceptions.ConnectTimeout, exceptions.SSLError):
            with self.subTest(exception=kind.__name__):
                self.reply(envelope({}), error=kind("failure " + " ".join((TOKEN, *secrets))))
                result = self.run_main("create", "--path", "x.md", "--content", "x", env={
                    "FNS_PROXY": proxy, "FNS_PASSWORD": password})
                self.assert_failure(result, 3, secrets=secrets)
                self.assertEqual(len(self.transport_calls), 1)
                self.assert_transport_security(secrets)

    def test_business_and_http_messages_redact_all_known_secrets(self):
        password = "synthetic-login-password-sensitive"
        proxy_user, proxy_password = "synthetic-error-proxy-user", "synthetic-error-proxy-password"
        proxy = "http://" + proxy_user + ":" + proxy_password + "@proxy.invalid:8080"
        secrets = (password, proxy, proxy_user, proxy_password)
        for status, exit_code in ((200, 5), (500, 3)):
            with self.subTest(status=status):
                self.reply(envelope(None, code=305, message="failure " + " ".join((TOKEN, *secrets))), status=status)
                result = self.run_main("read", "--path", "x.md", env={"FNS_PROXY": proxy, "FNS_PASSWORD": password})
                self.assert_failure(result, exit_code, secrets=secrets)
                self.assertEqual(len(self.transport_calls), 1)
                self.assert_transport_security(secrets)

    def test_environment_proxies_selected_manually_with_no_proxy_when_explicit_proxy_unset(self):
        for scheme in ("http", "https"):
            for bypass in (False, True):
                with self.subTest(scheme=scheme, bypass=bypass):
                    proxy = "http://synthetic-env-proxy:8080"
                    self.reply(envelope({}))
                    result = self.run_main("read", "--path", "x.md", env={
                        "FNS_BASE_URL": scheme + "://contract.invalid", "FNS_ALLOW_HTTP": "1",
                        "http_proxy": proxy, "https_proxy": proxy, "NO_PROXY": "*" if bypass else ""})
                    self.assert_success(result)
                    self.assert_transport_security()
                    proxies = self.transport_calls[0]["proxies"]
                    if bypass:
                        self.assertFalse(proxies.get(scheme) or proxies.get("all"))
                    else:
                        self.assertEqual(proxies.get(scheme), proxy)

    def test_ca_bundle_priority_and_verification_remain_enabled(self):
        requests_ca = str(self.root / "requests-ca.pem")
        curl_ca = str(self.root / "curl-ca.pem")
        Path(requests_ca).write_text(TLS_CERT, encoding="ascii")
        Path(curl_ca).write_text(TLS_CERT, encoding="ascii")
        for environment in ({}, {"CURL_CA_BUNDLE": curl_ca}, {"REQUESTS_CA_BUNDLE": requests_ca},
                            {"REQUESTS_CA_BUNDLE": requests_ca, "CURL_CA_BUNDLE": curl_ca},
                            {"REQUESTS_CA_BUNDLE": "", "CURL_CA_BUNDLE": curl_ca}):
            with self.subTest(environment=environment):
                self.reply(envelope({}))
                self.assert_success(self.run_main("read", "--path", "x.md", env=environment))
                self.assert_transport_security()

    def test_signal_handler_and_existing_timer_restored_on_success_and_failure(self):
        original_handler = signal.getsignal(signal.SIGALRM)
        original_timer = signal.getitimer(signal.ITIMER_REAL)
        sentinel = lambda *_: None
        try:
            for error in (None, self.requests_library.exceptions.ReadTimeout("synthetic-timeout")):
                with self.subTest(failure=error is not None):
                    signal.signal(signal.SIGALRM, sentinel)
                    signal.setitimer(signal.ITIMER_REAL, 20, 3)
                    self.reply(envelope({}), error=error)
                    result = self.run_main("read", "--path", "x.md")
                    if error is None:
                        self.assert_success(result)
                    else:
                        self.assert_failure(result, 3)
                    self.assertIs(signal.getsignal(signal.SIGALRM), sentinel)
                    remaining, interval = signal.getitimer(signal.ITIMER_REAL)
                    self.assertGreater(remaining, 17)
                    self.assertLessEqual(remaining, 20)
                    self.assertEqual(interval, 3)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, original_handler)
            signal.setitimer(signal.ITIMER_REAL, *original_timer)

    def test_total_deadline_includes_response_json_decoding(self):
        self.reply(envelope({"delayed_decode_probe": True}))
        real_decode = json.JSONDecoder.raw_decode

        def slow_decode(decoder, value, *arguments, **kwargs):
            if "delayed_decode_probe" in value:
                time.sleep(2)
            return real_decode(decoder, value, *arguments, **kwargs)

        started = time.monotonic()
        # The public stdlib decoder seam also covers imported json.loads aliases
        # and directly constructed decoders, without naming client internals.
        with mock.patch.object(json.JSONDecoder, "raw_decode", new=slow_decode):
            result = self.run_main("read", "--path", "x.md", env={"FNS_TIMEOUT": "1"})
        self.assertLess(time.monotonic() - started, 1.8)
        self.assert_failure(result, 3)
        self.assertEqual(len(self.transport_calls), 1)

    def test_total_deadline_includes_environment_proxy_selection_and_restores_timer(self):
        original_handler = signal.getsignal(signal.SIGALRM)
        original_timer = signal.getitimer(signal.ITIMER_REAL)
        sentinel = lambda *_: None
        self.reply(envelope({}))
        try:
            signal.signal(signal.SIGALRM, sentinel)
            signal.setitimer(signal.ITIMER_REAL, 20, 3)
            with mock.patch.object(self.requests_library.utils, "get_environ_proxies",
                                   side_effect=lambda *_: (time.sleep(1.4) or {})) as select_proxy:
                started = time.monotonic()
                result = self.run_main("read", "--path", "x.md", env={"FNS_TIMEOUT": "1", "FNS_PROXY": None})
                elapsed = time.monotonic() - started
            self.assertGreater(elapsed, 0.7, "Exercise the actual selection deadline")
            self.assertLess(elapsed, 1.8, "Proxy selection must be inside the total request deadline")
            select_proxy.assert_called_once()
            self.assert_failure(result, 3)
            self.assertEqual(self.transport_calls, [], "Deadline must expire before transport is invoked")
            self.assertIs(signal.getsignal(signal.SIGALRM), sentinel)
            remaining, interval = signal.getitimer(signal.ITIMER_REAL)
            self.assertGreater(remaining, 17)
            self.assertLess(remaining, 20)
            self.assertEqual(interval, 3)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, original_handler)
            signal.setitimer(signal.ITIMER_REAL, *original_timer)


if __name__ == "__main__":
    unittest.main()
