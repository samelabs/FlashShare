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

from config import APP_NAME, BASE_URL
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
        b'<dc:creator><rdf:Seq><rdf:li>' + APP_NAME.encode() + b'</rdf:li></rdf:Seq></dc:creator>'
        b'<dc:description><rdf:Alt><rdf:li xml:lang="x-default">'
        b'Processed by ' + BASE_URL.encode() + b'/privacy at ' + now.encode() + b'</rdf:li></rdf:Alt></dc:description>'
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

    # ---- CP6.2: Parameter resolution (canonical params only) ----
    # Dispatch (governance.tool_params.enforce_params_decl) is the sole
    # params authority: missing → declaration default injected, illegal
    # enum → 422 before the handler runs. The handler reads the canonical
    # value directly and fails loud on any bypass, never guesses standard.
    preset = params["preset"]
    p = PRESETS[preset]

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
    """Identity handler — every input file comes back as one file item,
    unchanged. Regular execute flow (no special-casing anywhere)."""
    def _item(src):
        return {
            "kind": "file",
            "stream": src["stream"],
            "filename": src["filename"],
            "content_type": src["content_type"],
            "size": src["size"],
        }
    if "sources" in tool_input:
        return {"items": [_item(s) for s in tool_input["sources"]]}
    return {"items": [_item(tool_input)]}


register(
    tool_id="share",
    tool_type=TYPE_PRODUCE,
    label={
        "en": "Lossless Share", "zh": "无损分享", "ja": "ロスレス共有",
        "ko": "무손실 공유", "es": "Compartir sin pérdida", "ar": "مشاركة بدون فقدان",
        "de": "Verlustfreies Teilen", "ru": "Поделиться без потерь",
        "pt": "Compartilhar sem perda", "fr": "Partage sans perte",
    },
    desc={
            "fr": 'Partagez des fichiers sans perte.',
            "ko": '파일을 무손실로 공유합니다.',
            "ru": 'Делитесь файлами без потерь.',
            "ar": 'مشاركة الملفات دون فقدان.',
            "de": 'Dateien verlustfrei teilen.',
            "pt": 'Compartilhe arquivos sem perdas.',
            "ja": 'ファイルをロスレスで共有。',
            "zh": '无损分享文件，生成链接。',
            "es": 'Comparte archivos sin perdida.',
            "en": 'Share files losslessly and hand people a link: give '
                  'humans result_url (one page for all files); default '
                  'delivery 1 day / 50 downloads, adjustable via the '
                  'delivery object.',
        },
    icon="upload",
    accept=None,  # accepts all formats
    handler=share_handler,
    files_min=1, files_max=20,
    outputs=("file",),
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
    desc={
        "fr": "Supprime les metadonnees et les filigranes IA.",
        "ko": "이미지 메타데이터와 AI 워터마크를 제거합니다。",
        "ru": "Удаляет метаданные и водяные знаки ИИ.",
        "ar": "يزيل البيانات الوصفية والعلامات المائية للذكاء الاصطناعي.",
        "de": "Entfernt Metadaten und AI-Wasserzeichen.",
        "pt": "Remove metadados e marcas dagua de IA.",
        "ja": "画像のメタデータとAIウォーターマークを除去します。",
        "zh": "剥离图片元数据和AI暗水印，保护上传者隐私。",
        "es": "Elimina metadatos y marcas de agua de IA.",
        "en": "Strips metadata and disrupts AI watermarks from your images.",
    },
    icon="shield",
    accept={".bmp", ".gif", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"},
    handler=privacy_handler,
    params=[
        {"key": "preset", "type": "enum", "label": {"en": "Clean level", "zh": "净化强度"},
         "options": [
             {"value": "light", "label": {"en": "Light", "zh": "轻度", "ja": "軽量", "ko": "가볍게", "es": "Ligero", "ar": "خفيف", "de": "Leicht", "ru": "Лёгкий", "pt": "Leve", "fr": "Léger"}},
             {"value": "standard", "label": {"en": "Standard", "zh": "标准", "ja": "標準", "ko": "표준", "es": "Estándar", "ar": "قياسي", "de": "Standard", "ru": "Стандарт", "pt": "Padrão", "fr": "Standard"}},
             {"value": "strong", "label": {"en": "Strong", "zh": "增强", "ja": "強化", "ko": "강화", "es": "Fuerte", "ar": "قوي", "de": "Stark", "ru": "Сильный", "pt": "Forte", "fr": "Fort"}},
             {"value": "max", "label": {"en": "Max", "zh": "强力", "ja": "最大", "ko": "최대", "es": "Máx", "ar": "أقصى", "de": "Max", "ru": "Макс", "pt": "Máx", "fr": "Max"}},
         ],
         "default": "standard"},
    ],
)
