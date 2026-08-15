"""img2pdf tool — combine multiple images into one PDF.

Uses Pillow's native PDF encoder (save_all + append_images). Zero new
dependencies: Pillow 10.2 ships the PDF plugin.

Architecture:
    Multi-file image input → Pillow PDF encoder → single PDF output.
    accept mirrors compress (6 decoded image MIMEs — SVG/AVIF/HEIC are NOT
    Pillow-decodable today and stay out; see tool-paradigms.md image formats).

Page sizing: each page sized to the image at 150 DPI (resolution=150),
portrait/landscape follows the image aspect. EXIF orientation applied
(exif_transpose) so phone photos land correctly.
"""

import io

from PIL import Image, ImageOps

from tools import register, TYPE_PRODUCE, ToolError

MAX_SOURCES = 20
DPI = 150
ACCEPTED = {"image/jpeg", "image/png", "image/webp", "image/gif", "image/bmp", "image/tiff"}


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
        {"stream": BytesIO, "filename": str, "content_type": str, "size": int}
    """
    sources = tool_input.get("sources", [])
    if not sources:
        raise ToolError("err_select_file")
    if len(sources) > MAX_SOURCES:
        raise ToolError("err_too_many_files")

    pages = []
    try:
        for src in sources:
            ct = src.get("content_type", "")
            if ct not in ACCEPTED:
                raise ToolError("err_format_not_supported")

            stream = src["stream"]
            stream.seek(0)
            try:
                img = Image.open(stream)
                img.load()
            except Exception:
                raise ToolError("err_format_not_supported")

            # EXIF orientation → upright page (compress-tool convention)
            img = ImageOps.exif_transpose(img)

            # PDF encoder handles RGB/L/P natively; anything else (CMYK,
            # 16-bit, LA…) normalizes to RGB to stay in known-good territory.
            if img.mode not in ("RGB", "L", "P"):
                img = img.convert("RGB")
            if img.mode == "P":
                img = img.convert("RGB") if "transparency" in img.info else img

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
        "stream": out_buf,
        "filename": "images.pdf",
        "content_type": "application/pdf",
        "size": len(raw),
    }


# ── Registration ───────────────────────────────────────────

register(
    tool_id="img2pdf",
    tool_type=TYPE_PRODUCE,
    label={
        "en": "Images to PDF", "zh": "图片转PDF", "ja": "画像をPDFへ",
        "ko": "이미지를 PDF로", "es": "Imágenes a PDF", "ar": "صور إلى PDF",
        "de": "Bilder zu PDF", "ru": "Изображения в PDF", "pt": "Imagens para PDF",
        "fr": "Images en PDF",
    },
    icon="convert",
    match=lambda upload: (upload.get("content_type") or "").startswith("image/"),
    accept=ACCEPTED,
    handler=img2pdf_handler,
    multi=True,
)
