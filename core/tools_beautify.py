"""Beautify tool — wraps screenshots in styled containers.

Pure Pillow pipeline. Zero I/O dependencies.
Handler receives a stream (bytes in), returns a stream (bytes out).

Architecture:
    Fixed pipeline: rounded corners + drop shadow + background container.
    The ONLY variable is the background generator (bg_func).
    Each preset selects a different bg_func.

Background techniques (each preset uses a fundamentally different one):
    _solid          — flat color
    _gradient       — multi-stop vertical gradient
    _mesh           — blurred multi-color blobs (mesh gradient)
    _radial_glow    — bright center fading to dark edges
    _grid_pattern   — graph paper lines
    _dot_pattern    — polka dots
    _wallpaper      — blurred + enhanced source image
"""

import gc
import io
import math
import random

from PIL import Image, ImageDraw, ImageFilter, ImageEnhance, ImageOps

from tools import register, TYPE_PRODUCE, ToolError

MAX_DIM = 1400
RADIUS = 16
PADDING = 60
SHADOW_BLUR = 30
SHADOW_ALPHA = 130
SHADOW_OFFSET = 6


# ── Background generators ──────────────────────────────────
# Each: (size: (w,h), src_img: Image) → RGB Image. Pure computation.

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
    flat = Image.new("RGBA", (sw, sh), (base_r, base_g, base_b, 255))
    flat.alpha_composite(canvas)
    return flat.convert("RGB").resize((w, h), Image.LANCZOS)


def _solid(size, color):
    return Image.new("RGB", size, color)


def _grid_pattern(size):
    """Light blue-gray grid paper."""
    w, h = size
    bg = Image.new("RGB", size, (238, 242, 248))
    d = ImageDraw.Draw(bg)
    for x in range(0, w, 28):
        d.line([(x, 0), (x, h)], fill=(205, 215, 226), width=1)
    for y in range(0, h, 28):
        d.line([(0, y), (w, y)], fill=(205, 215, 226), width=1)
    return bg


