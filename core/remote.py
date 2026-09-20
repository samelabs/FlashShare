"""Remote tool execution drivers (http supply contract, sync mode).

The platform routes file work to EXTERNAL capabilities while keeping the
four invariants:

    1. Input custody   — source stays in 518 storage; the endpoint pulls
                         it via a one-time signed URL (never a push).
    2. Output custody  — the endpoint returns BYTES (or a URL the platform
                         fetches); landing goes through the normal share
                         pipeline (sniff whitelist, zero trust).
    3. Metering        — remote calls pass the SAME execute gate (charge,
                         idempotency, refunds) as local tools.
    4. Secrets         — endpoint credentials live in platform_tools
                         rows, never reach any client surface.

Sync mode contract (this file): execute() dispatches, waits (bounded by
timeout_s), validates output, returns bytes. Callback/status verbs are
the async extension (phase 3) — the four-verb bus stays additive.

SSRF model (single authority per hop):
    - trusted endpoints (operator-managed rows, owner_id IS NULL):
      http and https, any host — the operator vouches (samelabs gate).
      Trusted transport keeps the classic urllib driver.
    - user endpoints (owner_id NOT NULL): https ONLY, public unicast
      IPs only. Every hop (initial request AND each redirect) resolves
      once, validates ALL resolved addresses, and pins the TCP connect
      to one of the validated sockaddrs — the HTTP stack never
      re-resolves the hostname, so DNS rebinding and redirect-SSRF
      cannot swap in an unvalidated address between check and connect.
"""

import http.client
import ipaddress
import json
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request

from config import MAX_UPLOAD_BYTES

DEFAULT_TIMEOUT_S = 60
MAX_TIMEOUT_S = 300
MAX_OUTPUT_BYTES = MAX_UPLOAD_BYTES
MAX_REDIRECTS = 5


class RemoteError(Exception):
    """Typed failure. code is one of:
    ssrf_blocked | bad_endpoint | timeout | http_status | bad_output |
    invalid_response | redirect_blocked
    The execute gate maps these to ToolError → refund + user error key.
    """

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


# ── SSRF guard ──

