#!/usr/bin/env python3
"""System tools execution canonical.

Every first-party tool registered via tools.load_all() must run end-to-end
through the REAL execution.worker.process_tool with fixed in-test fixtures:

    - returns the IO-1 {"items": [...]} shape
    - every item kind is inside the tool's declared outputs
    - every file item decodes (images open, PDF starts with %PDF, ZIP
      lists entries, ICO exposes its size table)

Specialized assertions pin the per-tool contracts (privacy metadata
stripping / format keeping / PSNR floor, compress auto+targets with
transparency survival, ico multi sizes, crop_white bbox).
A newly registered tool without a fixture FAILS this suite — real-run
coverage is mandatory for every system tool.
"""
import io
import math
import os
import random
import sys
import zipfile

import os as _os
import sys as _sys
_REPO = _os.path.abspath(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", ".."))

# ── FAIL-CLOSED GATE ─────────────────────────────────────────────────────
# Canonical integration tests run ONLY against an explicitly provided
# isolated service port + isolated test database. No defaults, no
# production fallbacks. Refuse to run before any HTTP request or DB write.
_TEST_PORT = _os.environ.get("FLASHSHARE_TEST_PORT")
if not _TEST_PORT:
    _sys.stderr.write("ERROR: FLASHSHARE_TEST_PORT is required (isolated service port, never the production port).\n")
    _sys.exit(2)
try:
    _TEST_PORT_INT = int(_TEST_PORT, 10)
except ValueError:
    _sys.stderr.write("ERROR: FLASHSHARE_TEST_PORT must be an integer port number.\n")
    _sys.exit(2)
if _TEST_PORT_INT == 8000:
    _sys.stderr.write("ERROR: FLASHSHARE_TEST_PORT=8000 is the production port; canonical tests must use an isolated port.\n")
    _sys.exit(2)
TEST_DB = _os.environ.get("TEST_DB_URL")
if not TEST_DB:
    _sys.stderr.write("ERROR: TEST_DB_URL is required (isolated test database DSN).\n")
    _sys.exit(2)

_sys.path.insert(0, _REPO)
_sys.path.insert(0, _os.path.join(_REPO, "core"))
_sys.path.insert(0, _os.path.join(_REPO, "app"))

from PIL import Image, ImageChops, ImageDraw, ImageStat

from tools import load_all, get_tool, ToolError
from governance.tool_params import enforce_params_decl
from execution.worker import process_tool

load_all()

FAIL = []
def check(name, ok, detail=""):
    print(("✓ " if ok else "✗ ") + name + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAIL.append(name)


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
    return tool, process_tool(tool_id, sources, params)


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
        check(f"privacy/{preset} filename photo_clean.jpg",
              it["filename"] == "photo_clean.jpg", it["filename"])
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
          and out["items"][0]["filename"] == "x_clean.jpg", out["items"][0]["filename"])


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
    _, out = run_tool("beautify", {"files": [(badge_png(), "badge.png", "image/png")],
                                   "params": {}})
    img = Image.open(io.BytesIO(out["items"][0]["data"]))
    src = Image.open(io.BytesIO(badge_png()))
    pad = 60
    check("beautify padded container (PADDING=60)",
          img.size == (src.size[0] + pad * 2, src.size[1] + pad * 2), str(img.size))


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

print()
if FAIL:
    print(f"FAILED {len(FAIL)} checks:")
    for name in FAIL:
        print("  ✗", name)
    sys.exit(1)
print("ALL SYSTEM-TOOLS CANONICAL CHECKS PASSED")
