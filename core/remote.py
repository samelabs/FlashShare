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
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from config import MAX_BATCH_BYTES, MAX_UPLOAD_BYTES
from execution.results import (MAX_RESULT_ITEMS, MAX_INLINE_RESULT_BYTES)  # noqa: E402

MIN_TIMEOUT_S = 5
DEFAULT_TIMEOUT_S = 60
MAX_TIMEOUT_S = 300
MAX_OUTPUT_BYTES = MAX_UPLOAD_BYTES
MAX_REDIRECTS = 5
# 提供者错误体读取上限: 非 2xx 的响应体是错误详情素材, 按这个上限读,
# 不按大文件上限读 (拒绝把 50MB 错误页拖进平台)。
MAX_ERROR_DETAIL_BYTES = 4096
# 南向 items 信封的专属 Content-Type (与 application/json 的"单 json 项"
# 语义区分)
ITEMS_ENVELOPE_CT = "application/vnd.518.items+json"
# 响应体分块读取的块大小; 每块之间复核总时限
CHUNK_BYTES = 64 * 1024


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

def validate_output(filename: str, content_type: str, body: bytes) -> tuple:
    """Zero-trust landing gate for endpoint products: size cap + sniff.
    Returns (body, final_filename) — same-family admission may
    re-identify the file; the caller lands it under the final name."""
    from security import sniff_allowed
    if len(body) > MAX_OUTPUT_BYTES:
        raise RemoteError("bad_output", f"{len(body)} bytes over cap")
    ok, final, final_name = sniff_allowed(filename, content_type or "",
                                          body[:512])
    if not ok:
        raise RemoteError("bad_output", f"sniff rejected: {final}")
    return body, final_name


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
                    headers: dict, timeout_s: int,
                    max_bytes: int | None = None,
                    deadline: float | None = None) -> tuple:
    """Single hop over the pinned transport. Returns (status, body,
    new_url_if_redirect, response_content_type, response_content_disposition).
    Never follows redirects itself.

    Body read cap is status-dependent: 2xx reads to max_bytes (default
    the platform file cap); a non-2xx body is error-detail material and
    reads to MAX_ERROR_DETAIL_BYTES only. The body is read in chunks with
    the total deadline re-checked before every chunk.

    Connect semantics: raw socket from the resolver's (family, socktype,
    proto), connect() receives the EXACT validated sockaddr — never the
    hostname — so no second DNS resolution can occur. Timeout failures
    stay typed as RemoteError('timeout').

    Total-time semantics: deadline is an absolute time.monotonic()
    moment bounding the whole hop (None → now + timeout_s). A
    threading.Timer shuts the raw socket down at the deadline so a
    slow-dripping peer cannot hold the slot past the budget; any
    exception at or past the deadline classifies as RemoteError('timeout')
    and stops address iteration."""
    if deadline is None:
        deadline = time.monotonic() + timeout_s
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
        timer = None
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RemoteError("timeout", "deadline exceeded")
            raw = socket.socket(family, socktype, proto)
            raw.settimeout(remaining)
            raw.connect(sa)  # exact validated sockaddr — never re-resolves

            def _abort(_raw=raw):
                # fires at the deadline: kill any in-flight recv on the
                # raw socket (the TLS wrapper propagates the shutdown)
                try:
                    _raw.shutdown(socket.SHUT_RDWR)
                except Exception:
                    pass
            timer = threading.Timer(remaining, _abort)
            timer.daemon = True
            timer.start()

            ctx = ssl.create_default_context()  # verify=REQUIRED, system CA
            tls_sock = ctx.wrap_socket(raw, server_hostname=hostname)
            conn = http.client.HTTPConnection(host=hostname, port=port,
                                              timeout=remaining)
            conn.sock = tls_sock  # pin: HTTP stack reuses OUR validated socket
            try:
                conn.request(method, path, body=body, headers=hop_headers)
                resp = conn.getresponse()
                if 200 <= resp.status < 300:
                    read_cap = (max_bytes if max_bytes is not None
                                else MAX_OUTPUT_BYTES)
                    cl = resp.getheader("Content-Length")
                    if cl is not None:
                        try:
                            if int(cl) > read_cap:
                                raise RemoteError(
                                    "too_large",
                                    f"content-length {cl} over {read_cap}")
                        except ValueError:
                            pass  # malformed CL — the read cap still guards
                else:
                    # error-detail material: a big error page is truncated,
                    # never aborted — the caller reports status + first
                    # MAX_ERROR_DETAIL_BYTES bytes (same cap as the trusted
                    # path's error-body read).
                    read_cap = MAX_ERROR_DETAIL_BYTES
                chunks = []
                total = 0
                while total <= read_cap:
                    rem = deadline - time.monotonic()
                    if rem <= 0:
                        raise RemoteError("timeout",
                                          "deadline exceeded during body read")
                    conn.sock.settimeout(rem)
                    chunk = resp.read(min(CHUNK_BYTES, read_cap + 1 - total))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    total += len(chunk)
                data = b"".join(chunks)
                return (resp.status, data, resp.getheader("Location"),
                        resp.getheader("Content-Type"),
                        resp.getheader("Content-Disposition"))
            finally:
                conn.close()
        except RemoteError:
            raise
        except (socket.timeout, TimeoutError) as exc:
            if time.monotonic() >= deadline:
                raise RemoteError("timeout", str(exc)) from exc
            last_exc, timed_out = exc, True
        except ssl.SSLCertVerificationError as exc:
            # certificate/hostname verification against the ORIGINAL host
            # failed — never downgraded for IP pinning
            raise RemoteError("http_status",
                              f"tls verify failed: {exc.verify_message}") from exc
        except http.client.HTTPException as exc:
            if time.monotonic() >= deadline:
                raise RemoteError("timeout", str(exc)) from exc
            last_exc = exc
        except OSError as exc:
            if time.monotonic() >= deadline:
                raise RemoteError("timeout", str(exc)) from exc
            last_exc = exc
        finally:
            if timer is not None:
                timer.cancel()
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
                   has_credential: bool = False,
                   max_bytes: int | None = None,
                   deadline: float | None = None) -> tuple:
    """Untrusted-safe request with manual redirect handling.

    Every hop re-validates: https-only, all resolved IPs public, connect
    pinned to a validated sockaddr. Redirects cap at MAX_REDIRECTS; the
    POST→GET downgrade on 301/302/303 drops the body AND the entity
    headers (Content-Type/Content-Length never leak the old POST size).

    deadline is an absolute time.monotonic() moment shared by EVERY hop
    (None → now + timeout_s): the whole redirect chain is one time
    budget, so a chain of individually-fast hops cannot drip past it.

    Returns (status, body, response_content_type,
    response_content_disposition) — the response identity travels with
    the bytes (non-2xx bodies read to the error detail cap, not the
    file cap).

    Credential boundary: with has_credential=True a cross-origin redirect
    (scheme/host/effective-port) is REJECTED — auth material must never
    reach a second origin. Same-origin redirects keep it. Without a
    credential, cross-origin redirects proceed through full revalidation.
    """
    if deadline is None:
        deadline = time.monotonic() + timeout_s
    headers = dict(headers or {})
    cur_url = url
    cur_method, cur_body = method, body
    cur_headers = dict(headers)
    seen = {url}
    for _hop in range(MAX_REDIRECTS + 1):
        status, data, new_url, _ctype, _cd = _pinned_request(
            cur_url, cur_method, cur_body, cur_headers, timeout_s,
            max_bytes=max_bytes, deadline=deadline)
        if status not in _REDIRECT_STATUSES:
            return status, data, _ctype, _cd
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

