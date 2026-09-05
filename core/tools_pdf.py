"""PDF merge tool — combine multiple PDF files into one.

Uses pypdf (pure Python, no C deps). Handler receives multiple sources
via the "sources" key (multi=True registration).

Architecture:
    Multi-file input → pypdf PdfMerger → single PDF output.
    Each source is validated as application/pdf before processing.
"""

import io

from pypdf import PdfReader, PdfWriter

from tools import register, TYPE_PRODUCE, ToolError

MAX_PAGES = 500
MAX_SOURCES = 20


def pdf_merge_handler(tool_input, params):
    """Merge multiple PDF files into one.

    Args:
        tool_input: {
            "sources": [
                {"stream": BytesIO, "filename": str, "content_type": str, "size": int},
                ...
            ]
        }
        params: {} (no presets, merge is a single operation)

    Returns:
        {"stream": BytesIO, "filename": str, "content_type": str, "size": int}
    """
    sources = tool_input.get("sources", [])
    if not sources:
        raise ToolError("err_select_file")
    if len(sources) > MAX_SOURCES:
        raise ToolError("err_too_many_files")

    writer = PdfWriter()
    total_pages = 0

    for src in sources:
        ct = src.get("content_type", "")
        if ct != "application/pdf":
            raise ToolError("err_format_not_supported")

        stream = src["stream"]
        stream.seek(0)

        try:
            reader = PdfReader(stream)
        except Exception:
            raise ToolError("err_invalid_pdf")

        for page in reader.pages:
            writer.add_page(page)
            total_pages += 1
            if total_pages > MAX_PAGES:
                raise ToolError("err_pdf_too_large")

        # Close reader to free memory
        reader.stream.close()

    buf = io.BytesIO()
    writer.write(buf)
    raw = buf.getvalue()
    writer.close()

    out_buf = io.BytesIO(raw)
    out_buf.seek(0)

    return {
        "stream": out_buf,
        "filename": "merged.pdf",
        "content_type": "application/pdf",
        "size": len(raw),
    }


# ── Registration ───────────────────────────────────────────

register(
    tool_id="pdf_merge",
    tool_type=TYPE_PRODUCE,
    label={
        "en": "Merge PDF", "zh": "PDF合并", "ja": "PDF結合",
        "ko": "PDF 병합", "es": "Combinar PDF", "ar": "دمج PDF",
        "de": "PDF zusammenführen", "ru": "Объединить PDF", "pt": "Mesclar PDF",
        "fr": "Fusionner PDF",
    },
    desc={
            "fr": 'Combinez plusieurs fichiers PDF en un seul document.',
            "ko": '여러 PDF 파일을 하나로 병합합니다.',
            "ru": 'Объедините несколько PDF-файлов в один документ.',
            "ar": 'دمج ملفات PDF متعددة في مستند واحد.',
            "de": 'Mehrere PDF-Dateien zu einem Dokument zusammenführen.',
            "pt": 'Combine vários arquivos PDF em um único documento.',
            "ja": '複数のPDFファイルを1つに結合します。',
            "zh": '将多个PDF文件合并为一个文档。',
            "es": 'Combina varios archivos PDF en un solo documento.',
            "en": 'Combine multiple PDF files into one document.',
        },
    icon="merge",
    accept={"application/pdf"},
    handler=pdf_merge_handler,
    multi=True,
)
