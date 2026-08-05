"""Beautify tool — wraps screenshots in styled containers.

Pure Pillow pipeline. Zero I/O dependencies.
Handler receives a stream (bytes in), returns a stream (bytes out).

Architecture:
    Fixed pipeline: rounded corners + drop shadow + background container.
    The ONLY variable is the background generator (bg_func).
    Each preset selects a different bg_func.
"""

import gc
import io
import math
import random

from PIL import Image, ImageDraw, ImageFilter, ImageEnhance, ImageChops, ImageOps

from tools import register, TYPE_PRODUCE, ToolError

MAX_DIM = 1400
RADIUS = 16
PADDING = 60
SHADOW_BLUR = 30
SHADOW_ALPHA = 130
SHADOW_OFFSET = 6


# ── Background generators ──────────────────────────────────
# Each: (size: (w,h)) → RGB Image. Pure computation, no I/O.

def _gradient(size, colors):
    """Multi-stop vertical linear gradient."""
    w, h = size
    g = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(g)
    n = len(colors) - 1
    for y in range(h):
        t = y / max(1, h - 1) * n
        seg = min(int(t), n - 1)
        lt = t - seg
        c1, c2 = colors[seg], colors[seg + 1]
        d.line([(0, y), (w, y)], fill=tuple(
            int(c1[j] + (c2[j] - c1[j]) * lt) for j in range(3)))
    return g


