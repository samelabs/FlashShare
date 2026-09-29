import os
import re

APP_NAME = "518.PLUS"

# ── Product version authority (single source: repo root VERSION) ──
# The machine-readable release authority is the VERSION file; every runtime
# product-version expression derives from it. No fallback, no git/tag/
# changelog inference — a missing or malformed VERSION fails loudly.


def _read_product_version() -> str:
    import pathlib
    vf = pathlib.Path(__file__).resolve().parent.parent / "VERSION"
    try:
        raw = vf.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"VERSION file unreadable: {vf}") from exc
    v = raw.strip()
    if not re.fullmatch(r"[0-9]+\.[0-9]+", v):
        raise RuntimeError(f"VERSION malformed: {raw!r} (expected major.minor)")
    return v


PRODUCT_VERSION = _read_product_version()
SEO_TITLE = "518.PLUS"
SEO_DESC = "Upload, share, done. Fast temporary file links with QR codes."

def _required_env(name: str) -> str:
    """Mandatory deployment value: no silent default — a missing
    variable aborts startup naming exactly what is absent."""
    val = (os.environ.get(name) or "").strip()
    if not val:
        raise RuntimeError(f"{name} environment variable must be set")
    return val


BASE_URL = _required_env("FLASHSHARE_BASE_URL").rstrip("/")
SOCIAL_IMAGE_PATH = "/static/images/og-image.png?v=3"
SOCIAL_IMAGE_URL = f"{BASE_URL}{SOCIAL_IMAGE_PATH}"

# Extra browser origins admitted on /mcp besides the BASE_URL origin
# (comma-separated, e.g. "https://app.example.com,http://localhost:6274").
MCP_ALLOWED_ORIGINS = tuple(
    o.strip() for o in os.environ.get("FLASHSHARE_MCP_ALLOWED_ORIGINS", "").split(",")
    if o.strip())

X_CLIENT_ID = os.environ.get("FLASHSHARE_X_CLIENT_ID", "")
X_CLIENT_SECRET = os.environ.get("FLASHSHARE_X_CLIENT_SECRET", "")

GOKAPI_URL = _required_env("FLASHSHARE_GOKAPI_URL").rstrip("/")
GOKAPI_KEY = _required_env("FLASHSHARE_GOKAPI_KEY")

SESSION_SECRET = os.environ.get("FLASHSHARE_SESSION_SECRET", "")

# Public support channel (email or URL). Empty = support surfaces hidden.
PUBLIC_SUPPORT_CHANNEL = os.environ.get("FLASHSHARE_PUBLIC_SUPPORT_CHANNEL", "").strip()
if not SESSION_SECRET:
    raise RuntimeError("FLASHSHARE_SESSION_SECRET environment variable must be set")

MAX_UPLOAD_BYTES = int(os.environ.get("FLASHSHARE_MAX_UPLOAD_MB", "50")) * 1024 * 1024
# Total size cap for one multi-file batch (upload + tool stages).
MAX_BATCH_BYTES = int(os.environ.get("FLASHSHARE_MAX_BATCH_MB", "100")) * 1024 * 1024
DAILY_UPLOADS = int(os.environ.get("FLASHSHARE_DAILY_UPLOADS", "100"))
DAILY_BYTES = int(os.environ.get("FLASHSHARE_DAILY_MB", "1024")) * 1024 * 1024

COOKIE_NAME = "flashshare_session"
SESSION_TTL = 30 * 24 * 3600
OAUTH_TTL = 10 * 60

PUBLIC_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,48}$")

ALLOWED_EXT = {
    # Images (SVG 属图像格式, 含于 image 族)
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".ico", ".heic", ".heif", ".tiff", ".tif", ".avif", ".svg",
    # Documents
    ".pdf", ".txt", ".csv", ".md", ".rtf",
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".odt", ".ods", ".odp",
    ".log", ".diff", ".patch",
    # Archives
    ".zip", ".rar", ".7z", ".gz", ".tar", ".bz2", ".xz", ".tgz",
    # Data
    ".json", ".xml", ".yaml", ".yml", ".toml", ".ini",
    # Code (收缩到高价值文本子集 — 平台是处理+分享, 非 IDE)
    ".py", ".js", ".ts", ".html", ".htm", ".css", ".sql",
    ".sh", ".tsx", ".jsx", ".go", ".rs", ".java", ".ipynb",
    # CAD / 3D (设计·工程场景)
    ".dwg", ".dxf", ".step", ".stp", ".iges", ".igs",
    ".stl", ".obj", ".fbx", ".glb", ".gltf", ".3ds", ".skp", ".blend",
    ".sldprt", ".iam", ".catpart",
    # Audio / Video (媒体场景)
    ".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a",
    ".mp4", ".mov", ".webm", ".avi", ".mkv",
}

BLOCKED_EXT = {
    ".exe", ".dll", ".bat", ".cmd", ".ps1",
    ".jar", ".msi", ".apk", ".ipa", ".dmg", ".deb", ".rpm",
    ".phtml", ".phar",
}
