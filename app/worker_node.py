"""FlashShare Worker Node — remote tool processing service.

Runs on a separate machine in the same subnet as the main server.
Receives tool execution requests, pulls source files from gokapi,
processes them with Pillow, writes results back to gokapi.

The main server never touches file bytes — only sends gokapi_id + params.

Deploy: uvicorn worker_node:app --host 0.0.0.0 --port 9000 --workers 1
"""

import io
import json
import logging
import urllib.parse
import http.client
from typing import Dict, Any, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

# Tool modules (must be in the same directory or Python path)
from tools import get_tool, get_default_preset, ToolError
import tools_registry   # noqa: F401 — registers share + privacy
import tools_beautify   # noqa: F401 — registers beautify
import tools_compress   # noqa: F401 — registers compress

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
logger = logging.getLogger("worker-node")

app = FastAPI(title="FlashShare Worker Node", docs_url=None, redoc_url=None)


class ProcessRequest(BaseModel):
    tool_id: str = Field(..., min_length=1, max_length=64)
    gokapi_id: str = Field(..., min_length=1, max_length=64)
    gokapi_url: str = Field(..., description="Base URL of gokapi, e.g. http://10.0.0.141:53842")
    gokapi_key: str = Field(..., description="Gokapi API key")
    filename: str = Field(..., description="Original filename")
    content_type: str = Field("application/octet-stream")
    size: int = Field(0)
    params: Dict[str, Any] = Field(default_factory=dict)


@app.get("/healthz")
async def healthz():
    return {"ok": True, "status": "worker_healthy"}


def _gokapi_pull(gokapi_url: str, gokapi_key: str, gokapi_id: str) -> tuple:
    """Pull file bytes from gokapi via hotlink API.
    Returns (file_bytes, content_type).
    """
    parsed = urllib.parse.urlparse(gokapi_url)
    host = parsed.hostname
    port = parsed.port or 80

    # Get file info to find hotlink URL
    conn = http.client.HTTPConnection(host, port, timeout=30)
    conn.request("GET", f"/api/files/list/{urllib.parse.quote(gokapi_id)}",
                 headers={"apikey": gokapi_key})
    resp = conn.getresponse()
    data = resp.read()
    conn.close()

    if resp.status != 200:
        raise RuntimeError(f"gokapi info failed: HTTP {resp.status}")

    info = json.loads(data.decode("utf-8"))

    # Get download URL — use the direct download endpoint
    download_url = info.get("UrlDownload", "")
    if not download_url:
        raise RuntimeError("no download URL from gokapi")

    # Rewrite to use the internal gokapi_url (strip external domain)
    download_path = urllib.parse.urlparse(download_url).path
    download_query = urllib.parse.urlparse(download_url).query

    conn2 = http.client.HTTPConnection(host, port, timeout=60)
    path = download_path
    if download_query:
        path += "?" + download_query
    conn2.request("GET", path)
    resp2 = conn2.getresponse()
    file_bytes = resp2.read()
    conn2.close()

    if resp2.status != 200:
        raise RuntimeError(f"gokapi download failed: HTTP {resp2.status}")

    content_type = info.get("ContentType", "application/octet-stream")
    return file_bytes, content_type


def _gokapi_push(gokapi_url: str, gokapi_key: str, file_bytes: bytes,
                 filename: str, content_type: str,
                 expiry_days: int, allowed_downloads: int) -> dict:
    """Upload processed file to gokapi. Returns gokapi file info dict."""
    parsed = urllib.parse.urlparse(gokapi_url)
    host = parsed.hostname
    port = parsed.port or 80

    boundary = "----FlashShareWorkerBoundary"

    body = b""
    # file field (multipart)
    body += f"--{boundary}\r\n".encode()
    body += f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode()
    body += f"Content-Type: {content_type}\r\n\r\n".encode()
    body += file_bytes
    body += b"\r\n"
    # expiry
    body += f"--{boundary}\r\n".encode()
    body += b'Content-Disposition: form-data; name="expire"\r\n\r\n'
    body += str(expiry_days * 24).encode()  # gokapi uses hours
    body += b"\r\n"
    # downloads
    body += f"--{boundary}\r\n".encode()
    body += b'Content-Disposition: form-data; name="allowedDownloads"\r\n\r\n'
    body += str(allowed_downloads).encode()
    body += b"\r\n"
    body += f"--{boundary}--\r\n".encode()

    conn = http.client.HTTPConnection(host, port, timeout=60)
    conn.request("POST", "/api/files/add", body=body, headers={
        "apikey": gokapi_key,
        "Content-Type": f"multipart/form-data; boundary={boundary}",
    })
    resp = conn.getresponse()
    resp_data = resp.read()
    conn.close()

    if resp.status != 200:
        raise RuntimeError(f"gokapi upload failed: HTTP {resp.status} {resp_data[:200]}")

    return json.loads(resp_data.decode("utf-8"))


@app.post("/process")
async def process(req: ProcessRequest):
    """Execute a tool on a file stored in gokapi.

    Flow:
        1. Pull source from gokapi (internal network)
        2. Run Pillow handler
        3. Push result back to gokapi
        4. Return new gokapi_id + metadata (file bytes never leave this machine)
    """
    # Resolve default preset
    params = req.params
    if "preset" not in params:
        default = get_default_preset(req.tool_id)
        if default:
            params["preset"] = default

    try:
        # 1. Pull source file from gokapi
        source_bytes, source_type = _gokapi_pull(
            req.gokapi_url, req.gokapi_key, req.gokapi_id
        )
        logger.info("pulled %d bytes from gokapi/%s", len(source_bytes), req.gokapi_id)

        # 2. Run tool handler
        tool = get_tool(req.tool_id)
        if not tool:
            return JSONResponse({"ok": False, "error": "tool_not_found"}, 404)

        tool_input = {
            "stream": io.BytesIO(source_bytes),
            "filename": req.filename,
            "content_type": req.content_type,
            "size": len(source_bytes),
        }
        tool_output = tool["handler"](tool_input, params)
        output_bytes = tool_output["stream"].read()
        logger.info("processed %s: %d → %d bytes", req.tool_id,
                    len(source_bytes), len(output_bytes))

        # 3. Lifecycle params
        expiry_days = params.get("expiry_days", 1)
        if expiry_days not in (1, 3, 7):
            expiry_days = 1
        allowed_downloads = params.get("allowed_downloads", 50)
        if allowed_downloads not in (1, 10, 50, 100, 200):
            allowed_downloads = 50

        # 4. Push result to gokapi
        gokapi_result = _gokapi_push(
            req.gokapi_url, req.gokapi_key, output_bytes,
            tool_output["filename"], tool_output["content_type"],
            expiry_days, allowed_downloads,
        )
        info = gokapi_result.get("FileInfo", gokapi_result)
        logger.info("pushed result to gokapi/%s", info.get("Id", "?"))

        return {
            "ok": True,
            "result": {
                "gokapi_id": info.get("Id", ""),
                "url_download": info.get("UrlDownload", ""),
                "url_hotlink": info.get("UrlHotlink", ""),
                "expire_at": info.get("ExpireAt"),
                "filename": tool_output["filename"],
                "content_type": tool_output["content_type"],
                "size": len(output_bytes),
                "orig_size": len(source_bytes),
            }
        }

    except ToolError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, 400)
    except Exception as exc:
        logger.exception("worker process failed")
        return JSONResponse({"ok": False, "error": str(exc)}, 500)
