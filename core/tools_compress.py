"""Compress tool — shrinks image to fit a target file size.

Pure Pillow pipeline. Zero I/O dependencies.

Use case: uploading scanned documents / photos to sites with size limits
(e.g. "max 2MB"). Binary-searches JPEG quality to hit the target.
If original is already under target, passes through unchanged.
"""

import gc
import io

from PIL import Image, ImageOps

from tools import register, TYPE_PRODUCE, ToolError

MAX_DIM = 4096  # don't upscale, but allow large scans

# Target sizes in bytes
TARGETS = {
    "1mb":  1_048_576,
    "2mb":  2_097_152,
    "5mb":  5_242_880,
    "10mb": 10_485_760,
}


def _save_jpeg(img, quality):
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality, progressive=True, optimize=True)
    return buf.getvalue()


def compress_handler(tool_input, params):
    """Compress an image to fit within a target file size.

    If the original is already under target, returns it unchanged.
    Otherwise binary-searches JPEG quality to find the highest quality
    that fits. If still over, progressively downscales dimensions.
    """
    # ---- CP6.1: Format validation ----
    ct = tool_input.get("content_type", "")
    if not ct.startswith("image/"):
        raise ToolError("err_format_not_supported")

    # ---- CP6.2: Parameter resolution ----
    preset = params.get("preset", "2mb")
    target = TARGETS.get(preset, TARGETS["2mb"])
    orig_size = tool_input.get("size", 0)

    # ---- CP6.3: Already under target → pass through ----
    if orig_size and orig_size <= target:
        tool_input["stream"].seek(0)
        return {
            "stream": tool_input["stream"],
            "filename": tool_input["filename"],
            "content_type": tool_input["content_type"],
            "size": orig_size,
        }

    # ---- CP6.4: Decode + orient ----
    img = Image.open(tool_input["stream"])
    img = ImageOps.exif_transpose(img)
    img = img.convert("RGB")
    if max(img.size) > MAX_DIM:
        img.thumbnail((MAX_DIM, MAX_DIM), Image.LANCZOS)

    w, h = img.size

    # ---- CP6.5: Binary search quality to hit target ----
    # Quick probe at q60 to estimate achievable size
    probe = _save_jpeg(img, 60)
    if len(probe) <= target:
        # Target is achievable without downscaling — find best quality
        lo, hi = 55, 95
        best_bytes = probe
        while lo <= hi:
            mid = (lo + hi) // 2
            raw = _save_jpeg(img, mid)
            if len(raw) <= target:
                best_bytes = raw
                lo = mid + 1
            else:
                hi = mid - 1
    else:
        # Can't fit at any reasonable quality — downscale then retry
        best_bytes = probe
        cur_w, cur_h = w, h
        while len(best_bytes) > target and max(cur_w, cur_h) > 480:
            cur_w = int(cur_w * 0.75)
            cur_h = int(cur_h * 0.75)
            scaled = img.resize((cur_w, cur_h), Image.LANCZOS)
            # Try q70 on scaled version
            test = _save_jpeg(scaled, 70)
            if len(test) <= target:
                # Good enough — now fine-tune quality up
                lo2, hi2 = 65, 90
                best_bytes = test
                while lo2 <= hi2:
                    mid = (lo2 + hi2) // 2
                    raw = _save_jpeg(scaled, mid)
                    if len(raw) <= target:
                        best_bytes = raw
                        lo2 = mid + 1
                    else:
                        hi2 = mid - 1
                break
            best_bytes = test
        # Final cleanup if still over (extreme edge case)
        if len(best_bytes) > target:
            scaled = img.resize((max(480, int(w*0.5)), max(480, int(h*0.5))), Image.LANCZOS)
            best_bytes = _save_jpeg(scaled, 50)

    raw = best_bytes
    del img, best_bytes
    gc.collect()

    out_buf = io.BytesIO(raw)
    out_buf.seek(0)

    orig_name = tool_input.get("filename", "image")
    stem = orig_name.rsplit(".", 1)[0] if "." in orig_name else orig_name
    out_name = f"compressed_{stem}.jpg"

    return {
        "stream": out_buf,
        "filename": out_name,
        "content_type": "image/jpeg",
        "size": len(raw),
    }


# ── Registration ──

_PRESET_LABELS = {
    "1mb":  {"en": "≤ 1 MB", "zh": "≤ 1 MB", "ja": "≤ 1 MB", "ko": "≤ 1 MB", "es": "≤ 1 MB", "ar": "≤ 1 MB", "de": "≤ 1 MB"},
    "2mb":  {"en": "≤ 2 MB", "zh": "≤ 2 MB", "ja": "≤ 2 MB", "ko": "≤ 2 MB", "es": "≤ 2 MB", "ar": "≤ 2 MB", "de": "≤ 2 MB"},
    "5mb":  {"en": "≤ 5 MB", "zh": "≤ 5 MB", "ja": "≤ 5 MB", "ko": "≤ 5 MB", "es": "≤ 5 MB", "ar": "≤ 5 MB", "de": "≤ 5 MB"},
    "10mb": {"en": "≤ 10 MB","zh": "≤ 10 MB","ja": "≤ 10 MB","ko": "≤ 10 MB","es": "≤ 10 MB","ar": "≤ 10 MB","de": "≤ 10 MB"},
}

register(
    tool_id="compress",
    tool_type=TYPE_PRODUCE,
    label={
        "en": "Compress", "zh": "图片压缩", "ja": "画像圧縮",
        "ko": "이미지 압축", "es": "Comprimir imagen", "ar": "ضغط الصورة",
        "de": "Bild komprimieren", "ru": "Сжатие фото", "pt": "Comprimir imagem",
        "fr": "Compresser l'image",
    },
    desc={
            "fr": "Reduire la taille de l'image avec controle qualite.",
            "ko": '품질을 제어하며 이미지 크기를 줄입니다.',
            "ru": 'Уменьшение размера изображения с контролем качества.',
            "ar": 'تقليل حجم الصورة مع التحكم بالجودة.',
            "de": 'Bildgroße reduzieren mit Qualitätskontrolle.',
            "pt": 'Reduz o tamanho da imagem controlando a qualidade.',
            "ja": '画像ファイルサイズを品質を保ちつつ圧縮。',
            "zh": '压缩图片体积，可控质量。',
            "es": 'Reduce el tamano de la imagen controlando la calidad.',
            "en": 'Reduce image file size with quality control.',
        },
    icon="compress",
    accept={".bmp", ".gif", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"},
    handler=compress_handler,
    params=[
        {"key": "preset", "type": "enum", "label": {"en": "Target size", "zh": "目标体积"},
         "options": [{"value": pid, "label": lbl} for pid, lbl in _PRESET_LABELS.items()],
         "default": "2mb"},
    ],
)
