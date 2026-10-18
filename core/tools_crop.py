"""crop_white tool — remove white margins around content.

Pure Pillow pipeline. Zero I/O dependencies.

Use case: scanned documents / screenshots with white borders around the
actual content. Detects the content bounding box via grayscale
thresholding (near-white → background), crops to it.

Guards:
    - Full-white image → returns original unchanged (getbbox() is None).
    - Content fills the canvas → bbox ≈ canvas → unchanged.
    - Tolerance 245 on L channel absorbs JPEG noise around pure white.
    - A minimum crop gain (1% of area) prevents pointless re-encodes.
"""

import io
import os

from PIL import Image, ImageOps

from tools import out_filename, register, ToolError

ACCEPTED = {".bmp", ".gif", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}

# Below this gray level a pixel counts as content (255 = pure white).
WHITE_THRESHOLD = 245
# Don't re-encode if the crop would shave less than this fraction of area.
MIN_GAIN = 0.01

_FORMAT = {
    "image/jpeg": ("JPEG", "jpg"),
    "image/png": ("PNG", "png"),
    "image/webp": ("WebP", "webp"),
    "image/gif": ("GIF", "gif"),
    "image/bmp": ("BMP", "bmp"),
    "image/tiff": ("TIFF", "tiff"),
}


def _bbox_of_content(img):
    """Bounding box of non-near-white pixels, or None if none found."""
    gray = img.convert("L")
    mask = gray.point(lambda p: 255 if p < WHITE_THRESHOLD else 0)
    bbox = mask.getbbox()
    gray.close()
    mask.close()
    return bbox


def crop_white_handler(tool_input, params):
    """Crop white margins from an image.

    Args:
        tool_input: {"stream", "filename", "content_type", "size"}
        params: {} (single operation, no presets)

    Returns:
        {"items": [{"kind": "file", "stream", "filename",
                    "content_type", "size"}]}
    """
    filename = tool_input.get("filename") or ""
    ext = os.path.splitext(filename.lower())[1]
    ct = (tool_input.get("content_type") or "").lower()
    # Tool Contract accept is canonical extension vocabulary. Keep the handler
    # in the same domain, then independently require a MIME we can re-encode.
    if ext not in ACCEPTED or ct not in _FORMAT:
        raise ToolError("err_format_not_supported")

    stream = tool_input["stream"]
    stream.seek(0)
    try:
        img = Image.open(stream)
        img.load()
    except Exception:
        raise ToolError("err_format_not_supported")

    img = ImageOps.exif_transpose(img)

    # Flatten alpha onto white BEFORE detection so transparent areas do not
    # read as content nor stay transparent in the cropped output.
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        base = Image.new("RGB", rgba.size, (255, 255, 255))
        base.paste(rgba, mask=rgba.getchannel("A"))
        img.close()
        img = base
    elif img.mode != "RGB":
        img = img.convert("RGB")

    w, h = img.size
    bbox = _bbox_of_content(img)

    # Guards: all-white (no content) / content fills canvas / negligible
    # gain → return the original bytes untouched (no re-encode).
    if bbox is None or (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]) >= (w * h) * (1 - MIN_GAIN):
        img.close()
        stream.seek(0)
        return {
            "items": [{
                "kind": "file",
                "stream": stream,
                "filename": tool_input["filename"],
                "content_type": ct,
                "size": tool_input.get("size", 0),
            }],
        }

    out = img.crop(bbox)
    img.close()

    fmt, out_ext = _FORMAT[ct]
    if fmt == "JPEG" and out.mode != "RGB":
        out = out.convert("RGB")
    buf = io.BytesIO()
    out.save(buf, fmt, optimize=False)
    out.close()
    raw = buf.getvalue()

    orig_name = tool_input.get("filename", "image")
    stem = orig_name.rsplit(".", 1)[0] if "." in orig_name else orig_name

    return {
        "items": [{
            "kind": "file",
            "stream": io.BytesIO(raw),
            "filename": out_filename(orig_name, out_ext),
            "content_type": ct,
            "size": len(raw),
        }],
    }


# ── Registration ───────────────────────────────────────────

register(
    tool_id="crop_white",
    label={
        "en": "Crop White", "zh": "裁白", "ja": "白トリミング",
        "ko": "흰색 자르기", "es": "Recortar blanco", "ar": "اقتصاص الأبيض",
        "de": "Weiß abschneiden", "ru": "Обрезать белые поля", "pt": "Recortar branco",
        "fr": "Rogner le blanc",
    },
    desc={
            "fr": 'Supprime les marges blanches autour du contenu.',
            "ko": '콘텐츠 주변의 흰 여백을 제거합니다.',
            "ru": 'Удаляет белые поля вокруг содержимого.',
            "ar": 'يزيل الهوامش البيضاء حول المحتوى.',
            "de": 'Entfernt weiße Ränder um den Inhalt.',
            "pt": 'Remove as margens brancas ao redor do conteúdo.',
            "ja": 'コンテンツ周囲の白い余白を切り取ります。',
            "zh": '裁掉内容周围的白色边缘。',
            "es": 'Elimina los márgenes blancos alrededor del contenido.',
            "en": 'Removes white margins around the content.',
        },
    accept=ACCEPTED,
    handler=crop_white_handler,
)
