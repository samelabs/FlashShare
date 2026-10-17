"""Privacy tool — strips metadata and disrupts AI image watermarks.

Pure Pillow pipeline. Zero I/O dependencies.
Handler receives a stream (bytes in), returns IO-1 items (bytes out).
Never imports gokapi, database, or any storage/record layer.

Pipeline:
    decode → exif_transpose → ICC→sRGB → cap at 4096
    → noise injection → Gaussian blur → USM sharpen → resample roundtrip
    → clean re-encode (no EXIF/XMP/IPTC/comment chunks)

Output format keeps the input family: PNG (alpha preserved) → PNG,
JPEG → JPEG; any other accepted input → PNG when it carries
transparency, else JPEG. The clean file is never larger than 4096 on
its longest edge.
"""

import gc
import io

from PIL import Image, ImageFilter, ImageChops, ImageOps

from tools import out_filename, register, ToolError

MAX_DIM = 4096  # same ceiling as compress — no extra shrink below it

PRESETS = {
    "light":    {"noise_sigma": 4,  "noise_blend": 0.05, "blur": 0.4, "usm_r": 1,   "usm_p": 60,  "usm_t": 2, "scale": 0.96},
    "standard": {"noise_sigma": 12, "noise_blend": 0.10, "blur": 0.5, "usm_r": 1,   "usm_p": 60,  "usm_t": 2, "scale": 0.93},
    "strong":   {"noise_sigma": 16, "noise_blend": 0.12, "blur": 0.8, "usm_r": 1,   "usm_p": 80,  "usm_t": 2, "scale": 0.90},
    "max":      {"noise_sigma": 20, "noise_blend": 0.15, "blur": 1.0, "usm_r": 1.5, "usm_p": 100, "usm_t": 1, "scale": 0.90},
}


def _has_alpha(img: Image.Image) -> bool:
    if img.mode in ("RGBA", "LA"):
        return True
    return img.mode == "P" and "transparency" in img.info


def _to_srgb(img: Image.Image) -> Image.Image:
    """Convert pixels into the sRGB space when an ICC profile is attached.

    The profile itself is not carried over — outputs embed no metadata."""
    icc = img.info.get("icc_profile")
    if not icc:
        return img
    try:
        from PIL import ImageCms
        src = ImageCms.ImageCmsProfile(io.BytesIO(icc))
        dst = ImageCms.createProfile("sRGB")
        mode = "RGBA" if _has_alpha(img) else "RGB"
        return ImageCms.profileToProfile(img, src, dst, outputMode=mode)
    except Exception:
        # Broken/unsupported profile: pixel values pass through and every
        # metadata chunk is still dropped by the clean re-encode.
        return img


def _roundtrip(channel: Image.Image, scale: float, w: int, h: int) -> Image.Image:
    """Downscale-then-upscale resample roundtrip (breaks watermark grid
    alignment); final size is exactly (w, h) so channels stay aligned."""
    tw = max(1, int(w * scale))
    th = max(1, int(h * scale))
    channel = channel.resize((tw, th), Image.LANCZOS)
    return channel.resize((w, h), Image.LANCZOS)


