#!/bin/bash
# FlashShare FastAPI launcher — called by systemd
# env 来源 = systemd EnvironmentFile=/etc/flashshare.env (权威);
# 此处的 source 仅为手动直跑 (非 systemd) 时的兜底, 不可读则跳过。
cd /opt/flashshare
if [ -r /etc/flashshare.env ]; then
    set -a
    source /etc/flashshare.env
    set +a
fi
exec /opt/flashshare/venv/bin/python -m uvicorn app.main:app \
    --host 127.0.0.1 \
    --port 8000 \
    --workers 1 \
    --proxy-headers \
    --forwarded-allow-ips=127.0.0.1