def _read_deadline_bounded(stream, cap: int, deadline: float, what: str) -> bytes:
    """Chunked read with a total deadline. read1 is preferred: it returns
    after ONE underlying system call, so a slow-dripping body cannot pin a
    single chunk read past the deadline (urllib responses expose no
    per-read timeout API — the open-time timeout bounds each system call,
    the deadline bounds the whole body). Reads at most cap+1 bytes so
    overflow stays detectable by length."""
    _read1 = getattr(stream, "read1", None) or stream.read
    chunks = []
    total = 0
    while total <= cap:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RemoteError("timeout", f"{what} deadline exceeded")
        try:
            chunk = _read1(min(CHUNK_BYTES, cap + 1 - total))
        except (socket.timeout, TimeoutError) as exc:
            raise RemoteError("timeout", str(exc)) from exc
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)


def _http_call(endpoint: str, req_body: bytes, req_ct: str,
               headers: dict, timeout_s: int, trusted: bool,
               deadline: float | None = None) -> tuple:
    """Send one southbound request. Returns (status, body,
    response_content_type, response_content_disposition).

    2xx bodies read to the platform file cap; non-2xx bodies read to the
    error-detail cap (they are diagnostic material, never products).
    deadline (absolute time.monotonic(), None → now + timeout_s) is the
    per-call total budget shared with the result-file URL fetch."""
    if deadline is None:
        deadline = time.monotonic() + timeout_s
    if not trusted:
        # Stored-row defense: a reserved credential header (Host, framing,
        # ...) must fail typed BEFORE any network request.
        _reject_reserved_auth_headers(headers)
        # Untrusted: pinned transport with redirect + credential policy.
        # Transport forces Host/UA; credential headers ride along.
        hdrs = {"Content-Type": req_ct,
                "User-Agent": "518.PLUS-Dispatcher/1.0 (tool dispatch)",
                **headers}
        return pinned_request(
            endpoint, method="POST", body=req_body, headers=hdrs,
            timeout_s=timeout_s, has_credential=bool(headers),
            deadline=deadline)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RemoteError("timeout", "deadline exceeded")
    req = urllib.request.Request(
        endpoint, data=req_body, method="POST",
        headers={"Content-Type": req_ct,
                 # Self-describing UA: default Python-urllib is blocked by
                 # WAFs (e.g. Cloudflare browser-integrity 1010) on user
                 # endpoints that sit behind one.
                 "User-Agent": "518.PLUS-Dispatcher/1.0 (tool dispatch)",
                 **headers})
    try:
        with urllib.request.urlopen(req, timeout=min(timeout_s, remaining)) as resp:
            return (resp.status,
                    _read_deadline_bounded(resp, MAX_OUTPUT_BYTES, deadline,
                                           "response body"),
                    resp.headers.get("Content-Type"),
                    resp.headers.get("Content-Disposition"))
    except urllib.error.HTTPError as exc:
        # error bodies read to the SAME diagnostic cap as the pinned path
        try:
            err_body = _read_deadline_bounded(exc, MAX_ERROR_DETAIL_BYTES,
                                              deadline, "error body")
        except RemoteError:
            raise
        except Exception:
            err_body = b""
        return (exc.code, err_body,
                exc.headers.get("Content-Type") if exc.headers else None,
                exc.headers.get("Content-Disposition") if exc.headers else None)
    except urllib.error.URLError as exc:
        if isinstance(getattr(exc, "reason", None), socket.timeout):
            raise RemoteError("timeout", str(exc.reason)) from exc
        raise RemoteError("http_status", str(exc)) from exc
    except socket.timeout as exc:
        raise RemoteError("timeout", "") from exc