def privacy_handler(tool_input: dict, params: dict) -> dict:
    """Strip metadata and disrupt AI watermarks from one image.

    Args:
        tool_input: {"stream": file-like, "filename": str, "content_type": str, "size": int}
        params: {"preset": str, ...}

    Returns:
        {"items": [{"kind": "file", "stream": BytesIO, "filename": str,
                    "content_type": str, "size": int}]}

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

    # ---- CP6.3: Decode + orient + ICC→sRGB ----
    try:
        img = Image.open(tool_input["stream"])
        img.load()
    except ToolError:
        raise
    except Exception as exc:
        raise ToolError("err_format_not_supported") from exc
    img = ImageOps.exif_transpose(img)
    img = _to_srgb(img)

    # Output family: PNG stays PNG (alpha preserved), JPEG stays JPEG;
    # other accepted formats keep transparency via PNG, else JPEG.
    if ct == "image/png":
        keep_png = True
    elif ct == "image/jpeg":
        keep_png = False
    else:
        keep_png = _has_alpha(img)

    alpha = None
    if _has_alpha(img):
        rgba = img.convert("RGBA")
        alpha = rgba.getchannel("A")
        img = rgba.convert("RGB")
    elif img.mode != "RGB":
        img = img.convert("RGB")

    # ---- CP6.4: Cap the longest edge (no shrink below the ceiling) ----
    if max(img.size) > MAX_DIM:
        img.thumbnail((MAX_DIM, MAX_DIM), Image.LANCZOS)
        if alpha is not None:
            alpha.thumbnail((MAX_DIM, MAX_DIM), Image.LANCZOS)
    w, h = img.size

    # ---- CP6.5: Noise injection (disrupt watermark signal) ----
    noise = Image.effect_noise((w, h), p["noise_sigma"]).convert("RGB")
    img = ImageChops.blend(img, noise, p["noise_blend"])
    del noise
    gc.collect()

    # ---- CP6.6: Gaussian blur (cover high-frequency watermark) ----
    img = img.filter(ImageFilter.GaussianBlur(radius=p["blur"]))

    # ---- CP6.7: USM sharpen (recover edges, not watermark) ----
    img = img.filter(ImageFilter.UnsharpMask(
        radius=p["usm_r"], percent=p["usm_p"], threshold=p["usm_t"]))

    # ---- CP6.8: Resample roundtrip (break watermark grid alignment) ----
    scale = p["scale"]
    img = _roundtrip(img, scale, w, h)
    if alpha is not None:
        alpha = _roundtrip(alpha, scale, w, h)

    # ---- CP6.9: Clean output (no metadata chunks are written) ----
    buf = io.BytesIO()
    if keep_png:
        out = Image.merge("RGBA", (*img.split(), alpha)) if alpha is not None else img
        out.save(buf, "PNG")
        out_ext, out_ct = "png", "image/png"
    else:
        img.save(buf, "JPEG", quality=90, progressive=True)
        out_ext, out_ct = "jpg", "image/jpeg"
    raw = buf.getvalue()
    del img, alpha
    gc.collect()

    out_buf = io.BytesIO(raw)
    out_buf.seek(0)

    orig_name = tool_input.get("filename", "image")
    out_name = out_filename(orig_name, out_ext)

    return {
        "items": [{
            "kind": "file",
            "stream": out_buf,
            "filename": out_name,
            "content_type": out_ct,
            "size": len(raw),
        }],
    }


# ---- Registration ----

# ── Default tool: Share (plain upload, no processing) ──
# Registered first so it appears as the leftmost tool button.
# When selected (or by default when no tool is selected), files upload
# normally with no processing — identical to the pre-tool behavior.

def demo_shout_handler(tool_input, params):
    """Demo text-only tool (files 0..0): the chat-form reference shape."""
    text = str((params or {}).get("text") or "")
    return {"items": [{"kind": "text", "text": text.upper()}]}


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
        "fr": "Supprime les metadonnees et les filigranes IA. La sortie est re-encodee — les pixels bougent legerement a tous les niveaux (par conception).",
        "ko": "이미지 메타데이터와 AI 워터마크를 제거합니다. 출력은 재인코딩되며 모든 단계에서 픽셀이 약간 변합니다(설계상 동작).",
        "ru": "Удаляет метаданные и водяные знаки ИИ. Результат перекодируется — пиксели слегка меняются на любом уровне (так задумано).",
        "ar": "يزيل البيانات الوصفية والعلامات المائية للذكاء الاصطناعي. يعاد ترميز المخرجات وتتغير البكسلات قليلًا في كل المستويات (بشكل مقصود).",
        "de": "Entfernt Metadaten und AI-Wasserzeichen. Die Ausgabe wird neu kodiert — Pixel verschieben sich auf jeder Stufe leicht (beabsichtigt).",
        "pt": "Remove metadados e marcas dagua de IA. A saída é recodificada — os pixels mudam levemente em todos os níveis (por design).",
        "ja": "画像のメタデータとAIウォーターマークを除去します。出力は再エンコードされ、どのレベルでもピクセルがわずかに変化します（仕様）。",
        "zh": "剥离图片元数据和AI暗水印，保护上传者隐私。输出会整体重编码，各档位像素均有轻微扰动（设计使然，暗水印干扰是全幅的）。",
        "es": "Elimina metadatos y marcas de agua de IA. La salida se recodifica: los píxeles cambian ligeramente en todos los niveles (por diseño).",
        "en": "Strips metadata and disrupts AI watermarks from your images. The output is re-encoded — pixels shift slightly at every level (by design; watermark disruption is whole-image).",
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

register(
    "demo_shout", "Shout", "wand", demo_shout_handler,
    accept=None, files_min=0, files_max=0, outputs=("text",),
    desc={"en": "Turns your text into UPPERCASE — a demo of the chat-form input.",
          "zh": "把你的文本变成大写——对话式输入的参考工具。"},
    params=[
        {"key": "text", "type": "text", "label": {"en": "Text", "zh": "文本"},
         "required": True},
    ],
)
