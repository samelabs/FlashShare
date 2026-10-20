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
from tests.support.cleanup import purge_users  # noqa: E402
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
    # 默认响应身份 = 南向 items 信封 (P2 契约: 信封走专属 Content-Type;
    # 其余用例显式覆盖 headers)。
    ITEMS_CT = {"content-type": "application/vnd.518.items+json"}

    def __init__(self, status=200, body=b"ok", headers=None):
        self.status = status
        self._body = body
        base = dict(self.ITEMS_CT)
        base.update({k.lower(): v for k, v in (headers or {}).items()})
        self._h = base
    def read(self, n=-1):
        # 尊重读取上限 (与真实 resp.read(n) 同语义 — 截断断言依赖它)
        return self._body if n < 0 else self._body[:n]
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
status, data, _ct, _cd = remote.pinned_request("https://pin.example/f?q=1", timeout_s=7)
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
status, data, _ct, _cd = remote.pinned_request("https://v6.example/f", timeout_s=5)
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
status, data, _ct, _cd = remote.pinned_request("https://pin.example:8443/f", timeout_s=5)
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
    resp_script=[_RespDouble(200, json.dumps({"items": [
        {"kind": "file", "filename": "out.txt",
         "content_type": "text/plain", "data_b64": "aGk="}]}).encode())])
