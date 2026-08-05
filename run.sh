#!/bin/bash
# FlashShare FastAPI launcher — called by systemd
cd /opt/flashshare
set -a
source /etc/flashshare.env
set +a
exec /usr/bin/python3 -m uvicorn app.main:app \
    --host 127.0.0.1 \
    --port 8000 \
    --workers 1 \
    --proxy-headers \
    --forwarded-allow-ips=127.0.0.1
