#!/usr/bin/env python3
"""Tool chain — Local/Remote execution → Result settlement: every system
tool end to end, launch-stability guards, failure boundaries of the
execution pool, and result settlement atomicity.
"""
from _harness import REPO, TEST_PORT, check, finish, purge_users, section  # fail-closed gate runs on import


# ════════════════════════════════════════════════════════════════════════
# System tools execution
#
# System tools execution canonical.
#
# Every first-party tool registered via tools.load_all() must run end-to-end
# through the REAL execution.worker.process_tool with fixed in-test fixtures:
#
#     - returns the IO-1 {"items": [...]} shape
#     - every item kind is inside the tool's declared outputs
#     - every file item decodes (images open, PDF starts with %PDF, ZIP
#       lists entries, ICO exposes its size table)
#
# Specialized assertions pin the per-tool contracts (privacy metadata
# stripping / format keeping / PSNR floor, compress auto+targets with
# transparency survival, ico multi sizes, crop_white bbox).
# A newly registered tool without a fixture FAILS this suite — real-run
# coverage is mandatory for every system tool.
# ════════════════════════════════════════════════════════════════════════
section('System tools execution')

import io
import math
import os
import random
import sys
import zipfile

import os as _os
import sys as _sys


from PIL import Image, ImageChops, ImageDraw, ImageStat

from tools import load_all, get_tool, ToolError
from governance.tool_params import enforce_params_decl
from execution.worker import process_tool

load_all()


# ═══════════════════════ fixture builders (deterministic) ═══════════════

def _photo_bytes(w=800, h=600, sigma=4, seed=42):
    """Photo-like RGB content: smooth gradients + seeded sensor noise."""
    rng = random.Random(seed)
    img = Image.new("RGB", (w, h))
    px = img.load()
    for y in range(h):
        for x in range(0, w, 4):
            c = (int(80 + 60 * x / w), int(40 + 120 * y / h), int(160 - 80 * x / w))
            for dx in range(4):
                if x + dx < w:
                    n = rng.gauss(0, sigma)
                    px[x + dx, y] = (max(0, min(255, int(c[0] + n))),
                                     max(0, min(255, int(c[1] + n))), c[2])
    d = ImageDraw.Draw(img)
    d.ellipse([w * .3, h * .25, w * .7, h * .75], outline=(255, 230, 90), width=5)
    d.rectangle([w * .1, h * .1, w * .2, h * .9], fill=(30, 200, 120))
    return img


def photo_jpeg(w=800, h=600, quality=95, sigma=4, seed=42):
    buf = io.BytesIO()
    _photo_bytes(w, h, sigma, seed).save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def save_png(img):
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def opaque_png(w=900, h=700, seed=11):
    return save_png(_photo_bytes(w, h, sigma=8, seed=seed))


def alpha_png(w=300, h=200):
    """Logo-like RGBA: large fully-transparent regions + solid + semi."""
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([40, 40, 260, 160], fill=(200, 40, 40, 255))
    d.rectangle([60, 60, 240, 140], fill=(255, 220, 90, 128))
    for x in range(w):
        d.line([(x, 0), (x, 10)], fill=(30, 90, 200, x * 255 // w))
    return save_png(img)


def big_alpha_png():
    """>1MB noisy RGBA (noise tile pasted, fast to build)."""
    rng = random.Random(5)
    tile_src = Image.new("RGBA", (64, 64))
    tp = tile_src.load()
    for y in range(64):
        for x in range(64):
            tp[x, y] = (rng.randrange(256), rng.randrange(256),
                        rng.randrange(256), rng.randrange(256))
    img = Image.new("RGBA", (1200, 900))
    for ty in range(0, 900, 64):
        for tx in range(0, 1200, 64):
            img.paste(tile_src, (tx, ty))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, 200, 900], fill=(0, 0, 0, 0))  # transparent band
    return save_png(img)


def white_bordered_png():
    """220x160 content centered in a 300x240 white canvas."""
    img = Image.new("RGB", (300, 240), (255, 255, 255))
    d = ImageDraw.Draw(img)
    d.rectangle([40, 50, 259, 209], fill=(10, 120, 220))
    d.ellipse([120, 100, 180, 160], fill=(250, 200, 40))
    return save_png(img)


def badge_png():
    img = Image.new("RGBA", (64, 40), (0, 0, 0, 0))
    ImageDraw.Draw(img).ellipse([4, 4, 59, 35], fill=(230, 60, 60, 255))
    return save_png(img)


def one_page_pdf(rgb=(30, 90, 200)):
    buf = io.BytesIO()
    Image.new("RGB", (120, 160), rgb).save(buf, "PDF")
    return buf.getvalue()


def _jpeg_with_exif_xmp():
    """JPEG bytes carrying an EXIF APP1 (Software tag) and an XMP APP1."""
    raw = photo_jpeg(quality=92, sigma=4, seed=42)
    value = b"SystemToolsProbe"
    tiff = b"II*\x00" + b"\x08\x00\x00\x00"          # TIFF header, IFD0 @8
    tiff += b"\x01\x00"                              # 1 IFD entry
    tiff += b"\x0e\x01\x02\x00" + len(value).to_bytes(4, "little") \
        + (8 + 2 + 12 + 4).to_bytes(4, "little")     # Software, ASCII
    tiff += b"\x00\x00\x00\x00"                      # next IFD = none
    tiff += value
    exif_app1 = b"\xff\xe1" + (6 + len(tiff) + 2).to_bytes(2, "big") \
        + b"Exif\x00\x00" + tiff
    xmp = (b'<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?>'
           b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF'
           b' xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
           b'<rdf:Description>probe</rdf:Description></rdf:RDF></x:xmpmeta>'
           b'<?xpacket end="w"?>')
    xmp_app1 = b"\xff\xe1" + (len(xmp) + 2).to_bytes(2, "big") + xmp
    soi = raw[:2]
    return soi + exif_app1 + xmp_app1 + raw[2:]


def psnr_rgb(a, b):
    """PSNR (dB) between two same-size images, RGB channels."""
    if a.size != b.size:
        b = b.resize(a.size, Image.LANCZOS)
    a, b = a.convert("RGB"), b.convert("RGB")
    st = ImageStat.Stat(ImageChops.difference(a, b))
    mse = sum(r * r for r in st.rms) / 3.0
    if mse == 0:
        return float("inf")
    return 10 * math.log10(255 * 255 / mse)


# ═══════════════════════ fixture registry ═══════════════════════════════
# tool_id → [ {files: [(bytes, name, ct)], params: {...}}, ... ]
# EVERY registered first-party tool must appear here at least once.

FIXTURES = {
    "share": [
        {"files": [(b"alpha-bytes-1", "a.txt", "text/plain"),
                   (b"beta-bytes-22", "b.txt", "text/plain")], "params": {}},
    ],
    "privacy": [
        {"files": [(_jpeg_with_exif_xmp(), "photo.jpg", "image/jpeg")],
         "params": {"preset": "light"}},
        {"files": [(alpha_png(), "logo.png", "image/png")],
         "params": {"preset": "standard"}},
        {"files": [(badge_png(), "badge.png", "image/png")],
         "params": {"preset": "max"}},
        {"files": [(save_png(_photo_bytes(4200, 300, sigma=4, seed=9)),
                    "wide.png", "image/png")],
         "params": {"preset": "light"}},
    ],
    "compress": [
        {"files": [(photo_jpeg(), "shot.jpg", "image/jpeg")],
         "params": {"preset": "auto"}},
        {"files": [(opaque_png(), "flat.png", "image/png")],
         "params": {"preset": "auto"}},
        {"files": [(big_alpha_png(), "logo-big.png", "image/png")],
         "params": {"preset": "1mb"}},
        {"files": [(photo_jpeg(), "small.jpg", "image/jpeg")],
         "params": {"preset": "1mb"}},  # already under target → auto rules
    ],
    "beautify": [
        {"files": [(badge_png(), "badge.png", "image/png")],
         "params": {}},
        {"files": [(badge_png(), "story.png", "image/png")],
         "params": {"preset": "blur", "aspect": "9:16"}},
    ],
    "crop_white": [
        {"files": [(white_bordered_png(), "scan.png", "image/png")],
         "params": {}},
    ],
    "ico": [
        {"files": [(badge_png(), "mark.png", "image/png")],
         "params": {"preset": "multi"}},
        {"files": [(badge_png(), "mark.png", "image/png")],
         "params": {"preset": "ico_32"}},
    ],
    "img2pdf": [
        {"files": [(badge_png(), "a.png", "image/png"),
                   (opaque_png(w=200, h=150, seed=3), "b.png", "image/png")],
         "params": {}},
    ],
    "pdf_merge": [
        {"files": [(one_page_pdf(), "one.pdf", "application/pdf"),
                   (one_page_pdf((200, 60, 60)), "two.pdf", "application/pdf")],
         "params": {}},
    ],
    "zip_pack": [
        {"files": [(b"first-payload", "one.txt", "text/plain"),
                   (b"second-payload", "two.log", "text/plain")],
         "params": {}},
    ],
}

# Extra per-target-preset runs (each must land ≤ its byte budget)
_big_alpha = big_alpha_png()
for _preset in ("2mb", "5mb", "10mb"):
    FIXTURES["compress"].append(
        {"files": [(_big_alpha, "logo-big.png", "image/png")],
         "params": {"preset": _preset}})


# ═══════════════════════ generic runner + assertions ════════════════════

def decode_check(item):
    """Decode one file item by content family; returns error string or None."""
    data = item["data"]
    ct = item["content_type"]
    try:
        if ct.startswith("image/"):
            img = Image.open(io.BytesIO(data))
            img.load()
            if img.size[0] < 1 or img.size[1] < 1:
                return "decoded image has empty dimensions"
            return None
        if ct == "application/pdf":
            if not data.startswith(b"%PDF"):
                return "PDF magic missing"
            from pypdf import PdfReader
            if len(PdfReader(io.BytesIO(data)).pages) < 1:
                return "PDF has no pages"
            return None
        if ct == "application/zip":
            names = zipfile.ZipFile(io.BytesIO(data)).namelist()
            if not names:
                return "empty ZIP"
            return None
        return None
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"


def run_tool(tool_id, fx):
    tool = get_tool(tool_id)
    params = enforce_params_decl(tool.get("params") or [], fx.get("params") or {})
    sources = [(data, name, ct, len(data)) for data, name, ct in fx["files"]]
    return tool, process_tool(tool_id, sources, params, None)


def run_generic(tool_id):
    """Run every fixture of one tool with the generic contract assertions."""
    for i, fx in enumerate(FIXTURES[tool_id]):
        label = f"{tool_id}[{i}]"
        try:
            tool, out = run_tool(tool_id, fx)
        except ToolError as exc:
            check(f"{label} executes", False, f"ToolError {exc}")
            continue
        except Exception as exc:
            check(f"{label} executes", False, f"{type(exc).__name__}: {exc}")
            continue
        check(f"{label} items shape",
              isinstance(out, dict) and isinstance(out.get("items"), list)
              and len(out["items"]) >= 1, str(out)[:100])
        declared = set(tool.get("outputs") or ("file",))
        kinds_ok = all(it.get("kind") in declared for it in out["items"])
        check(f"{label} kinds ⊆ declared outputs {sorted(declared)}", kinds_ok,
              str([it.get("kind") for it in out["items"]]))
        for j, item in enumerate(out["items"]):
            if item["kind"] != "file":
                continue
            err = decode_check(item)
            check(f"{label} file[{j}] decodes ({item['filename']})", err is None, err or "")


# ═══════════════════════ specialized assertions ═════════════════════════

def spec_privacy():
    # metadata-free output for every preset
    raws = {}
    for preset in ("light", "standard", "strong", "max"):
        _, out = run_tool("privacy", {"files": [(_jpeg_with_exif_xmp(), "photo.jpg", "image/jpeg")],
                                      "params": {"preset": preset}})
        it = out["items"][0]
        raws[preset] = it["data"]
        img = Image.open(io.BytesIO(it["data"]))
        check(f"privacy/{preset} JPEG→JPEG", it["content_type"] == "image/jpeg"
              and img.format == "JPEG", f"{img.format} {it['content_type']}")
        # 产出名规范: 原名 + 4 位短随机 (tools.out_filename 单源)
        import re as _re
        check(f"privacy/{preset} filename = stem + short random",
              bool(_re.fullmatch(r"photo_[0-9a-f]{4}\.jpg", it["filename"])),
              it["filename"])
        no_exif = (b"Exif\x00\x00" not in it["data"]
                   and b"xpacket" not in it["data"]
                   and b"ICC_PROFILE" not in it["data"]
                   and img.info.get("exif") is None
                   and img.info.get("icc_profile") is None
                   and img.info.get("XMP") is None
                   and img.info.get("comment") is None)
        check(f"privacy/{preset} strips EXIF/XMP/ICC/comment", no_exif,
              str({k: bool(v) for k, v in img.info.items() if k != "dpi"})[:120])
    # PSNR floors (1:1, same dimensions as the decoded original)
    orig = Image.open(io.BytesIO(_jpeg_with_exif_xmp())).convert("RGB")
    for preset, floor in (("light", 35.0), ("max", 28.0)):
        img = Image.open(io.BytesIO(raws[preset]))
        val = psnr_rgb(orig, img)
        check(f"privacy/{preset} PSNR ≥ {floor:.0f}dB", val >= floor, f"{val:.1f}dB")

    # PNG transparency survives, dimensions kept 1:1
    _, out = run_tool("privacy", {"files": [(alpha_png(), "logo.png", "image/png")],
                                  "params": {"preset": "standard"}})
    it = out["items"][0]
    img = Image.open(io.BytesIO(it["data"]))
    src = Image.open(io.BytesIO(alpha_png()))
    check("privacy PNG→PNG", it["content_type"] == "image/png" and img.format == "PNG",
          f"{img.format} {it['content_type']}")
    check("privacy PNG alpha kept (mode RGBA + transparent px stay 0)",
          img.mode == "RGBA" and img.size == src.size
          and img.getchannel("A").getextrema()[0] == 0
          and src.getchannel("A").getextrema()[0] == 0,
          f"mode={img.mode} alpha_min={img.getchannel('A').getextrema()[0]}")

    # longest-edge cap: 4200px wide input → ≤4096, still PNG
    _, out = run_tool("privacy", {"files": [(save_png(_photo_bytes(4200, 300, sigma=4, seed=9)),
                                             "wide.png", "image/png")],
                                  "params": {"preset": "light"}})
    img = Image.open(io.BytesIO(out["items"][0]["data"]))
    check("privacy caps longest edge at 4096", max(img.size) <= 4096, str(img.size))

    # other formats: no-alpha → JPEG, alpha → PNG
    bmp = io.BytesIO()
    Image.new("RGB", (60, 40), (10, 200, 30)).save(bmp, "BMP")
    _, out = run_tool("privacy", {"files": [(bmp.getvalue(), "x.bmp", "image/bmp")],
                                  "params": {"preset": "standard"}})
    check("privacy BMP(no alpha)→JPEG", out["items"][0]["content_type"] == "image/jpeg"
          and bool(__import__("re").fullmatch(r"x_[0-9a-f]{4}\.jpg",
                out["items"][0]["filename"])), out["items"][0]["filename"])


def spec_compress():
    # auto on JPEG: same format, strictly smaller, decodes
    fx = {"files": [(photo_jpeg(), "shot.jpg", "image/jpeg")], "params": {"preset": "auto"}}
    _, out = run_tool("compress", fx)
    it = out["items"][0]
    check("compress/auto JPEG smaller + stays JPEG",
          it["content_type"] == "image/jpeg" and len(it["data"]) < len(fx["files"][0][0]),
          f"{len(fx['files'][0][0])}→{len(it['data'])} {it['content_type']}")

    # auto on opaque PNG: smaller
    png = opaque_png()
    _, out = run_tool("compress", {"files": [(png, "flat.png", "image/png")],
                                   "params": {"preset": "auto"}})
    it = out["items"][0]
    check("compress/auto PNG smaller", it["content_type"] == "image/png"
          and len(it["data"]) < len(png), f"{len(png)}→{len(it['data'])}")

    # already-under-target file: re-encoded smaller, NOT passthrough
    small = photo_jpeg()
    _, out = run_tool("compress", {"files": [(small, "small.jpg", "image/jpeg")],
                                   "params": {"preset": "1mb"}})
    it = out["items"][0]
    check("compress/1mb under-target JPEG still shrinks",
          len(it["data"]) < len(small), f"{len(small)}→{len(it['data'])}")

    # every byte-budget preset lands ≤ target with transparency intact
    src = Image.open(io.BytesIO(_big_alpha))
    zero_band = [(x, y) for x in range(0, 200) for y in range(0, 900, 5)
                 if src.getpixel((x, y))[3] == 0]
    for preset, target in (("1mb", 1_048_576), ("2mb", 2_097_152),
                           ("5mb", 5_242_880), ("10mb", 10_485_760)):
        _, out = run_tool("compress", {"files": [(_big_alpha, "logo-big.png", "image/png")],
                                       "params": {"preset": preset}})
        it = out["items"][0]
        img = Image.open(io.BytesIO(it["data"])).convert("RGBA")
        kept = max(img.getpixel(p)[3] for p in zero_band)
        check(f"compress/{preset} ≤ budget + PNG + alpha ≤ 8 on transparent px",
              len(it["data"]) <= target and it["content_type"] == "image/png"
              and img.size == src.size and kept <= 8,
              f"{len(it['data'])}B {it['content_type']} max_alpha={kept}")


