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

SSRF model:
    - trusted endpoints (operator-managed rows, owner_id IS NULL):
      http and https, any host — the operator vouches (samelabs gate).
    - user endpoints (owner_id NOT NULL): https ONLY, public unicast
      IPs only — resolved-IP check defeats DNS rebinding; loopback /
      link-local / private / reserved ranges rejected.
"""

import ipaddress
import json
import socket
import urllib.error
import urllib.parse
import urllib.request

from config import MAX_UPLOAD_BYTES

DEFAULT_TIMEOUT_S = 60
MAX_TIMEOUT_S = 300
MAX_OUTPUT_BYTES = MAX_UPLOAD_BYTES


class RemoteError(Exception):
    """Typed failure. code is one of:
    ssrf_blocked | bad_endpoint | timeout | http_status | bad_output |
    invalid_response
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
        infos = socket.getaddrinfo(parts.hostname, parts.port or 443,
                                   proto=socket.IPPROTO_TCP)
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


# ── HTTP driver (plain file-processing API) ──

def _http_call(endpoint: str, payload: dict, headers: dict,
               timeout_s: int) -> tuple:
    body = json.dumps(payload).encode()
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
            return resp.status, resp.read(MAX_OUTPUT_BYTES + 1)
    except urllib.error.HTTPError as exc:
        detail = exc.read(500).decode("utf-8", "replace")
        raise RemoteError("http_status", f"{exc.code} {detail}") from exc
    except urllib.error.URLError as exc:
        if isinstance(getattr(exc, "reason", None), socket.timeout):
            raise RemoteError("timeout", str(exc.reason)) from exc
        raise RemoteError("http_status", str(exc)) from exc
    except socket.timeout as exc:
        raise RemoteError("timeout", str(exc)) from exc


def drive_http(endpoint_decl: dict, sources: list, params: dict,
               auth_headers: dict, trusted: bool) -> dict:
    """sources: [{filename, content_type, size, fetch_url}] (pull model).
    Returns {filename, content_type, bytes} — validated, ready to land."""
    url = guard_endpoint(endpoint_decl["url"], trusted)
    timeout_s = min(int(endpoint_decl.get("timeout_s") or DEFAULT_TIMEOUT_S),
                    MAX_TIMEOUT_S)
    payload = {
        "job_inputs": [
            {"filename": s["filename"], "content_type": s["content_type"],
             "size": s["size"], "url": s["fetch_url"]}
            for s in sources
        ],
        "params": params,
    }
    status, body = _http_call(url, payload, auth_headers, timeout_s)
    if status != 200:
        raise RemoteError("http_status", f"{status}")
    try:
        out = json.loads(body)
    except Exception:
        # raw-bytes mode: body IS the file
        return _wrap_raw(endpoint_decl, body)
    # json mode: {filename?, content_type?, data_b64 | output_url}
    if "output_url" in out:
        data = _pull_output_url(out["output_url"], timeout_s, trusted)
        return _wrap_raw(endpoint_decl, data, out.get("filename"))
    if "data_b64" in out:
        import base64
        try:
            data = base64.b64decode(out["data_b64"], validate=True)
        except Exception as exc:
            raise RemoteError("bad_output", "b64 decode failed") from exc
        return _wrap_raw(endpoint_decl, data, out.get("filename"))
    raise RemoteError("invalid_response", "no data_b64/output_url/raw body")


def _pull_output_url(output_url: str, timeout_s: int, trusted: bool) -> bytes:
    """Platform fetches the produced file (delivery verb 3, URL variant).

    The URL is ENDPOINT-CONTROLLED — it goes through the SAME SSRF guard
    as the endpoint itself. Untrusted tools: https + public IP only
    (a malicious endpoint must not turn the platform into an internal
    fetcher). Trusted (operator) rows keep the vouched exemption."""
    if not str(output_url).startswith("https://"):
        raise RemoteError("bad_output", "output_url must be https")
    guard_endpoint(str(output_url), trusted)
    req = urllib.request.Request(output_url)
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return resp.read(MAX_OUTPUT_BYTES + 1)
    except Exception as exc:
        raise RemoteError("bad_output", f"output fetch failed: {exc}") from exc


def _wrap_raw(decl: dict, body: bytes, filename: str | None = None) -> dict:
    # Landing identity: endpoint-provided filename > sniffed placeholder.
    # 文件名是开发者 API 自定义机制, 平台不限定 — 端点返回什么
    # 用什么; 端点未给时用产物魔数定型生成占位名 (result.<ext>)。
    if not filename:
        ext = _sniff_ext(body[:64])
        if not ext:
            raise RemoteError("bad_output", "unknown output format (no filename given)")
        filename = "result" + ext
    name = filename
    ctype = decl.get("output_content_type") or "application/octet-stream"
    validated = validate_output(name, ctype, body)
    return {"filename": name, "content_type": sniff_final_type(name, ctype, body),
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


