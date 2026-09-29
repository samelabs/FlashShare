"""ICO Favicon Generator tool.

Converts any image (PNG/JPEG/GIF/BMP/WebP/TIFF) to a multi-resolution ICO file.
Pure Pillow, zero I/O dependencies.
"""

import io
import os
from PIL import Image
from tools import register, ToolError

ACCEPTED_EXTS = {".bmp", ".gif", ".ico", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}

# ICO output sizes
ICO_SIZES = [(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


# ICO output size presets ("multi" = every size in one file)
ICO_PRESETS = {
    "multi":   ICO_SIZES,
    "ico_16":  [(16, 16)],
    "ico_32":  [(32, 32)],
    "ico_48":  [(48, 48)],
    "ico_64":  [(64, 64)],
    "ico_128": [(128, 128)],
    "ico_256": [(256, 256)],
}


def ico_handler(tool_input, params):
    """Convert image to multi-resolution ICO favicon.

    Flow:
        1. Validate canonical filename extension
        2. Open image, convert to RGBA
        3. Center-crop to square (favicon must be square)
        4. Save as ICO with the selected size preset ("multi" = all six)

    Returns:
        {"items": [{"kind": "file", "stream": BytesIO, "filename": str,
                    "content_type": "image/x-icon", "size": int}]}
    """
    filename = tool_input.get("filename") or ""
    ext = os.path.splitext(filename.lower())[1]
    if ext not in ACCEPTED_EXTS:
        raise ToolError("err_format_not_supported")

    stream = tool_input["stream"]
    stream.seek(0)

    try:
        img = Image.open(stream)
        img.load()
    except Exception:
        raise ToolError("err_format_not_supported")

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

    # Resolve preset (canonical params only) — enforce_params_decl is the
    # sole params authority: missing → declaration default injected,
    # illegal enum → 422 before the handler runs. Direct canonical read;
    # fail loud on any bypass.
    preset = params["preset"]
    sizes = ICO_PRESETS[preset]
    needed = max(max(w, h) for w, h in sizes)

    # Generate ICO — let Pillow resize from source in one step
    # (pre-resizing then saving adds unwanted extra sizes + double-scaling blur)
    # 小图守卫: Pillow 对源图小于请求尺寸的 size 会静默丢弃 → 可能产出空 ICO;
    # 先放大到目标再保存。
    if min(img.size) < needed:
        img = img.resize((max(needed, img.size[0]), max(needed, img.size[1])),
                         Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, format="ICO", sizes=sizes)
    out.seek(0)

    # Derive output filename
    original = tool_input.get("filename", "favicon")
    base = original.rsplit(".", 1)[0] if "." in original else original
    out_name = f"{base}.ico"

    return {
        "items": [{
            "kind": "file",
            "stream": out,
            "filename": out_name,
            "content_type": "image/x-icon",
            "size": len(out.getvalue()),
        }],
    }


register(
    tool_id="ico",
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
    desc={
            "fr": 'Générer des icônes ICO de différentes tailles.',
            "ko": '이미지에서 다양한 크기의 ICO 아이콘 생성.',
            "ru": 'Создание ICO-иконок разных размеров из изображения.',
            "ar": 'إنشاء أيقونات ICO بأحجام مختلفة من صورة.',
            "de": 'ICO-Symbole in verschiedenen Größen aus einem Bild erstellen.',
            "pt": 'Gerar ícones ICO em vários tamanhos a partir de uma imagem.',
            "ja": '画像から各サイズのICOアイコンを生成。',
            "zh": '图片生成各尺寸 ICO 图标。',
            "es": 'Generar iconos ICO de varios tamaños desde una imagen.',
            "en": 'Generate ICO icons in various sizes from an image.',
        },
    icon="favicon",
    accept=ACCEPTED_EXTS,
    params=[
        {"key": "preset", "type": "enum", "label": {"en": "Icon size", "zh": "图标尺寸"},
         "options": [
             {"value": "multi", "label": {"en": "All sizes 16-256", "zh": "多尺寸 16-256", "ja": "全サイズ 16-256", "ko": "모든 크기 16-256", "es": "Todos los tamaños 16-256", "ar": "جميع الأحجام 16-256", "de": "Alle Größen 16-256", "ru": "Все размеры 16-256", "pt": "Todos os tamanhos 16-256", "fr": "Toutes tailles 16-256"}},
             {"value": "ico_16", "label": {"en": "16x16", "zh": "16x16", "ja": "16x16", "ko": "16x16", "es": "16x16", "ar": "16x16", "de": "16x16", "ru": "16x16", "pt": "16x16", "fr": "16x16"}},
             {"value": "ico_32", "label": {"en": "32x32", "zh": "32x32", "ja": "32x32", "ko": "32x32", "es": "32x32", "ar": "32x32", "de": "32x32", "ru": "32x32", "pt": "32x32", "fr": "32x32"}},
             {"value": "ico_48", "label": {"en": "48x48", "zh": "48x48", "ja": "48x48", "ko": "48x48", "es": "48x48", "ar": "48x48", "de": "48x48", "ru": "48x48", "pt": "48x48", "fr": "48x48"}},
             {"value": "ico_64", "label": {"en": "64x64", "zh": "64x64", "ja": "64x64", "ko": "64x64", "es": "64x64", "ar": "64x64", "de": "64x64", "ru": "64x64", "pt": "64x64", "fr": "64x64"}},
             {"value": "ico_128", "label": {"en": "128x128", "zh": "128x128", "ja": "128x128", "ko": "128x128", "es": "128x128", "ar": "128x128", "de": "128x128", "ru": "128x128", "pt": "128x128", "fr": "128x128"}},
             {"value": "ico_256", "label": {"en": "256x256", "zh": "256x256", "ja": "256x256", "ko": "256x256", "es": "256x256", "ar": "256x256", "de": "256x256", "ru": "256x256", "pt": "256x256", "fr": "256x256"}},
         ],
         "default": "multi"},
    ],
    handler=ico_handler,
)