def spec_ico():
    _, out = run_tool("ico", {"files": [(badge_png(), "mark.png", "image/png")],
                              "params": {"preset": "multi"}})
    img = Image.open(io.BytesIO(out["items"][0]["data"]))
    sizes = sorted(img.info.get("sizes") or [])
    want = [(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    check("ico/multi carries all six sizes", sizes == want, str(sizes))

    _, out = run_tool("ico", {"files": [(badge_png(), "mark.png", "image/png")],
                              "params": {"preset": "ico_32"}})
    img = Image.open(io.BytesIO(out["items"][0]["data"]))
    sizes = sorted(img.info.get("sizes") or [])
    check("ico/ico_32 carries exactly 32x32", sizes == [(32, 32)], str(sizes))


def spec_crop_white():
    _, out = run_tool("crop_white", {"files": [(white_bordered_png(), "scan.png", "image/png")],
                                     "params": {}})
    it = out["items"][0]
    img = Image.open(io.BytesIO(it["data"]))
    w, h = img.size
    # content bbox is 220x160 (borders 40/50 + anti-alias slack)
    check("crop_white removes white margins", 210 <= w <= 226 and 150 <= h <= 166,
          f"{w}x{h}")
    check("crop_white keeps PNG family", it["content_type"] == "image/png",
          it["content_type"])

    allwhite = io.BytesIO()
    Image.new("RGB", (50, 50), (255, 255, 255)).save(allwhite, "PNG")
    data = allwhite.getvalue()
    _, out = run_tool("crop_white", {"files": [(data, "w.png", "image/png")],
                                     "params": {}})
    check("crop_white all-white passthrough (unchanged bytes)",
          out["items"][0]["data"] == data, "")


def PdfPages(data):
    from pypdf import PdfReader
    return PdfReader(io.BytesIO(data)).pages


def spec_multi_file_tools():
    # share: byte-identical passthrough, one item per source
    _, out = run_tool("share", {"files": [(b"alpha-bytes-1", "a.txt", "text/plain"),
                                          (b"beta-bytes-22", "b.txt", "text/plain")],
                                "params": {}})
    payload = [(it["filename"], bytes(it["data"])) for it in out["items"]]
    check("share passthrough identical items",
          payload == [("a.txt", b"alpha-bytes-1"), ("b.txt", b"beta-bytes-22")],
          str(payload)[:80])

    # img2pdf: one page per image
    _, out = run_tool("img2pdf", {"files": [(badge_png(), "a.png", "image/png"),
                                             (opaque_png(w=200, h=150, seed=3), "b.png", "image/png")],
                                  "params": {}})
    check("img2pdf 2 images → 2 pages", len(PdfPages(out["items"][0]["data"])) == 2, "")

    # pdf_merge: page count = sum
    _, out = run_tool("pdf_merge", {"files": [(one_page_pdf(), "one.pdf", "application/pdf"),
                                              (one_page_pdf((200, 60, 60)), "two.pdf", "application/pdf")],
                                    "params": {}})
    check("pdf_merge 2×1page → 2 pages", len(PdfPages(out["items"][0]["data"])) == 2, "")

    # zip_pack: every source reachable in the archive
    _, out = run_tool("zip_pack", {"files": [(b"first-payload", "one.txt", "text/plain"),
                                             (b"second-payload", "two.log", "text/plain")],
                                   "params": {}})
    zf = zipfile.ZipFile(io.BytesIO(out["items"][0]["data"]))
    names = set(zf.namelist())
    check("zip_pack contains every source", {"one.txt", "two.log"} <= names, str(names))


def spec_beautify():
    def _run(params):
        _, out = run_tool("beautify", {"files": [(badge_png(), "badge.png", "image/png")],
                                       "params": params})
        return Image.open(io.BytesIO(out["items"][0]["data"]))

    src = Image.open(io.BytesIO(badge_png()))
    img = _run({})
    pad_x = (img.size[0] - src.size[0]) / 2
    pad_y = (img.size[1] - src.size[1]) / 2
    check("beautify original: symmetric padding around the unscaled shot",
          pad_x == pad_y and pad_x >= 32, f"{src.size} -> {img.size}")
    # social canvases: fixed export sizes (platform-native, never re-cropped)
    for aspect, size in {"1:1": (1080, 1080), "4:5": (1080, 1350),
                         "3:4": (1080, 1440), "9:16": (1080, 1920),
                         "16:9": (1600, 900)}.items():
        got = _run({"aspect": aspect}).size
        check(f"beautify {aspect} → {size[0]}x{size[1]} export", got == size, str(got))
    # the declaration carries no text overlay parameters
    from tools import _registry
    keys = [p["key"] for t in _registry if t["id"] == "beautify"
            for p in (t.get("params") or [])]
    check("beautify params = style + canvas only", keys == ["preset", "aspect"], str(keys))


# ═══════════════════════ main ═══════════════════════════════════════════

# coverage guard: a registered tool without fixtures fails the suite
from tools import _registry
_ids = [t["id"] for t in _registry]
missing = [tid for tid in _ids if tid not in FIXTURES]
check("every registered first-party tool has a real-run fixture", not missing,
      f"missing fixtures: {missing}")
stale = [tid for tid in FIXTURES if tid not in _ids]
check("fixtures only reference registered tools", not stale, f"stale: {stale}")

for tid in _ids:
    if tid in FIXTURES:
        run_generic(tid)

spec_privacy()
spec_compress()
spec_ico()
spec_crop_white()
spec_multi_file_tools()
spec_beautify()


# ════════════════════════════════════════════════════════════════════════
# Launch-stability guards
#
# Launch hardening canonical — the seven launch-stability guards.
#
#   H1*  Remote results carry a CUMULATIVE file-bytes ceiling
#        (config MAX_BATCH_BYTES): exceeding it raises RemoteError
#        'too_large' and no further item is fetched or decoded (both the
#        url and data_b64 paths count).
#   H2*  One local handler run is bounded by LOCAL_EXEC_TIMEOUT_S (120):
#        a wedged worker times out into pool reset + old-worker
#        terminate and the EXISTING err_endpoint_timeout refund path;
#        the in-flight slot returns to zero and the next call executes.
#   H4*  The tiny-form surfaces (/api/client-error, /auth/login,
#        /auth/register) reject bodies over 64KB with 413
#        err_payload_too_large before the concurrency semaphore.
#   H5*  Receipts stuck running are settled by the hourly housekeeping
#        sweep (older_than_s=1800): stale rows settle failed with their
#        exact usage released; fresh receipts stay running.
#   H6*  NaN/Inf number params reject (param_invalid); notifications are
#        never answered (including ping) and the tools/list cursor maps
#        Infinity to -32602; the password cap is bcrypt's own 72 UTF-8
#        bytes on the single validate_password point (register/change/
#        set all share it).
#   H7*  QR minting endpoints: payloads beyond the fixed encoder capacity
#        (78 bytes) answer typed 400/404 — never a 500; the site QR mints
#        same-site paths only; QR responses are cacheable.
#
# Fail-closed env guard; per-run unique names; fixtures cleaned at end.
# ════════════════════════════════════════════════════════════════════════
section('Launch-stability guards')

import asyncio
import base64
import concurrent.futures as _cf
import hashlib
import http.client as _hc
import json as _json
import os as _os
import sys as _sys
import time as _ts
import uuid as _uuid


_HOST = ("127.0.0.1", int(TEST_PORT))
_u = "lh" + _uuid.uuid4().hex[:6]
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9Q"
    "DwADhgGAWjR9awAAAABJRU5ErkJggg==")


def _req(method, path, body=None, headers=None):
    conn = _hc.HTTPConnection(*_HOST, timeout=120)
    payload = None
    if body is not None:
        payload = body if isinstance(body, bytes) else _json.dumps(body).encode()
    conn.request(method, path, payload, dict(headers or {}))
    r = conn.getresponse()
    data = r.read()
    hdrs = {k.lower(): v for k, v in r.getheaders()}
    setc = r.getheader("Set-Cookie") or ""
    conn.close()
    try:
        return r.status, hdrs, (_json.loads(data) if data else None), setc
    except Exception:
        return r.status, hdrs, data.decode(errors="replace"), setc


def _form(fields):
    out = b""
    for k, v in fields.items():
        out += (f'--B\r\nContent-Disposition: form-data; name="{k}"\r\n'
                f'\r\n{v}\r\n').encode()
    return out + b"--B--\r\n", "multipart/form-data; boundary=B"


def _mcp_raw(raw_body: bytes, token: str):
    conn = _hc.HTTPConnection(*_HOST, timeout=120)
    conn.request("POST", "/mcp", raw_body, {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {token}"})
    r = conn.getresponse()
    data = r.read()
    conn.close()
    try:
        return r.status, (_json.loads(data) if data else None)
    except Exception:
        return r.status, None


def _register(name):
    b, ct = _form({"username": name, "password": "Test12345",
                   "password_confirm": "Test12345", "email": f"{name}@t.co"})
    _req("POST", "/auth/register", b, {"Content-Type": ct})
    b, ct = _form({"login": name, "password": "Test12345"})
    _st, _h, _d, sc = _req("POST", "/auth/login", b, {"Content-Type": ct})
    ck = sc.split(";")[0]
    _st, _h, d_k, _sc = _req("POST", "/api/keys", {"name": "lh"},
                             {"Content-Type": "application/json", "Cookie": ck})
    from database import db
    with db() as con:
        uid = int(con.execute("select id from users where username=%s",
                              (name,)).fetchone()["id"])
    return uid, ck, d_k["key"]["key"]


uid = None
try:
    uid, ck, TOK = _register(_u + "a")
    check("F0 fixture registered", uid and TOK, uid)

    # ═══ H1. cumulative remote file-bytes ceiling ═══
    import remote as _remote
    _CAP = 1000
    _orig_cap = _remote.MAX_BATCH_BYTES
    _remote.MAX_BATCH_BYTES = _CAP
    _fetches = []

    def _spy_fetch(url):
        _fetches.append(url)
        return PNG + b"\x00" * (100 - len(PNG))

    try:
        _out = {"items": [
            {"kind": "file", "filename": f"x{i}.png",
             "content_type": "image/png",
             "url": f"https://ep.example/{i}.png"}
            for i in range(1, 21)]}
        try:
            _remote.parse_endpoint_items(_out, _spy_fetch)
            _code = "no-error"
        except _remote.RemoteError as exc:
            _code = exc.code
        check("H1 cumulative overage raises too_large", _code == "too_large", _code)
        check("H1 items after the breach are never fetched",
              len(_fetches) == 11, len(_fetches))
        # under the ceiling everything still lands
        _fetches.clear()
        _small = {"items": [
            {"kind": "file", "filename": f"s{i}.png",
             "content_type": "image/png",
             "url": f"https://ep.example/s{i}.png"}
            for i in range(3)]}
        _res = _remote.parse_endpoint_items(_small, _spy_fetch)
        check("H1 under the ceiling all items land",
              len(_fetches) == 3 and [r["size"] for r in _res] == [100] * 3,
              (len(_fetches), len(_res)))
    finally:
        _remote.MAX_BATCH_BYTES = _orig_cap

    # ═══ H2. local execution timeout → reset/terminate/refund path ═══
    import execution.orchestrator as _orch
    from execution.gate import run as gate_run
    from execution.invocation import ToolInvocation
    from execution.results import StagedItems
    from execution import ExecutionError
    from execution.pool import inflight_count
    from governance.catalog import resolve as _resolve

    check("H2a LOCAL_EXEC_TIMEOUT_S contract is 120",
          _orch.LOCAL_EXEC_TIMEOUT_S == 120, _orch.LOCAL_EXEC_TIMEOUT_S)

    class _StuckExecutor:
        def submit(self, fn, *a, **k):
            return _cf.Future()  # never resolves

    async def _stuck():
        return await _orch._run_handler(_StuckExecutor(), lambda *a: None,
                                        "share", [], {})

    async def _ok():
        return StagedItems(order=["text"],
                           inline=[{"kind": "text", "text": "ok"}])

    def _inv(ref):
        return ToolInvocation("share", [], {}, {"expiry_days": 1,
                                                "allowed_downloads": 50}, ref)

    tool, terr = _resolve("share", uid)
    tool["id"] = tool["tool_key"]
    check("H2b fixture: share is a local tool", terr is None
          and tool.get("execution") == "local", (terr, tool and tool.get("execution")))

    _orig_to = _orch.LOCAL_EXEC_TIMEOUT_S
    _orch.LOCAL_EXEC_TIMEOUT_S = 0.5
    try:
        _p, err, status = asyncio.run(
            gate_run({"id": uid}, tool, _inv(f"lh-h2-{_u}"), _stuck))
    finally:
        _orch.LOCAL_EXEC_TIMEOUT_S = _orig_to
    check("H2c wedged handler → err_endpoint_timeout (existing refund path)",
          status == 400 and err.get("error") == "err_endpoint_timeout"
          and err.get("ok") is False, (status, err))
    check("H2d in-flight slot released after the timeout",
          inflight_count() == 0, inflight_count())
    from database import db
    with db() as con:
        _row = con.execute(
            "select status from tool_calls where user_id=%s and client_ref=%s",
            (uid, f"lh-h2-{_u}")).fetchone()
    check("H2e the attempt is finalized failed", _row and _row["status"] == "failed",
          _row and dict(_row))
    _p, err, _s = asyncio.run(
        gate_run({"id": uid}, tool, _inv(f"lh-h2b-{_u}"), _ok))
    check("H2f the next call executes normally",
          err is None and _p.get("ok") is True and inflight_count() == 0,
          (err, inflight_count()))

    # ═══ H4. tiny-form bodies capped at 64KB → 413 ═══
    _big = b"x" * 70000
    st, _h, d, _sc = _req("POST", "/api/client-error", _big,
                          {"Content-Type": "application/json"})
    check("H4a oversized /api/client-error → 413 err_payload_too_large",
          st == 413 and d.get("ok") is False
          and d.get("error") == "err_payload_too_large", (st, d))
    st, _h, d, _sc = _req("POST", "/auth/login", _big,
                          {"Content-Type": "application/json"})
    check("H4b oversized /auth/login → 413 err_payload_too_large",
          st == 413 and (d or {}).get("error") == "err_payload_too_large", (st, d))
    st, _h, d, _sc = _req("POST", "/auth/register", _big,
                          {"Content-Type": "application/json"})
    check("H4c oversized /auth/register → 413 err_payload_too_large",
          st == 413 and (d or {}).get("error") == "err_payload_too_large", (st, d))
    st, _h, d, _sc = _req("POST", "/api/client-error",
                          b'{"lang":"en","url":"/","msg":"probe"}',
                          {"Content-Type": "application/json"})
    check("H4d normal-size client-error still accepted",
          st == 200 and (d or {}).get("ok") is True, (st, d))

    # ═══ H5. hourly sweep settles stale running receipts ═══
    from sharing.ingress_receipt import claim_ingress_quota
    from sharing.housekeeping import housekeeping_once
    from repo import ingress_requests as _ir

    def _claim(ref, size=100):
        h = hashlib.sha256(ref.encode()).hexdigest()
        receipt, usage_ids, replay = claim_ingress_quota(
            uid, "127.0.0.1", ref, h, [size], scope="lh")
        return int(receipt["id"]), list(usage_ids)

    old_id, old_usage = _claim(f"lh-old-{_u}")
    new_id, new_usage = _claim(f"lh-new-{_u}")
    with db() as con:
        con.execute("update ingress_requests set started_at=%s where id=%s",
                    (int(_ts.time()) - 3600, old_id))
    sweep = housekeeping_once()
    with db() as con:
        _st_old = dict(con.execute(
            "select status from ingress_requests where id=%s",
            (old_id,)).fetchone())
        _st_new = dict(con.execute(
            "select status from ingress_requests where id=%s",
            (new_id,)).fetchone())
        _usage_left = [int(r["id"]) for r in con.execute(
            "select id from upload_usage where user_id=%s", (uid,)).fetchall()]
    check("H5a stale running receipt settled by the hourly sweep",
          sweep.get("receipts_recovered", 0) >= 1 and _st_old["status"] == "failed",
          (sweep, _st_old))
    check("H5b stale receipt's exact usage released",
          not (set(old_usage) & set(_usage_left)), (old_usage, _usage_left))
    check("H5c fresh receipt stays running with its usage intact",
          _st_new["status"] == "running"
          and set(new_usage) <= set(_usage_left), (_st_new, _usage_left))

    # ═══ H6a. NaN/Inf number params reject ═══
    from governance.tool_params import enforce_params_decl, ParamsValueError
    _decl = [{"key": "n", "type": "number", "label": "N", "min": 0, "max": 10}]
    for _v in (float("nan"), float("inf"), float("-inf")):
        try:
            enforce_params_decl(_decl, {"n": _v})
            _res6 = "accepted"
        except ParamsValueError as exc:
            _res6 = str(exc)
        check(f"H6a non-finite {_v} → param_invalid",
              _res6 == "param_invalid", _res6)
    check("H6a finite number passes the same declaration",
          enforce_params_decl(_decl, {"n": 1.5}) == {"n": 1.5},
          enforce_params_decl(_decl, {"n": 1.5}))
    # through the real MCP surface: a user tool with a number param,
    # called with a raw NaN literal (python json accepts it) — the
    # stored-contract enforcement must reject it as param_invalid
    _st, _h, _d, _sc = _req("POST", "/api/usertools", {
        "label": f"Num {_u}",
        "url": "https://93.184.216.34/x", "timeout_s": 30,
        "format_families": "image", "accept": [".png"],
        "files_min": 1, "files_max": 1, "outputs": ["text"],
        "params": [{"key": "n", "type": "number", "label": "N",
                    "min": 0, "max": 10}], "cost": 0},
        {"Content-Type": "application/json", "Cookie": ck})
    _numkey = (_d or {}).get("tool_key")
    check("H6a fixture: number-param tool created", _st == 200 and _numkey, (_st, _d))
    _nan_body = (f'{{"jsonrpc":"2.0","id":30,"method":"tools/call","params":{{'
                 f'"name":"{_numkey}","arguments":{{"params":{{"n":NaN}},'
                 f'"client_ref":"lh6a-{_u}"}}}}}}').encode()
    _st30, _d30 = _mcp_raw(_nan_body, TOK)
    _t30 = (((_d30 or {}).get("result") or {}).get("structuredContent")
            or {})
    check("H6a MCP NaN param → isError param_invalid naming the field",
          (_d30 or {}).get("result", {}).get("isError") is True
          and _t30.get("error") == "param_invalid"
          and _t30.get("field") == "n", str(_d30)[:160])

    # ═══ H6b. notifications never answered; cursor OverflowError ═══
    st_np, d_np = _mcp_raw(b'{"jsonrpc":"2.0","method":"ping"}', TOK)
    check("H6b ping notification → 202, no response body",
          st_np == 202 and d_np is None, (st_np, d_np))
    st_p, d_p = _mcp_raw(b'{"jsonrpc":"2.0","id":31,"method":"ping"}', TOK)
    check("H6b ping request still answered",
          st_p == 200 and ((d_p or {}).get("result") or {}) == {}, (st_p, d_p))
    st_c, d_c = _mcp_raw(b'{"jsonrpc":"2.0","id":32,"method":"tools/list",'
                         b'"params":{"cursor":1e999}}', TOK)
    check("H6b Infinity cursor → -32602 invalid cursor",
          ((d_c or {}).get("error") or {}).get("code") == -32602, str(d_c)[:120])
    st_c2, d_c2 = _mcp_raw(b'{"jsonrpc":"2.0","id":33,"method":"tools/list",'
                           b'"params":{"cursor":0}}', TOK)
    check("H6b cursor 0 still pages fine",
          st_c2 == 200 and ((d_c2 or {}).get("result") or {}).get("tools") is not None,
          st_c2)

    # ═══ H6c. password cap = bcrypt's 72 UTF-8 bytes (single point) ═══
    from identity.auth import validate_password
    check("H6c 72 bytes pass, 73 bytes reject",
          validate_password("a" * 72) is None
          and validate_password("a" * 73) == "err_password_long",
          (validate_password("a" * 72), validate_password("a" * 73)))
    check("H6c multibyte counted in bytes (24 CJK ok, 25 CJK reject)",
          validate_password("漢" * 24) is None
          and validate_password("漢" * 25) == "err_password_long",
          (validate_password("漢" * 24), validate_password("漢" * 25)))
    b, ct = _form({"username": _u + "pw", "password": "漢" * 25,
                   "password_confirm": "漢" * 25,
                   "email": f"{_u}pw@t.co"})
    st_pw, _h, d_pw, _sc = _req("POST", "/auth/register", b, {
        "Content-Type": ct, "Accept": "application/json"})
    check("H6c register with a 75-byte password → typed err_password_long",
          (d_pw or {}).get("error") == "err_password_long", (st_pw, d_pw))

    # ═══ H7. QR surfaces: capacity guard + same-site-only + cacheable ═══
    # The fixed V4-L encoder holds 78 payload bytes and RAISES beyond
    # that; every public minting endpoint must reject out-of-capacity
    # input as a typed 4xx, never surface a 500. The site QR mints
    # same-site paths only (no scheme/protocol-relative/backslash/colon)
    # and all QR responses are cacheable.
    long_p = "/" + "a" * 100
    st, _h, _d, _sc = _req("GET", "/qr.svg?p=" + long_p)
    check("H7a site QR over capacity → 400 (no 500 from the encoder)",
          st == 400, st)
    for bad, why in (("%2F%2Fevil.com", "protocol-relative"),
                     ("https%3A%2F%2Fevil.com", "scheme"),
                     ("%5Cevil", "backslash"),
                     ("javascript%3Aalert(1)", "colon")):
        st, _h, _d, _sc = _req("GET", "/qr.svg?p=" + bad)
        check(f"H7a site QR rejects {why} → 400", st == 400, (bad, st))
    st, h, _d, _sc = _req("GET", "/qr.svg?p=%2F")
    check("H7a site QR home path → 200 svg + Cache-Control",
          st == 200 and "svg" in (h.get("content-type") or "")
          and "max-age" in (h.get("cache-control") or ""), (st, h.get("cache-control")))
    st, _h, _d, _sc = _req("GET", "/tool/compress/qr.svg")
    check("H7b bare-id tool QR → 200", st == 200, st)
    st, _h, _d, _sc = _req("GET", "/tool/no_such_tool_xyz/qr.svg")
    check("H7b unknown tool QR → 404 (page's own surface)",
          st == 404, st)
    st, _h, _d, _sc = _req("GET", "/r/no_such_result/qr.svg")
    check("H7b unknown result QR → 404 (no existence oracle)",
          st == 404, st)

    # ═══ H7c. QR encoder invariants: data-region size + standard samples ═══
    # The V4 (33×33) data region is exactly 807 modules (100 codewords × 8
    # + 7 remainder bits); the historical reservation bug marked two data
    # cells as format area and shrank it to 805, desynchronizing every
    # data bit after them. The three fixed samples (short URL, typical
    # /r/{id} address, 78-byte capacity cap) are generated by the Nayuki
    # QR-Code-generator reference (see the fixture header) and must match
    # the produced matrices cell by cell.
    from utils import _qr_function_modules, qr_svg  # noqa: E402
    _mods, _res = _qr_function_modules(33)
    _free = sum(1 for _row in _res for _c in _row if not _c)
    check("H7c V4 non-reserved modules == 807 (100 codewords × 8 + 7 remainder bits)",
          _free == 807, _free)
    import re as _re
    with open(_os.path.join(REPO, "tests", "support", "qr_v4_samples.json")) as _f:
        _samples = _json.load(_f)
    for _case in _samples["cases"]:
        _grid = [[0] * 33 for _ in range(33)]
        for _m in _re.finditer(r'<rect x="(\d+)" y="(\d+)"', qr_svg(_case["payload"])):
            _x, _y = int(_m.group(1)) // 8 - 4, int(_m.group(2)) // 8 - 4
            if 0 <= _x < 33 and 0 <= _y < 33:
                _grid[_y][_x] = 1
        _diff = sum(1 for _yy in range(33) for _xx in range(33)
                    if _grid[_yy][_xx] != int(_case["matrix"][_yy][_xx]))
        check(f'H7c qr_svg({_case["bytes"]}-byte payload) == reference sample cell by cell',
              _diff == 0, _diff)

    # ═══ H8. Gokapi listing stays off the event loop ═══
    # The uploads listing performs a slow HTTP call to Gokapi; run in the
    # event loop it stalls every concurrent request (healthz included).
    # The fake Gokapi accepts a list-latency injection (/ci/delay): with a
    # 1s listing in flight, a concurrent /healthz must answer < 200ms.
    _gk_base = _os.environ.get("FLASHSHARE_GOKAPI_URL",
                               "http://127.0.0.1:53898").rstrip("/")
    def _goki_delay(ms: int) -> int:
        _c = _hc.HTTPConnection(_gk_base.replace("http://", ""), timeout=5)
        _c.request("POST", "/ci/delay",
                   body=_json.dumps({"ms": ms}).encode(),
                   headers={"Content-Type": "application/json",
                            "apikey": _os.environ.get("FLASHSHARE_GOKAPI_KEY", "")})
        _r = _c.getresponse()
        _r.read()
        _c.close()
        return _r.status
    _uid8, _ck8, _key8 = _register(f"{_u}h8")
    check("H8 fake list delay injected (1s)", _goki_delay(1000) == 200)
    def _pull_uploads():
        st_u, _hu, _du, _scu = _req("GET", "/api/uploads?offset=0",
                                    headers={"Cookie": _ck8})
        return st_u
    _pull_fut = _cf.ThreadPoolExecutor(1).submit(_pull_uploads)
    _t0 = _ts.time()
    st_hz, _hh, _dh, _sch = _req("GET", "/healthz")
    _hz_ms = int((_ts.time() - _t0) * 1000)
    _st_up = _pull_fut.result()
    check("H8 /healthz answers <200ms while a Gokapi listing is in flight",
          st_hz == 200 and _st_up == 200 and _hz_ms < 200,
          f"healthz={st_hz} uploads={_st_up} { _hz_ms }ms")
    check("H8 delay reset to zero", _goki_delay(0) == 200)

    # ═══ I1. image pixel ceiling (64 MP): oversized → typed failure,
    # exact refund, call failed; a normal image executes through the
    # same path. Protects tools.open_image's pre-decode pixel check and
    # the worker RLIMIT_AS pairing. Sample: one 9000×9000 mode-"1" PNG
    # (~10 KB file, 81 MP) + one opaque PNG; compress priced at 50
    # credits for this section, restored after. Deps: live app + worker
    # pool + Postgres; runtime ~4s.
    from database import db as _db_i1
    from governance.pricing import set_pricing as _set_cost_i1
    from economy.billing import grant as _grant_i1
    from economy.billing import spendable_balance as _sb_i1
    _set_cost_i1("compress", 50, True)
    _iu1, _ick1, _ikey1 = _register(f"{_u}i1")
    _grant_i1(_iu1, 5000, "manual_topup", note="i1")
    _bal_i1 = _sb_i1(_iu1)
    import io as _io_i1
    from PIL import Image as _Image_i1
    _buf_i1 = _io_i1.BytesIO()
    _Image_i1.new("1", (9000, 9000), 0).save(_buf_i1, "PNG")
    _OVER_PNG = _buf_i1.getvalue()

    def _compress_i1(payload, fname, ref):
        return _mcp_raw(_json.dumps({
            "jsonrpc": "2.0", "id": 421, "method": "tools/call",
            "params": {"name": "compress", "arguments": {
                "files": [{"filename": fname, "b64": payload}],
                "params": {"preset": "auto"}, "client_ref": ref}}}).encode(),
            _ikey1)

    st, d = _compress_i1(base64.b64encode(_OVER_PNG).decode(),
                         "huge.png", f"i1over-{_u}")
    _sc_i1 = (((d or {}).get("result") or {}).get("structuredContent")
              or {}).get("error")
    with _db_i1() as con:
        _row_i1 = con.execute(
            "select status, error from tool_calls where user_id=%s"
            " and tool_id='compress' order by id desc limit 1",
            (_iu1,)).fetchone()
        _n_ref_i1 = int(con.execute(
            "select count(*) c from ledger where user_id=%s"
            " and kind='refund'", (_iu1,)).fetchone()["c"])
    check("I1a 81 MP image → err_image_too_large, refund exact, call failed",
          st == 200
          and ((d or {}).get("result") or {}).get("isError") is True
          and _sc_i1 == "err_image_too_large"
          and _row_i1 is not None and _row_i1["status"] == "failed"
          and _row_i1["error"] == "err_image_too_large"
          and _n_ref_i1 >= 1 and _sb_i1(_iu1) == _bal_i1,
          (st, _sc_i1, dict(_row_i1) if _row_i1 else None,
           _n_ref_i1, _sb_i1(_iu1), _bal_i1))

    st, d = _compress_i1(base64.b64encode(opaque_png()).decode(),
                         "flat.png", f"i1ok-{_u}")
    _items_i1 = (((d or {}).get("result") or {}).get("structuredContent")
                 or {}).get("items")
    check("I1b normal image executes under the ceiling (charged 50)",
          st == 200 and _items_i1 and _sb_i1(_iu1) == _bal_i1 - 50,
          (st, str(d)[:120], _sb_i1(_iu1)))
    _set_cost_i1("compress", 0, True)
    with _db_i1() as con:
        purge_users(con, [_iu1])
finally:
    if uid:
        from database import db
        with db() as con:
            con.execute("delete from tool_calls where user_id=%s", (uid,))
            con.execute("delete from ledger where user_id=%s", (uid,))
            con.execute("delete from upload_usage where user_id=%s", (uid,))
            con.execute("delete from ingress_requests where user_id=%s", (uid,))
            purge_users(con, [uid])


# ════════════════════════════════════════════════════════════════════════
# Execution failure boundaries
#
# Execution resource canonical — correctness of failure boundaries.
#
# Fault-injection ONLY (no real hogs, no infinite loops, no OOM, no
# process-kill stress): bounded reads fail closed at cap+1 and close the
# stream; source bytes are validated against declared size and budgets
# incrementally; BrokenProcessPool maps to a typed execution failure that
# converges through the existing gate refund path; pool reset under
# concurrency adopts exactly one replacement; a worker-death failure
# converges through the REAL gate (exact charge refund, terminal failed,
# no duplicate refund, no running call, balance restored); remote (http)
# executions are capped globally and per user BEFORE the claim (429 /
# 503, zero tool_call rows, zero charge) and release on settle.
# ════════════════════════════════════════════════════════════════════════
section('Execution failure boundaries')

import os as _os
import sys as _sys


import io
import time
ts = int(time.time())

from config import MAX_UPLOAD_BYTES
from database import db
from execution.orchestrator import _read_source_bounded, _SourceTooLarge
from execution import pool as pool_mod

def _mkuser(name):
    with db() as con:
        row = con.execute(
            "insert into users (username, email, password_hash, display_name,"
            " status, created_at, last_login_at) values (%s, %s, 'x', 'T',"
            " 'active', %s, %s) returning id",
            (name, f"{name}@t.co", ts, ts)).fetchone()
    return int(row["id"])

_u_exb = "eb" + _os.urandom(4).hex()

# ══════════ A. bounded source read (fault injection) ══════════
class _Stream:
    def __init__(self, data):
        self._buf = io.BytesIO(data)
        self.closed = False
    def read(self, n=-1):
        return self._buf.read(n)
    def close(self):
        self.closed = True

s = _Stream(b"x" * 100)
data = _read_source_bounded(s, 100, MAX_UPLOAD_BYTES)
check("A1 bounded read 正常读全 + 关闭", data == b"x" * 100 and s.closed)

s = _Stream(b"x" * 50)
try:
    _read_source_bounded(s, 50, 30)
    check("A2 实际体超 budget → fail closed + 关闭", False, "no raise")
except _SourceTooLarge:
    check("A2 实际体超 budget → fail closed + 关闭", s.closed)

class _NeverRead:
    def read(self, n=-1):
        raise AssertionError("body must not be read")
    def close(self):
        self.closed = True
s2 = _NeverRead()
try:
    _read_source_bounded(s2, MAX_UPLOAD_BYTES + 1, MAX_UPLOAD_BYTES)
    check("A3 声明 size 超 cap → 零读取早拒 + 关闭", False, "no raise")
except _SourceTooLarge:
    check("A3 声明 size 超 cap → 零读取早拒 + 关闭", getattr(s2, "closed", False))

# 实际体大 / 声明小 (DB 谎报) → fail closed
s3 = _Stream(b"x" * 40)
try:
    _read_source_bounded(s3, 5, 30)   # declared 5, actual 40, budget 30
    check("A4 实际体>声明且超 budget → fail closed", False, "no raise")
except _SourceTooLarge:
    check("A4 实际体>声明且超 budget → fail closed", True)

# multi aggregate incremental: (4,4,9) budget 10 → stops before 3rd body
stops = None
total = 0
for i, size in enumerate((4, 4, 9)):
    st = _Stream(b"y" * size)
    try:
        d = _read_source_bounded(st, size, 10 - total)
    except _SourceTooLarge:
        stops = i
        break
    total += len(d)
check("A5 multi 聚合 incremental — 第三个文件前停止", stops == 2, f"stopped_at={stops}")

# ══════════ B. worker bounded output (real worker fn, tiny caps) ══════════
from execution import worker as worker_mod

def _handler_returning(n):
    def _h(tool_input, params):
        return {"items": [{"kind": "file",
                           "stream": io.BytesIO(b"z" * n),
                           "content_type": "t/plain",
                           "filename": "o.bin"}]}
    return _h

import tools as _tools
_tools._registry.append({"id": "zz_at_cap", "handler": _handler_returning(8)})
_tools._registry.append({"id": "zz_cap1", "handler": _handler_returning(9)})
import config as _cfg
_orig_cap = _cfg.MAX_UPLOAD_BYTES
try:
    _cfg.MAX_UPLOAD_BYTES = 8
    r = worker_mod.process_tool("zz_at_cap", [(b"", "i", "t", 0)], {})
    check("B1 output 恰在 cap (8) 成功",
          r["items"][0]["kind"] == "file"
          and len(r["items"][0]["data"]) == 8, str(r)[:80])
    try:
        worker_mod.process_tool("zz_cap1", [(b"", "i", "t", 0)], {})
        check("B2 output cap+1 → ToolError fail closed", False, "no raise")
    except Exception as e:
        # typed contract codes cross the pool boundary as tools.ToolError
        # (dispatch converts to a 4xx); ValueError here would surface 500
        from tools import ToolError as _TE
        check("B2 output cap+1 → ToolError fail closed",
              isinstance(e, _TE) and "output_too_large" in str(e),
              f"{type(e).__name__}: {e}")
finally:
    _cfg.MAX_UPLOAD_BYTES = _orig_cap
    _tools._registry[:] = [t for t in _tools._registry
                           if t["id"] not in ("zz_at_cap", "zz_cap1")]

# ══════════ C. storage bounded (short-read fault injection, tiny cap) ══════════
import storage as _storage_mod
import config as _cfg_mod

class _ShortReadRaw:
    """故意 short-read: 每次 read(n) 最多 2~3 bytes — 证明 chunked 逻辑
    不依赖单次 read(n) 返回 n bytes 的语义。"""
    def __init__(self, body): self._buf = memoryview(body); self._pos = 0
    def read(self, n=-1):
        if n is None or n < 0:
            n = len(self._buf) - self._pos
        take = min(n, 2 + (self._pos % 2))   # 2~3 bytes per call
        chunk = bytes(self._buf[self._pos:self._pos + take])
        self._pos += take
        return chunk
    def close(self): self.closed = True

def _mk_open(body):
    holder = {}
    def _open(fid):
        r = _ShortReadRaw(body)
        holder["raw"] = r
        return 200, {}, r
    return _open, holder

_orig_open = _storage_mod.open_gokapi_internal_download
_orig_cap_c = _cfg_mod.MAX_UPLOAD_BYTES
CAP_C = 32
_cfg_mod.MAX_UPLOAD_BYTES = CAP_C
try:
    _open1, _h1 = _mk_open(b"q" * (CAP_C + 1))
    _storage_mod.open_gokapi_internal_download = _open1
    try:
        _storage_mod.read("f1")
        check("C1 storage cap+1 (short-read) → ValueError + stream 关闭",
              False, "no raise")
    except ValueError:
        check("C1 storage cap+1 (short-read) → ValueError + stream 关闭",
              getattr(_h1["raw"], "closed", False) is True)
    _open2, _h2 = _mk_open(b"q" * CAP_C)
    _storage_mod.open_gokapi_internal_download = _open2
    status_c, _hdrs_c, body_c = _storage_mod.read("f2")
    check("C2 storage exact cap (short-read) → 成功读全 32 bytes + 关闭",
          status_c == 200 and len(body_c.getvalue()) == CAP_C
          and getattr(_h2["raw"], "closed", False) is True,
          f"len={len(body_c.getvalue())}")
finally:
    _storage_mod.open_gokapi_internal_download = _orig_open
    _cfg_mod.MAX_UPLOAD_BYTES = _orig_cap_c

# ══════════ D. BrokenProcessPool: submit / await 注入 + CAS 重建 ══════════
import asyncio
from concurrent.futures.process import BrokenProcessPool
from execution import pool as pool_mod

async def _run_with(pool):
    from execution.orchestrator import _run_handler
    return await _run_handler(pool, lambda *a: None, "t",
                              [(b"", "f", "t", 0)], {})

class _FakePool:
    def __init__(self, tag): self.tag = tag; self.shut = False
    _broken = False
    def shutdown(self, **k): self.shut = True

class _SubmitBroken:          # raises at submit (run_in_executor entry)
    _broken = True
    shut = False
    def shutdown(self, **k): self.shut = True
    def submit(self, *a, **k):
        raise BrokenProcessPool("died at submit")

class _AwaitBroken:           # submit ok, failure surfaces at await
    _broken = True
    shut = False
    def shutdown(self, **k): self.shut = True
    def submit(self, *a, **k):
        import concurrent.futures as _cf
        fut = _cf.Future()
        fut.set_exception(BrokenProcessPool("died at await"))
        return fut

_old_new_factory = pool_mod._new_pool
_replacement_seq = iter(f"R{i}" for i in range(10))

def _install_fake_global(observed):
    """global _pool = observed broken pool; _new_pool → lightweight fake."""
    pool_mod._pool = observed
    pool_mod._new_pool = lambda: _FakePool(next(_replacement_seq))

def _restore_real_pool():
    pool_mod._new_pool = _old_new_factory
    real = pool_mod._new_pool()
    with pool_mod._pool_lock:
        pool_mod._pool = real

# D1: submit 阶段 — observed pool IS the global, CAS 真正触发
sb = _SubmitBroken(); sb.shut = False
_install_fake_global(sb)
# pool_reset_count dead-code check removed (FX-2)

# D2: await 阶段 — 重新安装新 observed pool, 同样断言
ab = _AwaitBroken(); ab.shut = False
_install_fake_global(ab)
# pool-reset counter checks removed (dead fn, FX-2)

# D3: CAS single replacement — N callers race reset_pool(observed_old)
try:
    observed = _FakePool("cas-observed")
    _install_fake_global(observed)
    import threading as _th
    barr = _th.Barrier(8)
    results = []
    def _racer():
        barr.wait(timeout=10)
        results.append(pool_mod.reset_pool(observed, "race"))
    ths = [_th.Thread(target=_racer) for _ in range(8)]
    [t.start() for t in ths]
    [t.join(20) for t in ths]
    reps = {id(r) for r in results}
    check("D3 CAS: 8 并发 reset 同一 observed → 全部拿到同一 replacement",
          len(results) == 8 and len(reps) == 1, f"distinct={len(reps)}")
    cnt0 = pool_mod._reset_count
    r_stale = pool_mod.reset_pool(observed, "stale")
    check("D3b 迟到 caller → 复用既有 replacement, 零新建",
          r_stale is results[0] and pool_mod._reset_count == cnt0)
    check("D3c get_pool() == replacement", pool_mod.get_pool() is results[0])
finally:
    _restore_real_pool()

# ══════════ D4. 超时回收: 真实 ProcessPool + 卡死 handler ══════════
# shutdown(wait=False) 清空 _processes 后再读它是 AttributeError (旧
# worker 全部存活)。reset_pool(terminate_workers=True) 在 shutdown 前快照
# 进程表并显式 terminate→join→kill: 卡死 worker 必须消失, 并发两个超时
# 只产生一次 reset, 下一次调用立刻可用, 超时调用退款恰好一次。
import execution.orchestrator as _orch_mod
from execution.orchestrator import _run_handler, execute_local
from execution import ExecutionError

_wtag = _os.urandom(3).hex()
_wedge_id = f"tmpwedge_{_wtag}"
_quick_id = f"tmpquick_{_wtag}"
_wfile = _os.path.join(REPO, "core", f"tools_tmpwedge_{_wtag}.py")
with open(_wfile, "w", encoding="utf-8") as _f:
    _f.write(f'''
import os
import time
from tools import register

_PIDS = os.path.join(os.path.dirname(__file__), "tmpwedge_pids_{_wtag}.txt")


def _wedge(tool_input, params):
    with open(_PIDS, "a") as f:
        f.write(str(os.getpid()) + "\\n")
    time.sleep(600)
    return {{"items": []}}


def _quick(tool_input, params):
    return {{"items": [{{"kind": "text", "text": "quick-ok"}}]}}


register(tool_id="{_wedge_id}", label={{"en": "Wedge"}}, handler=_wedge,
         accept={{".png"}}, tag="image")
register(tool_id="{_quick_id}", label={{"en": "Quick"}}, handler=_quick,
         accept={{".png"}}, tag="image", outputs=("text",))
''')
_wuser = None
_d4_pools = []
try:
    import multiprocessing as _mp4
    from concurrent.futures import ProcessPoolExecutor as _PPE4
    from economy.billing import grant as _grant4, spendable_balance as _sb4
    from execution.invocation import ToolInvocation, SourceFile
    from execution import gate as _gate4

    # spawn 上下文的 worker 会把本测试脚本当主模块重跑 (线性脚本副作用
    # 全量重放); 测试用显式 fork 上下文的真实池。fork 子进程按
    # worker.process_tool 的 load_all() 重扫注册表发现临时模块 — 把
    # _loaded 复位让子进程重扫。
    import tools as _tools4
    _tools4._loaded = False
    _fork4 = _mp4.get_context("fork")
    _orig_new_pool4 = pool_mod._new_pool
    _d4_pool = _PPE4(max_workers=pool_mod.WORKERS, mp_context=_fork4)
    _d4_pools.append(_d4_pool)
    pool_mod._pool = _d4_pool
    pool_mod._new_pool = lambda: _PPE4(max_workers=pool_mod.WORKERS,
                                       mp_context=_fork4)

    _wuser = _mkuser(_u_exb + "w")
    with db() as con:
        for _tid in (_wedge_id, _quick_id):
            con.execute(
                "insert into platform_tools (tool_key, owner_id, tool_id,"
                " execution, label_json, visibility, enabled, created_at,"
                " updated_at, accept_json, format_family)"
                " values (%s, NULL, %s, 'local', %s, 'public', TRUE,"
                " %s, %s, '[\".png\"]', 'image')",
                (_tid, _tid, '{"en": "D4"}', ts, ts))
            con.execute(
                "insert into pricing (tool_id, cost, enabled, updated_at)"
                " values (%s, 25, TRUE, %s)", (_tid, ts))
    _grant4(_wuser, 1000, "manual_topup", note="d4")
    _bal4 = _sb4(_wuser)

    _cnt4 = pool_mod._reset_count
    _orig_to4 = _orch_mod.LOCAL_EXEC_TIMEOUT_S
    _orch_mod.LOCAL_EXEC_TIMEOUT_S = 2
    _tool_row4 = {"id": _wedge_id, "tool_key": _wedge_id, "owner_id": None,
                  "execution": "local", "files_min": 1, "files_max": 1,
                  "outputs_json": '["text"]'}

    async def _gate_wedge(ref):
        inv = ToolInvocation(_wedge_id, [SourceFile("p", "t")], {},
                             {"expiry_days": 7, "allowed_downloads": 5},
                             f"d4:{ref}")
        return await _gate4.run(
            {"id": _wuser}, _tool_row4, inv,
            lambda: execute_local({"ip": "127.0.0.1"}, _tool_row4, [], {},
                                  {"id": _wuser},
                                  pool_mod.default_process_tool(),
                                  pool_mod._pool,
                                  {"expiry_days": 7, "allowed_downloads": 5}))
    # 第一波: 单个 gate 调用 — 超时收敛的错误码与退款路径
    try:
        _res4, _err4, _st4 = asyncio.run(_gate_wedge("a"))
    finally:
        pass
    check("D4 wedged gate call → 400 err_endpoint_timeout",
          _err4 is not None and _err4.get("error") == "err_endpoint_timeout"
          and _st4 == 400, f"{_st4} {_err4}")
    # 第二波: 并发两个裸超时 — 只产生一次 reset (先到者的 reset 杀掉
    # worker, 后到者的 future 以 BrokenProcessPool 收敛也是合法终态)
    async def _bare_wedge():
        return await _run_handler(pool_mod._pool,
                                  pool_mod.default_process_tool(),
                                  _wedge_id, [], {})
    async def _two_bare():
        return await asyncio.gather(_bare_wedge(), _bare_wedge(),
                                    return_exceptions=True)
    try:
        _r4a, _r4b = asyncio.run(_two_bare())
    finally:
        _orch_mod.LOCAL_EXEC_TIMEOUT_S = _orig_to4
    check("D4b exactly one reset per wave (single + concurrent pair)",
          pool_mod._reset_count == _cnt4 + 2
          and all(isinstance(r, BaseException) or r is None
                  or isinstance(r, tuple) for r in (_r4a, _r4b)),
          f"{_cnt4} → {pool_mod._reset_count} r={str((_r4a, _r4b))[:80]}")
    _wedge_pids = []
    try:
        with open(_os.path.join(REPO, "core",
                                f"tmpwedge_pids_{_wtag}.txt")) as _f:
            _wedge_pids = [int(x) for x in _f.read().split() if x.strip()]
    except OSError:
        pass
    def _pid_dead(pid):
        try:
            _os.kill(pid, 0)
            return False
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
    check("D4c all wedged workers terminated (wave 1 + wave 2)",
          len(_wedge_pids) == 3 and all(_pid_dead(p) for p in _wedge_pids),
          _wedge_pids)
    with db() as con:
        _n_ref4 = con.execute(
            "select count(*) c from ledger where user_id=%s"
            " and kind='refund'", (_wuser,)).fetchone()["c"]
        _n_con4 = con.execute(
            "select count(*) c from ledger where user_id=%s"
            " and kind='consume'", (_wuser,)).fetchone()["c"]
        _n_run4 = con.execute(
            "select count(*) c from tool_calls where user_id=%s"
            " and status='running'", (_wuser,)).fetchone()["c"]
    check("D4d the timeout refunded exactly once (1 consume / 1 refund)",
          int(_n_con4) == 1 and int(_n_ref4) == 1 and int(_n_run4) == 0,
          f"consume={_n_con4} refund={_n_ref4} running={_n_run4}")
    check("D4e balance restored", _sb4(_wuser) == _bal4,
          f"{_sb4(_wuser)} vs {_bal4}")
    _new_pool4 = pool_mod.get_pool()
    _d4_pools.append(_new_pool4)
    async def _next_call():
        return await _run_handler(_new_pool4, pool_mod.default_process_tool(),
                                  _quick_id, [], {})
    _out4 = asyncio.run(_next_call())
    check("D4f next call succeeds on the replacement pool immediately",
          isinstance(_out4, dict) and _out4.get("items")
          and _out4["items"][0]["text"] == "quick-ok", str(_out4)[:100])
finally:
    _orch_mod.LOCAL_EXEC_TIMEOUT_S = 120
    pool_mod._new_pool = _orig_new_pool4
    for _p in _d4_pools:
        try:
            _p.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
    _restore_real_pool()
    try:
        _os.remove(_wfile)
    except OSError:
        pass
    try:
        _os.remove(_os.path.join(REPO, "core", f"tmpwedge_pids_{_wtag}.txt"))
    except OSError:
        pass
    if _wuser is not None:
        with db() as con:
            con.execute("delete from tool_calls where user_id=%s", (_wuser,))
            con.execute("delete from ledger where user_id=%s", (_wuser,))
            for _tid in (_wedge_id, _quick_id):
                con.execute("delete from pricing where tool_id=%s", (_tid,))
                con.execute("delete from platform_tools where tool_key=%s",
                            (_tid,))
        with db() as con:
            purge_users(con, [_wuser])

# ══════════ E. 真实 gate 收敛 (worker_pool_broken → tool_failed/refund) ══════════
_eb_user_id = None
_eb_tool = None
try:
    from economy.billing import grant as _grant, spendable_balance
    from repo.ledger import by_idem_key

    _eb_tool = f"ebtool_{_u_exb}"
    import json as _json
    user_id = _mkuser(_u_exb + "u")
    _eb_user_id = user_id
    with db() as con:
        con.execute(
            "insert into platform_tools (tool_key, owner_id, tool_id, execution,"
            " label_json, visibility,"
            " enabled, created_at, updated_at, accept_json, format_family)"
            " values (%s, NULL, %s, 'local', %s,"
            " 'public', TRUE, %s, %s, '[\".png\"]', 'image')",
            (f"ebtool_{_u_exb}", f"ebtool_{_u_exb}",
             _json.dumps({"en": "EB"}), ts, ts))
        con.execute(
            "insert into pricing (tool_id, cost, enabled, updated_at)"
            " values (%s, 25, TRUE, %s)", (f"ebtool_{_u_exb}", ts))
    _grant(user_id, 1000, "manual_topup", note="eb fixture")

    from execution.invocation import ToolInvocation, SourceFile

    inv = ToolInvocation(
        tool_id=f"ebtool_{_u_exb}",
        files=[SourceFile("pub_x", "tok_x")],
        params={}, delivery={"expiry_days": 7, "allowed_downloads": 10},
        client_ref=f"eb:{_u_exb}")

    async def _boom():
        raise RuntimeError("worker_pool_broken")

    from execution import gate
    bal_before = spendable_balance(user_id)
    result, err, status = asyncio.run(gate.run(
        {"id": user_id},
        {"id": f"ebtool_{_u_exb}", "tool_key": f"ebtool_{_u_exb}",
         "owner_id": None},
        inv, _boom))
    check("E1 gate 响应: tool_failed / 500",
          err is not None and err.get("error") == "tool_failed" and status == 500,
          f"{err} {status}")

    with db() as con:
        call = con.execute(
            "select id, status, finished_at from tool_calls"
            " where user_id=%s and tool_id=%s order by id desc limit 1",
            (user_id, f"ebtool_{_u_exb}")).fetchone()
    check("E2 tool_call terminal failed + finished_at 非空",
          call is not None and call["status"] == "failed"
          and call["finished_at"] is not None, str(dict(call) if call else None))

    call_id = int(call["id"])
    with db() as con:
        charge = by_idem_key(con, user_id, f"call:{call_id}")
        refund = by_idem_key(con, user_id, f"refund:ledger:{charge['id']}")
    check("E3 exact consume 存在 (-25)",
          charge is not None and int(charge["delta"]) == -25,
          str(dict(charge) if charge else None))
    check("E4 exact refund 存在且等额 (+25, 同一 charge 引用)",
          refund is not None and int(refund["delta"]) == 25
          and refund["ref_id"] == str(charge["id"]),
          str(dict(refund) if refund else None))
    check("E5 最终余额恢复 (1000)",
          spendable_balance(user_id) == bal_before,
          f"{bal_before} → {spendable_balance(user_id)}")
    with db() as con:
        n_refunds = int(con.execute(
            "select count(*) c from ledger where user_id=%s and kind='refund'"
            " and ref_id=%s", (user_id, str(charge["id"]))).fetchone()["c"])
        running = int(con.execute(
            "select count(*) c from tool_calls where user_id=%s"
            " and status='running'", (user_id,)).fetchone()["c"])
    check("E6 无重复 refund / 无遗留 running",
          n_refunds == 1 and running == 0, f"refunds={n_refunds} running={running}")

finally:
    # FK-safe fixture cleanup: 只删本测试的实体 (随机前缀)
    if _eb_user_id is not None:
        with db() as con:
            con.execute("delete from tool_calls where user_id=%s", (_eb_user_id,))
            con.execute("delete from ledger where user_id=%s", (_eb_user_id,))
    if _eb_tool is not None:
        with db() as con:
            con.execute("delete from pricing where tool_id=%s", (_eb_tool,))
            con.execute("delete from platform_tools where tool_key=%s", (_eb_tool,))
    if _eb_user_id is not None:
        with db() as con:
            purge_users(con, [_eb_user_id])
    print("cleanup: boundaries fixtures removed")

# ══════════ F. semantic ══════════
_orch_src = open(_os.path.join(REPO, "core", "execution", "orchestrator.py"),
                 encoding="utf-8").read()
_gate_src = open(_os.path.join(REPO, "core", "execution", "gate.py"),
                 encoding="utf-8").read()
check("F1 无 err_worker_died / 无新经济状态",
      "err_worker_died" not in _orch_src and "resource" not in _gate_src.lower())

# ══════════ G. stored execution vocabulary fail-closed (C58) ══════════
# dispatch must reject any stored execution outside local|http BEFORE the
# metering gate (no claim/charge/network), with no default-else local.
from execution.dispatch import dispatch_execute as _dx

def _fake_request():
    class _R:
        class _c:
            client = "127.0.0.1"
        headers = {}
    return _R()

_ts58 = int(time.time())
# The fresh schema carries platform_tools_execution_ck; the dispatch-level
# rejection cases simulate a PRE-CHECK stored row (legacy DB) by dropping
# the constraint on this isolated test DB for the duration, then restoring.
with db() as con:
    con.execute("ALTER TABLE platform_tools DROP CONSTRAINT IF EXISTS platform_tools_execution_ck")

def _mkrow58(execution):
    with db() as con:
        uid = con.execute(
            "insert into users (username, email, password_hash, display_name,"
            " status, created_at, last_login_at) values (%s, %s, 'x', 'T',"
            " 'active', %s, %s) returning id",
            (f"eb58_{execution}_{_os.urandom(3).hex()}",
             f"eb58_{execution}_{_os.urandom(3).hex()}@t.co",
             _ts58, _ts58)).fetchone()["id"]
        con.execute(
            "insert into platform_tools (tool_key, owner_id, tool_id,"
            " execution, label_json, visibility, enabled,"
            " created_at, updated_at, accept_json, format_family) values (%s, NULL, %s, %s,"
            " '{}', 'public', TRUE, %s, %s, '[\".png\"]', 'image')"
            " on conflict (tool_key) do update set execution=excluded.execution",
            (f"eb58/{execution}", execution, execution, _ts58, _ts58))
    return uid

_INV58 = {
    "tool_id": None,  # filled per case
    "source": {"files": [{"public_id": "aaaaaaaa", "source_token": "tok"}]},
    "params": {},
    "delivery": {"expiry_days": 1, "allowed_downloads": 10},
    "client_ref": "eb58-" + _os.urandom(4).hex(),
}
for _exec58 in ("mcp", "grpc", "wasm"):
    _uid = _mkrow58(_exec58)
    _inv = dict(_INV58)
    _inv["tool_id"] = f"eb58/{_exec58}"
    _user58 = {"id": _uid, "username": f"u58_{_exec58}"}
    import asyncio as _a58
    try:
        _pl, _err, _st = _a58.run(_dx(_fake_request(), _user58, _inv))
    except Exception as _exc58:
        _pl, _err, _st = None, {"raw": str(_exc58)[:60]}, "EXC"
    check(f"G-{_exec58} dispatch 拒未知 execution (gate/charge 前 503)",
          _st == 503 and (_err or {}).get("error") == "tool_unavailable",
          f"{_st} {str(_err)[:60]}")
    with db() as con:
        _n = con.execute(
            "select count(*) c from tool_calls where user_id=%s", (_uid,)
        ).fetchone()["c"]
    check(f"G-{_exec58} 拒绝时零 tool_call claim", int(_n) == 0, f"{_n}")
    with db() as con:
        _n2 = con.execute(
            "select count(*) c from ledger where user_id=%s", (_uid,)
        ).fetchone()["c"]
    check(f"G-{_exec58} 拒绝时零 charge/consume", int(_n2) == 0, f"{_n2}")

# local + http rows still resolve into their normal branches (admitted set)
for _exec58 in ("local", "http"):
    _mkrow58(_exec58)
    check(f"G-{_exec58} row 可创建 (canonical vocabulary 接受)",
          True)
from governance.tool_supply import _EXECUTIONS
check("G-vocabulary authoring validator 只接受 local|http",
      _EXECUTIONS == ("local", "http"))

# drop the pre-CHECK test rows, then restore the fresh-schema CHECK
with db() as con:
    con.execute("delete from platform_tools where tool_key like 'eb58/%%'")
    con.execute("ALTER TABLE platform_tools ADD CONSTRAINT platform_tools_execution_ck"
                " CHECK (execution IN ('local','http'))")

# startup invariant: invalid stored execution aborts (fresh DB rows)
from governance.catalog import validate_catalog_rows as _vcr
with db() as con:
    con.execute("ALTER TABLE platform_tools DROP CONSTRAINT IF EXISTS platform_tools_execution_ck")
    con.execute(
        "insert into platform_tools (tool_key, owner_id, tool_id, execution,"
        " label_json, visibility, enabled, accept_json, format_family, created_at, updated_at)"
        " values ('eb58/bad_exec_inv', NULL, 'bad_exec_inv', 'mcp',"
        " '{}', 'private', FALSE, '[\".png\"]', 'image', %s, %s)", (_ts58, _ts58))
try:
    _vcr()
    check("G-startup stored execution='mcp' → invariant fail", False, "no raise")
except RuntimeError as _e58:
    check("G-startup stored execution='mcp' → invariant fail",
          "execution" in str(_e58), str(_e58)[:80])
with db() as con:
    con.execute("delete from platform_tools where tool_key='eb58/bad_exec_inv'")
    con.execute("ALTER TABLE platform_tools DROP CONSTRAINT IF EXISTS platform_tools_execution_ck")
    con.execute(
        "insert into platform_tools (tool_key, owner_id, tool_id, execution,"
        " label_json, visibility, enabled, accept_json, format_family, created_at, updated_at)"
        " values ('eb58/bad_exec_inv2', NULL, 'bad_exec_inv2', 'quantum',"
        " '{}', 'unlisted', TRUE, '[\".png\"]', 'image', %s, %s)", (_ts58, _ts58))
try:
    _vcr()
    check("G-startup arbitrary execution='quantum' → invariant fail", False, "no raise")
except RuntimeError as _e58:
    check("G-startup arbitrary execution='quantum' → invariant fail",
          "execution" in str(_e58), str(_e58)[:80])
with db() as con:
    con.execute("delete from platform_tools where tool_key='eb58/bad_exec_inv2'")
    con.execute("ALTER TABLE platform_tools ADD CONSTRAINT platform_tools_execution_ck"
                " CHECK (execution IN ('local','http'))")
with db() as con:
    con.execute("delete from platform_tools where tool_key='eb58/bad_exec_inv2'")

# fresh schema carries the CHECK (probed via a fresh connection DDL artifact)
with db() as con:
    _ck = con.execute(
        "select count(*) c from pg_constraint"
        " where conname='platform_tools_execution_ck'").fetchone()["c"]
check("G-fresh schema platform_tools.execution CHECK (local,http) 存在",
      int(_ck) == 1, str(_ck))

# cleanup seeded rows
with db() as con:
    con.execute("delete from platform_tools where tool_key like 'eb58/%%'")
    purge_users(con, [r["id"] for r in con.execute(
        "select id from users where username like 'eb58_%%'").fetchall()])

# ══════════ H. remote (http) execution backpressure ══════════
# Global + per-user in-flight caps reject BEFORE the claim (no
# tool_call row, no charge) and release when the call settles.
_h_users = []
_h_tool = f"ebhttp_{_u_exb}"
try:
    from economy.billing import grant as _grant_h, spendable_balance as _bal_h
    with db() as con:
        con.execute(
            "insert into platform_tools (tool_key, owner_id, tool_id, execution,"
            " label_json, visibility,"
            " enabled, created_at, updated_at, accept_json, format_family)"
            " values (%s, NULL, %s, 'http', %s,"
            " 'public', TRUE, %s, %s, '[\".png\"]', 'image')",
            (_h_tool, _h_tool, _json.dumps({"en": "EBH"}), ts, ts))
        con.execute(
            "insert into pricing (tool_id, cost, enabled, updated_at)"
            " values (%s, 25, TRUE, %s)", (_h_tool, ts))
    _hu1 = _mkuser(_u_exb + "h1")
    _hu2 = _mkuser(_u_exb + "h2")
    _hu3 = _mkuser(_u_exb + "h3")
    _h_users = [_hu1, _hu2, _hu3]
    for _uid_h in _h_users:
        _grant_h(_uid_h, 1000, "manual_topup", note="ebh fixture")

    from execution.invocation import ToolInvocation, SourceFile
    from execution import gate as _gate_mod
    from execution.pool import (REMOTE_MAX_INFLIGHT, REMOTE_PER_USER_MAX,
                                remote_inflight_count as _r_inflight)

    check("H0 不变量: 远程全局上限低于重路径闸门 (8), 每用户 ≤ 全局",
          0 < REMOTE_MAX_INFLIGHT < 8
          and REMOTE_PER_USER_MAX <= REMOTE_MAX_INFLIGHT,
          (REMOTE_MAX_INFLIGHT, REMOTE_PER_USER_MAX))

    _H_TOOL = {"id": _h_tool, "tool_key": _h_tool,
               "owner_id": None, "execution": "http"}

    async def _http_call(uid, ref, hold):
        async def _orch():
            await hold.wait()
            raise RuntimeError("tool_failed")
        inv = ToolInvocation(
            tool_id=_h_tool,
            files=[SourceFile("pub_h", "tok_h")],
            params={}, delivery={"expiry_days": 1, "allowed_downloads": 1},
            client_ref=ref)
        return await _gate_mod.run({"id": uid}, _H_TOOL, inv, _orch)

    async def _scenario():
        hold = asyncio.Event()
        # per-user budget: two held calls occupy user h1's whole quota
        t1 = asyncio.create_task(_http_call(_hu1, f"hh1-{_u_exb}", hold))
        t2 = asyncio.create_task(_http_call(_hu1, f"hh2-{_u_exb}", hold))
        await asyncio.sleep(0.5)
        bal_a = _bal_h(_hu1)
        r_reject_user = await _http_call(_hu1, f"hh3-{_u_exb}", hold)
        bal_b = _bal_h(_hu1)
        # global budget: two other users fill the remaining slots
        t4 = asyncio.create_task(_http_call(_hu2, f"hh4-{_u_exb}", hold))
        t5 = asyncio.create_task(_http_call(_hu2, f"hh5-{_u_exb}", hold))
        await asyncio.sleep(0.5)
        r_reject_global = await _http_call(_hu3, f"hh6-{_u_exb}", hold)
        hold.set()
        held = await asyncio.gather(t1, t2, t4, t5)
        # everything settled → slots released → a new call passes through
        r_after = await _http_call(_hu3, f"hh7-{_u_exb}", hold)
        return r_reject_user, r_reject_global, r_after, held, bal_a, bal_b

    (r_reject_user, r_reject_global, r_after, _held,
     bal_a, bal_b) = asyncio.run(_scenario())

    check("H1 同一用户第 3 个并发 http 调用 → 429 err_rate_limited",
          r_reject_user[0] is None
          and r_reject_user[1] == {"ok": False, "error": "err_rate_limited"}
          and r_reject_user[2] == 429, str(r_reject_user)[:100])
    check("H2 全局第 5 个并发 http 调用 → 503 err_busy",
          r_reject_global[0] is None
          and r_reject_global[1] == {"ok": False, "error": "err_busy"}
          and r_reject_global[2] == 503, str(r_reject_global)[:100])
    check("H3 两种拒绝都不扣费 (拒绝前后余额一致)",
          bal_a == bal_b, f"{bal_a} vs {bal_b}")
    with db() as con:
        for _ref_h, _expect_h in ((f"hh3-{_u_exb}", 0), (f"hh6-{_u_exb}", 0),
                                  (f"hh7-{_u_exb}", 1)):
            _n_h = int(con.execute(
                "select count(*) c from tool_calls where client_ref=%s",
                (_ref_h,)).fetchone()["c"])
            check(f"H4 client_ref …{_ref_h[-3:]} tool_call 行数 = {_expect_h}",
                  _n_h == _expect_h, _n_h)
    check("H5 占住的 4 个调用正常走完失败结算 (tool_failed/500), 名额释放后新调用可进场",
          all(o[0] is None and o[1].get("error") == "tool_failed"
              and o[2] == 500 for o in _held)
          and r_after[0] is None and r_after[1].get("error") == "tool_failed"
          and r_after[2] == 500,
          str([o[2] for o in _held]) + str(r_after[2]))
    check("H6 场景结束在途归零, 余额恢复",
          _r_inflight() == 0
          and all(_bal_h(u) == 1000 for u in _h_users),
          (_r_inflight(), [_bal_h(u) for u in _h_users]))

finally:
    # FK-safe fixture cleanup: 只删本测试的实体 (随机前缀)
    for _uid_h in _h_users:
        with db() as con:
            con.execute("delete from tool_calls where user_id=%s", (_uid_h,))
            con.execute("delete from ledger where user_id=%s", (_uid_h,))
    if _h_tool:
        with db() as con:
            con.execute("delete from pricing where tool_id=%s", (_h_tool,))
            con.execute("delete from platform_tools where tool_key=%s", (_h_tool,))
    if _h_users:
        with db() as con:
            purge_users(con, _h_users)
    print("cleanup: remote backpressure fixtures removed")


# ════════════════════════════════════════════════════════════════════════
# Result settlement atomicity
#
# Result settlement atomicity canonical.
#
# No durable/public tool result without a committed settlement: the
# orchestrator only STAGES the physical Gokapi object; uploads row +
# result binding + earning/revenue land in ONE
# gate transaction. Fault injection proves rollbacks, staged cleanup,
# exact refund, commit-ACK-loss reconciliation (durable ok → success, no
# refund), unreadable-outcome (stays running, no refund), startup
# recovery, and same-client_ref response-loss replay (single execution /
# consume / uploads row / settlement).
# ════════════════════════════════════════════════════════════════════════
section('Result settlement atomicity')

import os as _os
import sys as _sys


import asyncio
import json as _json
import time
ts_rsa = int(time.time())
_u_rsa = "rs" + _os.urandom(4).hex()

from database import db
from config import BASE_URL

def _mkuser(name):
    with db() as con:
        row = con.execute(
            "insert into users (username, email, password_hash, display_name,"
            " status, created_at, last_login_at) values (%s, %s, 'x', 'T',"
            " 'active', %s, %s) returning id",
            (name, f"{name}@t.co", ts_rsa, ts_rsa)).fetchone()
    return int(row["id"])

from economy.billing import grant as _grant, spendable_balance
from repo.ledger import by_idem_key
from execution.results import StagedItems, StagedFileItem

def row_id_of(res):
    with db() as con:
        return con.execute(
            "select id from tool_calls where id=%s",
            (res["call_id"],)).fetchone()["id"]

def _mkstaged(tag="x", **kw):
    d = dict(gokapi_id=f"gk_{_u_rsa}_{tag}",
             filename="out.bin", content_type="application/octet-stream",
             size=10, expiry_days=7, allowed_downloads=5,
             expire_at=ts_rsa + 86400, url_hotlink="", ip="127.0.0.1")
    d.pop("tool_id", None)   # 闭包误传容忍
    d.update(kw)
    ip = d.pop("ip", "127.0.0.1")
    osz = d.pop("orig_size", None)
    return StagedItems(
        order=["file"], inline=[],
        files=[StagedFileItem(**d)],
        ip=ip, **({"orig_size": osz} if osz is not None else {}))

def _mktool(tool_id, owner_id=None, cost=25):
    with db() as con:
        con.execute(
            "insert into platform_tools (tool_key, owner_id, tool_id,"
            " execution, label_json,"
            " visibility, enabled, created_at, updated_at, accept_json, format_family)"
            " values (%s, %s, %s, 'local', %s,"
            " 'public', TRUE, %s, %s, '[\".png\"]', 'image')",
            (tool_id, owner_id, tool_id,
             _json.dumps({"en": "RS"}), ts_rsa, ts_rsa))
        con.execute(
            "insert into pricing (tool_id, cost, enabled, updated_at)"
            " values (%s, %s, TRUE, %s)", (tool_id, cost, ts_rsa))

# ══════════ A. staging-only contract ══════════
st = _mkstaged(tag="contract")
check("A1 StagedItems 不携带下载地址/公开 id 字段",
      not hasattr(st, "download_url") and not hasattr(st, "public_id")
)
_orch_src = open(_os.path.join(REPO, "core", "execution", "orchestrator.py"),
                 encoding="utf-8").read()
check("A2 orchestrator 不再调用 save_upload_record (pipeline 模块已删)",
      "save_upload_record" not in _orch_src
      and not _os.path.exists(_os.path.join(
          REPO, "core", "execution", "pipeline.py")))

# ══════════ B. atomic success transaction (first-party) ══════════
from execution import gate

user1 = _mkuser(_u_rsa + "u1")
tool1 = f"rstool1_{_u_rsa}"
_mktool(tool1, owner_id=None, cost=25)
_grant(user1, 1000, "manual_topup", note="rs")

from execution.invocation import ToolInvocation, SourceFile
_ref_seq = [0]
def _inv_rsa(tool_id, ref=None):
    if ref is None:  # client_ref is required (operation identity); fixtures
        _ref_seq[0] += 1  # default to a fresh unique ref per call
        ref = f"fix:{tool_id}:{_ref_seq[0]}:{_u_rsa}"
    return ToolInvocation(tool_id=tool_id,
                          files=[SourceFile("p", "t")], params={},
                          delivery={"expiry_days": 7, "allowed_downloads": 5},
                          client_ref=ref)

async def _stage_ok(tool_id, tag="ok", **kw):
    return _mkstaged(tag=tag, **kw)

def _run_gate(user, tool_id, ref, staged_fn, tool_owner=None):
    return asyncio.run(gate.run(
        {"id": user},
        {"id": tool_id, "tool_key": tool_id,
         "owner_id": tool_owner},
        _inv_rsa(tool_id, ref), staged_fn))

n_uploads_before = None
with db() as con:
    n_uploads_before = int(con.execute(
        "select count(*) c from uploads where user_id=%s",
        (user1,)).fetchone()["c"])

res, err, st1 = _run_gate(user1, tool1, None, lambda: _stage_ok(tool1))
check("B1 成功 settlement → success response", res and res.get("ok"))
with db() as con:
    row = con.execute(
        "select t.id, t.status, t.result_public_id,"
        " t.result_json from tool_calls t"
        " where t.user_id=%s and t.tool_id=%s order by t.id desc limit 1",
        (user1, tool1)).fetchone()
    _rj0 = _json.loads(row["result_json"]) if row["result_json"] else {}
    row = dict(row)
    row["public_id"] = next((i.get("public_id") for i in _rj0.get("items", [])
                             if i.get("kind") == "file"), None)
    n_uploads_after = int(con.execute(
        "select count(*) c from uploads where user_id=%s",
        (user1,)).fetchone()["c"])
check("B2 uploads 恰新增一行", n_uploads_after - n_uploads_before == 1,
      f"{n_uploads_before}→{n_uploads_after}")
check("B3 tool_call ok + result binding (upload_id 列已退役)",
      row["status"] == "ok" and row["result_public_id"] is not None)
# the result owns a fresh public id (/r/{id}); file items carry
# their own uploads public ids
rj = _json.loads(row["result_json"])
check("B4 result_public_id is the /r/ id (own identity)",
      row["result_public_id"] == rj.get("result_id")
      and row["result_public_id"] != row["public_id"],
      (row["result_public_id"], row["public_id"]))
check("B5 first file item publicId == uploads.public_id",
      rj["items"][0].get("public_id") == row["public_id"]
      and rj["items"][0].get("kind") == "file", rj["items"][:1])
# B6 详情在 B6b (绑定 call id 的 revenue 聚合)
from repo import ledger as _led
with db() as con:
    rev = con.execute(
        "select coalesce(sum(delta),0) s from ledger where kind='platform_revenue'"
        " and ref_id=%s", (str(row["id"]),)).fetchone()["s"]
check("B6b platform_revenue = 25 (绑定 call id)", int(rev) == 25, str(rev))

# ══════════ C. third-party earning/commission 同事务 ══════════
# C1 断言 rate 30 的拆账; 抽佣率是 settings 里的全局态 (别的套件会删/
# 复原它), 本套件自种所需值, 不依赖运行顺序留下的库态
with db() as con:
    con.execute(
        "insert into settings (key, value, updated_at) values"
        " ('commission_rate', '30', %s) on conflict (key) do update"
        " set value = excluded.value", (ts_rsa,))
owner = _mkuser(_u_rsa + "owner")
user2 = _mkuser(_u_rsa + "u2")
tool2 = f"rstool2_{_u_rsa}"
_mktool(tool2, owner_id=owner, cost=100)
_grant(user2, 1000, "manual_topup", note="rs")
res2, err2, _ = _run_gate(user2, tool2, None, lambda: _stage_ok(tool2),
                          tool_owner=owner)
with db() as con:
    r2 = con.execute(
        "select t.id from tool_calls t where t.user_id=%s and t.tool_id=%s"
        " order by t.id desc limit 1", (user2, tool2)).fetchone()
    earn = con.execute(
        "select coalesce(sum(delta),0) s from ledger where kind='provider_earn'"
        " and ref_id=%s", (str(r2["id"]),)).fetchone()["s"]
    comm = con.execute(
        "select coalesce(sum(delta),0) s from ledger where kind='platform_commission'"
        " and ref_id=%s", (str(r2["id"]),)).fetchone()["s"]
check("C1 third-party: earning 70 + commission 30 (rate 30)",
      int(earn) == 70 and int(comm) == 30, f"earn={earn} comm={comm}")

# ══════════ D. fault injection: R1 (uploads insert 后 / finalize 前) ══════════
user3 = _mkuser(_u_rsa + "u3")
tool3 = f"rstool3_{_u_rsa}"
_mktool(tool3, owner_id=None, cost=25)
_grant(user3, 1000, "manual_topup", note="rs")
bal3 = spendable_balance(user3)

import execution.results as _pipe
_orig_land = _pipe.land_items_result
def _land_fail_after_upload(con, user_id, staged, base_url, delivery):
    _upload_id, result_pid, rec = _orig_land(con, user_id, staged, base_url, delivery)
    raise RuntimeError("R1: fault after uploads INSERT")
import execution.gate as _gate_mod_rsa
import core.execution.gate as _g2

# R1 走完整 gate 路径: patch pipeline 模块 (gate._finish 函数内 import 的
# 正是 execution.pipeline 模块属性)
def _r1_gate_run():
    _pipe.land_items_result = _land_fail_after_upload
    try:
        return _run_gate(user3, tool3, None, lambda: _stage_ok(tool3, tag="r1"))
    finally:
        _pipe.land_items_result = _orig_land
res3, err3, st3 = _r1_gate_run()
with db() as con:
    up = con.execute(
        "select count(*) c from uploads where user_id=%s", (user3,)).fetchone()["c"]
    t3 = con.execute(
        "select status, result_public_id from tool_calls"
        " where user_id=%s and tool_id=%s order by id desc limit 1",
        (user3, tool3)).fetchone()
    refund3 = con.execute(
        "select l.delta from ledger l where l.user_id=%s and l.kind='refund'"
        " order by l.id desc limit 1", (user3,)).fetchone()
check("D1 R1: uploads row=0 / call failed / 无 result binding",
      int(up) == 0 and t3["status"] == "failed"
      and t3["result_public_id"] is None and t3["result_public_id"] is None,
      f"up={up} status={t3['status']}")
check("D2 R1: exact refund (+25) → 余额恢复",
      refund3 is not None and int(refund3["delta"]) == 25
      and spendable_balance(user3) == bal3,
      f"refund={refund3 and refund3['delta']} bal={spendable_balance(user3)}")

# ══════════ E. fault injection: R2 (finalize 后 / earning 写入时) ══════════
user4 = _mkuser(_u_rsa + "u4")
tool4 = f"rstool4_{_u_rsa}"
_mktool(tool4, owner_id=None, cost=25)
_grant(user4, 1000, "manual_topup", note="rs")
bal4 = spendable_balance(user4)

import repo.ledger as _rled
_orig_fok = _rled.finalize_call_ok
def _fok_then_fail(con, call_id, rpid, rjson, fin_at):
    row = _orig_fok(con, call_id, rpid, rjson, fin_at)
    raise RuntimeError("R2: fault after finalize, before revenue")
# gate 顶部 module 级 from repo.ledger import finalize_call_ok →
# patch execution.gate 模块属性 (测试顶部 from execution import gate 的
# 同一模块对象)
_orig_gate_fok = _gate_mod_rsa.finalize_call_ok
_gate_mod_rsa.finalize_call_ok = _fok_then_fail
try:
    res4, err4, st4 = _run_gate(user4, tool4, None, lambda: _stage_ok(tool4, tag="r2"))
except Exception:
    pass
_gate_mod_rsa.finalize_call_ok = _orig_gate_fok
with db() as con:
    up4 = con.execute(
        "select count(*) c from uploads where user_id=%s", (user4,)).fetchone()["c"]
    t4 = con.execute(
        "select status, result_public_id from tool_calls where user_id=%s"
        " and tool_id=%s order by id desc limit 1",
        (user4, tool4)).fetchone()
check("E1 R2: 整事务回滚 (uploads=0 / call 非 ok / 无 binding)",
      int(up4) == 0 and t4["status"] == "failed" and t4["result_public_id"] is None,
      f"up={up4} status={t4['status']}")
check("E2 R2: exact refund → 余额恢复",
      spendable_balance(user4) == bal4, str(spendable_balance(user4)))

# ══════════ F. commit-ACK 丢失 → durable ok 收敛 (case A) ══════════
user5 = _mkuser(_u_rsa + "u5")
tool5 = f"rstool5_{_u_rsa}"
_mktool(tool5, owner_id=None, cost=25)
_grant(user5, 1000, "manual_topup", note="rs")
bal5 = spendable_balance(user5)

_deleted = []
import storage as _gk
_orig_del = _gk.delete
_gk.delete = lambda gid: _deleted.append(gid)

# 制造: settlement COMMIT 成功但 _finish 在 commit 后抛 (模拟 ACK 丢失):
_orig_spend = None
import economy.billing as _bill
class _CommitAckLoss(Exception):
    pass
# 手法: monkeypatch db 上下文? 更简单: 跑一次正常成功, 然后手动把 call 拨回
# running 前状态再重放? 不行。真实手法: patch spendable_balance (commit 后
# 调用) 抛异常 — _finish 的 balance 段在事务外? 看代码: balance 在 with 外。
# 那么: patch json.dumps? 不行。直接模拟: 正常跑完后, 人为把 _finish 包上
# commit 后异常 — 用一个 wrapper: 跑 gate.run 两次不行。
# 采用: patch _g2.finalize_call_ok 正常执行 + 让 record_platform_income 抛
# → 这是 R2 (rollback)。ACK 丢失场景需要 commit 后异常 — patch
# spendable_balance 抛 (balance 段在 commit 后, 是 response 构造):
# ACK-loss: settlement 真实 commit 成功, 但 db() 上下文在 commit 后抛
# (连接断/ACK 丢失) — patch gate 模块的 db 工厂, 返回的连接 commit()
# 先真提交再抛。
_orig_finish = _gate_mod_rsa._finish
def _finish_ack_loss(*args, **kwargs):
    # 真实执行 settlement (完整 commit), 然后模拟连接在 ACK 返回前断掉:
    # 客户端视角 _finish 抛异常, 但 durable fact 已提交。
    _orig_finish(*args, **kwargs)
    raise RuntimeError("ACK loss: connection dropped after commit")

_gate_mod_rsa._finish = _finish_ack_loss
try:
    res5, err5, st5 = _run_gate(user5, tool5, None, lambda: _stage_ok(tool5, tag="ack"))
except Exception:
    res5, err5, st5 = None, None, None
_gate_mod_rsa._finish = _orig_finish
with db() as con:
    t5 = con.execute(
        "select status, result_json from tool_calls"
        " where user_id=%s and tool_id=%s order by id desc limit 1",
        (user5, tool5)).fetchone()
    n_refund5 = int(con.execute(
        "select count(*) c from ledger where user_id=%s and kind='refund'",
        (user5,)).fetchone()["c"])
    n_earn5 = int(con.execute(
        "select count(*) c from ledger where kind='platform_revenue'"
        " and ref_id=(select max(id)::text from tool_calls where user_id=%s)",
        (user5,)).fetchone()["c"])
check("F1 commit-ACK 丢失 → durable ok 被识别, 返回 durable success",
      t5["status"] == "ok" and res5 is not None and res5.get("ok")
      and res5.get("replay") is True,
      f"status={t5['status']} res={res5 and dict(res5)}")
check("F2 durable ok → 零 refund (无双重退款)",
      n_refund5 == 0, str(n_refund5))
check("F3 durable ok → 零 staged delete", not _deleted, str(_deleted))
check("F4 durable ok → 无重复 earning (恰 1)", n_earn5 == 1, str(n_earn5))
durable_rj = _json.loads(t5["result_json"])
check("F5 返回的是 durable result_json (同 publicId)",
      res5["result"]["result_id"] == durable_rj["result_id"])
_gk.delete = _orig_del

# ══════════ G. outcome DB 不可读 → unresolved (case C) ══════════
user6 = _mkuser(_u_rsa + "u6")
tool6 = f"rstool6_{_u_rsa}"
_mktool(tool6, owner_id=None, cost=25)
_grant(user6, 1000, "manual_topup", note="rs")
bal6 = spendable_balance(user6)

_orig_rso = _gate_mod_rsa.resolve_success_outcome
def _rso_unreachable(call_id):
    # DB outcome unreadable → strict tri-state UNRESOLVED (返回值形状与
    # 真实 resolve 一致)
    from execution.gate import OUTCOME_UNRESOLVED
    return {"outcome": OUTCOME_UNRESOLVED}
_gate_mod_rsa.resolve_success_outcome = _rso_unreachable
_gk_deleted6 = []
_gk.delete = lambda gid: _gk_deleted6.append(gid)
# 让 _finish 本身失败 (R1 fault) + outcome 不可读 → unresolved
_pipe.land_items_result = _land_fail_after_upload
try:
    res6, err6, st6 = _run_gate(user6, tool6, None, lambda: _stage_ok(tool6, tag="g"))
except Exception:
    res6, err6, st6 = None, None, None
finally:
    _pipe.land_items_result = _orig_land
    _gate_mod_rsa.resolve_success_outcome = _orig_rso
with db() as con:
    t6 = con.execute(
        "select status from tool_calls where user_id=%s and tool_id=%s"
        " order by id desc limit 1", (user6, tool6)).fetchone()
check("G1 outcome 不可读 → err_metering_failed",
      err6 is not None and err6.get("error") == "err_metering_failed",
      str(err6))
check("G2 call 保持 running (未裁决)", t6["status"] == "running", t6["status"])
check("G3 零 refund", spendable_balance(user6) == bal6 - 25,
      f"{spendable_balance(user6)} vs {bal6-25} (charge 保留)")
check("G4 零 staged delete", not _gk_deleted6, str(_gk_deleted6))

# ══════════ H. known-rollback → staged cleanup attempted ══════════
user7 = _mkuser(_u_rsa + "u7")
tool7 = f"rstool7_{_u_rsa}"
_mktool(tool7, owner_id=None, cost=25)
_grant(user7, 1000, "manual_topup", note="rs")
bal7 = spendable_balance(user7)
_staged7 = _mkstaged(tag="cleanup7")
_gk_deleted7 = []
_gk.delete = lambda gid: _gk_deleted7.append(gid)
async def _stage7():
    return _staged7
_pipe.land_items_result = _land_fail_after_upload
try:
    res7, err7, st7 = _run_gate(user7, tool7, None, _stage7)
except Exception:
    pass
finally:
    _pipe.land_items_result = _orig_land
check("H1 known rollback → staged Gokapi cleanup 被调用",
      _staged7.files[0].gokapi_id in _gk_deleted7, str(_gk_deleted7))
check("H2 known rollback → exact refund + terminal failed",
      spendable_balance(user7) == bal7)
with db() as con:
    t7 = con.execute(
        "select status from tool_calls where user_id=%s and tool_id=%s"
        " order by id desc limit 1", (user7, tool7)).fetchone()
    up7 = con.execute(
        "select count(*) c from uploads where user_id=%s",
        (user7,)).fetchone()["c"]
check("H3 零 public result (uploads=0 / failed)",
      t7["status"] == "failed" and int(up7) == 0)
_gk.delete = _orig_del

# ══════════ I. startup recovery: running+charge 无 result → refund+failed ══════════
user8 = _mkuser(_u_rsa + "u8")
tool8 = f"rstool8_{_u_rsa}"
_mktool(tool8, owner_id=None, cost=25)
_grant(user8, 1000, "manual_topup", note="rs")
with db() as con:
    from repo.ledger import claim_call
    anchor8, _ = claim_call(con, f"startup_{_u_rsa}", user8, tool8, 25, "{}", ts_rsa,
                             commission_rate_snapshot=30,
                             invocation_hash="a" * 64)
call8 = int(anchor8["id"])
from economy.billing import consume as _consume
_charge8 = _consume(user8, 25, "consume", ref_type="tool_call",
                    ref_id=str(call8), idem_key=f"call:{call8}",
                    note="startup fixture")
bal8_pre = spendable_balance(user8)
out8 = gate.recover_abandoned_calls()
with db() as con:
    t8 = con.execute(
        "select status, result_public_id from tool_calls"
        " where id=%s", (call8,)).fetchone()
check("I1 startup recovery: running+charge → exact refund+failed",
      t8["status"] == "failed" and t8["result_public_id"] is None
      and t8["result_public_id"] is None
      and spendable_balance(user8) == bal8_pre + 25,
      f"status={t8['status']} bal={spendable_balance(user8)}/{bal8_pre+25}")
check("I2 recovery case 无 result mapping (atomicity 保证)",
      t8["result_public_id"] is None and t8["result_public_id"] is None)

# ══════════ J. same client_ref response-loss replay ══════════
user9 = _mkuser(_u_rsa + "u9")
tool9 = f"rstool9_{_u_rsa}"
_mktool(tool9, owner_id=None, cost=25)
_grant(user9, 1000, "manual_topup", note="rs")
ref9 = f"replay_{_u_rsa}"
_exec_count = {"n": 0}
async def _stage9():
    _exec_count["n"] += 1
    return _mkstaged(tag="replay9")
resA, errA, stA = _run_gate(user9, tool9, ref9, _stage9)
# 模拟 response loss: 客户端重试同一 client_ref
resB, errB, stB = _run_gate(user9, tool9, ref9, _stage9)
with db() as con:
    n_calls9 = int(con.execute(
        "select count(*) c from tool_calls where user_id=%s and tool_id=%s"
        " and client_ref=%s", (user9, tool9, ref9)).fetchone()["c"])
    n_uploads9 = int(con.execute(
        "select count(*) c from uploads where user_id=%s",
        (user9,)).fetchone()["c"])
    n_consume9 = int(con.execute(
        "select count(*) c from ledger where user_id=%s and kind='consume'"
        " and idem_key like 'call:%%'", (user9,)).fetchone()["c"])
    n_rev9 = int(con.execute(
        "select count(*) c from ledger where kind='platform_revenue'"
        " and ref_id=(select max(id)::text from tool_calls where user_id=%s)",
        (user9,)).fetchone()["c"])
check("J1 replay → replay=True / 同 call_id / 同 resultId",
      resB.get("replay") is True and resB["call_id"] == resA["call_id"]
      and resB["result"]["result_id"] == resA["result"]["result_id"],
      f"A={resA['call_id']} B={resB['call_id']}")
check("J2 handler/stage 只执行一次", _exec_count["n"] == 1, str(_exec_count))
check("J3 恰一行 tool_call / 一行 uploads / 一次 consume / 一次 settlement",
      n_calls9 == 1 and n_uploads9 == 1 and n_consume9 == 1 and n_rev9 == 1,
      f"calls={n_calls9} up={n_uploads9} consume={n_consume9} rev={n_rev9}")

# ══════════ K. non-ok result DB constraint ══════════
def _db_rejects(sql, params=None):
    try:
        with db() as con:
            con.execute(sql, params or ())
        return False
    except Exception:
        return True
check("K1 non-ok + result_public_id → DB reject",
      _db_rejects("insert into tool_calls (client_ref, user_id, tool_id,"
                  " status, cost_snapshot, started_at, finished_at,"
                  " result_public_id) values (%s,%s,%s,'failed',0,%s,%s,'x')",
                  (f"kr1_{_u_rsa}", user9, tool9, ts_rsa, ts_rsa)))
check("K2 non-ok + result_public_id → DB reject",
      _db_rejects("insert into tool_calls (client_ref, user_id, tool_id,"
                  " status, cost_snapshot, started_at, finished_at,"
                  " result_public_id) values (%s,%s,%s,'failed',0,%s,%s,'x')",
                  (f"kr2_{_u_rsa}", user9, tool9, ts_rsa, ts_rsa)))
check("K3 ok + upload_id NULL 合法 (非文件成功不设限; hash 必带)",
      not _db_rejects("insert into tool_calls (client_ref, user_id,"
                      " tool_id, status, cost_snapshot, invocation_hash,"
                      " started_at, finished_at) values"
                      " (%s,%s,%s,'ok',0,%s,%s,%s)",
                      (f"kr3_{_u_rsa}", user9, tool9, "c" * 64, ts_rsa, ts_rsa)))
check("K3b ok + NULL invocation_hash → reject (replay authority 需 hash)",
      _db_rejects("insert into tool_calls (client_ref, user_id,"
                  " tool_id, status, cost_snapshot, started_at,"
                  " finished_at) values (%s,%s,%s,'ok',0,%s,%s)",
                  (f"kr3b_{_u_rsa}", user9, tool9, ts_rsa, ts_rsa)))

# ═══ L-block retired: save_upload_record deleted (no production caller
# since the direct-share branch removal; the coherence passb dead-code item) ═══

# ══════════ M. strict tri-state outcome regression ══════════
from execution.gate import (OUTCOME_DURABLE_SUCCESS, OUTCOME_PROVEN_ROLLBACK,
                            OUTCOME_UNRESOLVED, resolve_success_outcome)

# M1/O1: ok + valid result_json → durable success (F 已覆盖, 单元级复证)
check("M1 ok + valid json → OUTCOME_DURABLE_SUCCESS",
      resolve_success_outcome(int(row_id_of(res5)))["outcome"]
      == OUTCOME_DURABLE_SUCCESS)

# M2/O2: ok + malformed result_json → UNRESOLVED (durable 经济事实不可退)
userM = _mkuser(_u_rsa + "m2")
toolM = f"rstm2_{_u_rsa}"
_mktool(toolM, owner_id=None, cost=25)
_grant(userM, 1000, "manual_topup", note="rs")
resM, _, _ = _run_gate(userM, toolM, None, lambda: _stage_ok(toolM, tag="m2"))
with db() as con:
    cm2 = con.execute(
        "select id from tool_calls where user_id=%s and tool_id=%s"
        " order by id desc limit 1", (userM, toolM)).fetchone()
    con.execute("update tool_calls set result_json='{malformed'"
                " where id=%s", (cm2["id"],))
balM_pre = spendable_balance(userM)     # 已 consume 25 (settlement 成功)
outM = resolve_success_outcome(int(cm2["id"]))
check("M2 ok + malformed json → OUTCOME_UNRESOLVED",
      outM["outcome"] == OUTCOME_UNRESOLVED, str(outM))
check("M2b 零 refund / status 仍 ok / 经济事实不动",
      spendable_balance(userM) == balM_pre)
with db() as con:
    stM = con.execute("select status from tool_calls where id=%s",
                      (cm2["id"],)).fetchone()
    earnM = con.execute(
        "select coalesce(sum(delta),0) s from ledger where"
        " kind='platform_revenue' and ref_id=%s",
        (str(cm2["id"]),)).fetchone()
check("M2c status 仍 ok + revenue 保持", stM["status"] == "ok"
      and int(earnM["s"]) == 25, f"{stM['status']} rev={earnM['s']}")

# M3/O3: row missing → UNRESOLVED
outM3 = resolve_success_outcome(987654321)
check("M3 row missing → OUTCOME_UNRESOLVED",
      outM3["outcome"] == OUTCOME_UNRESOLVED, str(outM3))

# M4/O4: running → PROVEN_ROLLBACK (唯一可 refund 的状态)
userM4 = _mkuser(_u_rsa + "m4")
toolM4 = f"rstm4_{_u_rsa}"
_mktool(toolM4, owner_id=None, cost=25)
with db() as con:
    from repo.ledger import claim_call
    anchorM4, _ = claim_call(con, f"m4_{_u_rsa}", userM4, toolM4, 25, "{}", ts_rsa,
                              commission_rate_snapshot=30,
                              invocation_hash="b" * 64)
outM4 = resolve_success_outcome(int(anchorM4["id"]))
check("M4 running → OUTCOME_PROVEN_ROLLBACK (唯一 refund 路径)",
      outM4["outcome"] == OUTCOME_PROVEN_ROLLBACK, str(outM4))

# M5: terminal failed → UNRESOLVED (不许当 rollback)
with db() as con:
    from repo.ledger import finalize_call_failed
    finalize_call_failed(con, int(anchorM4["id"]), "failed", "t", ts_rsa)
outM5 = resolve_success_outcome(int(anchorM4["id"]))
check("M5 terminal failed → OUTCOME_UNRESOLVED (不猜)",
      outM5["outcome"] == OUTCOME_UNRESOLVED, str(outM5))

# ══════════ N. source lifecycle: 不 eager-delete ══════════
# N1: settlement rollback → source 保留 + 同 invocation 可重试成功
userN = _mkuser(_u_rsa + "n1")
toolN = f"rstn1_{_u_rsa}"
_mktool(toolN, owner_id=None, cost=25)
_grant(userN, 1000, "manual_topup", note="rs")
refN = f"retry_{_u_rsa}"
_src_deleted = []
_gk2 = _gk
_gk_orig2 = _gk2.delete
_gk2.delete = lambda gid: _src_deleted.append(gid)
# attempt 1: settlement fault (R1) → rollback → staged deleted, source live
_pipe.land_items_result = _land_fail_after_upload
async def _stN():
    return _mkstaged(tag="n1")
try:
    rN1, eN1, _ = _run_gate(userN, toolN, refN, _stN)
finally:
    _pipe.land_items_result = _orig_land
with db() as con:
    tN1 = con.execute(
        "select status from tool_calls where user_id=%s and tool_id=%s"
        " and client_ref=%s order by id desc limit 1",
        (userN, toolN, refN)).fetchone()
    upN = con.execute(
        "select count(*) c from uploads where user_id=%s",
        (userN,)).fetchone()
check("N1 attempt1 failed + 零 public result",
      tN1["status"] == "failed" and int(upN["c"]) == 0,
      f"{tN1['status']} up={upN['c']}")
# staged result 被清 (attempt 输出), source 无关 (无删除权)
check("N1b staged cleanup 只删 attempt result",
      _mkstaged(tag="n1").files[0].gokapi_id in _src_deleted, str(_src_deleted))

# attempt 2: 同 user/tool/client_ref/source — failed row 不挡 claim
rN2, eN2, _ = _run_gate(userN, toolN, refN, _stN)
with db() as con:
    tN2 = con.execute(
        "select status from tool_calls where user_id=%s"
        " and tool_id=%s and client_ref=%s order by id desc limit 1",
        (userN, toolN, refN)).fetchone()
    n_upN = int(con.execute(
        "select count(*) c from uploads where user_id=%s",
        (userN,)).fetchone()["c"])
    n_refundN = int(con.execute(
        "select count(*) c from ledger where user_id=%s and kind='refund'",
        (userN,)).fetchone()["c"])
check("N2 attempt2 同 invocation 重试成功 (failed 不占 live ref)",
      rN2 is not None and rN2.get("ok") and tN2["status"] == "ok",
      f"{rN2 and rN2.get('ok')} {tN2['status']}")
check("N2b 最终恰一个 public result / consume1 已退",
      n_upN == 1 and n_refundN == 1, f"up={n_upN} refunds={n_refundN}")
check("N2c source 未被 execution 删除 (零 source delete)",
      all(gid == _mkstaged(tag="n1").files[0].gokapi_id for gid in _src_deleted)
      and len(_src_deleted) == 1, str(_src_deleted))
_gk2.delete = _gk_orig2

# N3: durable source (uploads row) 不被 execution 删除
userN3 = _mkuser(_u_rsa + "n3")
toolN3 = f"rstn3_{_u_rsa}"
_mktool(toolN3, owner_id=None, cost=0)
_grant(userN3, 100, "manual_topup", note="rs")
from repo.uploads import insert_record as _ins_up
from utils import generate_public_id, public_file_url
with db() as con:
    pubN3 = generate_public_id(con)
    dur_gid = f"durable_src_{_u_rsa}"
    _ins_up(con, userN3, "127.0.0.1", pubN3, dur_gid, "in.txt",
            "text/plain", 10, 30, 100, ts_rsa + 86400 * 30,
            public_file_url(pubN3), "", ts_rsa)
delN3 = []
_gk2.delete = lambda gid: delN3.append(gid)
rN3, eN3, _ = _run_gate(userN3, toolN3, None, lambda: _stage_ok(toolN3, tag="n3"))
_gk2.delete = _gk_orig2
with db() as con:
    src_row = con.execute(
        "select count(*) c from uploads where public_id=%s",
        (pubN3,)).fetchone()
check("N3 durable source 执行后不被删除 (row 保留 + 零 delete 调用)",
      src_row is not None and int(src_row["c"]) == 1
      and dur_gid not in delN3, f"rows={src_row['c']} deleted={delN3}")

# semantic: orchestrator 无 source eager-delete
_orch2 = open(_os.path.join(REPO, "core", "execution", "orchestrator.py"),
              encoding="utf-8").read()
# the coherence pass: orchestrator owns ALL-OR-NOTHING staging cleanup (a later item
# failing deletes every staged-this-call object before the failure
# settles). Settlement cleanup stays in gate.py (PROVEN_ROLLBACK).
check("S1 orchestrator 暂存清理走 storage 适配器 (全量回收)",
      "storage_delete" in _orch2 and "delete_gokapi_file" not in _orch2)

# ══════════ O. 1e400 数值溢出 → typed 失败 + 退款 (真实 remote dispatch) ══════════
# json.loads 的 parse_constant 只拦 NaN/Infinity 字面量; 1e400 解析为
# inf。溢出值必须在响应解释层被拒 (invalid_response): call failed、
# consume 退款、零 payout/commission/revenue、result_json NULL — 绝不
# 把 Infinity 写进成功结果。
userOW = _mkuser(_u_rsa + "uow")
userO = _mkuser(_u_rsa + "uo")
toolO = f"rstoolo_{_u_rsa}"
with db() as con:
    con.execute(
        "insert into platform_tools (tool_key, owner_id, tool_id,"
        " execution, label_json, endpoint_json, accept_json, format_family,"
        " files_min, files_max, visibility, enabled, created_at, updated_at)"
        " values (%s, %s, %s, 'http', %s, %s, '[\".png\"]', 'image',"
        " 1, 2, 'public', TRUE, %s, %s)",
        (toolO, userOW, toolO, _json.dumps({"en": "RS-O"}),
         _json.dumps({"url": "https://ovf.example/api", "timeout_s": 30}),
         ts_rsa, ts_rsa))
    con.execute(
        "insert into pricing (tool_id, cost, enabled, updated_at)"
        " values (%s, 25, TRUE, %s)", (toolO, ts_rsa))
_grant(userO, 1000, "manual_topup", note="rs")
balO = spendable_balance(userO)

import remote as _rm
_orig_pinO, _orig_resO = _rm._pinned_request, _rm._resolve_host
_rm._resolve_host = lambda h, p: [
    (_rm.socket.AF_INET, _rm.socket.SOCK_STREAM,
     _rm.socket.IPPROTO_TCP, "", ("93.184.216.34", p))]
def _ovf_hop(url, method="GET", body=None, headers=None, timeout_s=60,
             max_bytes=None, deadline=None):
    return 200, b'{"x": 1e400}', None, "application/json", None
_rm._pinned_request = _ovf_hop
async def _remote_ovf():
    from execution.orchestrator import execute_remote
    return await execute_remote(
        {"ip": "127.0.0.1"},
        {"id": toolO, "tool_key": toolO, "owner_id": userOW,
         "endpoint_json": _json.dumps(
             {"url": "https://ovf.example/api", "timeout_s": 30})},
        [], {}, {"id": userO},
        {"expiry_days": 7, "allowed_downloads": 5})
try:
    resO, errO, _ = _run_gate(userO, toolO, None, _remote_ovf,
                              tool_owner=userOW)
except Exception:
    resO, errO = None, None
finally:
    _rm._pinned_request = _orig_pinO
    _rm._resolve_host = _orig_resO
with db() as con:
    tO = con.execute(
        "select id, status, result_json from tool_calls"
        " where user_id=%s and tool_id=%s order by id desc limit 1",
        (userO, toolO)).fetchone()
    _earnO = con.execute(
        "select count(*) c from ledger where kind in"
        " ('provider_earn','platform_commission','platform_revenue')"
        " and ref_id=%s", (str(tO["id"]),)).fetchone()["c"]
check("O1 1e400 overflow → typed failure (err_endpoint_failed)",
      errO is not None and errO.get("error") == "err_endpoint_failed"
      and resO is None, str(errO))
check("O2 call failed + result_json NULL",
      tO["status"] == "failed" and tO["result_json"] is None,
      dict(tO) if tO else None)
check("O3 consume refunded (balance restored)",
      spendable_balance(userO) == balO,
      f"{spendable_balance(userO)} vs {balO}")
check("O4 zero payout/commission/revenue rows", int(_earnO) == 0, _earnO)


# ══════════ CX: 取消收敛 — gate task 取消不得孤儿化已提交的扣费、
# 不得提前归还背压名额、不得退款已持久化的成功。受控交错 (Events
# 精确锚定线程内时刻), 断言的是持久事实 (ledger / tool_calls / 名额)。
# ══════════
import threading as _thr_c
from unittest.mock import patch as _patch_c
from execution import gate as _gate_c
from execution.invocation import ToolInvocation as _InvC
from execution.pool import inflight_count as _infl_c
from economy.billing import grant as _grant_c, spendable_balance as _bal_c
from governance.catalog import resolve as _resolve_c

section('Cancellation convergence: obligations survive gate cancel')

_u_cx = "cx" + _uuid.uuid4().hex[:6]
with db() as con:
    _row_cx = con.execute(
        "insert into users (username, email, password_hash, display_name,"
        " status, created_at, last_login_at) values (%s, %s, 'x', 'CX',"
        " 'active', %s, %s) returning id",
        (_u_cx, f"{_u_cx}@t.co", 1730000000, 1730000000)).fetchone()
    uid_cx = int(_row_cx["id"])
_grant_c(uid_cx, 500, "grant", idem_key=f"cx-grant-{_u_cx}")
_tool_c, _terr_c = _resolve_c("share", uid_cx)
_tool_c["id"] = _tool_c["tool_key"]
check("CX0 fixture: share resolved as a local tool",
      _terr_c is None and _tool_c.get("execution") == "local", _terr_c)


def _inv_c(ref):
    return _InvC("share", [], {}, {"expiry_days": 1,
                                   "allowed_downloads": 50}, ref)


def _call_row(ref):
    with db() as con:
        return con.execute(
            "select id, status, error from tool_calls where user_id=%s"
            " and client_ref=%s", (uid_cx, ref)).fetchone()


# ── CX1: consume 已提交后取消 (工作线程暂停中) → 收敛线程、精确退款、
#    failed 收尾、名额在结算后才归还; 同 ref 重试不再 409。
def _cx1():
    ref = f"cx1-{_u_cx}"
    entered, release = _thr_c.Event(), _thr_c.Event()
    _orig_consume = _gate_c.consume
    orch_n = {"n": 0}

    def _paused_consume(*a, **k):
        charge = _orig_consume(*a, **k)
        entered.set()
        if not release.wait(5):
            raise RuntimeError("cx1 release timeout")
        return charge

    async def _orch():
        orch_n["n"] += 1
        raise RuntimeError("cx1 must not execute")

    async def _scenario():
        with _patch_c("execution.affordability.effective_quote",
                      return_value={"enabled": True, "cost": 5}), \
             _patch_c.object(_gate_c, "consume", _paused_consume):
            task = asyncio.create_task(
                _gate_c.run({"id": uid_cx}, _tool_c, _inv_c(ref), _orch))
            if not await asyncio.to_thread(entered.wait, 3):
                raise RuntimeError("cx1: consume never committed")
            slot_before = _infl_c()
            task.cancel()
            await asyncio.sleep(0.1)   # cancellation handler runs; the
            slot_mid = _infl_c()       # worker is STILL parked (no release yet)
            _thr_c.Timer(0.05, release.set).start()
            try:
                await task
                cancelled = False
            except asyncio.CancelledError:
                cancelled = True
            return cancelled, slot_before, slot_mid, _infl_c()

    bal_before = _bal_c(uid_cx)
    cancelled, slot_before, slot_mid, slot_after = asyncio.run(_scenario())
    row = _call_row(ref)
    check("CX1a cancel after committed consume → CancelledError propagates "
          "after convergence", cancelled is True, cancelled)
    check("CX1b slot held across the cancel (released only after settlement)",
          slot_before == 1 and slot_mid == 1 and slot_after == 0,
          (slot_before, slot_mid, slot_after))
    check("CX1c exact refund — balance restored",
          _bal_c(uid_cx) == bal_before,
          f"{_bal_c(uid_cx)} vs {bal_before}")
    check("CX1d call settled failed('cancelled') — not left running",
          row is not None and row["status"] == "failed"
          and row["error"] == "cancelled", dict(row) if row else None)
    check("CX1e orchestrate never ran for the cancelled attempt",
          orch_n["n"] == 0, orch_n["n"])
    # same client_ref retry: a NEW claim proceeds (never 409 in-progress)
    async def _orch_ok():
        orch_n["n"] += 1
        from execution.results import StagedItems
        return StagedItems(order=["text"],
                           inline=[{"kind": "text", "text": "ok"}])
    with _patch_c("execution.affordability.effective_quote",
                  return_value={"enabled": True, "cost": 5}):
        payload, err, status = asyncio.run(
            _gate_c.run({"id": uid_cx}, _tool_c, _inv_c(ref), _orch_ok))
    check("CX1f same-ref retry after cancelled settle → executes (no 409)",
          err is None and payload.get("ok") is True and status is None,
          (status, err))
    return payload


_cx1()


# ── CX2: 取消落在执行段中位 → 已开始的执行不可丢弃: 整个 orchestrate
#    被保护并收敛 (受既有执行超时约束), 按实际结果结算后取消才传播。
#    收敛为成功 → durable ok (扣费保留、可重放); 收敛为失败 → 精确退款。
def _cx2():
    ref = f"cx2a-{_u_cx}"
    started = asyncio.Event()
    go = asyncio.Event()

    async def _orch_ok():
        from execution.results import StagedItems
        started.set()
        await go.wait()          # real work parked; released during convergence
        return StagedItems(order=["text"],
                           inline=[{"kind": "text", "text": "ok"}])

    async def _scenario():
        with _patch_c("execution.affordability.effective_quote",
                      return_value={"enabled": True, "cost": 5}):
            task = asyncio.create_task(
                _gate_c.run({"id": uid_cx}, _tool_c, _inv_c(ref), _orch_ok))
            await started.wait()
            task.cancel()
            await asyncio.sleep(0.1)   # cancellation handler runs; work parked
            slot_mid = _infl_c()
            asyncio.get_running_loop().call_later(0.05, go.set)
            try:
                await task
                cancelled = False
            except asyncio.CancelledError:
                cancelled = True
            return cancelled, slot_mid, _infl_c()

    bal_before = _bal_c(uid_cx)
    cancelled, slot_mid, slot_after = asyncio.run(_scenario())
    row = _call_row(ref)
    check("CX2a cancel during execution → converged work still settled "
          "as durable success (charged, replayable)",
          cancelled is True and slot_mid == 1 and slot_after == 0
          and _bal_c(uid_cx) == bal_before - 5
          and row is not None and row["status"] == "ok",
          (cancelled, slot_mid, slot_after, _bal_c(uid_cx) - bal_before,
           dict(row) if row else None))
    # replay of the converged success: no re-execution, no second charge
    async def _never():
        raise RuntimeError("cx2a replay must not re-execute")

    async def _replay():
        with _patch_c("execution.affordability.effective_quote",
                      return_value={"enabled": True, "cost": 5}):
            return await _gate_c.run({"id": uid_cx}, _tool_c, _inv_c(ref),
                                     _never)
    payload, err, _s = asyncio.run(_replay())
    check("CX2a2 same-ref retry → replay of the converged result",
          err is None and (payload or {}).get("replay") is True
          and _bal_c(uid_cx) == bal_before - 5,
          (err, (payload or {}).get("replay")))

    # converged FAILURE: the parked work raises after release → exact
    # refund + failed(the execution's own code), cancel propagates
    ref = f"cx2b-{_u_cx}"
    started2 = asyncio.Event()
    go2 = asyncio.Event()

    async def _orch_fail():
        started2.set()
        await go2.wait()
        raise ExecutionError("err_endpoint_timeout")

    async def _scenario2():
        with _patch_c("execution.affordability.effective_quote",
                      return_value={"enabled": True, "cost": 5}):
            task = asyncio.create_task(
                _gate_c.run({"id": uid_cx}, _tool_c, _inv_c(ref), _orch_fail))
            await started2.wait()
            task.cancel()
            asyncio.get_running_loop().call_later(0.05, go2.set)
            try:
                await task
                cancelled = False
            except asyncio.CancelledError:
                cancelled = True
            return cancelled, _infl_c()

    bal_before = _bal_c(uid_cx)
    cancelled, slot_after = asyncio.run(_scenario2())
    row = _call_row(ref)
    check("CX2b converged execution failure → exact refund, failed(its code)",
          cancelled is True and slot_after == 0
          and _bal_c(uid_cx) == bal_before
          and row is not None and row["status"] == "failed"
          and row["error"] == "err_endpoint_timeout",
          (cancelled, slot_after, _bal_c(uid_cx) - bal_before,
           dict(row) if row else None))


_cx2()


# ── CX3: 取消落在 _finish 线程暂停中, 成功结算随后落地 → durable ok
#    站住 (不退款不删结果), 同 ref 重试重放, 不二次扣费。
def _cx3():
    ref = f"cx3-{_u_cx}"
    entered, release = _thr_c.Event(), _thr_c.Event()
    _orig_finish = _gate_c._finish

    def _paused_finish(*a, **k):
        entered.set()
        if not release.wait(5):
            raise RuntimeError("cx3 release timeout")
        return _orig_finish(*a, **k)

    async def _orch():
        from execution.results import StagedItems
        return StagedItems(order=["text"],
                           inline=[{"kind": "text", "text": "ok"}])

    async def _scenario():
        with _patch_c("execution.affordability.effective_quote",
                      return_value={"enabled": True, "cost": 5}), \
             _patch_c.object(_gate_c, "_finish", _paused_finish):
            task = asyncio.create_task(
                _gate_c.run({"id": uid_cx}, _tool_c, _inv_c(ref), _orch))
            if not await asyncio.to_thread(entered.wait, 3):
                raise RuntimeError("cx3: finish never started")
            task.cancel()
            await asyncio.sleep(0.1)   # finish thread is STILL parked here
            slot_mid = _infl_c()
            _thr_c.Timer(0.05, release.set).start()
            try:
                await task
                cancelled = False
            except asyncio.CancelledError:
                cancelled = True
            return cancelled, slot_mid, _infl_c()

    bal_before = _bal_c(uid_cx)
    cancelled, slot_mid, slot_after = asyncio.run(_scenario())
    row = _call_row(ref)
    check("CX3a cancel during finalize thread → converged, then cancelled",
          cancelled is True, cancelled)
    check("CX3a2 slot held while the finalize thread is still parked",
          slot_mid == 1, slot_mid)
    check("CX3b durable success is NOT refunded (stays charged)",
          _bal_c(uid_cx) == bal_before - 5,
          f"{_bal_c(uid_cx)} vs {bal_before - 5}")
    check("CX3c call status ok with a persisted result",
          row is not None and row["status"] == "ok"
          and row["error"] is None, dict(row) if row else None)
    check("CX3d slot released", slot_after == 0, slot_after)
    # same client_ref retry: replay of the durable result, no re-execution,
    # no second charge
    async def _orch_never():
        raise RuntimeError("cx3 replay must not re-execute")

    async def _retry():
        with _patch_c("execution.affordability.effective_quote",
                      return_value={"enabled": True, "cost": 5}):
            return await _gate_c.run({"id": uid_cx}, _tool_c, _inv_c(ref),
                                     _orch_never)

    payload, err, status = asyncio.run(_retry())
    check("CX3e same-ref retry → replay (durable result, single charge)",
          err is None and payload.get("ok") is True
          and payload.get("replay") is True
          and _bal_c(uid_cx) == bal_before - 5,
          (status, err, _bal_c(uid_cx)))


_cx3()

# ── CX4: 三个取消场景后零 running 残留 — 恢复器无事可做 (取消路径不
#    依赖重启收尾)。
_rec_cx = _gate_c.recover_abandoned_calls()
check("CX4a no running calls left behind (recovery has nothing to settle)",
      _rec_cx["scanned"] == 0, _rec_cx)


# ── CX5: 结算异常 + 核验阶段取消 — resolve_success_outcome 已读到
#    proven_rollback 后线程暂停中取消: 取消不得跳过清理/精确退款/failed
#    收尾 (取消只能在已知持久义务完成后传播)。
def _cx5():
    ref = f"cx5-{_u_cx}"
    entered, release = _thr_c.Event(), _thr_c.Event()
    _orig_resolve = _gate_c.resolve_success_outcome

    def _paused_resolve(call_id):
        out = _orig_resolve(call_id)          # real read: running ⇒ rollback
        entered.set()
        if not release.wait(5):
            raise RuntimeError("cx5 release timeout")
        return out

    async def _orch():
        from execution.results import StagedItems
        return StagedItems(order=["text"],
                           inline=[{"kind": "text", "text": "ok"}])

    async def _scenario():
        with _patch_c("execution.affordability.effective_quote",
                      return_value={"enabled": True, "cost": 5}), \
             _patch_c.object(_gate_c, "_finish",
                             side_effect=RuntimeError("settle failed")), \
             _patch_c.object(_gate_c, "resolve_success_outcome",
                             _paused_resolve):
            task = asyncio.create_task(
                _gate_c.run({"id": uid_cx}, _tool_c, _inv_c(ref), _orch))
            if not await asyncio.to_thread(entered.wait, 3):
                raise RuntimeError("cx5: resolve never ran")
            task.cancel()
            _thr_c.Timer(0.05, release.set).start()
            try:
                await task
                cancelled = False
            except asyncio.CancelledError:
                cancelled = True
            return cancelled, _infl_c()

    bal_before = _bal_c(uid_cx)
    cancelled, slot_after = asyncio.run(_scenario())
    row = _call_row(ref)
    check("CX5a cancel during rollback verification → exact refund completes",
          cancelled is True and _bal_c(uid_cx) == bal_before,
          (cancelled, _bal_c(uid_cx) - bal_before))
    check("CX5b call settled failed (never left running)",
          row is not None and row["status"] == "failed", dict(row) if row else None)
    check("CX5c slot released only after the settlement", slot_after == 0,
          slot_after)
    _rec5 = _gate_c.recover_abandoned_calls()
    check("CX5d nothing left for restart recovery", _rec5["scanned"] == 0,
          _rec5)


_cx5()


# ── CX6: 真实执行 + 输出暂存取消窗口 — 真 orchestrator (execute_local)
#    + 受控 executor; 输出写经隔离 storage adapter (真 write 打进 fake
#    gokapi 后暂停, 结果未交回)。取消时: 名额仍持有; 收敛后: 对象被接住
#    成为 durable 结果 (非孤儿), 账本扣费一次, 状态 ok, 重放可用。
def _cx6():
    import concurrent.futures as _cf_c
    from execution import orchestrator as _orch_mod_c
    import storage as _storage_c
    ref = f"cx6-{_u_cx}"
    entered, release = _thr_c.Event(), _thr_c.Event()
    _orig_write = _orch_mod_c.storage_write
    _gids6 = []

    def _paused_write(stream, filename, ct, ed, ad, size):
        out = _orig_write(stream, filename, ct, ed, ad, size)  # REAL write
        _gids6.append(out["FileInfo"]["Id"])
        entered.set()
        if not release.wait(5):
            raise RuntimeError("cx6 release timeout")
        return out

    _tool6 = {"id": "share", "tool_key": "share", "execution": "local",
              "owner_id": None, "outputs_json": '["file"]'}
    _expl6 = _cf_c.ThreadPoolExecutor(max_workers=1)

    def _pt6(tool_id, sources, params, body):
        return {"items": [{"kind": "file", "filename": "out6.bin",
                           "content_type": "application/octet-stream",
                           "data": b"cx6 payload"}]}

    async def _orch_real():
        return await _orch_mod_c.execute_local(
            {"ip": "127.0.0.1"}, _tool6, [], {}, {"id": uid_cx},
            _pt6, _expl6, {"expiry_days": 1, "allowed_downloads": 5})

    async def _scenario():
        with _patch_c("execution.affordability.effective_quote",
                      return_value={"enabled": True, "cost": 5}), \
             _patch_c.object(_orch_mod_c, "storage_write", _paused_write):
            task = asyncio.create_task(
                _gate_c.run({"id": uid_cx}, _tool6, _inv_c(ref), _orch_real))
            if not await asyncio.to_thread(entered.wait, 3):
                raise RuntimeError("cx6: write never landed")
            task.cancel()
            await asyncio.sleep(0.1)     # write thread still parked here
            slot_mid = _infl_c()
            _thr_c.Timer(0.05, release.set).start()
            try:
                await task
                cancelled = False
            except asyncio.CancelledError:
                cancelled = True
            return cancelled, slot_mid, _infl_c()

    bal_before = _bal_c(uid_cx)
    cancelled, slot_mid, slot_after = asyncio.run(_scenario())
    row = _call_row(ref)
    _obj_live = bool(_gids6) and _storage_c.info(_gids6[0]) is not None
    check("CX6a slot held while the output write thread is still parked",
          slot_mid == 1 and slot_after == 0, (slot_mid, slot_after))
    check("CX6b converged write → object caught as the durable result "
          "(not an orphan), call ok, charged once",
          cancelled is True and row is not None and row["status"] == "ok"
          and _obj_live and _bal_c(uid_cx) == bal_before - 5,
          (cancelled, dict(row) if row else None, _obj_live,
           _bal_c(uid_cx) - bal_before))
    # replay: same result, object still the result, no second charge
    async def _never6():
        raise RuntimeError("cx6 replay must not re-execute")

    async def _replay6():
        with _patch_c("execution.affordability.effective_quote",
                      return_value={"enabled": True, "cost": 5}):
            return await _gate_c.run({"id": uid_cx}, _tool6, _inv_c(ref),
                                     _never6)
    payload, err, _s = asyncio.run(_replay6())
    check("CX6c same-ref retry → replay, object intact, single charge",
          err is None and (payload or {}).get("replay") is True
          and _storage_c.info(_gids6[0]) is not None
          and _bal_c(uid_cx) == bal_before - 5,
          (err, (payload or {}).get("replay")))
    # fixture cleanup: the staged result object + local executor
    for _gid6 in _gids6:
        try:
            _storage_c.delete(_gid6)
        except Exception:
            pass
    _expl6.shutdown(wait=False)


_cx6()


# ── CX7: durable helper 终态 — 外层重复取消/底层异常/底层取消/带待完成
#    工作的事件循环关闭。底层取消与关闭场景在子进程里跑: 外部超时即
#    判红, 绝不挂住 CI。
def _cx7():
    import subprocess as _sp_c
    import sys as _sys_c
    from execution.durable import spawn as _spawn_c, await_durable as _ad_c

    import time as _time_c
    # (a) repeated outer cancels are absorbed; the result still lands
    async def _a():
        t = _spawn_c(lambda: (_time_c.sleep(0.3), "done")[1])
        outer = asyncio.ensure_future(_ad_c(t))
        for _ in range(3):
            await asyncio.sleep(0.06)
            outer.cancel()
        try:
            res, cancelled = await asyncio.wait_for(outer, 5)
            return res, cancelled
        except asyncio.TimeoutError:
            return "timeout", None
    res_a, cancelled_a = asyncio.run(_a())
    check("CX7a repeated outer cancels absorbed — result still returns",
          res_a == "done" and cancelled_a is True, (res_a, cancelled_a))

    # (b) the underlying task's own exception propagates (not a cancel)
    async def _b():
        def _boom():
            raise RuntimeError("boom")
        try:
            await _ad_c(_spawn_c(_boom))
            return "no-raise"
        except RuntimeError:
            return "runtimeerror"
        except asyncio.CancelledError:
            return "cancelled"
    check("CX7b underlying exception propagates as itself",
          asyncio.run(_b()) == "runtimeerror", "")

    # (c)+(d) run in a subprocess: the pre-fix helper busy-loops on a
    # cancelled underlying task (never yields to the loop), so only an
    # external timeout can bound the broken behavior.
    _script_c = '''
import asyncio, sys, time
sys.path.insert(0, "core")
from execution.durable import spawn, await_durable

async def main():
    inner = asyncio.ensure_future(asyncio.sleep(30))
    async def absorb():
        return await await_durable(inner)
    a = asyncio.ensure_future(absorb())
    await asyncio.sleep(0.05)
    a.cancel()               # outer cancel → absorb enters phase 2
    await asyncio.sleep(0.05)
    inner.cancel()           # the underlying task itself is cancelled
    t0 = time.monotonic()
    try:
        await asyncio.wait_for(asyncio.shield(a), 5)
        print("UNEXPECTED_RESULT")
    except asyncio.CancelledError:
        print("UNDERLYING_CANCEL_PROPAGATED", time.monotonic() - t0 < 3)
    except asyncio.TimeoutError:
        print("ABSORB_HUNG")
asyncio.run(main())
print("UNDERLYING_CANCEL_DONE")
'''
    try:
        _r = _sp_c.run([_sys_c.executable, "-c", _script_c],
                       cwd=REPO, capture_output=True, text=True, timeout=15)
        _ok_c = ("UNDERLYING_CANCEL_PROPAGATED True" in _r.stdout
                 and "UNDERLYING_CANCEL_DONE" in _r.stdout)
    except _sp_c.TimeoutExpired:
        _ok_c = False
    check("CX7c cancelled underlying task → prompt CancelledError, no hang",
          _ok_c, "")

    _script_d = '''
import asyncio, sys, time
sys.path.insert(0, "core")
from execution.durable import spawn, await_durable

async def main():
    task = spawn(lambda: time.sleep(0.05))
    async def absorber():
        return await await_durable(task)
    a = asyncio.ensure_future(absorber())
    await asyncio.sleep(0.02)                 # absorber parked in phase 1
    a.cancel()                                # phase 2 begins
    await asyncio.sleep(0.02)                 # absorber parked in phase 2
    task.cancel()                             # underlying cancelled too
    print("MAIN_RETURN_SHUTDOWN", flush=True)
    # main returns with the absorber mid-convergence — asyncio.run's
    # shutdown gather must complete: a busy-looping absorber hangs here

asyncio.run(main())
print("LOOP_CLOSED_CLEANLY", flush=True)
'''
    try:
        _r = _sp_c.run([_sys_c.executable, "-c", _script_d],
                       cwd=REPO, capture_output=True, text=True, timeout=15)
        _ok_d = ("LOOP_CLOSED_CLEANLY" in _r.stdout
                 and "MAIN_RETURN_SHUTDOWN" in _r.stdout)
    except _sp_c.TimeoutExpired:
        _ok_d = False
    check("CX7d loop shutdown with pending durable work closes cleanly",
          _ok_d, "")


_cx7()


# ── CX8/CX9: 取消意图不得丢失 — finish 等待期间取消后, converge 抛出
#    finish 自身异常进入核验: 已记录的取消必须贯穿核验/清理/结算, 持久
#    义务处理完毕后向调用方传播 CancelledError (而不是吞成正常返回)。
def _cx_finish_cancel(committed: bool):
    """Shared driver: cancel while _finish is parked; on release the
    wrapper raises (pre-commit, or AFTER a real committed _finish =
    ACK loss). Returns (cancelled, slot_after, ref)."""
    ref = f"cx{'9' if committed else '8'}-{_u_cx}"
    entered, release = _thr_c.Event(), _thr_c.Event()
    _orig_finish = _gate_c._finish

    def _finish_then_raise(*a, **kw):
        if committed:
            _orig_finish(*a, **kw)      # the settlement COMMITS
        entered.set()
        if not release.wait(5):
            raise RuntimeError("cx8/9 release timeout")
        raise RuntimeError("ack loss after commit" if committed
                           else "proven rollback before commit")

    async def _orch():
        from execution.results import StagedItems
        return StagedItems(order=["text"],
                           inline=[{"kind": "text", "text": "cancel intent"}])

    async def _scenario():
        with _patch_c("execution.affordability.effective_quote",
                      return_value={"enabled": True, "cost": 5}), \
             _patch_c.object(_gate_c, "_finish", _finish_then_raise):
            task = asyncio.create_task(
                _gate_c.run({"id": uid_cx}, _tool_c, _inv_c(ref), _orch))
            if not await asyncio.to_thread(entered.wait, 3):
                raise RuntimeError("cx8/9: finish never parked")
            task.cancel()
            await asyncio.sleep(0)      # cancellation delivered at the shield
            release.set()               # finish commits/raises, convergence ends
            try:
                await task
                cancelled = False
            except asyncio.CancelledError:
                cancelled = True
            return cancelled, _infl_c()

    return ref, asyncio.run(_scenario())


# CX8: 提交前异常 → 核验判 proven rollback → 清理+精确退款+failed,
# 完成后取消传播。
_bal8 = _bal_c(uid_cx)
_ref8, (_cancelled8, _slot8) = _cx_finish_cancel(committed=False)
_row8 = _call_row(_ref8)
check("CX8a cancel during finish + pre-commit exception → refund, failed",
      _row8 is not None and _row8["status"] == "failed"
      and _bal_c(uid_cx) == _bal8, dict(_row8) if _row8 else None)
check("CX8b the pending cancel finally propagates (intent not swallowed)",
      _cancelled8 is True, _cancelled8)
check("CX8c slot returned after the settlement", _slot8 == 0, _slot8)

# CX9: 已提交 + ACK 丢失 → 核验判 durable success → 结果保留、单次
# 扣费、可重放, 完成后取消传播。
_bal9 = _bal_c(uid_cx)
_ref9, (_cancelled9, _slot9) = _cx_finish_cancel(committed=True)
_row9 = _call_row(_ref9)
check("CX9a cancel during finish + committed ACK loss → durable ok, "
      "single charge, result kept",
      _row9 is not None and _row9["status"] == "ok"
      and _bal_c(uid_cx) == _bal9 - 5, dict(_row9) if _row9 else None)
check("CX9b the pending cancel finally propagates (intent not swallowed)",
      _cancelled9 is True, _cancelled9)
check("CX9c slot returned after the settlement", _slot9 == 0, _slot9)
# replay of the ACK-lost durable result: same ref, no re-execution, no
# second charge
async def _never9():
    raise RuntimeError("cx9 replay must not re-execute")


async def _replay9():
    with _patch_c("execution.affordability.effective_quote",
                  return_value={"enabled": True, "cost": 5}):
        return await _gate_c.run({"id": uid_cx}, _tool_c, _inv_c(_ref9),
                                 _never9)
_payload9, _err9, _s9 = asyncio.run(_replay9())
check("CX9d same-ref retry → replay, still single charge",
      _err9 is None and (_payload9 or {}).get("replay") is True
      and _bal_c(uid_cx) == _bal9 - 5,
      (_err9, (_payload9 or {}).get("replay")))


# ── CX10: 取消意图 × 底层结果 对照矩阵 — 取消落在余额读取或暂存清理
#    等待期间, 底层随后成功返回或抛普通异常: 四种组合下持久义务按实际
#    结果结算, 已观察到的取消最终都必须传播 (不得依赖成功解包元组)。
import storage as _storage_m_c
from execution.results import StagedItems as _StagedItems_c, \
    StagedFileItem as _StagedFileItem_c


def _cx10(stage: str, throws: bool):
    """stage 'balance' (committed ACK loss → durable-success path, pause
    the reconciliation balance read) or 'delete' (pre-commit failure →
    proven-rollback path, pause the staged cleanup). Returns
    (cancelled, slot_after, ref)."""
    ref = f"cx10{stage[0]}{'t' if throws else 's'}-{_u_cx}"
    entered, release = _thr_c.Event(), _thr_c.Event()
    _orig_finish = _gate_c._finish
    _orig_balance = _gate_c.spendable_balance
    _orig_delete = _storage_m_c.delete
    _calls = {"balance": 0}

    def _finish_patched(*a, **kw):
        if stage == "balance":
            _orig_finish(*a, **kw)          # commits → durable success
        raise RuntimeError("ack loss" if stage == "balance"
                           else "pre-commit failure")

    def _balance_patched(user_id):
        _calls["balance"] += 1
        out = _orig_balance(user_id)
        if _calls["balance"] == 2:          # the RECONCILIATION read
            entered.set()                   # (call 1 ran inside _finish)
            if not release.wait(5):
                raise RuntimeError("cx10 balance release timeout")
            if throws:
                raise RuntimeError("balance projection unavailable")
        return out

    def _delete_patched(gid):
        entered.set()
        if not release.wait(5):
            raise RuntimeError("cx10 delete release timeout")
        if throws:
            raise RuntimeError("delete acknowledgement lost")
        return _orig_delete(gid)

    if stage == "balance":
        async def _orch():
            return _StagedItems_c(order=["text"],
                                  inline=[{"kind": "text", "text": "m"}])
    else:
        async def _orch():
            return _StagedItems_c(
                order=["file"], files=[_StagedFileItem_c(
                    gokapi_id=f"cx10-{ref}", filename="cx10.bin",
                    content_type="application/octet-stream", size=8,
                    expiry_days=1, allowed_downloads=5, expire_at=1,
                    url_hotlink="")])

    async def _scenario():
        with _patch_c("execution.affordability.effective_quote",
                      return_value={"enabled": True, "cost": 5}), \
             _patch_c.object(_gate_c, "_finish", _finish_patched), \
             _patch_c.object(_gate_c, "spendable_balance",
                             _balance_patched), \
             _patch_c.object(_storage_m_c, "delete", _delete_patched):
            task = asyncio.create_task(
                _gate_c.run({"id": uid_cx}, _tool_c, _inv_c(ref), _orch))
            if not await asyncio.to_thread(entered.wait, 3):
                raise RuntimeError("cx10: pause point never reached")
            task.cancel()
            await asyncio.sleep(0)          # delivered at the durable await
            release.set()
            try:
                await task
                cancelled = False
            except asyncio.CancelledError:
                cancelled = True
            return cancelled, _infl_c()

    return ref, asyncio.run(_scenario())


for _stage10, _want_status, _want_delta in (("balance", "ok", -5),
                                            ("delete", "failed", 0)):
    for _throws10 in (False, True):
        _bal10 = _bal_c(uid_cx)
        _ref10, (_cancelled10, _slot10) = _cx10(_stage10, _throws10)
        _row10 = _call_row(_ref10)
        _tag10 = f"CX10-{_stage10}-{'throws' if _throws10 else 'returns'}"
        check(f"{_tag10} durable facts per outcome "
              f"(status={_want_status}, balance {_want_delta}, slot back)",
              _row10 is not None and _row10["status"] == _want_status
              and _bal_c(uid_cx) == _bal10 + _want_delta and _slot10 == 0,
              (dict(_row10) if _row10 else None,
               _bal_c(uid_cx) - _bal10, _slot10))
        check(f"{_tag10} observed cancel finally propagates",
              _cancelled10 is True, _cancelled10)

with db() as con:
    con.execute("delete from tool_calls where user_id=%s", (uid_cx,))
    con.execute("delete from uploads where user_id=%s", (uid_cx,))
    con.execute("delete from ledger where user_id=%s", (uid_cx,))
    purge_users(con, [uid_cx])


# ══════════ cleanup (FK-safe, prefix-scoped) ══════════
_uids = [user1, owner, user2, user3, user4, user5, user6, user7, user8, user9,
          userM, userM4, userN, userN3, userO, userOW]
with db() as con:
    for uid_rsa in _uids:
        con.execute("delete from operator_audit where actor_user_id=%s", (uid_rsa,))
with db() as con:
    for uid_rsa in _uids:
        con.execute("delete from tool_calls where user_id=%s", (uid_rsa,))
        con.execute("delete from uploads where user_id=%s", (uid_rsa,))
        con.execute("delete from ledger where user_id=%s", (uid_rsa,))
with db() as con:
    con.execute("delete from ledger where user_id in (%s)" % ",".join(str(u) for u in _uids))
    con.execute("delete from pricing where tool_id like %s", (f"rstool%_{_u_rsa}%",))
    # the M/N-section tools use their own prefixes — cover them too
    con.execute("delete from pricing where tool_id like %s", (f"rst%_{_u_rsa}%",))
    con.execute("delete from platform_tools where tool_key like %s",
                (f"rstool%_{_u_rsa}%",))
    con.execute("delete from platform_tools where tool_key like %s",
                (f"rst%_{_u_rsa}%",))
for uid_rsa in _uids:
    with db() as con:
        con.execute("delete from sessions where user_id=%s", (uid_rsa,))
        con.execute("delete from api_keys where user_id=%s", (uid_rsa,))
        purge_users(con, [uid_rsa])
print("cleanup: settlement fixtures removed")


finish()