def _dot_pattern(size, bg_color, dot_color, spacing=36, dot_radius=3):
    """Polka dot pattern on solid background."""
    w, h = size
    bg = Image.new("RGB", size, bg_color)
    d = ImageDraw.Draw(bg)
    for y in range(spacing // 2, h, spacing):
        for x in range(spacing // 2, w, spacing):
            d.ellipse([x - dot_radius, y - dot_radius,
                       x + dot_radius, y + dot_radius], fill=dot_color)
    return bg


def _radial_glow(size, center_color, edge_color):
    """Radial gradient: bright center fading to dark edges.

    Renders at 1/4 scale for performance, upscaled with LANCZOS.
    """
    w, h = size
    scale = 4
    sw, sh = w // scale, h // scale
    cx, cy = sw // 2, sh // 2
    max_r = int(math.sqrt(cx * cx + cy * cy))
    bg = Image.new("RGB", (sw, sh), edge_color)
    d = ImageDraw.Draw(bg)
    for r in range(max_r, 0, -1):
        t = 1.0 - (r / max_r)
        t = t * t  # ease-out for smoother glow
        color = tuple(
            int(edge_color[j] + (center_color[j] - edge_color[j]) * t)
            for j in range(3))
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=color)
    return bg.resize((w, h), Image.LANCZOS)


def _wallpaper(size, src_img):
    """Blurred + enhanced source image as wallpaper."""
    blurred = src_img.resize(size, Image.LANCZOS).filter(
        ImageFilter.GaussianBlur(radius=55))
    blurred = ImageEnhance.Color(blurred).enhance(1.3)
    blurred = ImageEnhance.Brightness(blurred).enhance(1.1)
    return blurred


# ── Preset definitions ─────────────────────────────────────

PRESETS = {
    "pure":      {"bg": lambda s, i: _solid(s, (252, 252, 254))},
    "carbon":    {"bg": lambda s, i: _solid(s, (17, 17, 22))},
    "dawn":      {"bg": lambda s, i: _gradient(s, [(248, 233, 216), (255, 200, 175), (250, 170, 165)])},
    "ocean":     {"bg": lambda s, i: _gradient(s, [(8, 18, 48), (0, 80, 156), (0, 163, 178)])},
    "aurora":    {"bg": lambda s, i: _mesh(s, [(72, 49, 157), (0, 199, 255), (0, 229, 161), (255, 107, 107)])},
    "spotlight": {"bg": lambda s, i: _radial_glow(s, (120, 200, 255), (12, 12, 18))},
    "sunset":    {"bg": lambda s, i: _gradient(s, [(255, 94, 98), (255, 154, 0), (255, 206, 84)])},
    "grid":      {"bg": lambda s, i: _grid_pattern(s)},
    "dots":      {"bg": lambda s, i: _dot_pattern(s, (255, 250, 245), (229, 216, 200))},
    "blur":      {"bg": lambda s, i: _wallpaper(s, i)},
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
    preset = params.get("preset", "aurora")
    p = PRESETS.get(preset, PRESETS["aurora"])

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
    "pure":      {"en": "Pure",      "zh": "纯白",   "ja": "ピュア",     "ko": "퓨어",      "es": "Puro",      "ar": "نقي",      "de": "Rein",      "ru": "Чистый",    "pt": "Puro",      "fr": "Pur"},
    "carbon":    {"en": "Carbon",    "zh": "暗夜",   "ja": "カーボン",   "ko": "카본",      "es": "Carbón",    "ar": "فحمي",     "de": "Karbon",    "ru": "Карбон",    "pt": "Carbono",   "fr": "Carbone"},
    "dawn":      {"en": "Dawn",      "zh": "晨光",   "ja": "朝光",       "ko": "새벽",      "es": "Alba",      "ar": "فجر",      "de": "Morgendämmerung", "ru": "Рассвет", "pt": "Amanhecer", "fr": "Aube"},
    "ocean":     {"en": "Ocean",     "zh": "深海",   "ja": "オーシャン", "ko": "오션",      "es": "Océano",    "ar": "محيط",     "de": "Ozean",     "ru": "Океан",     "pt": "Oceano",    "fr": "Océan"},
    "aurora":    {"en": "Aurora",    "zh": "极光",   "ja": "オーロラ",   "ko": "오로라",    "es": "Aurora",    "ar": "شفق",     "de": "Aurora",    "ru": "Аврора",    "pt": "Aurora",    "fr": "Aurore"},
    "spotlight": {"en": "Spotlight", "zh": "聚光",   "ja": "スポット",   "ko": "스포트라이트", "es": "Foco",   "ar": "كشاف",    "de": "Spotlight", "ru": "Прожектор",  "pt": "Holofote",  "fr": "Projecteur"},
    "sunset":    {"en": "Sunset",    "zh": "日落",   "ja": "サンセット", "ko": "선셋",      "es": "Atardecer", "ar": "غروب",     "de": "Sonnenuntergang", "ru": "Закат", "pt": "Pôr do sol", "fr": "Coucher"},
    "grid":      {"en": "Grid",      "zh": "方格",   "ja": "グリッド",   "ko": "그리드",    "es": "Cuadrícula","ar": "شبكة",    "de": "Raster",    "ru": "Сетка",     "pt": "Grade",     "fr": "Grille"},
    "dots":      {"en": "Dots",      "zh": "波点",   "ja": "ドット",     "ko": "도트",      "es": "Puntos",    "ar": "نقاط",    "de": "Punkte",    "ru": "Точки",     "pt": "Pontos",    "fr": "Points"},
    "blur":      {"en": "Backdrop",   "zh": "衬底",   "ja": "背景ぼかし", "ko": "배경",      "es": "Fondo",      "ar": "خلفية",    "de": "Hintergrund", "ru": "Фон",     "pt": "Fundo",     "fr": "Fond"},
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
