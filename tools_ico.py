"""ICO Favicon Generator tool.

Converts any image (PNG/JPEG/GIF/BMP/WebP/TIFF) to a multi-resolution ICO file.
Pure Pillow, zero I/O dependencies.
"""

import io
from PIL import Image
from tools import register, TYPE_PRODUCE, ToolError

ACCEPT_MIMES = {
    "image/png", "image/jpeg", "image/gif",
    "image/bmp", "image/webp", "image/tiff",
    "image/x-icon", "image/vnd.microsoft.icon",
}

# ICO output sizes
ICO_SIZES = [(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


# ICO output size presets
ICO_PRESETS = {
    "ico_standard": [(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    "ico_web":      [(16, 16), (32, 32), (48, 48)],
    "ico_app":      [(64, 64), (128, 128), (256, 256)],
    "ico_single":   [(256, 256)],
}


def ico_handler(tool_input, params):
    """Convert image to multi-resolution ICO favicon.

    Flow:
        1. Validate content_type
        2. Open image, convert to RGBA
        3. Center-crop to square (favicon must be square)
        4. Save as ICO with selected size preset
    """
    ct = tool_input.get("content_type", "")
    if ct not in ACCEPT_MIMES:
        raise ToolError("unsupported_format")

    stream = tool_input["stream"]
    stream.seek(0)

    try:
        img = Image.open(stream)
        img.load()
    except Exception:
        raise ToolError("invalid_image")

    # Convert to RGBA for proper alpha channel in ICO
    if img.mode != "RGBA":
        img = img.convert("RGBA")

    # Center-crop to square
    w, h = img.size
    if w != h:
        side = min(w, h)
        left = (w - side) // 2
        top = (h - side) // 2
        img = img.crop((left, top, left + side, top + side))

    # Resolve preset
    preset = params.get("preset", "ico_standard")
    sizes = ICO_PRESETS.get(preset, ICO_PRESETS["ico_standard"])

    # Generate ICO
    out = io.BytesIO()
    img.save(out, format="ICO", sizes=sizes)
    out.seek(0)

    # Derive output filename
    original = tool_input.get("filename", "favicon")
    base = original.rsplit(".", 1)[0] if "." in original else original
    out_name = f"{base}.ico"

    return {
        "stream": out,
        "filename": out_name,
        "content_type": "image/x-icon",
        "size": len(out.getvalue()),
    }


register(
    tool_id="ico",
    tool_type=TYPE_PRODUCE,
    label={
        "en": "ICO Favicon",
        "zh": "图标生成",
        "ja": "ICO作成",
        "ko": "ICO 만들기",
        "es": "ICO Favicon",
        "ar": "أيقونة ICO",
        "de": "ICO Favicon",
        "ru": "ICO Иконка",
        "pt": "ICO Favicon",
        "fr": "ICO Favicon",
    },
    icon="convert",
    match=lambda upload: upload.get("content_type", "") in ACCEPT_MIMES,
    accept=ACCEPT_MIMES,
    presets=[
        {"id": "ico_standard", "label": {"en": "All Sizes (16-256px)", "zh": "全尺寸 (16-256px)", "ja": "全サイズ (16-256px)", "ko": "전체 크기 (16-256px)", "es": "Todos (16-256px)", "ar": "كل المقاسات (16-256px)", "de": "Alle (16-256px)"}, "default": True},
        {"id": "ico_web", "label": {"en": "Web (16-48px)", "zh": "网页 (16-48px)", "ja": "Web (16-48px)", "ko": "웹 (16-48px)", "es": "Web (16-48px)", "ar": "ويب (16-48px)", "de": "Web (16-48px)"}, "default": False},
        {"id": "ico_app", "label": {"en": "App (64-256px)", "zh": "应用 (64-256px)", "ja": "アプリ (64-256px)", "ko": "앱 (64-256px)", "es": "App (64-256px)", "ar": "تطبيق (64-256px)", "de": "App (64-256px)"}, "default": False},
        {"id": "ico_single", "label": {"en": "Single 256px", "zh": "单尺寸 256px", "ja": "単一 256px", "ko": "단일 256px", "es": "Single 256px", "ar": "مفرد 256px", "de": "Einzel 256px"}, "default": False},
    ],
    handler=ico_handler,
)
