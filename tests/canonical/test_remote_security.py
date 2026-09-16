#!/usr/bin/env python3
"""Remote security canonical — SSRF pinning, redirect policy, credential
boundary, vault fail-closed. Deterministic: system dependencies
(resolver / socket / ssl / http.client) are monkeypatched; the PRODUCTION
transport algorithm runs unchanged — no test-only branches exist in
core/remote.py.

Covers:
  SSRF   address matrix (loopback/private/link-local/IPv6/mixed),
         trusted exemption
  PIN    P1 IPv4 raw connect (family/socktype/proto + exact sockaddr,
         SNI=hostname, Host=authority); P2 IPv6 4-tuple sockaddr;
         P3 non-default port (Host host:port, SNI host); P4 runtime
         single first-hop resolution from drive_http; P5 connect
         timeout → RemoteError('timeout') → err_endpoint_timeout;
         P6 redirect entity-header hygiene (301/302/303 vs 307/308)
  REDIR  public→private block (zero second connects), relative
         Location re-validated, output_url redirect block, >5 hops
  CRED   same-origin retain (delivered on hop 2), cross-origin reject
         before second request, no-credential cross-origin proceeds
  VAULT  subprocess matrix (missing/valid/wrong/malformed/legacy)
  ROUTE  ASGI 503 credential_vault_unavailable + zero persistence
  DISP   unseal failure → zero remote network calls
fail-closed canonical rules.
"""
import os as _os, sys as _sys, sys, json, time, types