def dispatch_payload(sources: list, params: dict) -> dict:
    """The southbound request body, ONE constructor (IO-1):

        {"files": [{"filename", "content_type", "size", "url"}],
         "params": {...}}   (pull model; signed one-time fetch URLs)

    `sources` are the orchestrator's upload dicts; nothing else ever
    rides in the body (delivery is a platform fact; credentials go in
    headers). The public developer guide's example request is checked
    against THIS function (canonical test) — the docs cannot drift
    from the contract."""
    return {
        "files": [
            {"filename": s["filename"], "content_type": s["content_type"],
             "size": s["size"], "url": s["url"]}
            for s in sources
        ],
        "params": params,
    }


def parse_endpoint_items(out, url_fetch) -> list:
    """Validate/normalize ONE decoded endpoint response against the
    southbound contract (IO-1):

        {"items": [{"kind":"text","text": str}
                   | {"kind":"json","data": <json>}
                   | {"kind":"file", "filename": str,
                      "content_type": str, "data_b64" | "url"}]}

    Shape violations raise RemoteError("invalid_response"); every file
    item is fetched (url, through `url_fetch` — the caller's SSRF-guarded
    transport) or decoded (data_b64) and normalized to a landing result
    ({filename, content_type, bytes, size}). The developer guide's
    example response is validated by THIS function (canonical test)."""
    if not isinstance(out, dict) or not isinstance(out.get("items"), list):
        raise RemoteError("invalid_response", "missing items array")
    items = out["items"]
    if len(items) > MAX_RESULT_ITEMS:
        raise RemoteError("invalid_response", "too many items")

    import base64
    results = []
    inline_total = 0
    file_total = 0
    for it in items:
        if not isinstance(it, dict):
            raise RemoteError("invalid_response", "item must be an object")
        kind = it.get("kind")
        if kind == "text":
            text = it.get("text")
            if not isinstance(text, str):
                raise RemoteError("invalid_response", "text item needs text")
            inline_total += len(text.encode("utf-8"))
            if inline_total > MAX_INLINE_RESULT_BYTES:
                raise RemoteError("invalid_response", "inline payload too large")
            results.append({"kind": "text", "text": text})
        elif kind == "json":
            data = it.get("data")
            try:
                blob = json.dumps(data, ensure_ascii=False, allow_nan=False)
            except (TypeError, ValueError) as exc:
                raise RemoteError("invalid_response",
                                  "json item data not serializable") from exc
            inline_total += len(blob.encode("utf-8"))
            if inline_total > MAX_INLINE_RESULT_BYTES:
                raise RemoteError("invalid_response", "inline payload too large")
            results.append({"kind": "json", "data": data})
        elif kind == "file":
            if "url" in it:
                data = url_fetch(it["url"])
            elif "data_b64" in it:
                try:
                    data = base64.b64decode(it["data_b64"], validate=True)
                except Exception as exc:
                    raise RemoteError("bad_output",
                                      "b64 decode failed") from exc
            else:
                raise RemoteError("invalid_response",
                                  "file item needs data_b64 or url")
            # Cumulative file budget: the same platform batch ceiling
            # (config MAX_BATCH_BYTES) ingress enforces on the way in.
            # Once exceeded, no further item is fetched or decoded.
            file_total += len(data)
            if file_total > MAX_BATCH_BYTES:
                raise RemoteError(
                    "too_large",
                    f"file items total {file_total} over {MAX_BATCH_BYTES}")
            wrapped = _wrap_raw(data, it.get("filename"),
                                it.get("content_type"))
            results.append({"kind": "file",
                            "filename": wrapped["filename"],
                            "content_type": wrapped["content_type"],
                            "size": len(wrapped["bytes"]),
                            "bytes": wrapped["bytes"]})
        else:
            raise RemoteError("invalid_response", "unknown item kind")
    return results


