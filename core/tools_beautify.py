"""Beautify tool — wraps screenshots in styled containers.

Pure Pillow pipeline. Zero I/O dependencies.

Architecture:
    Fixed pipeline: rounded corners + drop shadow + background container.
    The ONLY variable is the background generator (bg_func).
    Each preset selects a fundamentally different bg_func.

Background generators (each uses a distinct Pillow technique):
    _gradient       — 1px strip + LANCZOS upscale (smooth, no banding)
    _mesh           — blurred color blobs at 1/2 scale (mesh gradient)
    _radial_glow    — Image.blend + GaussianBlur (light falloff)
    _solid          — flat color
    _grid_pattern   — graph paper lines
    _dot_pattern    — polka dots
    _wallpaper      — blurred + enhanced source image
    _diagonal       — diagonal gradient via rotated strip
    _noise          — effect_noise texture overlay

Theme names are scenario-driven (what users share on social media):
    纯白/暗黑/暖阳/深海/极光/聚光/日落/方格/波点/衬底/对角/磨砂
"""

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
    """Multi-stop vertical gradient via 1px strip + LANCZOS upscale.

    Renders a 1-pixel-wide strip at full height, then resizes horizontally.
    LANCZOS interpolation produces perfectly smooth gradients with zero banding.
    """
    w, h = size
    n = len(colors) - 1
    strip = Image.new("RGB", (1, h))
    px = strip.load()
    for y in range(h):
        t = y / max(1, h - 1) * n
        seg = min(int(t), n - 1)
        lt = t - seg
        c1, c2 = colors[seg], colors[seg + 1]
        px[0, y] = tuple(int(c1[j] + (c2[j] - c1[j]) * lt) for j in range(3))
    return strip.resize((w, h), Image.LANCZOS)


def _diagonal(size, colors, angle=135):
    """Diagonal gradient via L-channel strip + resize + rotate.

    Rotate without expand to keep target size. BICUBIC resample.
    """
    w, h = size
    n = len(colors) - 1
    strip = Image.new("RGB", (1, h))
    px = strip.load()
    for y in range(h):
        t = y / max(1, h - 1) * n
        seg = min(int(t), n - 1)
        lt = t - seg
        c1, c2 = colors[seg], colors[seg + 1]
        px[0, y] = tuple(int(c1[j] + (c2[j] - c1[j]) * lt) for j in range(3))
    full = strip.resize((w, h), Image.LANCZOS)
    return full.rotate(angle, resample=Image.BICUBIC, fillcolor=colors[-1])


