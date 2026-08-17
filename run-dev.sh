#!/bin/bash
# 本地开发启动（全容器化）：宿主 53844（app）+ 53842（gokapi）
# 首次启动会构建镜像（依赖版本镜像生产，见 Dockerfile.dev / requirements.txt）
# 访问 http://localhost:53844 ；gokapi 管理台 http://localhost:53842/admin
set -e
cd "$(dirname "$0")"
docker compose -f docker-compose.dev.yml up -d --build
echo ""
echo "  FlashShare:  http://localhost:53844"
echo "  Gokapi:      http://localhost:53842/admin"
echo "  日志:        docker logs -f flashshare-dev-app"