def encode_query_params(params: dict) -> str:
    """正文工具的 params → 查询参数串 (值编码单点)。

    数字取十进制文本, 整数值不带 .0 (5.0 → "5"); 布尔 true/false;
    enum / text 原样 (百分号编码)。"""
    parts = []
    for k, v in (params or {}).items():
        if isinstance(v, bool):
            sv = "true" if v else "false"
        elif isinstance(v, float):
            sv = str(int(v)) if v.is_integer() else str(v)
        elif isinstance(v, int):
            sv = str(v)
        else:
            sv = str(v)
        parts.append(f"{urllib.parse.quote(str(k), safe='')}"
                     f"={urllib.parse.quote(sv, safe='')}")
    return "&".join(parts)


def url_with_params(url: str, params: dict) -> str:
    """端点 URL 自带的查询参数保留; params 追加在其后。"""
    extra = encode_query_params(params)
    if not extra:
        return url
    parts = urllib.parse.urlsplit(url)
    query = f"{parts.query}&{extra}" if parts.query else extra
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path, query, parts.fragment))


def endpoint_param_query_conflict(url: str, params_decl) -> bool:
    """声明期冲突判定: 端点 URL 自带查询参数键与 params 声明键撞名。
    文件工具 (params 进 JSON 请求体) 不适用 — 仅正文工具 (params 进
    查询参数) 需要此检查; 供给校验按 execution 分流。"""
    try:
        parts = urllib.parse.urlsplit(url or "")
        url_keys = {k for k, _v in urllib.parse.parse_qsl(parts.query or "",
                                                          keep_blank_values=True)}
    except ValueError:
        return False
    decl_keys = {str(p.get("key")) for p in (params_decl or [])
                 if isinstance(p, dict) and p.get("key")}
    return bool(url_keys & decl_keys)


def _filename_from_content_disposition(cd: str | None) -> str | None:
    """Content-Disposition → filename (带引号与裸值两形态); 无 filename
    段返回 None。"""
    if not cd:
        return None
    for part in cd.split(";"):
        part = part.strip()
        if part.lower().startswith("filename="):
            value = part[len("filename="):].strip()
            if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
                value = value[1:-1]
            return value or None
    return None


