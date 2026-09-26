"""Result model (IO-1): one call's result is an ordered list of items.

Item shapes (canonical, exactly these keys):
  {"kind": "text", "text": str}
  {"kind": "json", "data": <json value>}
  {"kind": "file", "filename": str, "content_type": str, "size": int}

Hard limits (constants, enforced at every producer boundary):
  MAX_RESULT_ITEMS        total items per call
  MAX_INLINE_RESULT_BYTES combined text+json payload bytes (UTF-8)
  file size               platform per-file upload cap (config)

Storage split: file items land one Gokapi object + one uploads row each
(each with its own public_id, delivery policy from the invocation);
text/json items live inside tool_calls.result_json. The whole result
owns one public id (tool_calls.result_public_id) served at /r/{id}.
Settlement (all file landings + tool_call ok + earnings) is ONE
transaction — gate._finish owns it, this module never commits.
"""

from dataclasses import dataclass, field

MAX_RESULT_ITEMS = 20
MAX_INLINE_RESULT_BYTES = 1_000_000
ITEM_KINDS = ("text", "json", "file")


class ItemsError(ValueError):
    """str(exc) is the typed error code (err_items_*)."""


@dataclass(frozen=True)
class StagedFileItem:
    """One file item physically staged in Gokapi; durable identity is
    minted only inside the settlement transaction."""
    gokapi_id: str
    filename: str
    content_type: str
    size: int
    expiry_days: int
    allowed_downloads: int
    expire_at: int | None
    url_hotlink: str


@dataclass(frozen=True)
class StagedItems:
    """The execution output of one call: ordered items where text/json
    are carried inline and file items are staged. `order` records the
    item kinds in result order (parallel reference into inline/files)."""
    order: list[str] = field(default_factory=list)
    inline: list[dict] = field(default_factory=list)   # text/json items
    files: list[StagedFileItem] = field(default_factory=list)
    ip: str = ""
    orig_size: int | None = None


def validate_inline_item(item: dict, inline_budget: int) -> dict:
    """Validate/normalize one text|json item; returns the canonical item.
    Raises ItemsError with typed codes."""
    if not isinstance(item, dict):
        raise ItemsError("err_items_shape")
    kind = item.get("kind")
    if kind == "text":
        text = item.get("text")
        if not isinstance(text, str):
            raise ItemsError("err_items_shape")
        if len(text.encode("utf-8")) > MAX_INLINE_RESULT_BYTES \
                or len(text.encode("utf-8")) > inline_budget:
            raise ItemsError("err_items_too_large")
        return {"kind": "text", "text": text}
    if kind == "json":
        import json as _json
        data = item.get("data")
        try:
            blob = _json.dumps(data, ensure_ascii=False)
        except (TypeError, ValueError):
            raise ItemsError("err_items_shape") from None
        if len(blob.encode("utf-8")) > MAX_INLINE_RESULT_BYTES \
                or len(blob.encode("utf-8")) > inline_budget:
            raise ItemsError("err_items_too_large")
        return {"kind": "json", "data": data}
    raise ItemsError("err_items_shape")


def validate_items_declaration(items) -> list[dict]:
    """Validate a handler/endpoint item list BEFORE any staging:
    shape, count limit, combined inline budget. File items must carry
    filename/content_type (bytes already read by the caller). Returns
    the normalized list."""
    if not isinstance(items, list):
        raise ItemsError("err_items_shape")
    if len(items) > MAX_RESULT_ITEMS:
        raise ItemsError("err_items_too_many")
    out = []
    inline_total = 0
    for it in items:
        if isinstance(it, dict) and it.get("kind") == "file":
            fn, ct = it.get("filename"), it.get("content_type")
            if not isinstance(fn, str) or not fn.strip() \
                    or not isinstance(ct, str) or not ct.strip():
                raise ItemsError("err_items_shape")
            out.append({"kind": "file", "filename": fn,
                        "content_type": ct})
            continue
        norm = validate_inline_item(it, MAX_INLINE_RESULT_BYTES - inline_total)
        if norm["kind"] == "text":
            inline_total += len(norm["text"].encode("utf-8"))
        else:
            import json as _json
            inline_total += len(_json.dumps(norm["data"],
                                            ensure_ascii=False).encode("utf-8"))
        out.append(norm)
    return out


def land_items_result(con, user_id: int, staged: StagedItems,
                      base_url: str, delivery: dict) -> tuple[int | None, str, dict]:
    """Land the durable result inside the CALLER's settlement
    transaction: every file item → one uploads row (own public_id,
    invocation delivery policy); mint the result public_id; build the
    canonical public result record (stored as tool_calls.result_json).

    The record's expireAt is delivery-owned for EVERY result shape:
    file results inherit the (latest) file expiry, text/json-only
    results expire at ts + delivery expiry_days — the /r page and the
    MCP structuredContent expires_at both read this single authority.

    Returns (first_file_upload_id | None, result_public_id, record)."""
    from repo.uploads import insert_record
    from utils import generate_public_id, public_file_url, now as _now

    ts = _now()
    upload_id = None
    final = []
    file_iter = iter(staged.files)
    inline_iter = iter(staged.inline)
    for kind in staged.order:
        if kind == "file":
            f = next(file_iter)
            public_id = generate_public_id(con)
            dl_url = public_file_url(public_id)
            uid = insert_record(
                con, user_id, staged.ip, public_id, f.gokapi_id,
                f.filename, f.content_type, f.size,
                f.expiry_days, f.allowed_downloads, f.expire_at,
                dl_url, f.url_hotlink, ts)
            if upload_id is None:
                upload_id = int(uid)
            final.append({
                "kind": "file",
                "filename": f.filename,
                "contentType": f.content_type,
                "size": f.size,
                "publicId": public_id,
                "gokapiId": f.gokapi_id,
                "page_url": dl_url,
                "download_url": f"{dl_url}?download=1",
                "expireAt": f.expire_at,
                "expiryDays": f.expiry_days,
                "allowedDownloads": f.allowed_downloads,
            })
        else:
            final.append(next(inline_iter))
    result_public_id = generate_public_id(con)
    # Delivery-owned result expiry: file results inherit the latest file
    # expiry; text/json-only results expire with the delivery window.
    expire_at = None
    for it in final:
        if it.get("kind") == "file" and it.get("expireAt"):
            exp = int(it["expireAt"])
            if expire_at is None or exp > expire_at:
                expire_at = exp
    if expire_at is None:
        expire_at = ts + int(delivery["expiry_days"]) * 86400
    record = {
        "resultId": result_public_id,
        "resultUrl": f"{base_url}/r/{result_public_id}",
        "expireAt": expire_at,
        "items": final,
    }
    if staged.orig_size is not None:
        record["origSize"] = staged.orig_size
    return upload_id, result_public_id, record
