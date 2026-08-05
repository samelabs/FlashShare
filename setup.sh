#!/usr/bin/env bash
# FlashShare first-time setup
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo "=== FlashShare Setup ==="

# 1. Check Python
if ! command -v python3 &>/dev/null; then
    echo "ERROR: python3 not found"
    exit 1
fi

PY_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
echo "Python: $PY_VER"

# 2. Create data directory
DATA_DIR="${FLASHSHARE_DB_DIR:-/var/lib/flashshare}"
sudo mkdir -p "$DATA_DIR"
sudo chown "$(whoami)" "$DATA_DIR"
echo "Data dir: $DATA_DIR"

# 3. Generate session secret if not set
SECRET_FILE=".env"
if [ ! -f "$SECRET_FILE" ]; then
    SECRET=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
    cat > "$SECRET_FILE" <<EOF
FLASHSHARE_SESSION_SECRET=$SECRET
FLASHSHARE_DB=$DATA_DIR/flashshare.sqlite
FLASHSHARE_ADDR=127.0.0.1:53843
FLASHSHARE_BASE_URL=http://localhost:53843

# X OAuth2 (optional)
FLASHSHARE_X_CLIENT_ID=
FLASHSHARE_X_CLIENT_SECRET=

# Gokapi backend
FLASHSHARE_GOKAPI_URL=http://127.0.0.1:53842
FLASHSHARE_GOKAPI_KEY=
EOF
    echo "Generated .env (review and fill in OAuth/Gokapi keys)"
else
    echo ".env already exists, skipping"
fi

# 4. Initialize database
python3 -c "
import os
from dotenv import load_dotenv
load_dotenv()
" 2>/dev/null || true

# Source .env for init
set -a
source "$SECRET_FILE" 2>/dev/null || true
set +a

python3 -c "
from database import init_db
init_db()
print('Database initialized')
"

echo ""
echo "=== Setup complete ==="
echo "Next steps:"
echo "  1. Edit .env and fill in X OAuth + Gokapi keys"
echo "  2. Run: python3 main.py"
echo "  3. Or set up systemd: see README.md"
