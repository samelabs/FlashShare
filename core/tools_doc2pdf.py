"""doc2pdf tool — convert office/text documents to PDF via LibreOffice headless.

Engine: soffice (system binary, peer-level dependency like gokapi/nginx).
Handler writes source to a temp dir, runs `soffice --headless --convert-to pdf`,
reads the produced PDF back. Pure file-in/file-out — no gokapi/db/HTTP access.

Concurrency: each conversion gets an isolated -env:UserInstallation profile
(same pattern the whole fleet uses) so parallel workers never share lock state.
"""

import io
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

from tools import register, ToolError

# Extensions LibreOffice converts reliably with writer/calc/impress filters.
ACCEPTED_EXT = {
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".odt": "application/vnd.oasis.opendocument.text",
    ".ods": "application/vnd.oasis.opendocument.spreadsheet",
    ".odp": "application/vnd.oasis.opendocument.presentation",
    ".rtf": "application/rtf",
    ".txt": "text/plain",
    ".csv": "text/csv",
    ".html": "text/html",
    ".htm": "text/html",
}

# Canonical extension tokens accepted by the registry (sorted for stable output)
ACCEPTED_EXTS = sorted(ACCEPTED_EXT.keys())

SOFFICE = os.environ.get("FLASHSHARE_SOFFICE", "soffice")
CONVERT_TIMEOUT = 60          # seconds per conversion
MAX_SOURCE_BYTES = 50 * 1024 * 1024  # matches site-wide upload cap
PDF_MAGIC = b"%PDF"

_profile_counter = [0]

# Deployment capability probe input: one plain line of text.
_PROBE_TEXT = "flashshare doc2pdf probe\n"
_PROBE_TIMEOUT = 60  # seconds; generous enough for a cold profile start

# Exact deployment packages the probe failure message names (Debian/Ubuntu
# naming; -nogui variants cover writer/calc/impress without an X stack).
REQUIRED_PACKAGES = ("libreoffice-writer-nogui", "libreoffice-calc-nogui",
                     "libreoffice-impress-nogui", "fonts-noto-cjk")


def probe_engine() -> None:
    """One-shot startup probe: convert a one-line txt end-to-end.

    Raises RuntimeError (detail in str) when the engine cannot produce a
    PDF — callers log the failure (naming the required packages) but do
    NOT block startup: doc2pdf returns its typed per-call error and every
    other surface keeps serving.
    """
    import tempfile
    from pathlib import Path as _Path
    # Stable (not per-run) profile: startup probes reuse the initialized
    # profile, so restarts probe in seconds instead of cold-starting
    # soffice every time. No concurrency here — one probe per process.
    profile_dir = _Path(tempfile.gettempdir()) / "flashshare_lo_probe_profile"
    profile_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="flashshare_doc2pdf_probe_") as workdir:
        src = _Path(workdir) / "probe.txt"
        src.write_text(_PROBE_TEXT, encoding="utf-8")
        try:
            proc = subprocess.run(
                [SOFFICE, "--headless", "--norestore", "--nolockcheck",
                 f"-env:UserInstallation={profile_dir.as_uri()}",
                 "--convert-to", "pdf", "--outdir", workdir, str(src)],
                capture_output=True, timeout=_PROBE_TIMEOUT)
        except FileNotFoundError:
            raise RuntimeError(
                f"'{SOFFICE}' executable not found") from None
        except subprocess.TimeoutExpired:
            raise RuntimeError("soffice timed out") from None
        out = _Path(workdir) / "probe.pdf"
        if proc.returncode != 0 or not out.exists()                 or not out.read_bytes().startswith(PDF_MAGIC):
            detail = (proc.stderr or proc.stdout or b"").decode(
                "utf-8", "replace").strip()[:300]
            raise RuntimeError(
                f"txt→pdf conversion failed (exit={proc.returncode})"
                + (f": {detail}" if detail else ""))