def _reject_json_constant(name):
    """json.loads parse_constant: NaN / Infinity / -Infinity are NOT
    legal JSON — a provider sending them is an invalid response (fail +
    refund), never silently parsed into float('nan')."""
    raise ValueError(f"non-finite JSON constant: {name}")


def _charset_of(ctype: str | None) -> str:
    """Content-Type 参数里的 charset (带引号形态容忍); 未指定 = UTF-8。"""
    for part in (ctype or "").split(";")[1:]:
        part = part.strip()
        if part.lower().startswith("charset="):
            value = part[len("charset="):].strip().strip('"')
            if value:
                return value
    return "utf-8"


def _is_json_media_type(base: str) -> bool:
    """application/json 及一切 application/*+json (问题报告、超媒体、
    自定义词汇) 都是结构化 JSON — 按单 json 项解释; 信封专属
    Content-Type 除外 (调用方先判)。"""
    return base == "application/json" or (
        base.startswith("application/") and base.endswith("+json"))


def parse_response_items(status: int, body: bytes, resp_ctype: str | None,
                         resp_cd: str | None, url_fetch) -> list:
    """按响应 Content-Type 解释南向响应 (两种工具同一规则):

      application/vnd.518.items+json → items 信封 (parse_endpoint_items)
      application/json 及 application/*+json → 一个 json 项
      text/*  (按 charset 参数解码, 缺省 UTF-8) → 一个 text 项
      其余                            → 一个文件项 (Content-Disposition
                                       文件名 > 嗅探扩展名; 识别不出
                                       判失败)

    非 2xx 一律 RemoteError("http_status") — 提供者状态与响应体片段
    由调用方组装进错误详情。"""
    if not (200 <= status < 300):
        snippet = body[:MAX_ERROR_DETAIL_BYTES].decode("utf-8", "replace")
        raise RemoteError("http_status", f"{status} {snippet}")
    base = (resp_ctype or "").split(";")[0].strip().lower()
    if base == ITEMS_ENVELOPE_CT:
        try:
            out = json.loads(body, parse_constant=_reject_json_constant)
        except Exception as exc:
            raise RemoteError("invalid_response",
                              "items envelope is not JSON") from exc
        return parse_endpoint_items(out, url_fetch)
    if _is_json_media_type(base):
        try:
            data = json.loads(body, parse_constant=_reject_json_constant)
            # allow_nan=False: a numeric overflow (1e400 → inf) parses as
            # a float and must fail typed here, never enter a result
            blob = json.dumps(data, ensure_ascii=False, allow_nan=False)
        except Exception as exc:
            raise RemoteError("invalid_response",
                              "json response body is not valid JSON") from exc
        if len(blob.encode("utf-8")) > MAX_INLINE_RESULT_BYTES:
            raise RemoteError("invalid_response", "inline payload too large")
        return [{"kind": "json", "data": data}]
    if base.startswith("text/"):
        charset = _charset_of(resp_ctype)
        try:
            text = body.decode(charset)
        except (UnicodeDecodeError, LookupError) as exc:
            raise RemoteError("invalid_response",
                              f"text response is not decodable"
                              f" ({charset})") from exc
        if len(text.encode("utf-8")) > MAX_INLINE_RESULT_BYTES:
            raise RemoteError("invalid_response", "inline payload too large")
        return [{"kind": "text", "text": text}]
    # 文件项: 响应就是产物字节
    filename = _filename_from_content_disposition(resp_cd)
    wrapped = _wrap_raw(body, filename, base or None)
    return [{"kind": "file",
             "filename": wrapped["filename"],
             "content_type": wrapped["content_type"],
             "size": len(wrapped["bytes"]),
             "bytes": wrapped["bytes"]}]


