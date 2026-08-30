#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(dirname -- "$0")"
SCRIPT_DIR="$(cd -- "$SCRIPT_DIR" && pwd)"
APP_DIR="${APP_DIR:-/workspace/fast-vc-service}"
FAST_VC_REPO="${FAST_VC_REPO:-https://github.com/Leroll/fast-vc-service.git}"
FAST_VC_REF="${FAST_VC_REF:-27eced54047fba4cdb42c41589345b5cbb3d6801}"
CONFIG_SOURCE="$SCRIPT_DIR/prod.yaml"
REFERENCE_SOURCE="$SCRIPT_DIR/resources/refs/ref-arabic-saudi-24k.wav"
DEPLOY_CONFIG="$APP_DIR/configs/seedvc-saudi.yaml"
DEPLOY_REFERENCE="$APP_DIR/resources/refs/ref-arabic-saudi-24k.wav"

if [ ! -f "$CONFIG_SOURCE" ]; then
  echo "Missing server config: $CONFIG_SOURCE" >&2
  exit 1
fi
if [ ! -f "$REFERENCE_SOURCE" ]; then
  echo "Missing Saudi reference WAV: $REFERENCE_SOURCE" >&2
  exit 1
fi

apt-get update
apt-get install -y --no-install-recommends \
  build-essential \
  curl \
  ffmpeg \
  git \
  libasound2-dev \
  libopus-dev \
  libopus0 \
  libportaudio2 \
  libsndfile1 \
  opus-tools \
  portaudio19-dev \
  python3-pip \
  python3-venv
rm -rf /var/lib/apt/lists/*

if [ ! -e "$APP_DIR" ]; then
  git clone --no-checkout --depth 1 "$FAST_VC_REPO" "$APP_DIR"
  git -C "$APP_DIR" fetch --depth 1 origin "$FAST_VC_REF"
  git -C "$APP_DIR" checkout --detach "$FAST_VC_REF"
  git -C "$APP_DIR" submodule update --init --recursive --depth 1
elif [ ! -d "$APP_DIR/.git" ]; then
  echo "APP_DIR exists but is not a Git checkout: $APP_DIR" >&2
  exit 1
elif [ "$(git -C "$APP_DIR" rev-parse HEAD)" != "$FAST_VC_REF" ]; then
  echo "Existing Fast-VC checkout is not the verified commit." >&2
  echo "Expected: $FAST_VC_REF" >&2
  echo "Actual:   $(git -C "$APP_DIR" rev-parse HEAD)" >&2
  echo "Set APP_DIR to a new path or update the checkout deliberately." >&2
  exit 1
fi

cd "$APP_DIR"
if [ ! -f .env ]; then
  cp .env.example .env
fi

python3 -m pip install --upgrade pip uv
uv sync

install -d "$APP_DIR/configs" "$APP_DIR/resources/refs" "$APP_DIR/outputs" "$APP_DIR/logs" "$APP_DIR/temp"
install -m 0644 "$CONFIG_SOURCE" "$DEPLOY_CONFIG"
install -m 0644 "$REFERENCE_SOURCE" "$DEPLOY_REFERENCE"

uv run python "$SCRIPT_DIR/validate_deployment.py" \
  --root "$APP_DIR" \
  --config "configs/seedvc-saudi.yaml"

if command -v nvidia-smi >/dev/null 2>&1; then
  nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader
else
  echo "WARNING: nvidia-smi is unavailable; GPU inference cannot be verified yet." >&2
fi

echo
echo "Fast-VC-Service is ready at $APP_DIR"
echo "Start it with:"
echo "  cd $APP_DIR"
echo "  uv run fast-vc serve --config configs/seedvc-saudi.yaml"
