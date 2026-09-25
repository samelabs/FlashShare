"""Worker process entry point for tool execution.

Runs in a subprocess via ProcessPoolExecutor. Receives serialized sources
+ params; the handler returns an items list (IO-1 result model); this
module reads every file-item stream into bounded bytes and validates the
full item set before anything is staged. Pillow memory (85MB) stays in
this process — OOM here doesn't kill the main server.
"""

import io
import sys

# Core modules are in ../core/
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent.parent))  # core/ (execution/ 的上一级)

from tools import get_tool


def _read_bounded(stream, cap: int) -> bytes:
    """Read at most cap+1 bytes; anything over the cap fails typed.
    The stream is closed whether the read succeeds or not."""
    try:
        chunks = []
        total = 0
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > cap:
                raise ValueError("err_output_too_large")
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        close = getattr(stream, "close", None)
        if close is not None:
            try:
                close()
            except Exception:
                pass


def process_tool(tool_id: str, sources: list, params: dict) -> dict:
    """Execute a tool handler in this subprocess.

    Args:
        tool_id: registered tool identifier
        sources: list of (bytes, filename, content_type, size) input
                 tuples — may be EMPTY for text-only tools
        params: canonical tool parameters

    Returns:
        {"items": [...]} where file items carry raw "data" bytes; the
        combined item set satisfies MAX_RESULT_ITEMS / inline budget /
        per-file cap (validated here, before staging).

    Raises:
        ValueError with typed err_* codes on contract violations.
    """
    # Ensure tools are registered in this process (important for pool respawn)
    from tools import load_all
    load_all()  # auto-discover every tools_*.py

    tool = get_tool(tool_id)
    if not tool:
        raise ValueError(f"Unknown tool: {tool_id}")

    handler = tool["handler"]

    if len(sources) == 1 and tool.get("files_max", 1) == 1:
        src_bytes, filename, ctype, size = sources[0]
        tool_input = {
            "stream": io.BytesIO(src_bytes),
            "filename": filename,
            "content_type": ctype,
            "size": size,
        }
    else:
        tool_input = {"sources": [
            {"stream": io.BytesIO(sb), "filename": fn,
             "content_type": ct, "size": sz}
            for sb, fn, ct, sz in sources
        ]}

    raw_output = handler(tool_input, params)
    if not isinstance(raw_output, dict) \
            or not isinstance(raw_output.get("items"), list):
        raise ValueError("err_items_shape")

    from config import MAX_UPLOAD_BYTES
    from execution.results import (validate_items_declaration,
                                   validate_inline_item, ItemsError,
                                   MAX_RESULT_ITEMS, MAX_INLINE_RESULT_BYTES)
    try:
        normalized = validate_items_declaration(raw_output["items"])
    except ItemsError as exc:
        raise ValueError(str(exc)) from exc

    inline_total = 0
    out_items = []
    for norm, raw in zip(normalized, raw_output["items"]):
        if norm["kind"] == "file":
            stream = raw.get("stream")
            data = raw.get("data")
            if data is None and stream is None:
                raise ValueError("err_items_shape")
            if data is None:
                data = _read_bounded(stream, MAX_UPLOAD_BYTES)
            if not isinstance(data, bytes) or not data:
                raise ValueError("err_items_shape")
            if len(data) > MAX_UPLOAD_BYTES:
                raise ValueError("err_output_too_large")
            out_items.append({"kind": "file",
                              "filename": norm["filename"],
                              "content_type": norm["content_type"],
                              "data": data})
        else:
            item = validate_inline_item(
                norm, MAX_INLINE_RESULT_BYTES - inline_total)
            if item["kind"] == "text":
                inline_total += len(item["text"].encode("utf-8"))
            else:
                import json as _json
                inline_total += len(_json.dumps(
                    item["data"], ensure_ascii=False).encode("utf-8"))
            out_items.append(item)

    if len(out_items) > MAX_RESULT_ITEMS:
        raise ValueError("err_items_too_many")
    return {"items": out_items}