def _is_public_ip(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    return not (addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_reserved or addr.is_multicast or addr.is_unspecified
                or addr.is_global is False)


def _resolve_host(hostname: str, port: int) -> list:
    """System resolver (tests monkeypatch this module function)."""
    return socket.getaddrinfo(hostname, port, proto=socket.IPPROTO_TCP)


# ── Credential-header authority boundary (user endpoints) ──
# User tools may carry API credentials in many header shapes (Authorization,
# X-API-Key, Api-Key, Private-Token, ...), but never in headers the
# TRANSPORT owns: routing/framing authority must not be developer input.
RESERVED_AUTH_HEADERS = {
    "host", "content-length", "content-type", "connection",
    "transfer-encoding", "te", "trailer", "upgrade",
    "proxy-connection", "expect", "user-agent",
}


def auth_header_allowed(name: str) -> bool:
    """User-tool credential header validator (denylist, case-insensitive).
    Operator (trusted) rows are NOT subject to this boundary."""
    return name.strip().lower() not in RESERVED_AUTH_HEADERS


def _reject_reserved_auth_headers(auth_headers: dict) -> None:
    """Runtime defense for untrusted dispatch: a stored credential header
    may claim a reserved name even though submission rejects them at the
    boundary. Fails typed BEFORE any network request; never logs the
    value."""
    for k in auth_headers:
        if k.strip().lower() in RESERVED_AUTH_HEADERS:
            raise RemoteError("bad_endpoint", "reserved credential header")


def guard_endpoint(url: str, trusted: bool) -> str:
    """Validate an endpoint URL before ANY platform request to it.
    Returns the normalized URL or raises RemoteError('ssrf_blocked')."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        raise RemoteError("bad_endpoint", "unparseable url")
    if trusted:
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise RemoteError("bad_endpoint", "need http(s) host")
        return url
    # user endpoints: https only, resolve and check ALL addressed IPs
    if parts.scheme != "https":
        raise RemoteError("ssrf_blocked", "user endpoints must be https")
    if not parts.hostname:
        raise RemoteError("bad_endpoint", "no host")
    try:
        port = parts.port or 443
    except ValueError:
        raise RemoteError("bad_endpoint", "malformed port")
    try:
        infos = _resolve_host(parts.hostname, port)
    except socket.gaierror:
        raise RemoteError("bad_endpoint", "dns failure")
    for info in infos:
        ip = info[4][0]
        # IPv6 literals from getaddrinfo carry no scope here
        if not _is_public_ip(ip.strip("%").strip("[]")):
            raise RemoteError("ssrf_blocked", f"non-public ip {ip}")
    return url


# ── Shared output validation (zero trust: sniff + size, no exemptions) ──

def validate_output(filename: str, content_type: str, body: bytes) -> bytes:
    from security import sniff_allowed
    if len(body) > MAX_OUTPUT_BYTES:
        raise RemoteError("bad_output", f"{len(body)} bytes over cap")
    ok, final = sniff_allowed(filename, content_type or "", body[:512])
    if not ok:
        raise RemoteError("bad_output", f"sniff rejected: {final}")
    return body


# ── Pinned HTTPS transport (untrusted URLs) ─────────────────────────────
#
# ONE production algorithm per hop:
#   resolve once (getaddrinfo) → EVERY resolved address must pass the
#   public-unicast policy → raw socket built from the resolver's own
#   (family, socktype, proto) and connected to the EXACT validated
#   sockaddr → TLS wrapped with the ORIGINAL hostname (SNI + certificate
#   verification) → http.client driven over that pre-connected socket so
#   the stack never re-resolves the hostname. The Host header carries the
#   correct HTTP authority (host or host:port, IPv6 literals bracketed).
#
# Tests replace system dependencies (resolver/socket/ssl/http.client) via
# monkeypatching; the algorithm itself is identical in production and test.
#
# guard_endpoint() is a SUBMISSION-TIME early check only; the runtime
# security authority is the pinned resolution below.

def _validated_addresses(url: str) -> tuple:
    """Parse + resolve + validate ALL addresses for this hop.
    Returns (hostname, port, http_authority, [(family, socktype, proto,
    sockaddr), ...])."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        raise RemoteError("bad_endpoint", "unparseable url")
    if parts.scheme != "https":
        raise RemoteError("ssrf_blocked", "user endpoints must be https")
    hostname = parts.hostname
    if not hostname:
        raise RemoteError("bad_endpoint", "no host")
    try:
        port = parts.port or 443
    except ValueError:
        raise RemoteError("bad_endpoint", "bad port")
    try:
        infos = _resolve_host(hostname, port)
    except socket.gaierror:
        raise RemoteError("bad_endpoint", "dns failure")
    addrs = []
    for info in infos:
        family, socktype, proto, _canon, sa = info
        ip = str(sa[0]).strip("%").strip("[]")
        if not _is_public_ip(ip):
            raise RemoteError("ssrf_blocked", f"non-public ip {ip}")
        if sa[1] != port:  # normalise resolved port to the URL port
            sa = (sa[0], port) + tuple(sa[2:])
        addrs.append((family, socktype, proto, sa))
    if not addrs:
        raise RemoteError("bad_endpoint", "no addresses")
    # HTTP authority: host[:port], IPv6 literal bracketed; default port
    # omitted (443 for https), non-default always explicit.
    if ":" in hostname and not hostname.startswith("["):
        host_hdr = f"[{hostname}]"
    else:
        host_hdr = hostname
    authority = host_hdr if port == 443 else f"{host_hdr}:{port}"
    return hostname, port, authority, addrs


def _pinned_request(url: str, method: str, body: bytes | None,
                    headers: dict, timeout_s: int) -> tuple:
    """Single hop over the pinned transport. Returns (status, body,
    new_url_if_redirect). Never follows redirects itself.

    Connect semantics: raw socket from the resolver's (family, socktype,
    proto), connect() receives the EXACT validated sockaddr — never the
    hostname — so no second DNS resolution can occur. Timeout failures
    stay typed as RemoteError('timeout')."""
    hostname, port, authority, addrs = _validated_addresses(url)
    parts = urllib.parse.urlsplit(url)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    # Transport-owned authority: Host (and framing headers) are forced —
    # a caller-supplied Host must never override the URL authority, even
    # if some future layer forgets to validate.
    hop_headers = {k: v for k, v in headers.items()
                   if k.strip().lower() not in ("host", "user-agent",
                                                "content-length",
                                                "transfer-encoding")}
    hop_headers["Host"] = authority
    hop_headers.setdefault("User-Agent",
                           "518.PLUS-Dispatcher/1.0 (tool dispatch)")

    last_exc: Exception | None = None
    timed_out = False
    for family, socktype, proto, sa in addrs:
        raw = tls_sock = None
        try:
            raw = socket.socket(family, socktype, proto)
            raw.settimeout(timeout_s)
            raw.connect(sa)  # exact validated sockaddr — never re-resolves
            ctx = ssl.create_default_context()  # verify=REQUIRED, system CA
            tls_sock = ctx.wrap_socket(raw, server_hostname=hostname)
            conn = http.client.HTTPConnection(host=hostname, port=port,
                                              timeout=timeout_s)
            conn.sock = tls_sock  # pin: HTTP stack reuses OUR validated socket
            try:
                conn.request(method, path, body=body, headers=hop_headers)
                resp = conn.getresponse()
                data = resp.read(MAX_OUTPUT_BYTES + 1)
                return resp.status, data, resp.getheader("Location")
            finally:
                conn.close()
        except RemoteError:
            raise
        except (socket.timeout, TimeoutError) as exc:
            last_exc, timed_out = exc, True
        except ssl.SSLCertVerificationError as exc:
            # certificate/hostname verification against the ORIGINAL host
            # failed — never downgraded for IP pinning
            raise RemoteError("http_status",
                              f"tls verify failed: {exc.verify_message}") from exc
        except OSError as exc:
            last_exc = exc
        finally:
            for s in (tls_sock, raw):
                if s is not None:
                    try:
                        s.close()
                    except Exception:
                        pass
    if timed_out:
        raise RemoteError("timeout", str(last_exc))
    raise RemoteError("http_status",
                      f"connect failed: {last_exc}")


_REDIRECT_STATUSES = (301, 302, 303, 307, 308)
_ENTITY_HEADERS = ("content-type", "content-length")


def _origin_of(url: str) -> tuple | None:
    """scheme+host+effective port; malformed port → None (caller fails
    typed — a bare ValueError must never escape the redirect loop)."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return None
    try:
        port = parts.port or 443
    except ValueError:
        return None
    return (parts.scheme, (parts.hostname or "").lower(), port)


def pinned_request(url: str, method: str = "GET", body: bytes | None = None,
                   headers: dict | None = None, timeout_s: int = 60,
                   has_credential: bool = False) -> tuple:
    """Untrusted-safe request with manual redirect handling.

    Every hop re-validates: https-only, all resolved IPs public, connect
    pinned to a validated sockaddr. Redirects cap at MAX_REDIRECTS; the
    POST→GET downgrade on 301/302/303 drops the body AND the entity
    headers (Content-Type/Content-Length never leak the old POST size).

    Credential boundary: with has_credential=True a cross-origin redirect
    (scheme/host/effective-port) is REJECTED — auth material must never
    reach a second origin. Same-origin redirects keep it. Without a
    credential, cross-origin redirects proceed through full revalidation.
    """
    headers = dict(headers or {})
    cur_url = url
    cur_method, cur_body = method, body
    cur_headers = dict(headers)
    seen = {url}
    for _hop in range(MAX_REDIRECTS + 1):
        status, data, new_url = _pinned_request(
            cur_url, cur_method, cur_body, cur_headers, timeout_s)
        if status not in _REDIRECT_STATUSES:
            return status, data
        if not new_url:
            raise RemoteError("redirect_blocked", "redirect without Location")
        new_url = urllib.parse.urljoin(cur_url, new_url)
        if new_url in seen:
            raise RemoteError("redirect_blocked", "redirect loop")
        try:
            nparts = urllib.parse.urlsplit(new_url)
        except ValueError:
            raise RemoteError("redirect_blocked", "unparseable Location")
        if nparts.scheme != "https":
            raise RemoteError("redirect_blocked",
                              "redirect to non-https rejected")
        try:
            _nport = nparts.port  # malformed port → typed, never ValueError
        except ValueError:
            raise RemoteError("redirect_blocked", "malformed redirect port")
        new_origin = _origin_of(new_url)
        if new_origin is None:
            raise RemoteError("redirect_blocked", "malformed redirect url")
        if has_credential and new_origin != _origin_of(cur_url):
            raise RemoteError(
                "redirect_blocked",
                "cross-origin redirect with credential rejected")
        seen.add(new_url)
        cur_url = new_url
        if status in (301, 302, 303) and cur_method != "GET":
            # urllib parity: method downgrades, body AND entity headers
            # are dropped — a stale Content-Length must never describe
            # a body-less GET.
            cur_method, cur_body = "GET", None
            cur_headers = {k: v for k, v in cur_headers.items()
                           if k.lower() not in _ENTITY_HEADERS}
    raise RemoteError("redirect_blocked", f"over {MAX_REDIRECTS} redirects")


# ── HTTP driver (plain file-processing API) ──

def _http_call(endpoint: str, payload: dict, headers: dict,
               timeout_s: int, trusted: bool) -> tuple:
    """Returns (status, body, endpoint_response_content_type|None)."""
    body = json.dumps(payload).encode()
    if not trusted:
        # Stored-row defense: a reserved credential header (Host, framing,
        # ...) must fail typed BEFORE any network request.
        _reject_reserved_auth_headers(headers)
        # Untrusted: pinned transport with redirect + credential policy.
        # Transport forces Host/UA; credential headers ride along.
        hdrs = {"Content-Type": "application/json",
                "User-Agent": "518.PLUS-Dispatcher/1.0 (tool dispatch)",
                **headers}
        status, data = pinned_request(
            endpoint, method="POST", body=body, headers=hdrs,
            timeout_s=timeout_s, has_credential=bool(headers))
        if status != 200:
            raise RemoteError("http_status", f"{status}")
        # Pinned transport returns the body only — no response identity.
        # Raw-byte products on this path carry no endpoint content_type;
        # landing MIME is derived from the canonical filename + bytes.
        return status, data, None
    req = urllib.request.Request(
        endpoint, data=body, method="POST",
        headers={"Content-Type": "application/json",
                 # Self-describing UA: default Python-urllib is blocked by
                 # WAFs (e.g. Cloudflare browser-integrity 1010) on user
                 # endpoints that sit behind one.
                 "User-Agent": "518.PLUS-Dispatcher/1.0 (tool dispatch)",
                 **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            ctype = resp.headers.get("Content-Type")
            return resp.status, resp.read(MAX_OUTPUT_BYTES + 1), ctype
    except urllib.error.HTTPError as exc:
        detail = exc.read(500).decode("utf-8", "replace")
        raise RemoteError("http_status", f"{exc.code} {detail}") from exc
    except urllib.error.URLError as exc:
        if isinstance(getattr(exc, "reason", None), socket.timeout):
            raise RemoteError("timeout", str(exc.reason)) from exc
        raise RemoteError("http_status", str(exc)) from exc
    except socket.timeout as exc:
        raise RemoteError("timeout", "") from exc


def drive_http(endpoint_decl: dict, sources: list, params: dict,
               auth_headers: dict, trusted: bool) -> dict:
    """sources: [{filename, content_type, size, fetch_url}] (pull model).
    Returns {filename, content_type, bytes} — validated, ready to land.

    Trusted rows: guard_endpoint (operator policy) then the classic
    urllib driver. Untrusted rows: NO pre-guard here — the pinned
    transport itself is the runtime security authority (single DNS
    resolution per hop); the route already ran guard_endpoint as an
    early submission-time check."""
    timeout_s = min(int(endpoint_decl.get("timeout_s") or DEFAULT_TIMEOUT_S),
                    MAX_TIMEOUT_S)
    url = (guard_endpoint(endpoint_decl["url"], trusted) if trusted
           else endpoint_decl["url"])
    payload = {
        "job_inputs": [
            {"filename": s["filename"], "content_type": s["content_type"],
             "size": s["size"], "url": s["fetch_url"]}
            for s in sources
        ],
        "params": params,
    }
    status, body, resp_ctype = _http_call(url, payload, auth_headers, timeout_s, trusted)
    if status != 200:
        raise RemoteError("http_status", f"{status}")
    try:
        out = json.loads(body)
    except Exception:
        # raw-bytes mode: body IS the file
        return _wrap_raw(body, content_type=resp_ctype)
    # json mode: {filename?, content_type?, data_b64 | output_url}
    # resp_ctype here is the JSON ENVELOPE's Content-Type — product MIME
    # comes only from the JSON product metadata (out.content_type) or, in
    # its absence, from the canonical filename + byte validation. The
    # envelope MIME must never be inherited by the wrapped product.
    if "output_url" in out:
        data = _pull_output_url(out["output_url"], timeout_s, trusted)
        return _wrap_raw(data, out.get("filename"), out.get("content_type"))
    if "data_b64" in out:
        import base64
        try:
            data = base64.b64decode(out["data_b64"], validate=True)
        except Exception as exc:
            raise RemoteError("bad_output", "b64 decode failed") from exc
        return _wrap_raw(data, out.get("filename"), out.get("content_type"))
    raise RemoteError("invalid_response", "no data_b64/output_url/raw body")


def _pull_output_url(output_url: str, timeout_s: int, trusted: bool) -> bytes:
    """Platform fetches the produced file (delivery verb 3, URL variant).

    The URL is ENDPOINT-CONTROLLED — it goes through the SAME SSRF guard
    as the endpoint itself. Untrusted tools: https + public IP only, and
    the fetch itself rides the pinned transport (redirects re-validated
    per hop) — a malicious endpoint must not turn the platform into an
    internal fetcher. Trusted (operator) rows keep the vouched exemption."""
    if not str(output_url).startswith("https://"):
        raise RemoteError("bad_output", "output_url must be https")
    if trusted:
        req = urllib.request.Request(output_url)
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                return resp.read(MAX_OUTPUT_BYTES + 1)
        except Exception as exc:
            raise RemoteError("bad_output", f"output fetch failed: {exc}") from exc
    status, data = pinned_request(str(output_url), method="GET",
                                  timeout_s=timeout_s)
    if status != 200:
        raise RemoteError("bad_output", f"output fetch status {status}")
    return data


def _wrap_raw(body: bytes, filename: str | None = None,
              content_type: str | None = None) -> dict:
    """Wrap raw endpoint product bytes into a canonical landing result.

    Inputs are the PRODUCT itself (endpoint-declared identity only):
      body — the product bytes; filename — optional endpoint filename;
      content_type — optional endpoint response content_type.
    No endpoint TOOL declaration is consulted here (既定产品裁定:
    output landing identity is endpoint-product-owned).

    Landing identity: endpoint filename > sniffed placeholder
    (result.<ext> by product magic). MIME: endpoint content_type, but a
    blank or generic application/octet-stream value never overrides a
    MIME the platform itself can determine from the canonical filename
    + byte validation (sniff_allowed falls back to mimetypes).
    """
    if not filename:
        ext = _sniff_ext(body[:64])
        if not ext:
            raise RemoteError("bad_output", "unknown output format (no filename given)")
        filename = "result" + ext
    name = filename
    given_ctype = (content_type or "").strip().lower()
    if given_ctype in ("", "application/octet-stream"):
        given_ctype = ""  # placeholder — do NOT let it mask the real MIME
    validated = validate_output(name, given_ctype, body)
    final_type = sniff_final_type(name, given_ctype, body)
    # sniff_final_type returns the declared type when sniff_allowed cannot
    # determine better; with an empty declaration it resolves via
    # mimetypes on the canonical filename. A non-generic endpoint type
    # wins as-is (validated above); a generic one must never survive.
    if not given_ctype and (not final_type or final_type.lower() == "application/octet-stream"):
        import mimetypes as _mt
        final_type = _mt.guess_type(name)[0] or "application/octet-stream"
    return {"filename": name, "content_type": final_type,
            "bytes": validated}


def _sniff_ext(head: bytes) -> str | None:
    """产物魔数 → 扩展名 (占位名生成用)。仅覆盖常见二进制格式; 未识别 → None。"""
    m = {
        b"\x89PNG\r\n\x1a\n": ".png",
        b"\xff\xd8": ".jpg",
        b"GIF87a": ".gif", b"GIF89a": ".gif",
        b"%PDF": ".pdf",
        b"BM": ".bmp",
        b"PK\x03\x04": ".zip",
        b"\x1f\x8b": ".gz",
        b"ID3": ".mp3", b"\xff\xfb": ".mp3", b"\xff\xf3": ".mp3",
        b"RIFF": ".webp",  # RIFF 容器细化需读 8-12 位 (WEBP/WAV), 保守归 webp
    }
    for magic, ext in m.items():
        if head.startswith(magic):
            if magic == b"RIFF" and len(head) >= 12 and head[8:12] == b"WAVE":
                return ".wav"
            if magic == b"RIFF" and len(head) >= 12 and head[8:12] != b"WEBP":
                continue
            return ext
    return None


def sniff_final_type(filename: str, declared: str, body: bytes) -> str:
    from security import sniff_allowed
    ok, final = sniff_allowed(filename, declared, body[:512])
    return final if ok else declared
