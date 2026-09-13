"""rounded_corners tool — add rounded corners to an image.

Pure Pillow pipeline. Zero I/O dependencies.
Handler receives a stream (bytes in), returns a stream (bytes out).
Never imports gokapi, database, or any storage/record layer.

Contract decisions (developer, 2026-09-13):
    - radius as enum presets relative to min(w,h): small 4%, medium 10%,
      large 20%. Presets keep results predictable and the UI declarative;
      free-form px invites pixel-perfect guessing per image size.
    - background: transparent (default) or white.
      * transparent forces PNG or WebP output (JPEG has no alpha):
        JPG-family input transparently re-encodes to PNG and the filename
        extension changes accordingly — the format change is honest and
        reflected in the returned filename/content_type.
      * white keeps the input's format family (JPEG stays JPEG).
    - output cap: same spirit as other image tools — max 4096px on the long
      edge, alpha preserved.

Guards:
    - tiny images (side < 8px) → radius would exceed half the side; clamp.
    - GIF input: first frame only (same convention as other image tools).
"""

import io
import os

from PIL import Image, ImageDraw

from tools import register, TYPE_PRODUCE, ToolError

ACCEPTED = {".bmp", ".gif", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}

MAX_DIM = 4096

# radius presets, fraction of min(w, h)
_RADIUS_PRESETS = {
    "small": 0.04,
    "medium": 0.10,
    "large": 0.20,
}

_FORMAT = {
    "image/jpeg": ("JPEG", "jpg"),
    "image/png": ("PNG", "png"),
    "image/webp": ("WebP", "webp"),
    "image/gif": ("GIF", "gif"),
    "image/bmp": ("BMP", "bmp"),
    "image/tiff": ("TIFF", "tiff"),
}

_ALPHA_FMTS = {"PNG", "WebP", "TIFF", "GIF"}


def rounded_corners_handler(tool_input: dict, params: dict) -> dict:
    """Apply rounded corners to an image.

    Args:
        tool_input: {"stream": file-like, "filename": str,
                     "content_type": str, "size": int}
        params: {"radius": "small"|"medium"|"large",
                 "background": "transparent"|"white"}
    """
    filename = tool_input.get("filename") or ""
    ext = os.path.splitext(filename.lower())[1]
    if ext not in ACCEPTED:
        raise ToolError("err_format_not_supported")

    radius = params.get("radius", "medium")
    if radius not in _RADIUS_PRESETS:
        raise ToolError("err_invalid_request")

    background = params.get("background", "transparent")
    if background not in ("transparent", "white"):
        raise ToolError("err_invalid_request")

    stream = tool_input["stream"]
    stream.seek(0)
    try:
        img = Image.open(stream)
        img.load()
    except Exception:
        raise ToolError("err_format_not_supported")

    fmt, out_ext = _FORMAT.get(tool_input.get("content_type", ""), (None, None))

    img = img.convert("RGBA")

    # output cap on long edge (same spirit as privacy tool's thumbnail cap)
    w, h = img.size
    if max(w, h) > MAX_DIM:
        img.thumbnail((MAX_DIM, MAX_DIM), Image.LANCZOS)
        w, h = img.size

    # clamp radius to physically valid range (never more than half the side)
    r = int(min(w, h) * _RADIUS_PRESETS[radius])
    r = max(1, min(r, min(w, h) // 2))

    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, w - 1, h - 1], radius=r, fill=255)

    if background == "white":
        out = Image.new("RGBA", (w, h), (255, 255, 255, 255))
        out.paste(img, (0, 0), mask)
        if fmt == "JPEG":
            out = out.convert("RGB")
    else:
        # transparent: carve alpha with the rounded mask
        alpha = img.getchannel("A")
        from PIL import ImageChops
        img.putalpha(ImageChops.multiply(alpha, mask))
        out = img
        # PNG/WebP/TIFF/GIF carry alpha; JPEG/BMP do not -> re-encode to PNG
        if fmt not in _ALPHA_FMTS:
            fmt, out_ext = "PNG", "png"

    buf = io.BytesIO()
    if fmt == "JPEG":
        out.save(buf, "JPEG", quality=90)
    else:
        out.save(buf, fmt)
    raw = buf.getvalue()
    out.close()
    img.close()

    orig_name = tool_input.get("filename", "image")
    stem = orig_name.rsplit(".", 1)[0] if "." in orig_name else orig_name
    out_ct = {("JPEG"): "image/jpeg", ("PNG"): "image/png", ("WebP"): "image/webp",
              ("GIF"): "image/gif", ("BMP"): "image/bmp", ("TIFF"): "image/tiff"}[fmt]

    return {
        "stream": io.BytesIO(raw),
        "filename": f"rounded_{stem}.{out_ext}",
        "content_type": out_ct,
        "size": len(raw),
    }


# ── Registration ───────────────────────────────────────────

register(
    tool_id="rounded_corners",
    tool_type=TYPE_PRODUCE,
    label={
        "en": "Rounded Corners", "zh": "圆角", "ja": "角丸",
        "ko": "둥근 모서리", "es": "Esquinas redondeadas", "ar": "زوايا دائرية",
        "de": "Abgerundete Ecken", "ru": "Скругление углов", "pt": "Cantos arredondados",
        "fr": "Coins arrondis",
    },
    desc={
        "en": "Add rounded corners to an image with transparent or white background.",
        "zh": "给图片加圆角，支持透明或白色背景。",
        "ja": "画像の角を丸くします。透明または白の背景に対応。",
        "ko": "이미지에 둥근 모서리를 추가합니다. 투명 또는 흰색 배경 지원.",
        "es": "Añade esquinas redondeadas con fondo transparente o blanco.",
        "ar": "أضف زوايا دائرية بخلفية شفافة أو بيضاء.",
        "de": "Fügt abgerundete Ecken mit transparentem oder weißem Hintergrund hinzu.",
        "ru": "Скругляет углы изображения с прозрачным или белым фоном.",
        "pt": "Adiciona cantos arredondados com fundo transparente ou branco.",
        "fr": "Arrondit les coins avec fond transparent ou blanc.",
    },
    icon="crop",
    accept=ACCEPTED,
    handler=rounded_corners_handler,
    params=[
        {"key": "radius", "type": "enum", "label": {"en": "Corner size", "zh": "圆角大小"},
         "options": [
             {"value": "small", "label": {"en": "Small", "zh": "小"}},
             {"value": "medium", "label": {"en": "Medium", "zh": "中"}},
             {"value": "large", "label": {"en": "Large", "zh": "大"}},
         ],
         "default": "medium"},
        {"key": "background", "type": "enum", "label": {"en": "Background", "zh": "背景"},
         "options": [
             {"value": "transparent", "label": {"en": "Transparent", "zh": "透明"}},
             {"value": "white", "label": {"en": "White", "zh": "白色"}},
         ],
         "default": "transparent"},
    ],
)
