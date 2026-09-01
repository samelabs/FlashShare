"""Privacy tool — strips metadata and disrupts AI image watermarks.

Pure Pillow pipeline. Zero I/O dependencies.
Handler receives a stream (bytes in), returns a stream (bytes out).
Never imports gokapi, database, or any storage/record layer.

Pipeline:
    decode → exif_transpose → convert(RGB) → thumbnail(2048)
    → noise injection → Gaussian blur → USM sharpen → resample roundtrip
    → clean JPEG save → inject XMP brand
"""

import gc
import io
from datetime import datetime, timezone

from PIL import Image, ImageFilter, ImageChops, ImageOps

from tools import register, TYPE_PRODUCE, ToolError

MAX_DIM = 2048

PRESETS = {
    "light":    {"noise_sigma": 4,  "noise_blend": 0.06, "blur": 0.5, "usm_r": 1,   "usm_p": 60,  "usm_t": 2, "scale": 0.93},
    "standard": {"noise_sigma": 12, "noise_blend": 0.10, "blur": 0.5, "usm_r": 1,   "usm_p": 60,  "usm_t": 2, "scale": 0.93},
    "strong":   {"noise_sigma": 16, "noise_blend": 0.12, "blur": 0.8, "usm_r": 1,   "usm_p": 80,  "usm_t": 2, "scale": 0.90},
    "max":      {"noise_sigma": 20, "noise_blend": 0.15, "blur": 1.0, "usm_r": 1.5, "usm_p": 100, "usm_t": 1, "scale": 0.88},
}


