"""ZIP pack tool — combine multiple files into one ZIP archive.

Uses zipfile (pure stdlib, no external deps). Handler receives multiple
sources (files window 1..20, one per input file).

Architecture:
    Multi-file input → zipfile ZipFile(DEFLATED) → single ZIP output.
    accept=None: any whitelisted format can be packed (CP4 whitelist
    already gates uploads, no per-source validation needed here).
"""

import io
import os
import zipfile

from tools import out_filename, register, ToolError

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
        {"items": [{"kind": "file", "stream": BytesIO, "filename": str,
                    "content_type": str, "size": int}]}
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
        "items": [{
            "kind": "file",
            "stream": out_buf,
            "filename": out_filename(None, "zip", default_stem="files"),
            "content_type": "application/zip",
            "size": len(raw),
        }],
    }


# ── Registration ───────────────────────────────────────────

register(
    tool_id="zip_pack",
    label={
        "en": "Zip Files", "zh": "ZIP打包", "ja": "ZIP圧縮",
        "ko": "ZIP 압축", "es": "Comprimir ZIP", "ar": "إنشاء ZIP",
        "de": "ZIP erstellen", "ru": "Создать ZIP", "pt": "Compactar ZIP",
        "fr": "Créer ZIP",
    },
    desc={
            "fr": 'Regroupez plusieurs fichiers dans une archive ZIP.',
            "ko": '여러 파일을 하나의 ZIP 아카이브로 묶습니다.',
            "ru": 'Объедините несколько файлов в один ZIP-архив.',
            "ar": 'اجمع عدة ملفات في أرشيف ZIP واحد.',
            "de": 'Mehrere Dateien zu einem ZIP-Archiv zusammenfassen.',
            "pt": 'Agrupe vários arquivos em um arquivo ZIP.',
            "ja": '複数のファイルを1つのZIPアーカイブにまとめます。',
            "zh": '将多个文件打包为一个ZIP压缩包。',
            "es": 'Agrupa varios archivos en un archivo ZIP.',
            "en": 'Pack multiple files into one ZIP archive.',
        },
    icon="archive",
    accept=None,
    handler=zip_pack_handler,
    files_min=1, files_max=20,
)
