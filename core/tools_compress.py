"""Compress tool — shrink an image within a size budget.

Pure Pillow pipeline. Zero I/O dependencies.

Two preset families:
    "auto" (recommended) — keep the source format and just re-encode
        smarter: JPEG → q82 progressive; PNG → lossless optimize, then
        256-color quantization (alpha-aware when transparent), keeping
        any result that lands ≤ 70% of the original. Falls back to
        whatever is still smaller than the original, else the original.
    "1mb"/"2mb"/"5mb"/"10mb" — hard byte budgets. Images carrying
        transparency ALWAYS stay PNG (quantize, downscale if needed —
        never JPEG, never alpha loss); opaque images binary-search
        JPEG quality, downscaling only when quality alone cannot fit.

Inputs already under budget follow the auto rules too — they get the
same re-encode attempt instead of a pass-through.
"""

import gc
import io

from PIL import Image, ImageOps

from tools import out_filename, register, ToolError

MAX_DIM = 4096  # don't upscale, but allow large scans

# Target sizes in bytes
TARGETS = {
    "1mb":  1_048_576,
    "2mb":  2_097_152,
    "5mb":  5_242_880,
    "10mb": 10_485_760,
}

# auto PNG acceptance: a re-encode must beat the original by this factor
AUTO_PNG_GAIN = 0.70


def _save_jpeg(img, quality):
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality, progressive=True, optimize=True)
    return buf.getvalue()


def _save_png(img):
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def _has_alpha(img):
    if img.mode in ("RGBA", "LA"):
        return True
    return img.mode == "P" and "transparency" in img.info


def _quantize(img):
    """256-color palette version. FASTOCTREE for RGBA (MEDIANCUT silently
    drops the alpha channel — palette tRNS keeps transparency); MEDIANCUT
    for opaque RGB (better quality)."""
    if _has_alpha(img):
        return img.quantize(colors=256, method=Image.Quantize.FASTOCTREE,
                            dither=Image.Dither.NONE)
    return img.quantize(colors=256, dither=Image.Dither.NONE)


def _decode(stream):
    """Open + orient. Returns the working image (RGBA when the source
    carries transparency, RGB otherwise) and its alpha flag. A stream
    Pillow cannot decode is a format error (typed err_*), never a 500."""
    try:
        img = Image.open(stream)
        img.load()
    except ToolError:
        raise
    except Exception as exc:
        raise ToolError("err_format_not_supported") from exc
    img = ImageOps.exif_transpose(img)
    if _has_alpha(img):
        return img.convert("RGBA"), True
    return img.convert("RGB"), False