def _mesh(size, colors, seed=42, blobs=6):
    """Multi-color mesh via blurred blobs on 1/4 scale, then upscale."""
    w, h = size
    scale = 4
    sw, sh = w // scale, h // scale
    # Start with mid-tone average of all colors as base (not colors[0])
    base_r = sum(c[0] for c in colors) // len(colors)
    base_g = sum(c[1] for c in colors) // len(colors)
    base_b = sum(c[2] for c in colors) // len(colors)
    canvas = Image.new("RGB", (sw, sh), (base_r, base_g, base_b)).convert("RGBA")
    random.seed(seed)
    for _ in range(blobs):
        cx = random.randint(0, sw)
        cy = random.randint(0, sh)
        r = random.randint(sw // 3, sw // 2)
        c = random.choice(colors)
        blob = Image.new("RGBA", (r * 2, r * 2), (0, 0, 0, 0))
        ImageDraw.Draw(blob).ellipse(
            [0, 0, r * 2 - 1, r * 2 - 1], fill=c + (180,))
        blob = blob.filter(ImageFilter.GaussianBlur(radius=r // 3))
        canvas.alpha_composite(blob, (cx - r, cy - r))
    # Flatten: composite RGBA onto itself's RGB base
    flat = Image.new("RGBA", (sw, sh), (base_r, base_g, base_b, 255))
    flat.alpha_composite(canvas)
    return flat.convert("RGB").resize((w, h), Image.LANCZOS)


def _solid(size, color):
    return Image.new("RGB", size, color)


def _grid_pattern(size):
    """Light grid paper."""
    w, h = size
    bg = Image.new("RGB", size, (248, 250, 252))
    d = ImageDraw.Draw(bg)
    for x in range(0, w, 28):
        d.line([(x, 0), (x, h)], fill=(218, 225, 232), width=1)
    for y in range(0, h, 28):
        d.line([(0, y), (w, y)], fill=(218, 225, 232), width=1)
    return bg


def _wallpaper(size, src_img):
    """Blurred + enhanced source image as wallpaper."""
    blurred = src_img.resize(size, Image.LANCZOS).filter(
        ImageFilter.GaussianBlur(radius=55))
    blurred = ImageEnhance.Color(blurred).enhance(1.3)
    blurred = ImageEnhance.Brightness(blurred).enhance(1.1)
    return blurred


# ── Preset definitions ─────────────────────────────────────

PRESETS = {
    "clean":     {"bg": lambda s, i: _solid(s, (242, 244, 247))},
    "dark":      {"bg": lambda s, i: _solid(s, (22, 22, 28))},
    "sunset":    {"bg": lambda s, i: _gradient(s, [(255, 94, 98), (255, 154, 0), (255, 218, 130)])},
    "ocean":     {"bg": lambda s, i: _gradient(s, [(3, 4, 94), (0, 119, 182), (144, 224, 239)])},
    "aurora":    {"bg": lambda s, i: _mesh(s, [(72, 49, 157), (0, 199, 255), (0, 229, 161), (255, 107, 107)])},
    "noir":      {"bg": lambda s, i: _mesh(s, [(13, 13, 20), (30, 10, 60), (10, 30, 50), (60, 10, 40)])},
    "royal":     {"bg": lambda s, i: _gradient(s, [(43, 12, 82), (135, 28, 135), (188, 28, 163)])},
    "grid":      {"bg": lambda s, i: _grid_pattern(s)},
    "wallpaper": {"bg": lambda s, i: _wallpaper(s, i)},
}


def beautify_handler(tool_input, params):
    """Wrap a screenshot in a styled container.

    Args:
        tool_input: {"stream": file-like, "filename": str, "content_type": str, "size": int}
        params: {"preset": str}

    Returns:
        {"stream": BytesIO, "filename": str, "content_type": str, "size": int}
    """
    # ---- CP6.1: Format validation ----
    ct = tool_input.get("content_type", "")
    if not ct.startswith("image/"):
        raise ToolError("err_format_not_supported")

    # ---- CP6.2: Parameter resolution ----
    preset = params.get("preset", "clean")
    p = PRESETS.get(preset, PRESETS["clean"])

    # ---- CP6.3: Decode + orient + downscale ----
    img = Image.open(tool_input["stream"])
    img = ImageOps.exif_transpose(img)
    img = img.convert("RGB")
    if max(img.size) > MAX_DIM:
        img.thumbnail((MAX_DIM, MAX_DIM), Image.LANCZOS)
    w, h = img.size

    # ---- CP6.4: Generate background ----
    pad = PADDING
    sw, sh = w + pad * 2, h + pad * 2
    bg = p["bg"]((sw, sh), img)

    # ---- CP6.5: Shadow ----
    shadow = Image.new("RGBA", (sw, sh), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle(
        [pad, pad + SHADOW_OFFSET, pad + w - 1, pad + h - 1 + SHADOW_OFFSET],
        radius=RADIUS, fill=(0, 0, 0, SHADOW_ALPHA))
    shadow = shadow.filter(ImageFilter.GaussianBlur(radius=SHADOW_BLUR))

    # ---- CP6.6: Rounded mask ----
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, w - 1, h - 1], radius=RADIUS, fill=255)

    # ---- CP6.7: Composite ----
    canvas = Image.new("RGBA", (sw, sh), (0, 0, 0, 0))
    canvas.paste(shadow, (0, 0), shadow)
    canvas.paste(img, (pad, pad + SHADOW_OFFSET), mask)
    bg_a = bg.convert("RGBA")
    bg_a.alpha_composite(canvas)

    # Flatten to RGB
    out = Image.new("RGB", (sw, sh), (0, 0, 0))
    out.paste(bg_a, mask=bg_a.split()[3])

    # ---- CP6.8: Clean output ----
    buf = io.BytesIO()
    out.save(buf, "PNG")
    raw = buf.getvalue()

    # Free intermediates
    del canvas, shadow, mask, bg, bg_a, out
    gc.collect()

    out_buf = io.BytesIO(raw)
    out_buf.seek(0)

    orig_name = tool_input.get("filename", "image")
    stem = orig_name.rsplit(".", 1)[0] if "." in orig_name else orig_name
    out_name = f"beautify_{stem}.png"

    return {
        "stream": out_buf,
        "filename": out_name,
        "content_type": "image/png",
        "size": len(raw),
    }


# ── Registration ───────────────────────────────────────────

_PRESET_LABELS = {
    "clean":     {"en": "Clean", "zh": "干净", "ja": "クリーン", "ko": "클린", "es": "Limpio", "ar": "نظيف", "de": "Sauber"},
    "dark":      {"en": "Dark", "zh": "深色", "ja": "ダーク", "ko": "다크", "es": "Oscuro", "ar": "داكن", "de": "Dunkel"},
    "sunset":    {"en": "Sunset", "zh": "日落", "ja": "サンセット", "ko": "선셋", "es": "Atardecer", "ar": "غروب", "de": "Sonnenuntergang"},
    "ocean":     {"en": "Ocean", "zh": "海洋", "ja": "オーシャン", "ko": "오션", "es": "Océano", "ar": "محيط", "de": "Ozean"},
    "aurora":    {"en": "Aurora", "zh": "极光", "ja": "オーロラ", "ko": "오로라", "es": "Aurora", "ar": "شفق", "de": "Aurora"},
    "noir":      {"en": "Noir", "zh": "暗夜", "ja": "ノワール", "ko": "누아르", "es": "Noir", "ar": "نوار", "de": "Noir"},
    "royal":     {"en": "Royal", "zh": "紫韵", "ja": "ロイヤル", "ko": "로열", "es": "Real", "ar": "ملكي", "de": "Königlich"},
    "grid":      {"en": "Grid", "zh": "方格", "ja": "グリッド", "ko": "그리드", "es": "Cuadrícula", "ar": "شبكة", "de": "Raster"},
    "wallpaper": {"en": "Wallpaper", "zh": "壁纸", "ja": "壁紙", "ko": "배경화면", "es": "Fondo", "ar": "خلفية", "de": "Hintergrund"},
}

register(
    tool_id="beautify",
    tool_type=TYPE_PRODUCE,
    label={
        "en": "Frame", "zh": "截图美化", "ja": "スクリーンショット美化",
        "ko": "스크린샷 미화", "es": "Embellecer captura", "ar": "تجميل لقطة الشاشة",
        "de": "Screenshot verschönern", "ru": "Украшение скриншота", "pt": "Embelezar captura",
        "fr": "Embellir la capture",
    },
    icon="wand",
    match=lambda upload: upload.get("content_type", "").startswith("image/"),
    accept={"image/jpeg", "image/png", "image/webp", "image/gif", "image/bmp", "image/tiff"},
    handler=beautify_handler,
    presets=[
        {"id": pid, "label": lbl, "default": (pid == "aurora")}
        for pid, lbl in _PRESET_LABELS.items()
    ],
)
