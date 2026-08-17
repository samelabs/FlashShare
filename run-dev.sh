#!/bin/bash
# 本地开发启动脚本 —— 与生产 run.sh 同构：source .env 后启动 uvicorn。
# 前置：docker compose -f docker-compose.dev.yml up -d（gokapi）
# 依赖：.venv（python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt fastapi uvicorn jinja2 python-multipart bcrypt pydantic）
set -a
source "$(dirname "$0")/.env"
set +a
exec "$(dirname "$0")/.venv/bin/python" -m uvicorn app.main:app \
    --host 127.0.0.1 \
    --port 8000 \
    --reload \
    --proxy-headers \
    --forwarded-allow-ips=127.0.0.1
