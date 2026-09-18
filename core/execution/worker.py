"""Worker process entry point for tool execution.

Runs in a subprocess via ProcessPoolExecutor.
Receives serialized tool_id + source_bytes + params, returns result_bytes.
Pillow memory (85MB) stays in this process — OOM here doesn't kill the main server.
"""

import io
import sys

# Core modules are in ../core/
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent.parent))  # core/ (execution/ 的上一级)

from tools import get_tool


def process_tool(tool_id: str, source_bytes: bytes, filename: str,
                 content_type: str, size: int, params: dict,
                 multi_sources: list = None) -> dict:
    """Execute a tool handler in this subprocess.

    Args:
        tool_id: registered tool identifier
        source_bytes: raw file bytes (single-file mode)
        filename: original filename
        content_type: MIME type
        size: byte count
        params: tool parameters (preset, etc.)
        multi_sources: list of (bytes, filename, content_type, size) tuples
                       for multi-file tools (multi=True). Overrides single-file args.

    Returns:
        {"output_bytes": bytes, "content_type": str, "filename": str, "size": int}
    """
    # Ensure tools are registered in this process (important for pool respawn)
    from tools import load_all
    load_all()  # auto-discover every tools_*.py

    tool = get_tool(tool_id)
    if not tool:
        raise ValueError(f"Unknown tool: {tool_id}")

    handler = tool["handler"]

    if multi_sources and tool.get("multi"):
        # Multi-file mode: build sources array
        sources = []
        for src_bytes, src_name, src_ct, src_size in multi_sources:
            sources.append({
                "stream": io.BytesIO(src_bytes),
                "filename": src_name,
                "content_type": src_ct,
                "size": src_size,
            })
        tool_input = {"sources": sources}
    else:
        # Single-file mode (default, unchanged)
        tool_input = {
            "stream": io.BytesIO(source_bytes),
            "filename": filename,
            "content_type": content_type,
            "size": size,
        }

    tool_output = handler(tool_input, params)

    # Bounded output read: at most MAX_UPLOAD_BYTES + 1 — anything over
    # the platform file cap fails immediately, never buffered whole first.
    # The handler stream is closed whether the read succeeds or not.
    from config import MAX_UPLOAD_BYTES
    stream = tool_output["stream"]
    try:
        chunks = []
        total = 0
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_UPLOAD_BYTES:
                raise ValueError("err_output_too_large")
            chunks.append(chunk)
        output_bytes = b"".join(chunks)
    finally:
        close = getattr(stream, "close", None)
        if close is not None:
            try:
                close()
            except Exception:
                pass

    return {
        "output_bytes": output_bytes,
        "content_type": tool_output["content_type"],
        "filename": tool_output["filename"],
        "size": len(output_bytes),
    }
