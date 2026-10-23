"""img2pdf tool — combine multiple images into one PDF.

Uses Pillow's native PDF encoder (save_all + append_images). Zero new
dependencies: Pillow 10.2 ships the PDF plugin.

Architecture:
    Multi-file image input → Pillow PDF encoder → single PDF output.
    accept mirrors compress (canonical image extensions — SVG/AVIF/HEIC are NOT
    Pillow-decodable today and stay out; see tool-paradigms.md image formats).

Page sizing: each page sized to the image at 150 DPI (resolution=150),
portrait/landscape follows the image aspect. EXIF orientation applied
(exif_transpose) so phone photos land correctly.
"""

import io
import os

from PIL import Image, ImageOps

from tools import open_image, out_filename, register, ToolError

MAX_SOURCES = 20
DPI = 150
ACCEPTED = {".bmp", ".gif", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}


def img2pdf_handler(tool_input, params):
    """Combine multiple images into a single PDF.

    Args:
        tool_input: {
            "sources": [
                {"stream": BytesIO, "filename": str, "content_type": str, "size": int},
                ...
            ]
        }
        params: {} (no presets — conversion is a single operation)

    Returns:
        {"items": [{"kind": "file", "stream": BytesIO, "filename": str,
                    "content_type": str, "size": int}]}
    """
    sources = tool_input.get("sources", [])
    if not sources:
        raise ToolError("err_select_file")
    if len(sources) > MAX_SOURCES:
        raise ToolError("err_too_many_files")

    pages = []
    try:
        for src in sources:
            filename = src.get("filename") or ""
            ext = os.path.splitext(filename.lower())[1]
            ct = (src.get("content_type") or "").lower()
            # accept is exact extension contract; MIME stays an independent
            # defense-in-depth check rather than being compared to ext tokens.
            if ext not in ACCEPTED or not ct.startswith("image/"):
                raise ToolError("err_format_not_supported")

            img = open_image(src["stream"])
            # EXIF orientation → upright page (compress-tool convention)
            img = ImageOps.exif_transpose(img)

            # Flatten alpha onto white (transparent logos must not land on
            # black when the PDF encoder drops the alpha channel).
            if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
                rgba = img.convert("RGBA")
                base = Image.new("RGB", rgba.size, (255, 255, 255))
                base.paste(rgba, mask=rgba.getchannel("A"))
                img.close()
                img = base
            # PDF encoder handles RGB/L/P natively; anything else (CMYK,
            # 16-bit…) normalizes to RGB to stay in known-good territory.
            if img.mode not in ("RGB", "L", "P"):
                img = img.convert("RGB")

            pages.append(img)

        buf = io.BytesIO()
        pages[0].save(
            buf,
            format="PDF",
            save_all=True,
            append_images=pages[1:],
            resolution=DPI,
        )
    finally:
        for p in pages:
            try:
                p.close()
            except Exception:
                pass

    raw = buf.getvalue()
    out_buf = io.BytesIO(raw)
    out_buf.seek(0)

    return {
        "items": [{
            "kind": "file",
            "stream": out_buf,
            "filename": out_filename(None, "pdf", default_stem="images"),
            "content_type": "application/pdf",
            "size": len(raw),
        }],
    }


# ── Registration ───────────────────────────────────────────

register(
    tool_id="img2pdf",
    label={
        "en": "Images to PDF", "zh": "图片转PDF", "ja": "画像をPDFへ",
        "ko": "이미지를 PDF로", "es": "Imágenes a PDF", "ar": "صور إلى PDF",
        "de": "Bilder zu PDF", "ru": "Изображения в PDF", "pt": "Imagens para PDF",
        "fr": "Images en PDF",
    },
    desc={
            "fr": 'Combinez plusieurs images en un document PDF.',
            "ko": '여러 이미지를 하나의 PDF 문서로 합칩니다.',
            "ru": 'Объедините несколько изображений в один PDF-документ.',
            "ar": 'اجمع عدة صور في مستند PDF واحد.',
            "de": 'Mehrere Bilder in einem PDF-Dokument zusammenführen.',
            "pt": 'Combine várias imagens em um documento PDF.',
            "ja": '複数の画像を1つのPDFドキュメントに結合します。',
            "zh": '将多张图片合并为一个PDF文档。',
            "es": 'Combina varias imágenes en un documento PDF.',
            "en": 'Combine multiple images into one PDF document.',
        },
    accept=ACCEPTED,
    tag="image",
    handler=img2pdf_handler,
    files_min=1, files_max=20,
)