def doc2pdf_handler(tool_input, params):
    """Convert a document to PDF.

    Args:
        tool_input: {"stream": BytesIO, "filename": str, "content_type": str, "size": int}
        params: {"preset": "auto"} — single operation, no variants yet.

    Returns:
        {"items": [{"kind": "file", "stream": BytesIO, "filename": str,
                    "content_type": "application/pdf", "size": int}]}
    """
    stream = tool_input.get("stream")
    filename = tool_input.get("filename") or "document"
    if stream is None:
        raise ToolError("err_select_file")

    ext = os.path.splitext(filename.lower())[1]
    if ext not in ACCEPTED_EXT:
        raise ToolError("err_format_not_supported")

    size = tool_input.get("size") or 0
    if size > MAX_SOURCE_BYTES:
        raise ToolError("err_file_too_large")

    stream.seek(0)
    source_bytes = stream.read()
    if not source_bytes:
        raise ToolError("err_empty_file")

    # Isolated profile per call — concurrent soffice instances must not share one.
    _profile_counter[0] += 1
    profile = f"-env:UserInstallation=file:///tmp/flashshare_lo_profile_{os.getpid()}_{_profile_counter[0]}_{int(time.time())}"

    with tempfile.TemporaryDirectory(prefix="flashshare_doc2pdf_") as workdir:
        src_path = Path(workdir) / f"source{ext}"
        src_path.write_bytes(source_bytes)
        del source_bytes

        try:
            proc = subprocess.run(
                [SOFFICE, "--headless", "--norestore", "--nolockcheck",
                 profile, "--convert-to", "pdf", "--outdir", workdir, str(src_path)],
                capture_output=True,
                timeout=CONVERT_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            raise ToolError("err_convert_timeout")
        except FileNotFoundError:
            raise ToolError("err_engine_unavailable")
        except Exception:
            raise ToolError("err_convert_failed")

        out_path = Path(workdir) / "source.pdf"
        if proc.returncode != 0 or not out_path.exists():
            raise ToolError("err_convert_failed")

        pdf_bytes = out_path.read_bytes()

    if not pdf_bytes.startswith(PDF_MAGIC):
        raise ToolError("err_convert_failed")

    # Output name mirrors the source stem: report.docx → report.pdf
    stem = os.path.splitext(os.path.basename(filename))[0]
    stem = re.sub(r'[\\/:*?"<>|]', "_", stem).strip() or "document"
    out_name = f"{stem}.pdf"

    out_buf = io.BytesIO(pdf_bytes)
    return {
        "items": [{
            "kind": "file",
            "stream": out_buf,
            "filename": out_name,
            "content_type": "application/pdf",
            "size": len(pdf_bytes),
        }],
    }


# ── Registration ───────────────────────────────────────────

register(
    tool_id="doc2pdf",
    label={
        "en": "Document to PDF", "zh": "文档转PDF", "ja": "ドキュメント→PDF変換",
        "ko": "문서를 PDF로", "es": "Documento a PDF", "ar": "مستند إلى PDF",
        "de": "Dokument zu PDF", "ru": "Документ в PDF", "pt": "Documento para PDF",
        "fr": "Document en PDF",
    },
    desc={
            "fr": 'Convertissez Word/Excel/PPT et autres documents en un PDF propre, prêt à imprimer ou partager.',
            "ko": 'Word/Excel/PPT 등 문서를 정확한 PDF로 변환하여 바로 인쇄하거나 공유할 수 있습니다.',
            "ru": 'Конвертируйте Word/Excel/PPT и другие документы в аккуратный PDF — сразу печатайте или делитесь.',
            "ar": 'حوّل مستندات Word/Excel/PPT وغيرها إلى PDF سليم جاهز للطباعة أو المشاركة.',
            "de": 'Wandle Word/Excel/PPT und andere Dokumente in ein sauberes PDF um – fertig zum Drucken oder Teilen.',
            "pt": 'Converta Word/Excel/PPT e outros documentos em um PDF limpo, pronto para imprimir ou compartilhar.',
            "ja": 'Word/Excel/PPT などのドキュメントを正確な PDF に変換。そのまま印刷・共有できます。',
            "zh": '上传Word/Excel/PPT等文档，在线转换为格式正确的PDF，可直接打印或分享。',
            "es": 'Convierte Word/Excel/PPT y otros documentos a un PDF limpio, listo para imprimir o compartir.',
            "en": 'Convert Word/Excel/PPT and other documents to a clean PDF, ready to print or share.',
        },
    icon="print",
    accept=set(ACCEPTED_EXTS),
    handler=doc2pdf_handler,
)