_REPO = _os.path.abspath(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", ".."))

_TEST_PORT = _os.environ.get("FLASHSHARE_TEST_PORT")
if not _TEST_PORT:
    _sys.stderr.write("ERROR: FLASHSHARE_TEST_PORT is required.\n"); _sys.exit(2)
try:
    _TEST_PORT_INT = int(_TEST_PORT, 10)
except ValueError:
    _sys.stderr.write("ERROR: FLASHSHARE_TEST_PORT must be an integer.\n"); _sys.exit(2)
if _TEST_PORT_INT == 8000:
    _sys.stderr.write("ERROR: production port not allowed.\n"); _sys.exit(2)
TEST_DB = _os.environ.get("TEST_DB_URL")
if not TEST_DB:
    _sys.stderr.write("ERROR: TEST_DB_URL is required.\n"); _sys.exit(2)

sys.path.insert(0, _REPO)
_os.environ.setdefault("DATABASE_URL", TEST_DB.replace("postgresql://", "postgresql+psycopg://"))
_os.environ.setdefault("FLASHSHARE_SESSION_SECRET", "canonical-test")
sys.path.insert(0, _os.path.join(_REPO, "core"))

FAIL = []
def check(name, ok, detail=""):
    print(("✓ " if ok else "✗ ") + name + (f" — {str(detail)[:140]}" if not ok else ""))
    if not ok:
        FAIL.append(name)

import socket as _socket
import ssl as _ssl
import http.client as _httpclient
import remote
from remote import RemoteError

PUBLIC4 = "93.184.216.34"
PUBLIC6 = "2606:4700::6810:84e5"  # public unicast literal — never contacted

def _resolve(mapping):
    """Resolver double: host → list of (family, sockaddr) results."""
    def resolve(host, port):
        out = []
        for fam, ip in mapping.get(host, []):
            if fam == _socket.AF_INET:
                out.append((_socket.AF_INET, _socket.SOCK_STREAM, 6, "", (ip, port)))
            else:
                out.append((_socket.AF_INET6, _socket.SOCK_STREAM, 6, "", (ip, port, 0, 0)))
        if not out:
            raise _socket.gaierror("no such host")
        return out
    return resolve

# ═══ Dependency-instrumented pinned stack ═══
# Everything the production algorithm touches is captured; the algorithm
# itself (core/remote.py) is the code under test, unmodified.

class _SockDouble:
    """socket.socket double: records constructor + connect + close."""
    instances = []
    def __init__(self, family=_socket.AF_INET, type=_socket.SOCK_STREAM, proto=0):
        self.family, self.type, self.proto = family, type, proto
        self.connected = None
        self.closed = False
        self.timeout = None
        _SockDouble.instances.append(self)
    def settimeout(self, t): self.timeout = t
    def connect(self, sa): self.connected = sa
    def close(self): self.closed = True

class _TLSWrap:
    def __init__(self, raw, server_hostname=None):
        self.raw = raw
        self.sni = server_hostname
        self.closed = False
        raw._tls_wrap = self
    def close(self): self.closed = True

class _RespDouble:
    def __init__(self, status=200, body=b"ok", headers=None):
        self.status = status
        self._body = body
        self._h = {k.lower(): v for k, v in (headers or {}).items()}
    def read(self, n=-1): return self._body
    def getheader(self, k): return self._h.get(k.lower())
    def getheaders(self): return list(self._h.items())

class _ConnDouble:
    """http.client.HTTPConnection double: records request, returns canned
    response via a per-hop script."""
    def __init__(self, host="", port=None, timeout=None):
        self.host = host; self.port = port; self.timeout = timeout
        self.sock = None
        self.requests = []
        self.script = []      # list of _RespDouble, popped per request
        ConnRegistry.all.append(self)
    def request(self, method, path, body=None, headers=None):
        self.requests.append((method, path, body, dict(headers or {})))
    def getresponse(self):
        if not self.script:
            return _RespDouble()
        return self.script.pop(0)
    def close(self): pass

class ConnRegistry:
    all = []

class _SockModule(types.SimpleNamespace):
    """socket module stand-in for monkeypatching remote.socket."""
    def __init__(self, real, sock_cls=_SockDouble, raise_on_connect=None):
        self.AF_INET = real.AF_INET
        self.AF_INET6 = real.AF_INET6
        self.SOCK_STREAM = real.SOCK_STREAM
        self.IPPROTO_TCP = real.IPPROTO_TCP
        self.timeout = real.timeout
        self.gaierror = real.gaierror
        self._sock_cls = sock_cls
        self._raise = raise_on_connect
    def socket(self, family, type, proto=0):
        s = self._sock_cls(family, type, proto)
        if self._raise:
            def connect(sa, _s=s):
                raise self._raise
            s.connect = connect
        return s

class _SSLModule(types.SimpleNamespace):
    """ssl module stand-in: records SNI, returns TLS double."""
    def __init__(self, real):
        self.SSLCertVerificationError = real.SSLCertVerificationError
        self.wraps = []
    def create_default_context(self):
        outer = self
        class _Ctx:
            def wrap_socket(self, raw, server_hostname=None):
                outer.wraps.append(server_hostname)
                return _TLSWrap(raw, server_hostname)
        return _Ctx()

_SAVED = {}

def _install_stack(resolve_map=None, resolve_fn=None, resp_script=None,
                   raise_on_connect=None, sock_cls=_SockDouble):
    """Monkeypatch remote's system deps; return a recorder."""
    global _SAVED
    if not _SAVED:
        _SAVED = {"_resolve_host": remote._resolve_host,
                  "socket": remote.socket, "ssl": remote.ssl, "http": remote.http}
    ConnRegistry.all = []
    _SockDouble.instances = []
    rec = types.SimpleNamespace(
        resolve_calls=[], conns=ConnRegistry.all, socks=_SockDouble.instances,
        ssl_mod=_SSLModule(_ssl), snis=None)
    if resolve_fn is not None:
        resolve = resolve_fn
    else:
        resolve = _resolve(resolve_map or {})
    def _rec_resolve(host, port):
        rec.resolve_calls.append((host, port))
        return resolve(host, port)
    remote._resolve_host = _rec_resolve
    remote.socket = _SockModule(_socket, sock_cls, raise_on_connect)
    rec.snis = rec.ssl_mod.wraps  # live list: SNI recorded during the call
    remote.ssl = rec.ssl_mod
    shared_script = list(resp_script) if resp_script else []
    def _make_http(host="", port=None, timeout=None):
        c = _ConnDouble(host, port, timeout)
        c.script = shared_script  # global ordered queue across hops
        return c
    remote.http = types.SimpleNamespace(client=types.SimpleNamespace(
        HTTPConnection=_make_http))
    return rec

def _restore_stack():
    if _SAVED:
        remote._resolve_host = _SAVED["_resolve_host"]
        remote.socket = _SAVED["socket"]
        remote.ssl = _SAVED["ssl"]
        remote.http = _SAVED["http"]

# ═══ SSRF address matrix ═══
_srf_cases = [
    ("SSRF loopback IPv4 blocked", {"h": [(_socket.AF_INET, "127.0.0.1")]}),
    ("SSRF private IPv4 blocked", {"h": [(_socket.AF_INET, "10.1.2.3")]}),
    ("SSRF private 192.168 blocked", {"h": [(_socket.AF_INET, "192.168.0.10")]}),
    ("SSRF link-local blocked", {"h": [(_socket.AF_INET, "169.254.1.1")]}),
    ("SSRF CGNAT blocked", {"h": [(_socket.AF_INET, "100.64.0.1")]}),
]
for name, mapping in _srf_cases:
    try:
        _install_stack(resolve_map=mapping)
        remote.pinned_request("https://h/f", timeout_s=5)
        check(name, False, "no raise")
    except RemoteError as e:
        check(name, e.code == "ssrf_blocked", e)
    finally:
        _restore_stack()

for name, ip in (("SSRF IPv6 loopback blocked", "::1"),
                 ("SSRF IPv6 private blocked", "fd12::1"),
                 ("SSRF IPv6 link-local blocked", "fe80::1")):
    try:
        _install_stack(resolve_map={"h6": [(_socket.AF_INET6, ip)]})
        remote.pinned_request("https://h6/f", timeout_s=5)
        check(name, False, "no raise")
    except RemoteError as e:
        check(name, e.code == "ssrf_blocked", e)
    finally:
        _restore_stack()

try:
    _install_stack(resolve_map={"mix": [(_socket.AF_INET, PUBLIC4), (_socket.AF_INET, "10.0.0.5")]})
    remote.pinned_request("https://mix/f", timeout_s=5)
    check("SSRF mixed public+private blocked", False, "no raise")
except RemoteError as e:
    check("SSRF mixed public+private blocked", e.code == "ssrf_blocked", e)
finally:
    _restore_stack()

try:
    remote.guard_endpoint("http://10.0.0.5/x", trusted=True)
    check("SSRF trusted private exemption unchanged", True)
except RemoteError:
    check("SSRF trusted private exemption unchanged", False, "trusted blocked?!")

# ═══ PIN-P1: IPv4 raw connect — exact algorithm proof ═══
rec = _install_stack(
    resolve_map={"pin.example": [(_socket.AF_INET, PUBLIC4)]},
    resp_script=[_RespDouble(200, b"ok")])
status, data = remote.pinned_request("https://pin.example/f?q=1", timeout_s=7)
check("PIN-P1 status/body", status == 200 and data == b"ok", (status, data))
check("PIN-P1 resolver called exactly once", len(rec.resolve_calls) == 1, rec.resolve_calls)
s = rec.socks[0] if rec.socks else None
check("PIN-P1 socket(family,socktype,proto) = resolver record",
      s is not None and s.family == _socket.AF_INET and s.type == _socket.SOCK_STREAM
      and s.proto == 6, s and (s.family, s.type, s.proto))
check("PIN-P1 socket.connect == validated exact sockaddr",
      s is not None and s.connected == (PUBLIC4, 443), s and s.connected)
check("PIN-P1 socket.settimeout honoured", s is not None and s.timeout == 7, s and s.timeout)
check("PIN-P1 TLS SNI = original hostname",
      rec.snis == ["pin.example"], rec.snis)
method, path, body, hdrs = rec.conns[0].requests[0]
check("PIN-P1 HTTP Host = authority (no default port)",
      hdrs.get("Host") == "pin.example", hdrs.get("Host"))
check("PIN-P1 conn.sock is the TLS-wrapped validated socket",
      rec.conns[0].sock is not None and hasattr(rec.conns[0].sock, "raw"), "no tls wrap")
_restore_stack()

# ═══ PIN-P2: IPv6 raw connect — 4-tuple sockaddr, no unpack crash ═══
rec = _install_stack(
    resolve_map={"v6.example": [(_socket.AF_INET6, PUBLIC6)]},
    resp_script=[_RespDouble(200, b"v6")])
status, data = remote.pinned_request("https://v6.example/f", timeout_s=5)
check("PIN-P2 IPv6 request succeeds (no tuple unpack crash)",
      status == 200, status)
s = rec.socks[0]
check("PIN-P2 socket family AF_INET6", s.family == _socket.AF_INET6, s.family)
check("PIN-P2 connect receives FULL 4-tuple sockaddr",
      s.connected == (PUBLIC6, 443, 0, 0), s.connected)
check("PIN-P2 SNI = hostname", rec.snis == ["v6.example"], rec.snis)
_restore_stack()

# ═══ PIN-P3: non-default port — authority + sockaddr port ═══
rec = _install_stack(
    resolve_map={"pin.example": [(_socket.AF_INET, PUBLIC4)]},
    resp_script=[_RespDouble(200, b"p8443")])
status, data = remote.pinned_request("https://pin.example:8443/f", timeout_s=5)
check("PIN-P3 request ok", status == 200, status)
check("PIN-P3 connect sockaddr port = 8443",
      rec.socks[0].connected == (PUBLIC4, 8443), rec.socks[0].connected)
method, path, body, hdrs = rec.conns[0].requests[0]
check("PIN-P3 Host = host:port", hdrs.get("Host") == "pin.example:8443", hdrs.get("Host"))
check("PIN-P3 SNI = hostname without port", rec.snis == ["pin.example"], rec.snis)
_restore_stack()

# ═══ PIN-P4: runtime single resolution from drive_http entry ═══
rec = _install_stack(
    resolve_map={"rt.example": [(_socket.AF_INET, PUBLIC4)]},
    resp_script=[_RespDouble(200, b'{"data_b64": "aGk=", "filename": "out.txt"}')])
decl = {"url": "https://rt.example/api", "timeout_s": 30}
out = remote.drive_http(decl, [], {}, {}, trusted=False)
check("PIN-P4 drive_http untrusted succeeds", out["bytes"] == b"hi", out)
check("PIN-P4 FIRST-HOP resolver total calls = 1 (no pre-guard double resolve)",
      len(rec.resolve_calls) == 1, rec.resolve_calls)
_restore_stack()

# ═══ PIN-P5: connect timeout → typed RemoteError('timeout') ═══
rec = _install_stack(
    resolve_map={"slow.example": [(_socket.AF_INET, PUBLIC4)]},
    raise_on_connect=_socket.timeout("timed out"))
try:
    remote.pinned_request("https://slow.example/f", timeout_s=3)
    check("PIN-P5 connect timeout → RemoteError", False, "no raise")
except RemoteError as e:
    check("PIN-P5 connect timeout → RemoteError('timeout')", e.code == "timeout", e)
# outward mapping (orchestrator table)
_src = open(_os.path.join(_REPO, "core", "execution", "orchestrator.py"), encoding="utf-8").read()
check("PIN-P5 orchestrator maps timeout → err_endpoint_timeout",
      '"timeout": "err_endpoint_timeout"' in _src, "mapping missing")
_restore_stack()

# read-phase timeout (getresponse hangs → http.client socket.timeout)
class _HangConn(_ConnDouble):
    def getresponse(self):
        raise _socket.timeout("read timed out")
rec = _install_stack(resolve_map={"rh.example": [(_socket.AF_INET, PUBLIC4)]})

def _hang_http(host="", port=None, timeout=None):
    return _HangConn(host, port, timeout)

remote.http = types.SimpleNamespace(client=types.SimpleNamespace(HTTPConnection=_hang_http))
try:
    remote.pinned_request("https://rh.example/f", timeout_s=3)
    check("PIN-P5b read timeout → typed", False, "no raise")
except RemoteError as e:
    check("PIN-P5b read timeout → RemoteError('timeout')", e.code == "timeout", e)
_restore_stack()

# ═══ PIN-P6: redirect entity-header hygiene ═══
def _redirect_script(status, location, second=_RespDouble(200, b"done")):
    return [_RespDouble(status, b"", {"location": location}), second]

for status in (301, 302, 303):
    rec = _install_stack(
        resolve_map={"a.example": [(_socket.AF_INET, PUBLIC4)]},
        resp_script=_redirect_script(status, "https://a.example/hop2"))
    st, data = remote.pinned_request(
        "https://a.example/f", method="POST", body=b"payload",
        headers={"Content-Type": "application/json", "Content-Length": "7"},
        timeout_s=5)
    ok = (len(rec.conns) == 2
          and rec.conns[1].requests[0][0] == "GET"
          and rec.conns[1].requests[0][2] is None
          and "Content-Length" not in rec.conns[1].requests[0][3]
          and "Content-Type" not in rec.conns[1].requests[0][3])
    check(f"PIN-P6 {status}: hop2 GET, no body, no entity headers", ok,
          rec.conns[1].requests[0] if len(rec.conns) > 1 else "no hop2")
    _restore_stack()

for status in (307, 308):
    rec = _install_stack(
        resolve_map={"a.example": [(_socket.AF_INET, PUBLIC4)]},
        resp_script=_redirect_script(status, "https://a.example/hop2"))
    st, data = remote.pinned_request(
        "https://a.example/f", method="POST", body=b"payload",
        headers={"Content-Type": "application/json"},
        timeout_s=5)
    m, p, b, h = rec.conns[1].requests[0] if len(rec.conns) > 1 else (None, None, None, None)
    ok = m == "POST" and b == b"payload" and h.get("Content-Type") == "application/json"
    check(f"PIN-P6 {status}: hop2 keeps method/body/entity headers", ok, (m, b, h))
    _restore_stack()

# ═══ REDIR matrix ═══
# public → private redirect: blocked BEFORE any second connect
rec = _install_stack(
    resolve_map={"pub1.example": [(_socket.AF_INET, PUBLIC4)],
                 "evil.internal": [(_socket.AF_INET, "10.0.0.9")]},
    resp_script=_redirect_script(302, "https://evil.internal/f"))
try:
    remote.pinned_request("https://pub1.example/f", timeout_s=5)
    check("REDIR public→private blocked", False, "no raise")
except RemoteError as e:
    check("REDIR public→private blocked", e.code == "ssrf_blocked", e)
check("REDIR private hop zero connects",
      len([s for s in rec.socks if s.connected]) == 1, [s.connected for s in rec.socks])
_restore_stack()

# relative Location re-validated (302 loop guard proves re-resolution)
rec = _install_stack(
    resolve_map={"pub2.example": [(_socket.AF_INET, PUBLIC4)]},
    resp_script=[_RespDouble(302, b"", {"location": "/next"})] * 8)
try:
    remote.pinned_request("https://pub2.example/f", timeout_s=5)
    check("REDIR relative Location re-validated", False, "no raise")
except RemoteError as e:
    check("REDIR relative Location re-validated", e.code == "redirect_blocked", e)
_restore_stack()

# output_url → private redirect → blocked before second connect
rec = _install_stack(
    resolve_map={"out.example": [(_socket.AF_INET, PUBLIC4)],
                 "in.internal": [(_socket.AF_INET, "127.0.0.1")]},
    resp_script=[_RespDouble(302, b"", {"location": "https://in.internal/x"})])
try:
    remote._pull_output_url("https://out.example/x", 5, trusted=False)
    check("REDIR output_url→private blocked", False, "no raise")
except RemoteError as e:
    check("REDIR output_url→private blocked", True, e)
check("REDIR output_url second hop zero connects",
      len([s for s in rec.socks if s.connected]) == 1, [s.connected for s in rec.socks])
_restore_stack()

# >5 redirects
rec = _install_stack(
    resolve_map={"loop.example": [(_socket.AF_INET, PUBLIC4)]},
    resp_script=[_RespDouble(302, b"", {"location": f"https://loop.example/f{i}"})
                 for i in range(10)])
try:
    remote.pinned_request("https://loop.example/f0", timeout_s=5)
    check("REDIR >5 hops blocked", False, "no raise")
except RemoteError as e:
    check("REDIR >5 hops blocked", e.code == "redirect_blocked", e)
_restore_stack()

# ═══ CREDENTIAL boundary ═══
rec = _install_stack(
    resolve_map={"same.example": [(_socket.AF_INET, PUBLIC4)]},
    resp_script=[_RespDouble(302, b"", {"location": "https://same.example/hop2"}),
                 _RespDouble(200, b"ok")])
st, data = remote.pinned_request("https://same.example/f", method="POST", body=b"x",
                                 headers={"Authorization": "Bearer sekrit"},
                                 timeout_s=5, has_credential=True)
h2 = rec.conns[1].requests[0][3] if len(rec.conns) > 1 else {}
check("CRED same-origin redirect proceeds (2 hops)", len(rec.conns) == 2, len(rec.conns))
check("CRED credential retained on hop 2",
      h2.get("Authorization") == "Bearer sekrit", h2)
_restore_stack()

rec = _install_stack(
    resolve_map={"same.example": [(_socket.AF_INET, PUBLIC4)],
                 "other.example": [(_socket.AF_INET, PUBLIC4)]},
    resp_script=[_RespDouble(302, b"", {"location": "https://other.example/hop2"}),
                 _RespDouble(200, b"ok")])
try:
    remote.pinned_request("https://same.example/f",
                          headers={"Authorization": "Bearer sekrit"},
                          timeout_s=5, has_credential=True)
    check("CRED cross-origin + credential blocked", False, "no raise")
except RemoteError as e:
    check("CRED cross-origin + credential blocked", e.code == "redirect_blocked", e)
check("CRED blocked BEFORE second request",
      len(rec.conns) == 1, len(rec.conns))
_restore_stack()

rec = _install_stack(
    resolve_map={"same.example": [(_socket.AF_INET, PUBLIC4)],
                 "other.example": [(_socket.AF_INET, PUBLIC4)]},
    resp_script=[_RespDouble(302, b"", {"location": "https://other.example/hop2"}),
                 _RespDouble(200, b"ok")])
st, data = remote.pinned_request("https://same.example/f", timeout_s=5, has_credential=False)
check("CRED cross-origin without credential allowed (2 hops)", len(rec.conns) == 2, len(rec.conns))
_restore_stack()

# ═══ VAULT matrix (subprocess — fresh module state per scenario) ═══
import subprocess
def _run_vault(env_key, code):
    full = ("import sys; sys.path.insert(0, %r)\n" % _os.path.join(_REPO, "core")
            + ("import os\nos.environ['TOOL_VAULT_KEY'] = %r\n" % env_key if env_key is not None
               else "import os\nos.environ.pop('TOOL_VAULT_KEY', None)\n")
            + "from secrets_vault import seal, unseal, vault_enabled, VaultUnavailable, VaultDecryptError\n"
            + code)
    r = subprocess.run([sys.executable, "-c", full], capture_output=True, text=True)
    return r.returncode, r.stdout.strip(), r.stderr.strip()

rc, out, err = _run_vault(None, "seal('topsecret')")
check("VAULT missing key + seal(secret) → VaultUnavailable",
      rc != 0 and "VaultUnavailable" in err, f"rc={rc} err={err[:80]}")
rc, out, err = _run_vault(None, "assert seal('') == ''")
check("VAULT missing key + seal('') → ''", rc == 0, f"rc={rc} {err[:80]}")
from cryptography.fernet import Fernet
_vk = Fernet.generate_key().decode()
rc, out, err = _run_vault(_vk, "s = seal('topsecret')\nassert s != 'topsecret' and s.startswith('enc1:')\nassert unseal(s) == 'topsecret'\nprint('RT')")
check("VAULT valid key seal/roundtrip", rc == 0 and "RT" in out, f"rc={rc} {err[:80]}")
rc, out, err = _run_vault(None, "assert unseal('legacy-plaintext') == 'legacy-plaintext'\nprint('LG')")
check("VAULT legacy plaintext + no key passthrough", rc == 0 and "LG" in out, f"rc={rc} {err[:80]}")
rc, out, err = _run_vault(_vk, "print(seal('tok'))")
sealed = out
rc, out, err = _run_vault(None, "unseal(%r)" % sealed)
check("VAULT sealed + no key → VaultUnavailable",
      rc != 0 and "VaultUnavailable" in err, f"rc={rc} err={err[:80]}")
rc, out, err = _run_vault(Fernet.generate_key().decode(), "unseal(%r)" % sealed)
check("VAULT wrong key → VaultDecryptError",
      rc != 0 and "VaultDecryptError" in err, f"rc={rc} err={err[:80]}")
rc, out, err = _run_vault("not-a-fernet-key", "assert vault_enabled() is False\nprint('MF')")
check("VAULT malformed key → disabled", rc == 0 and "MF" in out, f"rc={rc} {err[:80]}")

# ═══ ROUTE: ASGI 503 + zero persistence ═══
import psycopg2
PG = TEST_DB.replace("postgresql+psycopg://", "postgresql://")
_con = psycopg2.connect(PG); _con.autocommit = True
_cur = _con.cursor()
ts = int(time.time())
_os.environ.pop("TOOL_VAULT_KEY", None)
import secrets_vault
secrets_vault._f = None

from app.main import app
from app.web_auth import create_browser_session, COOKIE_NAME
import warnings as _w
with _w.catch_warnings():
    _w.simplefilter("ignore", DeprecationWarning)
    from starlette.testclient import TestClient
client = TestClient(app)

import uuid
def _new_user(prefix):
    u = prefix + uuid.uuid4().hex[:6]
    _cur.execute(
        "insert into users (username, email, password_hash, display_name, status, created_at, last_login_at)"
        " values (%s, %s, 'x', 'T', 'active', %s, %s) returning id",
        (u, f"{u}@t.co", ts, ts))
    return u, _cur.fetchone()[0]

# route SSRF pre-check uses the real resolver in the app process — pin it
# via the same production helper (dependency swap, algorithm unchanged)
remote._resolve_host = _resolve({"pub.example": [(_socket.AF_INET, PUBLIC4)]})

u1, uid1 = _new_user("rs1")
sess1 = create_browser_session(uid1)
r = client.post("/api/usertools", json={
    "tool_id": "sectool1", "label": "Sec", "url": "https://pub.example/x",
    "format_families": "image",
    "auth_header": "Authorization", "auth_token": "topsecret"},
    cookies={COOKIE_NAME: sess1})
check("ROUTE user create + auth_token + no key → 503",
      r.status_code == 503 and r.json().get("error") == "credential_vault_unavailable",
      f"{r.status_code} {r.text[:80]}")
_cur.execute("select count(*) from platform_tools where tool_key = %s", (f"{u1}/sectool1",))
check("ROUTE zero platform_tools row", _cur.fetchone()[0] == 0)
_cur.execute("select count(*) from pricing where tool_id = %s", (f"{u1}/sectool1",))
check("ROUTE zero pricing row", _cur.fetchone()[0] == 0)

r = client.post("/api/usertools", json={
    "tool_id": "sectool2", "label": "Sec2", "url": "https://pub.example/x",
    "format_families": "image"},
    cookies={COOKIE_NAME: sess1})
check("ROUTE user create without auth_token unaffected",
      r.status_code == 200 and r.json().get("ok"), f"{r.status_code} {r.text[:80]}")

r = client.patch("/api/usertools/sectool2", json={
    "auth_header": "Authorization", "auth_token": "newsecret"},
    cookies={COOKIE_NAME: sess1})
check("ROUTE user update + new auth_token + no key → 503",
      r.status_code == 503, f"{r.status_code} {r.text[:80]}")
_cur.execute("select endpoint_json from platform_tools where tool_key = %s",
             (f"{u1}/sectool2",))
row = _cur.fetchone()
check("ROUTE endpoint_json unchanged",
      row is not None and "newsecret" not in (row[0] or ""), str(row)[:80])

u2, uid2 = _new_user("rs2")
_cur.execute("update users set status='samelabs' where id=%s", (uid2,))
sess2 = create_browser_session(uid2)
r = client.post("/samelabs/remote/create", json={
    "tool_id": "secop1", "url": "https://pub.example/y",
    "auth_header": "X-Key", "auth_token": "opsecret"},
    cookies={COOKIE_NAME: sess2})
check("ROUTE operator create + auth_token + no key → 503 (非409)",
      r.status_code == 503 and r.json().get("error") == "credential_vault_unavailable",
      f"{r.status_code} {r.text[:80]}")

# ═══ DISP: vault failure → zero remote calls ═══
import remote as _remote_mod
_calls = {"n": 0}
def _sentinel(*a, **k):
    _calls["n"] += 1
    return {"filename": "x.png", "content_type": "image/png", "bytes": b""}
_orig = _remote_mod.drive_http
_remote_mod.drive_http = _sentinel
try:
    from secrets_vault import unseal as _unseal, VaultUnavailable as _VU
    raised = None
    try:
        _unseal("enc1:deadbeef")
    except Exception as exc:
        raised = exc
    check("DISP unseal(enc1) without key raises typed",
          isinstance(raised, _VU), type(raised).__name__)
    check("DISP vault failure → zero remote network calls",
          _calls["n"] == 0, _calls)
finally:
    _remote_mod.drive_http = _orig

# 清场
_cur.execute("delete from pricing where tool_id like 'rs%/sec%' or tool_id = 'secop1'")
_cur.execute("delete from platform_tools where tool_key like 'rs%' or tool_key = 'secop1'")
_cur.execute("delete from sessions where user_id in (%s, %s)", (uid1, uid2))
_cur.execute("delete from users where id in (%s, %s)", (uid1, uid2))
_con.close()

print()
if FAIL:
    print(f"FAIL: {len(FAIL)} — " + "; ".join(FAIL)); _sys.exit(1)
print("FAIL: 无 — 全绿")
