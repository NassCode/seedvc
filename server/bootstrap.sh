#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/workspace/fast-vc-service}"
REPO="https://github.com/Leroll/fast-vc-service.git"

apt-get update
apt-get install -y --no-install-recommends git python3-pip libopus-dev libopus0 opus-tools

if [ ! -d "$APP_DIR/.git" ]; then
  git clone --recursive "$REPO" "$APP_DIR"
fi

cd "$APP_DIR"
cp -n .env.example .env || true
python3 -m pip install --upgrade pip uv
uv sync

mkdir -p resources/refs outputs logs

echo
echo "Fast-VC-Service installed at $APP_DIR"
echo "Place your target reference WAV at:"
echo "  $APP_DIR/resources/refs/ref-24k.wav"
echo
echo "Then run:"
echo "  cd $APP_DIR && uv run fast-vc serve"
