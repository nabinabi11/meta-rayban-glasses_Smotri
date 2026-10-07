#!/usr/bin/env bash
# One-shot server setup: asks for your DashScope key, starts the container,
# prints the connection string for the app. Safe to re-run.
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker not found → installing (needs sudo)…"
  curl -fsSL https://get.docker.com | sudo sh
fi

if [ ! -f .env ]; then
  cp .env.example .env
  read -rp "Paste your Alibaba DashScope API key (sk-...): " KEY
  sed -i "s|^DASHSCOPE_API_KEY=.*|DASHSCOPE_API_KEY=${KEY}|" .env
  IP=$(curl -fsS https://api.ipify.org || true)
  [ -n "$IP" ] && sed -i "s|^# PUBLIC_URL=.*|PUBLIC_URL=ws://${IP}:8000|" .env
fi

mkdir -p data
sudo docker compose up -d --build
echo "Waiting for the server…"
for _ in $(seq 1 30); do
  curl -fsS http://127.0.0.1:8000/health >/dev/null 2>&1 && break
  sleep 1
done
sudo docker compose logs smotri | grep -A3 "Paste this" || sudo docker compose logs --tail 40 smotri
echo
echo "Open port 8000/tcp in your cloud firewall if the app can't connect."
