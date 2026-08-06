import os
import re

APP_NAME = "518.PLUS"
SEO_TITLE = "518.PLUS"
SEO_DESC = "Upload, share, done. Fast temporary file links with QR codes."

DB_PATH = os.environ.get("FLASHSHARE_DB", "/var/lib/flashshare/flashshare.sqlite")
BASE_URL = os.environ.get("FLASHSHARE_BASE_URL", "http://localhost:53843").rstrip("/")
ASSET_DIR = os.environ.get("FLASHSHARE_ASSET_DIR", "/opt/flashshare/static/images")
SOCIAL_IMAGE_PATH = "/static/images/og-image.png"
SOCIAL_IMAGE_URL = f"{BASE_URL}{SOCIAL_IMAGE_PATH}"

ADDR = os.environ.get("FLASHSHARE_ADDR", "127.0.0.1:53843")

X_CLIENT_ID = os.environ.get("FLASHSHARE_X_CLIENT_ID", "")
X_CLIENT_SECRET = os.environ.get("FLASHSHARE_X_CLIENT_SECRET", "")

GOKAPI_URL = os.environ.get("FLASHSHARE_GOKAPI_URL", "http://127.0.0.1:53842").rstrip("/")
GOKAPI_KEY = os.environ.get("FLASHSHARE_GOKAPI_KEY", "")

SESSION_SECRET = os.environ.get("FLASHSHARE_SESSION_SECRET", "")
if not SESSION_SECRET:
    raise RuntimeError("FLASHSHARE_SESSION_SECRET environment variable must be set")

MAX_UPLOAD_BYTES = int(os.environ.get("FLASHSHARE_MAX_UPLOAD_MB", "50")) * 1024 * 1024
DAILY_UPLOADS = int(os.environ.get("FLASHSHARE_DAILY_UPLOADS", "100"))
DAILY_BYTES = int(os.environ.get("FLASHSHARE_DAILY_MB", "1024")) * 1024 * 1024

COOKIE_NAME = "flashshare_session"
SESSION_TTL = 30 * 24 * 3600
OAUTH_TTL = 10 * 60

PUBLIC_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,48}$")

ALLOWED_EXT = {
    # Images
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".ico", ".heic", ".heif", ".tiff", ".tif", ".avif", ".svg",
    # Documents
    ".pdf", ".txt", ".csv", ".md", ".rtf",
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".odt", ".ods", ".odp",
    # Archives
    ".zip", ".rar", ".7z", ".gz", ".tar", ".bz2", ".xz", ".tgz",
    # Data
    ".json", ".xml", ".yaml", ".yml",
    # Code
    ".py", ".js", ".mjs", ".ts", ".html", ".htm", ".css",
    ".go", ".rs", ".java", ".c", ".cpp", ".h", ".rb", ".php", ".sh", ".sql",
}

BLOCKED_EXT = {
    ".exe", ".dll", ".bat", ".cmd", ".ps1",
    ".jar", ".msi", ".apk", ".ipa", ".dmg", ".deb", ".rpm",
    ".phtml", ".phar",
}