def _mesh(size, colors, seed=42, blobs=5):
    """Mesh gradient: blurred color blobs composited at 1/2 scale.

    Key: blur radius = blob_radius/2, large enough to fully dissolve blob edges.
    Uses local Random instance to avoid polluting global random state.
    """
    w, h = size
    scale = 2
    sw, sh = w // scale, h // scale
    avg = tuple(sum(c[i] for c in colors) // len(colors) for i in range(3))
    canvas = Image.new("RGBA", (sw, sh), avg + (255,))
    rng = random.Random(seed)
    for _ in range(blobs):
        cx = rng.randint(sw // 5, sw * 4 // 5)
        cy = rng.randint(sh // 5, sh * 4 // 5)
        r = rng.randint(max(sw, sh) // 2, max(sw, sh) * 3 // 4)
        c = rng.choice(colors)
        blob = Image.new("RGBA", (r * 2, r * 2), (0, 0, 0, 0))
        ImageDraw.Draw(blob).ellipse([0, 0, r * 2 - 1, r * 2 - 1], fill=c + (220,))
        blob = blob.filter(ImageFilter.GaussianBlur(radius=r // 2))
        canvas.alpha_composite(blob, (cx - r, cy - r))
    return canvas.convert("RGB").resize((w, h), Image.LANCZOS)


def _solid(size, color):
    return Image.new("RGB", size, color)


def _grid_pattern(size):
    """Light blue-gray grid paper."""
    w, h = size
    bg = Image.new("RGB", size, (238, 242, 248))
    d = ImageDraw.Draw(bg)
    for x in range(0, w, 28):
        d.line([(x, 0), (x, h)], fill=(185, 200, 215), width=1)
    for y in range(0, h, 28):
        d.line([(0, y), (w, y)], fill=(185, 200, 215), width=1)
    return bg


def _dot_pattern(size, bg_color, dot_color, spacing=32, dot_radius=4):
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
    """Radial gradient via Gaussian-blurred centered circle.

    Draws a bright circle at center, blends with edge_color background,
    then applies heavy GaussianBlur for smooth light falloff.
    """
    w, h = size
    scale = 2
    sw, sh = w // scale, h // scale
    cx, cy = sw // 2, sh // 2
    r = int(math.sqrt(cx * cx + cy * cy))
    bg = Image.new("RGB", (sw, sh), edge_color)
    glow = Image.new("RGB", (sw, sh), edge_color)
    ImageDraw.Draw(glow).ellipse([cx - r, cy - r, cx + r, cy + r], fill=center_color)
    glow = Image.blend(bg, glow, 0.75).filter(ImageFilter.GaussianBlur(radius=r // 2))
    return glow.resize((w, h), Image.LANCZOS)


def _wallpaper(size, src_img):
    """Blurred + enhanced source image as wallpaper."""
    w, h = size
    # Downscale → blur → upscale: saves ~200ms on large images
    small = src_img.resize((w // 2, h // 2), Image.LANCZOS)
    blurred = small.filter(ImageFilter.GaussianBlur(radius=30))
    blurred = blurred.resize((w, h), Image.LANCZOS)
    blurred = ImageEnhance.Color(blurred).enhance(1.3)
    blurred = ImageEnhance.Brightness(blurred).enhance(1.1)
    return blurred


def _noise_texture(size, base_color, noise_sigma=50, blend_alpha=0.1):
    """Frosted glass / matte noise texture.

    Generates noise at tiny scale (1/16), BoxBlur smooths, upscale.
    Low-alpha blend keeps PNG small while adding subtle texture.
    """
    w, h = size
    scale = 16
    sw, sh = max(2, w // scale), max(2, h // scale)
    base = Image.new("RGB", size, base_color)
    noise = Image.effect_noise((sw, sh), noise_sigma).convert("RGB")
    noise = noise.resize((w, h), Image.BOX)
    return Image.blend(base, noise, blend_alpha)


# ── Preset definitions ─────────────────────────────────────
# Each preset uses a fundamentally different visual technique.
# No two presets look similar — each has independent character.

PRESETS = {
    # ── Flat solids ──
    "pure":      {"bg": lambda s, i: _solid(s, (252, 252, 254))},
    "carbon":    {"bg": lambda s, i: _solid(s, (18, 18, 24))},

    # ── Linear gradients (vertical) ──
    "dawn":      {"bg": lambda s, i: _gradient(s, [(255, 248, 235), (255, 218, 185), (255, 183, 148)])},
    "ocean":     {"bg": lambda s, i: _gradient(s, [(8, 18, 48), (0, 80, 156), (0, 163, 178)])},
    "sunset":    {"bg": lambda s, i: _gradient(s, [(255, 94, 98), (255, 154, 0), (255, 206, 84)])},

    # ── Diagonal gradient ──
    "mint":      {"bg": lambda s, i: _diagonal(s, [(210, 250, 230), (120, 200, 180), (80, 160, 140)], angle=135)},

    # ── Mesh gradient ──
    "aurora":    {"bg": lambda s, i: _mesh(s, [(72, 49, 157), (0, 199, 255), (0, 229, 161), (124, 77, 255)], seed=7, blobs=5)},

    # ── Radial glow ──
    "spotlight": {"bg": lambda s, i: _radial_glow(s, (200, 225, 255), (8, 8, 14))},

    # ── Patterns ──
    "grid":      {"bg": lambda s, i: _grid_pattern(s)},
    "dots":      {"bg": lambda s, i: _dot_pattern(s, (252, 248, 242), (210, 192, 168), spacing=32, dot_radius=4)},

    # ── Source-based ──
    "blur":      {"bg": lambda s, i: _wallpaper(s, i)},

    # ── Noise texture ──
    "frost":     {"bg": lambda s, i: _noise_texture(s, (235, 238, 242), noise_sigma=60, blend_alpha=0.15)},
}


def beautify_handler(tool_input, params):
    """Wrap a screenshot in a styled container.

    Pipeline: decode → background → shadow (1/2 scale) → rounded mask → composite → PNG.
    """
    # ── 1. Format validation ──
    ct = tool_input.get("content_type", "")
    if not ct.startswith("image/"):
        raise ToolError("err_format_not_supported")

    # ── 2. Parameter resolution ──
    preset = params.get("preset", "aurora")
    p = PRESETS.get(preset, PRESETS["aurora"])

    # ── 3. Decode + orient + downscale ──
    img = Image.open(tool_input["stream"])
    img = ImageOps.exif_transpose(img).convert("RGB")
    if max(img.size) > MAX_DIM:
        img.thumbnail((MAX_DIM, MAX_DIM), Image.LANCZOS)
    w, h = img.size

    # ── 4. Generate background ──
    pad = PADDING
    sw, sh = w + pad * 2, h + pad * 2
    bg = p["bg"]((sw, sh), img)

    # ── 5. Shadow at 1/2 scale (performance) ──
    half_sw, half_sh = sw // 2, sh // 2
    half_pad = pad // 2
    half_w, half_h = w // 2, h // 2
    half_offset = SHADOW_OFFSET // 2
    half_radius = max(1, RADIUS // 2)
    half_blur = max(1, SHADOW_BLUR // 2)

    shadow_half = Image.new("RGBA", (half_sw, half_sh), (0, 0, 0, 0))
    ImageDraw.Draw(shadow_half).rounded_rectangle(
        [half_pad, half_pad + half_offset,
         half_pad + half_w - 1, half_pad + half_h - 1 + half_offset],
        radius=half_radius, fill=(0, 0, 0, SHADOW_ALPHA))
    shadow_half = shadow_half.filter(ImageFilter.GaussianBlur(radius=half_blur))
    shadow = shadow_half.resize((sw, sh), Image.LANCZOS)

    # ── 6. Rounded mask ──
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, w - 1, h - 1], radius=RADIUS, fill=255)

    # ── 7. Composite: shadow + image on background ──
    canvas = Image.new("RGBA", (sw, sh), (0, 0, 0, 0))
    canvas.paste(shadow, (0, 0), shadow)
    canvas.paste(img, (pad, pad + SHADOW_OFFSET), mask)

    bg_rgba = bg.convert("RGBA")
    bg_rgba.alpha_composite(canvas)

    # ── 8. Flatten to RGB + output ──
    out = bg_rgba.convert("RGB")

    buf = io.BytesIO()
    out.save(buf, "PNG", optimize=True)
    raw = buf.getvalue()

    orig_name = tool_input.get("filename", "image")
    stem = orig_name.rsplit(".", 1)[0] if "." in orig_name else orig_name
    out_name = f"beautify_{stem}.png"

    return {
        "stream": io.BytesIO(raw),
        "filename": out_name,
        "content_type": "image/png",
        "size": len(raw),
    }


# ── Registration ───────────────────────────────────────────

_PRESET_LABELS = {
    "pure":      {"en": "Pure",      "zh": "纯白",   "ja": "ピュア",     "ko": "퓨어",      "es": "Puro",      "ar": "نقي",      "de": "Rein",      "ru": "Чистый",    "pt": "Puro",      "fr": "Pur"},
    "carbon":    {"en": "Carbon",    "zh": "暗黑",   "ja": "カーボン",   "ko": "카본",      "es": "Carbón",    "ar": "فحمي",     "de": "Karbon",    "ru": "Карбон",    "pt": "Carbono",   "fr": "Carbone"},
    "dawn":      {"en": "Dawn",      "zh": "晨光",   "ja": "朝光",       "ko": "새벽",      "es": "Alba",      "ar": "فجر",      "de": "Morgendämmerung", "ru": "Рассвет", "pt": "Amanhecer", "fr": "Aube"},
    "ocean":     {"en": "Ocean",     "zh": "深海",   "ja": "オーシャン", "ko": "오션",      "es": "Océano",    "ar": "محيط",     "de": "Ozean",     "ru": "Океан",     "pt": "Oceano",    "fr": "Océan"},
    "aurora":    {"en": "Aurora",    "zh": "极光",   "ja": "オーロラ",   "ko": "오로라",    "es": "Aurora",    "ar": "شفق",      "de": "Aurora",    "ru": "Аврора",    "pt": "Aurora",    "fr": "Aurore"},
    "spotlight": {"en": "Spotlight", "zh": "聚光",   "ja": "スポット",   "ko": "스포트",    "es": "Foco",      "ar": "كشاف",     "de": "Spotlight", "ru": "Прожектор",  "pt": "Holofote",  "fr": "Projecteur"},
    "sunset":    {"en": "Sunset",    "zh": "日落",   "ja": "サンセット", "ko": "선셋",      "es": "Atardecer", "ar": "غروب",     "de": "Sonnenuntergang", "ru": "Закат", "pt": "Pôr do sol", "fr": "Coucher"},
    "grid":      {"en": "Grid",      "zh": "方格",   "ja": "グリッド",   "ko": "그리드",    "es": "Cuadrícula","ar": "شبكة",    "de": "Raster",    "ru": "Сетка",     "pt": "Grade",     "fr": "Grille"},
    "dots":      {"en": "Dots",      "zh": "波点",   "ja": "ドット",     "ko": "도트",      "es": "Puntos",    "ar": "نقاط",    "de": "Punkte",    "ru": "Точки",     "pt": "Pontos",    "fr": "Points"},
    "blur":      {"en": "Backdrop",  "zh": "衬底",   "ja": "背景ぼかし", "ko": "배경",      "es": "Fondo",     "ar": "خلفية",    "de": "Hintergrund", "ru": "Фон",     "pt": "Fundo",     "fr": "Fond"},
    "mint":      {"en": "Mint",      "zh": "薄荷",   "ja": "ミント",     "ko": "민트",      "es": "Menta",     "ar": "نعناع",   "de": "Minze",     "ru": "Мята",      "pt": "Menta",     "fr": "Menthe"},
    "frost":     {"en": "Frost",     "zh": "磨砂",   "ja": "フロスト",   "ko": "프로스트",  "es": "Escarcha",  "ar": "صقيع",    "de": "Frost",     "ru": "Мороз",     "pt": "Geada",     "fr": "Givre"},
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