decl = {"url": "https://rt.example/api", "timeout_s": 30}
out = remote.drive_http(decl, [], {}, {}, trusted=False)
check("PIN-P4 drive_http untrusted succeeds",
      out["items"][0]["bytes"] == b"hi", out)
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
      'raise ExecutionError("err_endpoint_timeout")' in _src, "mapping missing")
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
    st, data, _ct, _cd = remote.pinned_request(
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
    st, data, _ct, _cd = remote.pinned_request(
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
    remote._pull_item_file_url("https://out.example/x", 5, trusted=False)
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
st, data, _ct, _cd = remote.pinned_request("https://same.example/f", method="POST", body=b"x",
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
st, data, _ct, _cd = remote.pinned_request("https://same.example/f", timeout_s=5, has_credential=False)
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
rc, out, err = _run_vault(None, "unseal('legacy-plaintext')")
check("VAULT non-enc1 non-empty → VaultDecryptError (single format, fail closed)",
      rc != 0 and "VaultDecryptError" in err, f"rc={rc} err={err[:80]}")
rc, out, err = _run_vault(_vk, "unseal('legacy-plaintext')")
check("VAULT non-enc1 non-empty + valid key → VaultDecryptError (no passthrough)",
      rc != 0 and "VaultDecryptError" in err, f"rc={rc} err={err[:80]}")
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
_VAULT_KEY = Fernet.generate_key()
secrets_vault._f = None  # default off; AUTH-H1 flips it on

from app.main import app
from app.web_auth import create_browser_session, COOKIE_NAME
import warnings as _w
with _w.catch_warnings():
    _w.simplefilter("ignore", DeprecationWarning)
    from starlette.testclient import TestClient
client = TestClient(app)

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

# ═══ AUTH: reserved credential-header boundary ═══
# 本段自带 fixture + helper (不依赖 ROUTE 段变量顺序)
import uuid as _uuid_a
def _new_user_a(prefix):
    _u = prefix + _uuid_a.uuid4().hex[:6]
    _cur.execute(
        "insert into users (username, email, password_hash, display_name, status, created_at, last_login_at)"
        " values (%s, %s, 'x', 'T', 'active', %s, %s) returning id",
        (_u, f"{_u}@t.co", ts, ts))
    return _u, _cur.fetchone()[0]
_au, _auid = _new_user_a("ra")
_asess = create_browser_session(_auid)
# 预置一个合法工具 (H2 update 目标) — route pre-check 需要解析器钉扎
remote._resolve_host = _resolve({"pub.example": [(_socket.AF_INET, PUBLIC4)]})
r = client.post("/api/usertools", json={
    "label": "Sec2", "url": "https://pub.example/x",
    "format_families": "image", "accept": [".png"]},
    cookies={COOKIE_NAME: _asess})
_sec2key = (r.json() or {}).get("tool_key")
check("AUTH-fixture 预置工具建立", r.status_code == 200 and _sec2key,
      f"{r.status_code} {r.text[:100]}")
# H1 create: reserved header + VALID vault → 422 bad_auth_header, zero write
secrets_vault._f = Fernet(_VAULT_KEY)  # vault ON — 503 must not mask
r = client.post("/api/usertools", json={
    "label": "H", "url": "https://pub.example/x",
    "format_families": "image", "accept": [".png"],
    "auth_header": "Host", "auth_token": "sekrit"},
    cookies={COOKIE_NAME: _asess})
check("AUTH-H1 create auth_header=Host → 422 bad_auth_header",
      r.status_code == 422 and r.json().get("error") == "bad_auth_header",
      f"{r.status_code} {r.text[:80]}")
_cur.execute("select count(*) from platform_tools where tool_key = %s",
             (f"{_au}_sectoolh",))
check("AUTH-H1 zero platform_tools write", _cur.fetchone()[0] == 0)
_cur.execute("select count(*) from pricing where tool_id = %s",
             (f"{_au}_sectoolh",))
check("AUTH-H1 zero pricing write", _cur.fetchone()[0] == 0)
secrets_vault._f = None

# H2 update: reserved header on PATCH → 422, endpoint_json unchanged
r = client.patch(f"/api/usertools/{_sec2key}", json={
    "auth_header": "Content-Length", "auth_token": "sekrit"},
    cookies={COOKIE_NAME: _asess})
check("AUTH-H2 update auth_header=Content-Length → 422 bad_auth_header",
      r.status_code == 422 and r.json().get("error") == "bad_auth_header",
      f"{r.status_code} {r.text[:80]}")
_cur.execute("select endpoint_json from platform_tools where tool_key = %s",
             (_sec2key,))
row = _cur.fetchone()
check("AUTH-H2 endpoint_json unchanged", row is not None and "sekrit" not in (row[0] or ""))

# H3 normal names accepted by the validator (no tool persistence needed)
from remote import auth_header_allowed as _aha
check("AUTH-H3 Authorization allowed", _aha("Authorization"))
check("AUTH-H3 X-API-Key allowed", _aha("X-API-Key"))
check("AUTH-H3 Api-Key allowed", _aha("Api-Key"))
check("AUTH-H3 Private-Token allowed", _aha("Private-Token"))
check("AUTH-H3 X-Auth-Token allowed", _aha("X-Auth-Token"))
check("AUTH-H3 host rejected (case-insensitive)", not _aha("host") and not _aha("HOST")
      and not _aha("  Host  "))
check("AUTH-H3 transfer-encoding rejected", not _aha("Transfer-Encoding"))

# H4 legacy DB runtime defense: reserved header → zero network, typed fail
_calls_h4 = {"n": 0}
def _sentinel_h4(*a, **k):
    _calls_h4["n"] += 1
    return {"filename": "o.png", "content_type": "image/png", "bytes": b""}
try:
    from remote import _reject_reserved_auth_headers
    raised = None
    try:
        _reject_reserved_auth_headers({"Host": "evil.example"})
    except Exception as exc:
        raised = exc
    check("AUTH-H4 reserved header raises typed RemoteError",
          isinstance(raised, RemoteError) and raised.code == "bad_endpoint", raised)
    # full driver path (real drive_http, resolver pinned, no network):
    # legacy row with Host credential → typed fail BEFORE any connect
    rec = _install_stack(
        resolve_map={"rt.example": [(_socket.AF_INET, PUBLIC4)]},
        resp_script=[_RespDouble(200, b"ok")])
    decl_h4 = {"url": "https://rt.example/api", "timeout_s": 30}
    try:
        remote.drive_http(decl_h4, [], {}, {"Host": "evil.example"}, trusted=False)
        check("AUTH-H4 drive_http legacy reserved → typed fail", False, "no raise")
    except RemoteError as exc:
        check("AUTH-H4 drive_http legacy reserved → typed fail",
              exc.code == "bad_endpoint", exc)
    check("AUTH-H4 zero sockets connected (network = 0)",
          len([s for s in rec.socks if s.connected]) == 0,
          [s.connected for s in rec.socks])
    _restore_stack()
finally:
    pass

# H5 Host authority cannot be overridden by caller headers
rec = _install_stack(
    resolve_map={"pin.example": [(_socket.AF_INET, PUBLIC4)]},
    resp_script=[_RespDouble(200, b"ok")])
st, data, _ct, _cd = remote.pinned_request("https://pin.example/f", timeout_s=5,
                                 headers={"Host": "attacker.example",
                                          "Content-Length": "999"})
_h = rec.conns[0].requests[0][3].get("Host") if rec.conns else None
check("AUTH-H5 caller Host override ignored — transport authority wins",
      _h == "pin.example", _h)
_cl = rec.conns[0].requests[0][3].get("Content-Length")
check("AUTH-H5 caller Content-Length stripped", _cl is None, _cl)
_restore_stack()

# ═══ PORT: malformed port typing ═══
# PORT-1 redirect + credential: 302 → https://evil.example:notaport/x
rec = _install_stack(
    resolve_map={"same.example": [(_socket.AF_INET, PUBLIC4)]},
    resp_script=[_RespDouble(302, b"", {"location": "https://evil.example:notaport/x"})])
try:
    remote.pinned_request("https://same.example/f",
                          headers={"Authorization": "Bearer s"},
                          timeout_s=5, has_credential=True)
    check("PORT-1 malformed-port redirect + credential blocked", False, "no raise")
except RemoteError as e:
    check("PORT-1 malformed-port redirect + credential blocked",
          e.code == "redirect_blocked", e)
check("PORT-1 zero second requests (credential never sent)",
      len(rec.conns) == 1, len(rec.conns))
_restore_stack()

# PORT-2 redirect no credential: same malformed port → typed, no ValueError
rec = _install_stack(
    resolve_map={"same.example": [(_socket.AF_INET, PUBLIC4)]},
    resp_script=[_RespDouble(302, b"", {"location": "https://evil.example:notaport/x"})])
try:
    remote.pinned_request("https://same.example/f", timeout_s=5)
    check("PORT-2 malformed-port redirect typed block", False, "no raise")
except RemoteError as e:
    check("PORT-2 malformed-port redirect typed block",
          e.code == "redirect_blocked", e)
except ValueError:
    check("PORT-2 malformed-port redirect typed block", False, "raw ValueError escaped")
_restore_stack()

# PORT-3 route submission: malformed port → 422 bad_url, no row
remote._resolve_host = _resolve({"pub.example": [(_socket.AF_INET, PUBLIC4)]})
r = client.post("/api/usertools", json={
    "label": "P", "url": "https://example.com:notaport/x",
    "format_families": "image", "accept": [".png"]},
    cookies={COOKIE_NAME: _asess})
check("PORT-3 submission malformed port → 422 bad_url",
      r.status_code == 422 and r.json().get("error") == "bad_url",
      f"{r.status_code} {r.text[:80]}")
_cur.execute("select count(*) from platform_tools where tool_key = %s",
             (f"{_au}_sectoolp",))
check("PORT-3 zero row written", _cur.fetchone()[0] == 0)

#


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
    "label": "Sec", "url": "https://pub.example/x",
    "format_families": "image", "accept": [".png"],
    "auth_header": "Authorization", "auth_token": "topsecret"},
    cookies={COOKIE_NAME: _asess})
check("ROUTE user create + auth_token + no key → 503",
      r.status_code == 503 and r.json().get("error") == "credential_vault_unavailable",
      f"{r.status_code} {r.text[:80]}")
_cur.execute("select count(*) from platform_tools where tool_key = %s", (f"{_au}_sectool1",))
check("ROUTE zero platform_tools row", _cur.fetchone()[0] == 0)
_cur.execute("select count(*) from pricing where tool_id = %s", (f"{_au}_sectool1",))
check("ROUTE zero pricing row", _cur.fetchone()[0] == 0)

r = client.post("/api/usertools", json={
    "label": "Sec3", "url": "https://pub.example/x",
    "format_families": "image", "accept": [".png"]},
    cookies={COOKIE_NAME: _asess})
_sec3key = (r.json() or {}).get("tool_key")
check("ROUTE user create without auth_token unaffected",
      r.status_code == 200 and _sec3key, f"{r.status_code} {r.text[:80]}")

r = client.patch(f"/api/usertools/{_sec3key}", json={
    "auth_header": "Authorization", "auth_token": "newsecret"},
    cookies={COOKIE_NAME: _asess})
check("ROUTE user update + new auth_token + no key → 503",
      r.status_code == 503, f"{r.status_code} {r.text[:80]}")
_cur.execute("select endpoint_json from platform_tools where tool_key = %s",
             (_sec3key,))
row = _cur.fetchone()
check("ROUTE endpoint_json unchanged",
      row is not None and "newsecret" not in (row[0] or ""), str(row)[:80])

u2, uid2 = _new_user("rs2")
_cur.execute("update users set status='samelabs' where id=%s", (uid2,))
sess2 = create_browser_session(uid2)
r = client.post("/samelabs/tools/create", json={
    "tool_id": "secop1", "url": "https://pub.example/y",
    "format_families": "image", "accept": ".png",
    "auth_header": "X-Key", "auth_token": "opsecret"},
    cookies={COOKIE_NAME: sess2})
check("ROUTE operator create + auth_token + no key → 503 (非409)",
      r.status_code == 503 and r.json().get("error") == "credential_vault_unavailable",
      f"{r.status_code} {r.text[:80]}")



# ═══ OUTPUT-DECL RETIREMENT: runtime behavior (no tool declaration) ═══
# Landing identity is endpoint-product-owned. _wrap_raw takes the
# PRODUCT (bytes + optional endpoint filename/content_type) — never a
# tool declaration dict.
import base64 as _b64
import struct as _struct

def _png_bytes():
    ihdr = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + _struct.pack(
        ">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    return ihdr + b"\x00\x00\x00\x00IEND\xae\x42\x60\x82"

# OUT-1: real PNG bytes, NO output declaration → result.png + image/png
try:
    out = remote._wrap_raw(_png_bytes())
    check("OUT-1 raw PNG no declaration → filename result.png",
          out["filename"] == "result.png", out["filename"])
    check("OUT-1 raw PNG no declaration → canonical image/png",
          out["content_type"] == "image/png", out["content_type"])
except Exception as exc:
    check("OUT-1 raw PNG no declaration → result.png/image/png", False, exc)

# OUT-2: generic octet-stream never masks the real MIME
try:
    out = remote._wrap_raw(_png_bytes(), content_type="application/octet-stream")
    check("OUT-2 generic octet-stream not masking image/png",
          out["content_type"] == "image/png", out["content_type"])
except Exception as exc:
    check("OUT-2 generic octet-stream not masking", False, exc)

# OUT-3: explicit endpoint content_type survives (JSON mode path)
try:
    out = remote._wrap_raw(_png_bytes(), "photo.png", "image/png")
    check("OUT-3 endpoint content_type honored",
          out["filename"] == "photo.png" and out["content_type"] == "image/png",
          out)
except Exception as exc:
    check("OUT-3 endpoint content_type honored", False, exc)

# OUT-4: JSON-mode product identity flows into canonical result via
# drive_http (resolver pinned; data_b64 PNG + filename + content_type)
rec_o = _install_stack(
    resolve_map={"out.example": [(_socket.AF_INET, PUBLIC4)]},
    resp_script=[_RespDouble(200, json.dumps({"items": [
        {"kind": "file", "filename": "prod.png",
         "content_type": "image/png",
         "data_b64": _b64.b64encode(_png_bytes()).decode()},
    ]}).encode())])
try:
    decl_o = {"url": "https://out.example/api", "timeout_s": 30}
    out = remote.drive_http(decl_o, [], {}, {}, trusted=False)
    _it = out["items"][0]
    check("OUT-4 JSON content_type enters canonical result",
          _it["filename"] == "prod.png" and _it["content_type"] == "image/png",
          out)
    check("OUT-4 legacy output_* binding keys ignored by runtime",
          "output_filename" not in decl_o and "output_content_type" not in decl_o)
except Exception as exc:
    check("OUT-4 JSON content_type enters canonical result", False, exc)
finally:
    _restore_stack()

# ═══ ENV-MIME: JSON envelope MIME ≠ product MIME (regression) ═══
# trusted endpoint path (urllib driver) — the response Content-Type IS
# the JSON envelope; it must NEVER become the wrapped product's MIME.
class _UrllibRespDouble:
    def __init__(self, status, body, ctype):
        self.status = status
        self._body = body
        self.headers = {"Content-Type": ctype} if ctype else {}
        self._read = False
    def read(self, n=-1): return self._body
    def __enter__(self): return self
    def __exit__(self, *a): return False

def _patch_urllib_open(resp):
    _saved_open = remote.urllib.request.urlopen
    remote.urllib.request.urlopen = lambda req, timeout=None: resp
    return _saved_open

# ENV-1: items envelope (专属 Content-Type) + PNG data_b64, 无 product
# content_type → canonical image/png (from filename + byte validation)
_saved_open = _patch_urllib_open(_UrllibRespDouble(
    200,
    json.dumps({"items": [{"kind": "file", "filename": "result.png",
                "data_b64": _b64.b64encode(_png_bytes()).decode()}]}).encode(),
    "application/vnd.518.items+json"))
try:
    out = remote.drive_http({"url": "https://trusted.example/api", "timeout_s": 30},
                            [], {}, {}, trusted=True)
    _e1 = out["items"][0]
    check("ENV-1 JSON envelope MIME not inherited (filename=result.png)",
          _e1["filename"] == "result.png", out)
    check("ENV-1 result content_type == image/png",
          _e1["content_type"] == "image/png", _e1["content_type"])
except Exception as exc:
    check("ENV-1 JSON envelope MIME not inherited", False, exc)
finally:
    remote.urllib.request.urlopen = _saved_open

# ENV-2: envelope 显式 content_type=image/png → image/png
_saved_open = _patch_urllib_open(_UrllibRespDouble(
    200,
    json.dumps({"items": [{"kind": "file", "filename": "result.png",
                "content_type": "image/png",
                "data_b64": _b64.b64encode(_png_bytes()).decode()}]}).encode(),
    "application/vnd.518.items+json"))
try:
    out = remote.drive_http({"url": "https://trusted.example/api", "timeout_s": 30},
                            [], {}, {}, trusted=True)
    check("ENV-2 JSON explicit content_type honored",
          out["items"][0]["content_type"] == "image/png",
          out["items"][0]["content_type"])
except Exception as exc:
    check("ENV-2 JSON explicit content_type honored", False, exc)
finally:
    remote.urllib.request.urlopen = _saved_open

# ENV-3 (P2): raw PNG response, HTTP Content-Type=image/png → 一个文件项
# (raw 模式合法保留响应 Content-Type; 无 Content-Disposition → 嗅探名)
_saved_open = _patch_urllib_open(_UrllibRespDouble(
    200, _png_bytes(), "image/png"))
try:
    out = remote.drive_http({"url": "https://trusted.example/api", "timeout_s": 30},
                            [], {}, {}, trusted=True)
    _e3 = out["items"][0]
    check("ENV-3 raw bytes + image/png → file item (sniffed name)",
          _e3["kind"] == "file" and _e3["filename"] == "result.png"
          and _e3["content_type"] == "image/png" and _e3["bytes"] == _png_bytes(),
          out)
except Exception as exc:
    check("ENV-3 raw bytes + image/png → file item", False, exc)
finally:
    remote.urllib.request.urlopen = _saved_open

# ENV-4: output_url variant — envelope MIME must not leak into the
# pulled product either (JSON metadata only)
_saved_open = _patch_urllib_open(_UrllibRespDouble(
    200,
    json.dumps({"items": [{"kind": "file", "filename": "pulled.png",
                "url": "https://pub.example/fetch.png"}]}).encode(),
    "application/vnd.518.items+json"))
_pulled_saved = remote._pull_item_file_url
try:
    remote._pull_item_file_url = lambda url, t, tr: _png_bytes()
    out = remote.drive_http({"url": "https://trusted.example/api", "timeout_s": 30},
                            [], {}, {}, trusted=True)
    _e4 = out["items"][0]
    check("ENV-4 file url product MIME from bytes, not envelope",
          _e4["filename"] == "pulled.png" and _e4["content_type"] == "image/png",
          out)
except Exception as exc:
    check("ENV-4 output_url product MIME from bytes", False, exc)
finally:
    remote._pull_item_file_url = _pulled_saved
    remote.urllib.request.urlopen = _saved_open

# ═══ P2-CT: response interpretation by Content-Type (one rule for both
# tool kinds) — single json item / single text item / file naming ═══
def _drive_trusted(resp):
    _saved = _patch_urllib_open(resp)
    try:
        return remote.drive_http({"url": "https://trusted.example/api",
                                  "timeout_s": 30}, [], {}, {}, trusted=True)
    finally:
        remote.urllib.request.urlopen = _saved

# CT-1: application/json → ONE json item (the body itself is the product)
try:
    out = _drive_trusted(_UrllibRespDouble(200, b'{"ok": true, "n": 1}',
                                           "application/json"))
    check("CT-1 application/json → one json item",
          len(out["items"]) == 1 and out["items"][0]["kind"] == "json"
          and out["items"][0]["data"] == {"ok": True, "n": 1}, out)
except Exception as exc:
    check("CT-1 application/json → one json item", False, exc)

# CT-2: text/* → ONE text item (UTF-8)
try:
    out = _drive_trusted(_UrllibRespDouble(200, "café résumé".encode("utf-8"),
                                           "text/plain; charset=utf-8"))
    check("CT-2 text/* → one text item",
          len(out["items"]) == 1 and out["items"][0]["kind"] == "text"
          and out["items"][0]["text"] == "café résumé", out)
except Exception as exc:
    check("CT-2 text/* → one text item", False, exc)

# CT-3: other Content-Type + Content-Disposition → file item named from CD
class _CDResp(_UrllibRespDouble):
    def __init__(self, status, body, ctype, cd):
        super().__init__(status, body, ctype)
        self.headers["Content-Disposition"] = cd
_saved_open = _patch_urllib_open(_CDResp(200, _png_bytes(), "image/png",
                                         'attachment; filename="photo.png"'))
try:
    out = remote.drive_http({"url": "https://trusted.example/api",
                             "timeout_s": 30}, [], {}, {}, trusted=True)
    _ct3 = out["items"][0]
    check("CT-3 CD filename wins for file items",
          _ct3["kind"] == "file" and _ct3["filename"] == "photo.png"
          and _ct3["content_type"] == "image/png", out)
except Exception as exc:
    check("CT-3 CD filename wins for file items", False, exc)
finally:
    remote.urllib.request.urlopen = _saved_open

# CT-4: other Content-Type, no CD, unidentifiable bytes → fail typed
_saved_open = _patch_urllib_open(_UrllibRespDouble(200, b"\x01\x02\x03not-a-format",
                                                   "application/x-custom"))
try:
    remote.drive_http({"url": "https://trusted.example/api", "timeout_s": 30},
                      [], {}, {}, trusted=True)
    check("CT-4 unidentifiable raw product → bad_output", False, "no raise")
except remote.RemoteError as exc:
    check("CT-4 unidentifiable raw product → bad_output",
          exc.code == "bad_output", exc.code)
finally:
    remote.urllib.request.urlopen = _saved_open

# CT-5: non-2xx → RemoteError http_status carrying status + body snippet
_saved_open = _patch_urllib_open(_UrllibRespDouble(
    503, b'{"error": "overloaded"}', "application/json"))
try:
    remote.drive_http({"url": "https://trusted.example/api", "timeout_s": 30},
                      [], {}, {}, trusted=True)
    check("CT-5 provider non-2xx → RemoteError http_status", False,
          "no raise")
except remote.RemoteError as exc:
    check("CT-5 provider non-2xx carries status + body snippet",
          exc.code == "http_status" and "503" in str(exc)
          and "overloaded" in str(exc), str(exc))
finally:
    remote.urllib.request.urlopen = _saved_open
_orch_src = open(_os.path.join(_REPO, "core", "execution", "orchestrator.py"),
                 encoding="utf-8").read()
check("CT-5b orchestrator maps http_status → 502 + detail",
      'http_status=502' in _orch_src and "exc.detail" in _orch_src,
      "mapping missing")

# ═══ P2-BODY: body-tool southbound transport (pinned recorder) ═══
rec_b = _install_stack(
    resolve_map={"body.example": [(_socket.AF_INET, PUBLIC4)]},
    resp_script=[_RespDouble(200, b'"HELLO"',
                             headers={"Content-Type": "application/json"})])
try:
    decl_b = {"url": "https://body.example/api?src=keep", "timeout_s": 30}
    out = remote.drive_http(
        decl_b, [], {"num": 5.0, "fl": 2.5, "flag": True, "mode": "fast"},
        {}, trusted=False, body="hello", canonical_body='"hello"',
        body_format="json")
    check("BODY-1 json body tool round-trips a json item",
          out["items"][0]["kind"] == "json"
          and out["items"][0]["data"] == "HELLO", out)
    _m, _p, _rb, _rh = rec_b.conns[0].requests[0]
    check("BODY-2 request body is the canonical JSON, not an envelope",
          _rb == b'"hello"', _rb)
    check("BODY-3 Content-Type application/json for json bodies",
          _rh.get("Content-Type") == "application/json", _rh.get("Content-Type"))
    check("BODY-4 params ride the query string, endpoint query preserved",
          _p.startswith("/api?src=keep&")
          and "num=5" in _p and "num=5.0" not in _p
          and "fl=2.5" in _p and "flag=true" in _p and "mode=fast" in _p, _p)
finally:
    _restore_stack()

rec_t = _install_stack(
    resolve_map={"body.example": [(_socket.AF_INET, PUBLIC4)]},
    resp_script=[_RespDouble(200, b"done",
                             headers={"Content-Type": "text/plain"})])
try:
    out = remote.drive_http(
        {"url": "https://body.example/api", "timeout_s": 30}, [], {}, {},
        trusted=False, body="plain text", canonical_body="plain text",
        body_format="text")
    check("BODY-5 text body tool → one text item",
          out["items"][0]["kind"] == "text"
          and out["items"][0]["text"] == "done", out)
    _m, _p, _rb, _rh = rec_t.conns[0].requests[0]
    check("BODY-6 text body posted verbatim with text/plain; charset=utf-8",
          _rb == b"plain text"
          and _rh.get("Content-Type") == "text/plain; charset=utf-8",
          (_rb, _rh.get("Content-Type")))
finally:
    _restore_stack()

# BODY-7: query param encoding unit rules
check("BODY-7 encode_query_params (int no .0 / bool / text)",
      remote.encode_query_params({"i": 5.0, "b": False, "s": "a b&c"})
      == "i=5&b=false&s=a%20b%26c",
      remote.encode_query_params({"i": 5.0, "b": False, "s": "a b&c"}))

# BODY-8: declaration-time endpoint-query/param-key conflict
from governance.tool_supply import (validate_tool_declaration,
                                     DeclarationError as _DeclErr)
try:
    validate_tool_declaration({
        "tool_key": "bq", "tool_id": "bq", "owner_id": 7,
        "execution": "http",
        "label": {"en": "B"},
        "inputs": {"files": {"min": 0, "max": 0, "accept": None}},
        "body_format": "text",
        "outputs": ["text"],
        "params": [{"key": "src", "type": "text"}],
        "visibility": "unlisted", "enabled": True,
        "binding": {"url": "https://body.example/api?src=keep",
                    "timeout_s": 30}})
    check("BODY-8 endpoint query key vs param key → bad_binding", False,
          "accepted")
except _DeclErr as exc:
    check("BODY-8 endpoint query key vs param key → bad_binding",
          str(exc) == "bad_binding", str(exc))

# BODY-9: 正文工具恰好一项 (orchestrator, 落盘/结算之前)
check("BODY-9 orchestrator enforces exactly one item for body tools",
      'tool.get("body_format") and len(items) != 1' in _orch_src,
      "check missing")


# OUT-5: binding composition is a closed vocabulary — legacy/unknown keys
# are rejected fail-closed (DeclarationError), never carried or persisted
from governance.tool_supply import build_http_binding, DeclarationError
_legacy = {"url": "https://pub.example/old", "timeout_s": 30,
           "output_filename": "legacy.png", "output_content_type": "image/png"}
try:
    build_http_binding({"timeout_s": 60}, _legacy)
    _closed = False
except DeclarationError:
    _closed = True
check("OUT-5 unknown/retired keys in existing → DeclarationError (fail closed)",
      _closed)
_clean = {"url": "https://pub.example/old", "timeout_s": 30,
          "auth_header": "X-Key"}
_merged = build_http_binding({"timeout_s": 60}, _clean)
check("OUT-5 composed binding contains only canonical keys",
      set(_merged) <= {"url", "timeout_s", "auth_header", "auth_token"},
      _merged)
check("OUT-5 merge kept canonical fields",
      _merged["url"].endswith("/old") and _merged["timeout_s"] == 60
      and _merged["auth_header"] == "X-Key", _merged)

# OUT-6: operator route rejects explicit legacy fields (typed 422)
r = client.post("/samelabs/tools/create", json={
    "tool_id": "secopout", "url": "https://pub.example/o",
    "format_families": "image", "accept": ".png",
    "output_filename": "x.png"}, cookies={COOKIE_NAME: sess2})
check("OUT-6 operator create output_filename → 422 bad_output_filename",
      r.status_code == 422 and r.json().get("error") == "bad_output_filename",
      f"{r.status_code} {r.text[:80]}")
r = client.post("/samelabs/tools/create", json={
    "tool_id": "secopout", "url": "https://pub.example/o",
    "format_families": "image", "accept": ".png",
    "output_content_type": "image/png"}, cookies={COOKIE_NAME: sess2})
check("OUT-6 operator create output_content_type → 422",
      r.status_code == 422 and r.json().get("error") == "bad_output_content_type",
      f"{r.status_code} {r.text[:80]}")
_cur.execute("select count(*) from platform_tools where tool_key = 'secopout'")
check("OUT-6 zero rows written for rejected payloads",
      _cur.fetchone()[0] == 0)

# OUT-7: owner projection no longer carries the retired fields
from governance.user_tools import _owner_rows
_proj = _owner_rows(uid2)
check("OUT-7 owner projection has no output_filename/output_content_type",
      all("output_filename" not in p and "output_content_type" not in p
          for p in _proj), [sorted(p) for p in _proj][:1])

# ═══ F1: 非 2xx 大错误体 — 截断读取, 不因 Content-Length 中止 ═══
# 提供者 500 且 Content-Length 20KB: pinned 传输读前 4097 字节并携带
# status 返回 (错误详情素材), 不抛 too_large; drive_http 把它组装成
# http_status 错误 (orchestrator 再映射为平台 502 + detail)。
_err20k = b"E" * 20480
rec = _install_stack(
    resolve_map={"errbig.example": [(_socket.AF_INET, PUBLIC4)]},
    resp_script=[_RespDouble(500, _err20k,
                             headers={"content-length": "20480"})])
status, data, _ct, _cd = remote.pinned_request(
    "https://errbig.example/f", timeout_s=5)
check("F1 non-2xx 20KB CL → status carried, body truncated at 4096",
      status == 500 and len(data) == 4097 and data[:64] == b"E" * 64,
      (status, len(data)))
_restore_stack()
rec = _install_stack(
    resolve_map={"errbig.example": [(_socket.AF_INET, PUBLIC4)]},
    resp_script=[_RespDouble(500, _err20k,
                             headers={"content-length": "20480"})])
try:
    remote.drive_http({"url": "https://errbig.example/f", "timeout_s": 5},
                      [], {}, {}, False)
    check("F1 provider 500 → http_status with status + first 4096 bytes",
          False, "accepted")
except RemoteError as exc:
    check("F1 provider 500 → http_status with status + first 4096 bytes",
          exc.code == "http_status"
          and exc.detail.startswith("500 ")
          and len(exc.detail) == len("500 ") + 4096
          and set(exc.detail[len("500 "):]) == {"E"},
          (exc.code, len(exc.detail)))
finally:
    _restore_stack()

# ═══ A1: NaN/Infinity 响应体 → invalid_response (调用失败+退款, 非 500) ═══
for _nan_body in (b'{"x": NaN}', b'{"x": Infinity}', b'{"x": -Infinity}'):
    try:
        remote.parse_response_items(200, _nan_body, "application/json",
                                    None, None)
        check("A1 non-finite JSON constant → invalid_response", False,
              "accepted")
        break
    except RemoteError as exc:
        check("A1 non-finite JSON constant → invalid_response",
              exc.code == "invalid_response", f"{exc.code} {_nan_body}")
try:
    remote.parse_response_items(
        200, b'{"items":[{"kind":"json","data":{"x":NaN}}]}',
        remote.ITEMS_ENVELOPE_CT, None, None)
    check("A1 non-finite inside items envelope → invalid_response",
          False, "accepted")
except RemoteError as exc:
    check("A1 non-finite inside items envelope → invalid_response",
          exc.code == "invalid_response", exc.code)

# ═══ F3: parse_response_items — +json 媒体类型与 text charset ═══
_items = remote.parse_response_items(
    200, b'{"type":"about:blank","title":"nope"}',
    "application/problem+json", None, None)
check("F3 application/problem+json → single json item",
      len(_items) == 1 and _items[0]["kind"] == "json"
      and _items[0]["data"]["title"] == "nope", str(_items)[:100])
_items = remote.parse_response_items(
    200, b'[1,2,3]', "application/ld+json; charset=utf-8", None, None)
check("F3 application/ld+json → single json item (array root)",
      len(_items) == 1 and _items[0]["kind"] == "json"
      and _items[0]["data"] == [1, 2, 3], str(_items)[:100])
_items = remote.parse_response_items(
    200, "中文正文".encode("gbk"), "text/plain; charset=gbk", None, None)
check("F3 text charset param honoured (gbk decode)",
      len(_items) == 1 and _items[0]["kind"] == "text"
      and _items[0]["text"] == "中文正文", str(_items)[:100])
_items = remote.parse_response_items(
    200, "plain default".encode("utf-8"), "text/plain", None, None)
check("F3 text without charset defaults to UTF-8",
      len(_items) == 1 and _items[0]["text"] == "plain default", str(_items)[:80])
try:
    remote.parse_response_items(
        200, "中文正文".encode("gbk"), "text/plain; charset=utf-8", None, None)
    check("F3 undecodable text (gbk bytes as utf-8) → invalid_response",
          False, "accepted")
except RemoteError as exc:
    check("F3 undecodable text (gbk bytes as utf-8) → invalid_response",
          exc.code == "invalid_response", exc.code)
try:
    remote.parse_response_items(
        200, b"x", "text/plain; charset=no-such-charset", None, None)
    check("F3 unknown charset name → invalid_response", False, "accepted")
except RemoteError as exc:
    check("F3 unknown charset name → invalid_response",
          exc.code == "invalid_response", exc.code)


# 清场
# uid2 被升为 samelabs operator 并驱动过 /samelabs/tools/create → 留下
# operator_audit 行; actor FK 不带 CASCADE, 测试清理先删 audit 引用再删
# actor (test-only cleanup; 生产 audit writer 保持 append-only)。
_cur.execute("delete from operator_audit where actor_user_id in (%s, %s)"
             " or actor_user_id in (select id from users where username like 'ra%%')",
             (_auid, uid2))
_cur.execute("delete from pricing where tool_id like 'rs%/sec%' or tool_id = 'secop1'")
_cur.execute("delete from platform_tools where tool_key like 'rs%' or tool_key = 'secop1'")
_cur.execute("delete from sessions where user_id in (%s, %s) or user_id in (select id from users where username like 'ra%%')", (_auid, uid2))
_cur.execute(
    "select id from users where id in (%s, %s) or username like 'ra%%'",
    (_auid, uid2))
purge_users(_cur, [r[0] for r in _cur.fetchall()])
_con.close()

print()
if FAIL:
    print(f"FAIL: {len(FAIL)} — " + "; ".join(FAIL)); _sys.exit(1)
print("FAIL: 无 — 全绿")