def _inject_xmp(jpeg_bytes: bytes) -> bytes:
    """Inject XMP creator metadata into JPEG bytes (before first SOS)."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    xmp = (
        b'<?xpacket begin="\xef\xbb\xbf" id="W5M0MpCehiHzreSzNTczkc9d"?>'
        b'<x:xmpmeta xmlns:x="adobe:ns:meta/">'
        b'<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        b'<rdf:Description xmlns:dc="http://purl.org/dc/elements/1.1/">'
        b'<dc:creator><rdf:Seq><rdf:li>518.plus</rdf:li></rdf:Seq></dc:creator>'
        b'<dc:description><rdf:Alt><rdf:li xml:lang="x-default">'
        b'Processed by 518.plus/privacy at ' + now.encode() + b'</rdf:li></rdf:Alt></dc:description>'
        b'</rdf:Description>'
        b'</rdf:RDF>'
        b'</x:xmpmeta>'
        b'<?xpacket end="w"?>'
    )
    app1 = b'\xff\xe1' + (len(xmp) + 2).to_bytes(2, 'big') + xmp
    sos = jpeg_bytes.find(b'\xff\xda')
    if sos < 0:
        return jpeg_bytes
    return jpeg_bytes[:sos] + app1 + jpeg_bytes[sos:]


def privacy_handler(tool_input: dict, params: dict) -> dict:
    """Process an image to strip metadata and disrupt watermarks.

    Args:
        tool_input: {"stream": file-like, "filename": str, "content_type": str, "size": int}
        params: {"preset": str, ...}

    Returns:
        {"stream": BytesIO, "filename": str, "content_type": str, "size": int}

    Raises:
        ToolError if format is not supported.
    """
    # ---- CP6.1: Format validation (security boundary) ----
    ct = tool_input.get("content_type", "")
    if not ct.startswith("image/"):
        raise ToolError("err_format_not_supported")

    # ---- CP6.2: Parameter resolution (security boundary, silent fallback) ----
    preset = params.get("preset", "standard")
    p = PRESETS.get(preset, PRESETS["standard"])

    # ---- CP6.3: Decode + orient + downscale ----
    img = Image.open(tool_input["stream"])
    img = ImageOps.exif_transpose(img)
    img = img.convert("RGB")
    if max(img.size) > MAX_DIM:
        img.thumbnail((MAX_DIM, MAX_DIM), Image.LANCZOS)
    w, h = img.size

    # ---- CP6.4: Noise injection (disrupt watermark signal) ----
    noise = Image.effect_noise((w, h), p["noise_sigma"]).convert("RGB")
    img = ImageChops.blend(img, noise, p["noise_blend"])
    del noise
    gc.collect()

    # ---- CP6.5: Gaussian blur (cover high-frequency watermark) ----
    img = img.filter(ImageFilter.GaussianBlur(radius=p["blur"]))

    # ---- CP6.6: USM sharpen (recover edges, not watermark) ----
    img = img.filter(ImageFilter.UnsharpMask(
        radius=p["usm_r"], percent=p["usm_p"], threshold=p["usm_t"]))

    # ---- CP6.7: Resample roundtrip (break watermark grid alignment) ----
    scale = p["scale"]
    tw = max(1, int(w * scale))
    th = max(1, int(h * scale))
    img = img.resize((tw, th), Image.LANCZOS)
    img = img.resize((w, h), Image.LANCZOS)

    # ---- CP6.8: Clean output (metadata stripped, brand injected) ----
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90, progressive=True)
    raw = buf.getvalue()
    raw = _inject_xmp(raw)

    out_buf = io.BytesIO(raw)
    out_buf.seek(0)

    # Derive output filename from original
    orig_name = tool_input.get("filename", "image")
    stem = orig_name.rsplit(".", 1)[0] if "." in orig_name else orig_name
    out_name = f"privacy_{stem}.jpg"

    return {
        "stream": out_buf,
        "filename": out_name,
        "content_type": "image/jpeg",
        "size": len(raw),
    }


# ---- Registration ----

# ── Default tool: Share (plain upload, no processing) ──
# Registered first so it appears as the leftmost tool button.
# When selected (or by default when no tool is selected), files upload
# normally with no processing — identical to the pre-tool behavior.

def share_handler(tool_input, params):
    """Identity handler — passes the file through unchanged."""
    return {
        "stream": tool_input["stream"],
        "filename": tool_input["filename"],
        "content_type": tool_input["content_type"],
        "size": tool_input["size"],
    }


register(
    tool_id="share",
    tool_type=TYPE_PRODUCE,
    label={
        "en": "Lossless Share", "zh": "无损分享", "ja": "ロスレス共有",
        "ko": "무손실 공유", "es": "Compartir sin pérdida", "ar": "مشاركة بدون فقدان",
        "de": "Verlustfreies Teilen", "ru": "Скачивание без потерь",
        "pt": "Compartilhar sem perda", "fr": "Partage sans perte",
    },
    icon="upload",
    accept=None,  # accepts all formats
    handler=share_handler,
)

register(
    tool_id="privacy",
    tool_type=TYPE_PRODUCE,
    label={
        "en": "Clean",
        "zh": "图片净化",
        "ja": "画像クリーン",
        "ko": "이미지 정화",
        "es": "Limpiar imagen",
        "ar": "تنظيف الصورة",
        "de": "Bild bereinigen",
        "ru": "Очистка фото",
        "pt": "Limpar imagem",
        "fr": "Nettoyer l'image",
    },
    icon="shield",
    accept={"image/jpeg", "image/png", "image/webp", "image/gif", "image/bmp", "image/tiff"},
    handler=privacy_handler,
    presets=[
        {"id": "light",    "label": {"en": "Light",    "zh": "轻度",
                                     "ja": "軽量",  "ko": "가볍게", "es": "Ligero",
                                     "ar": "خفيف", "de": "Leicht", "ru": "Лёгкий",
                                     "pt": "Leve", "fr": "Léger"}, "default": False},
        {"id": "standard", "label": {"en": "Standard", "zh": "标准",
                                     "ja": "標準",  "ko": "표준", "es": "Estándar",
                                     "ar": "قياسي", "de": "Standard", "ru": "Стандарт",
                                     "pt": "Padrão", "fr": "Standard"}, "default": True},
        {"id": "strong",   "label": {"en": "Strong",   "zh": "增强",
                                     "ja": "強化",  "ko": "강화", "es": "Fuerte",
                                     "ar": "قوي", "de": "Stark", "ru": "Сильный",
                                     "pt": "Forte", "fr": "Fort"}, "default": False},
        {"id": "max",      "label": {"en": "Max",      "zh": "强力",
                                     "ja": "最大",  "ko": "최대", "es": "Máx",
                                     "ar": "أقصى", "de": "Max", "ru": "Макс",
                                     "pt": "Máx", "fr": "Max"}, "default": False},
    ],
)