def compress_handler(tool_input, params):
    """Compress an image within the selected size budget.

    Returns:
        {"items": [{"kind": "file", "stream": BytesIO, "filename": str,
                    "content_type": str, "size": int}]}
    """
    # ---- CP6.1: Format validation ----
    ct = tool_input.get("content_type", "")
    if not ct.startswith("image/"):
        raise ToolError("err_format_not_supported")

    # ---- CP6.2: Parameter resolution (canonical params only) ----
    # enforce_params_decl is the sole params authority: missing →
    # declaration default injected, illegal enum → 422 before the handler
    # runs. Direct canonical read; fail loud on any bypass.
    preset = params["preset"]
    orig_size = tool_input.get("size", 0)

    def _passthrough():
        tool_input["stream"].seek(0)
        return {
            "items": [{
                "kind": "file",
                "stream": tool_input["stream"],
                "filename": tool_input["filename"],
                "content_type": tool_input["content_type"],
                "size": orig_size,
            }],
        }

    def _result(raw, ext, out_ct):
        orig_name = tool_input.get("filename", "image")
        stem = orig_name.rsplit(".", 1)[0] if "." in orig_name else orig_name
        return {
            "items": [{
                "kind": "file",
                "stream": io.BytesIO(raw),
                "filename": out_filename(orig_name, ext),
                "content_type": out_ct,
                "size": len(raw),
            }],
        }

    # ---- CP6.3: Decode + orient (alpha kept: RGBA stays RGBA) ----
    img, has_alpha = _decode(tool_input["stream"])

    # ---- CP6.4: "auto" — same-format smart re-encode ----
    if preset == "auto":
        try:
            if ct == "image/jpeg":
                raw = _save_jpeg(img, 82)
                if not orig_size or len(raw) < orig_size:
                    return _result(raw, "jpg", "image/jpeg")
                return _passthrough()
            if ct == "image/png":
                # quality order: lossless optimize first, quantized second;
                # the first candidate landing ≤ 70% of the original wins
                ranked = [(_save_png(img), "lossless")]
                try:
                    ranked.append((_save_png(_quantize(img)), "quantized"))
                except Exception:
                    pass
                for raw, _kind in ranked:
                    if not orig_size or len(raw) <= orig_size * AUTO_PNG_GAIN:
                        return _result(raw, "png", "image/png")
                best = min(ranked, key=lambda c: len(c[0]))
                if not orig_size or len(best[0]) < orig_size:
                    return _result(best[0], "png", "image/png")
                return _passthrough()
            # other formats: no auto rule — the original is the answer
            return _passthrough()
        finally:
            img.close()
            gc.collect()

    # ---- CP6.5: byte-budget presets ----
    target = TARGETS[preset]

    # Already under budget: still try the auto rules before returning
    # the original unchanged.
    if orig_size and orig_size <= target:
        if ct in ("image/jpeg", "image/png"):
            tool_input["stream"].seek(0)
            sub = dict(tool_input)
            sub["stream"] = io.BytesIO(tool_input["stream"].read())
            out = compress_handler(sub, {"preset": "auto"})
            if out["items"][0]["size"] < orig_size:
                return out
        img.close()
        return _passthrough()

    # Transparent images: PNG only (quantize → downscale if needed).
    # Never JPEG, never alpha loss.
    if has_alpha:
        try:
            if max(img.size) > MAX_DIM:
                img.thumbnail((MAX_DIM, MAX_DIM), Image.LANCZOS)
            best = _save_png(_quantize(img))
            cur_w, cur_h = img.size
            while len(best) > target and max(cur_w, cur_h) > 480:
                cur_w = max(480, int(cur_w * 0.75))
                cur_h = max(480, int(cur_h * 0.75))
                scaled = img.resize((cur_w, cur_h), Image.LANCZOS)
                best = _save_png(_quantize(scaled))
            return _result(best, "png", "image/png")
        finally:
            img.close()
            gc.collect()

    # Opaque images: binary-search JPEG quality to hit the target.
    if max(img.size) > MAX_DIM:
        img.thumbnail((MAX_DIM, MAX_DIM), Image.LANCZOS)
    w, h = img.size

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
    return _result(raw, "jpg", "image/jpeg")


# ── Registration ──

_PRESET_LABELS = {
    "auto": {"en": "Smart (recommended)", "zh": "智能压缩（推荐）", "ja": "スマート（推奨）", "ko": "스마트 압축(추천)", "es": "Inteligente (recomendado)", "ar": "ذكي (موصى به)", "de": "Smart (empfohlen)", "ru": "Умное (рекомендуется)", "pt": "Inteligente (recomendado)", "fr": "Intelligent (recommandé)"},
    "1mb":  {"en": "≤ 1 MB", "zh": "≤ 1 MB", "ja": "≤ 1 MB", "ko": "≤ 1 MB", "es": "≤ 1 MB", "ar": "≤ 1 MB", "de": "≤ 1 MB", "ru": "≤ 1 МБ", "pt": "≤ 1 MB", "fr": "≤ 1 Mo"},
    "2mb":  {"en": "≤ 2 MB", "zh": "≤ 2 MB", "ja": "≤ 2 MB", "ko": "≤ 2 MB", "es": "≤ 2 MB", "ar": "≤ 2 MB", "de": "≤ 2 MB", "ru": "≤ 2 МБ", "pt": "≤ 2 MB", "fr": "≤ 2 Mo"},
    "5mb":  {"en": "≤ 5 MB", "zh": "≤ 5 MB", "ja": "≤ 5 MB", "ko": "≤ 5 MB", "es": "≤ 5 MB", "ar": "≤ 5 MB", "de": "≤ 5 MB", "ru": "≤ 5 МБ", "pt": "≤ 5 MB", "fr": "≤ 5 Mo"},
    "10mb": {"en": "≤ 10 MB","zh": "≤ 10 MB","ja": "≤ 10 MB","ko": "≤ 10 MB","es": "≤ 10 MB","ar": "≤ 10 MB","de": "≤ 10 MB","ru": "≤ 10 МБ","pt": "≤ 10 MB","fr": "≤ 10 Mo"},
}

register(
    tool_id="compress",
    label={
        "en": "Compress", "zh": "图片压缩", "ja": "画像圧縮",
        "ko": "이미지 압축", "es": "Comprimir imagen", "ar": "ضغط الصورة",
        "de": "Bild komprimieren", "ru": "Сжатие фото", "pt": "Comprimir imagem",
        "fr": "Compresser l'image",
    },
    desc={
            "fr": 'Reduire la taille de l\'image avec controle qualite.',
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
         "default": "auto"},
    ],
)
