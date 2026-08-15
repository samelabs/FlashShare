"""ZIP pack tool — combine multiple files into one ZIP archive.

Uses zipfile (pure stdlib, no external deps). Handler receives multiple
sources via the "sources" key (multi=True registration).

Architecture:
    Multi-file input → zipfile ZipFile(DEFLATED) → single ZIP output.
    accept=None: any whitelisted format can be packed (CP4 whitelist
    already gates uploads, no per-source validation needed here).
"""

import io
import os
import zipfile

from tools import register, TYPE_PRODUCE, ToolError

MAX_SOURCES = 20


def _unique_name(name: str, used: set) -> str:
    """Return a name not yet in `used`, registering it.

    ZIP entries with identical names are stored duplicated by zipfile;
    uniquify (photo.jpg → photo_1.jpg) so every source stays reachable.
    """
    if name not in used:
        used.add(name)
        return name
    base, ext = os.path.splitext(name)
    i = 1
    while f"{base}_{i}{ext}" in used:
        i += 1
    final = f"{base}_{i}{ext}"
    used.add(final)
    return final


def zip_pack_handler(tool_input, params):
    """Pack multiple files into one ZIP archive.

    Args:
        tool_input: {
            "sources": [
                {"stream": BytesIO, "filename": str, "content_type": str, "size": int},
                ...
            ]
        }
        params: {} (no presets, packing is a single operation)

    Returns:
        {"stream": BytesIO, "filename": str, "content_type": str, "size": int}
    """
    sources = tool_input.get("sources", [])
    if not sources:
        raise ToolError("err_select_file")
    if len(sources) > MAX_SOURCES:
        raise ToolError("err_too_many_files")

    buf = io.BytesIO()
    used = set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for src in sources:
            stream = src["stream"]
            stream.seek(0)
            # basename: strip any path components; empty → generic fallback
            name = os.path.basename(src.get("filename") or "") or "file"
            name = _unique_name(name, used)
            zf.writestr(name, stream.read())

    raw = buf.getvalue()
    out_buf = io.BytesIO(raw)
    out_buf.seek(0)

    return {
        "stream": out_buf,
        "filename": "files.zip",
        "content_type": "application/zip",
        "size": len(raw),
    }


# ── Registration ───────────────────────────────────────────

register(
    tool_id="zip_pack",
    tool_type=TYPE_PRODUCE,
    label={
        "en": "Zip Files", "zh": "ZIP打包", "ja": "ZIP圧縮",
        "ko": "ZIP 압축", "es": "Comprimir ZIP", "ar": "إنشاء ZIP",
        "de": "ZIP erstellen", "ru": "Создать ZIP", "pt": "Compactar ZIP",
        "fr": "Créer ZIP",
    },
    icon="archive",
    match=lambda upload: True,
    accept=None,
    handler=zip_pack_handler,
    multi=True,
)