def drive_http(endpoint_decl: dict, sources: list, params: dict,
               auth_headers: dict, trusted: bool,
               body=None, canonical_body=None,
               body_format: str | None = None) -> dict:
    """The ONE southbound developer contract (IO-1) — see
    dispatch_payload (file request) and parse_response_items (response).
    A response that does not match the contract raises RemoteError —
    the call fails and the caller refunds.

    文件工具: JSON 信封 (dispatch_payload); 正文工具: 取值原样 POST
    (text → text/plain; charset=utf-8, json → application/json,
    规范序列化字节), params 编码进查询参数 (端点自带查询保留)。

    Trusted rows: guard_endpoint (operator policy) then the classic
    urllib driver. Untrusted rows: the pinned transport itself is the
    runtime security authority (single DNS resolution per hop).

    timeout_s is the TOTAL per-call budget: one deadline covers the
    request (all redirect hops) AND the result-file URL fetch."""
    timeout_s = min(int(endpoint_decl.get("timeout_s") or DEFAULT_TIMEOUT_S),
                    MAX_TIMEOUT_S)
    deadline = time.monotonic() + timeout_s
    url = (guard_endpoint(endpoint_decl["url"], trusted) if trusted
           else endpoint_decl["url"])
    if body_format:
        if canonical_body is None:
            raise RemoteError("bad_endpoint", "body transport requires a"
                              " validated canonical body")
        req_body = canonical_body.encode("utf-8")
        req_ct = ("application/json" if body_format == "json"
                  else "text/plain; charset=utf-8")
        final_url = url_with_params(url, params)
    else:
        payload = dispatch_payload(sources, params)
        req_body = json.dumps(payload).encode()
        req_ct = "application/json"
        final_url = url
    status, resp_body, resp_ctype, resp_cd = _http_call(
        final_url, req_body, req_ct, auth_headers, timeout_s, trusted,
        deadline=deadline)
    items = parse_response_items(
        status, resp_body, resp_ctype, resp_cd,
        lambda u: _pull_item_file_url(u, timeout_s, trusted,
                                      deadline=deadline))
    return {"items": items}


def _pull_item_file_url(item_url: str, timeout_s: int, trusted: bool,
                        deadline: float | None = None) -> bytes:
    """Platform fetches the produced file (delivery verb 3, URL variant).

    The URL is ENDPOINT-CONTROLLED — it goes through the SAME SSRF guard
    as the endpoint itself. Untrusted tools: https + public IP only, and
    the fetch itself rides the pinned transport (redirects re-validated
    per hop) — a malicious endpoint must not turn the platform into an
    internal fetcher. Trusted (operator) rows keep the vouched exemption.

    Shares the caller's per-call deadline (None → now + timeout_s): the
    endpoint response and its result-file fetch are one time budget."""
    if not str(item_url).startswith("https://"):
        raise RemoteError("bad_output", "item url must be https")
    if deadline is None:
        deadline = time.monotonic() + timeout_s
    if trusted:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RemoteError("timeout", "deadline exceeded")
        req = urllib.request.Request(item_url)
        try:
            with urllib.request.urlopen(
                    req, timeout=min(timeout_s, remaining)) as resp:
                return _read_deadline_bounded(resp, MAX_OUTPUT_BYTES,
                                              deadline, "output fetch body")
        except RemoteError:
            raise
        except Exception as exc:
            raise RemoteError("bad_output", f"output fetch failed: {exc}") from exc
    status, data, _ctype, _cd = pinned_request(str(item_url), method="GET",
                                               timeout_s=timeout_s,
                                               deadline=deadline)
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
        from security import detect_ext
        ext = detect_ext(body[:64])
        if not ext:
            raise RemoteError("bad_output", "unknown output format (no filename given)")
        filename = "result" + ext
    # remote (endpoint-declared) filenames ride the SAME canonical
    # cleansing as every other surface — one sanitize_filename authority
    from utils import sanitize_filename as _sfn
    name = _sfn(filename)
    given_ctype = (content_type or "").strip().lower()
    if given_ctype in ("", "application/octet-stream", "application/json"):
        # placeholder — do NOT let it mask the real MIME. application/json
        # in raw mode is a mislabeled envelope (a parseable-JSON body never
        # reaches raw mode); a true .json product travels through the JSON
        # envelope with explicit product metadata instead.
        given_ctype = ""
    validated, name = validate_output(name, given_ctype, body)
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


def sniff_final_type(filename: str, declared: str, body: bytes) -> str:
    """Declared MIME checked against the sniffed filename (the caller
    passes sniff_allowed's final_filename — same-family admission may
    have re-identified the file before this runs)."""
    from security import sniff_allowed
    ok, final, _final_name = sniff_allowed(filename, declared, body[:512])
    return final if ok else declared
