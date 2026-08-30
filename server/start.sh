#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(dirname -- "$0")"
SCRIPT_DIR="$(cd -- "$SCRIPT_DIR" && pwd)"
APP_DIR="${APP_DIR:-/workspace/fast-vc-service}"
CONFIG="${CONFIG:-configs/seedvc-saudi.yaml}"
LOG_FILE="${LOG_FILE:-$APP_DIR/logs/service.log}"

export UV_CACHE_DIR="${UV_CACHE_DIR:-/workspace/.cache/uv}"
export HF_HOME="${HF_HOME:-/workspace/.cache/huggingface}"
export TORCH_HOME="${TORCH_HOME:-/workspace/.cache/torch}"

needs_bootstrap=0
if ! command -v uv >/dev/null 2>&1; then
  needs_bootstrap=1
elif [ ! -f "$APP_DIR/$CONFIG" ]; then
  needs_bootstrap=1
elif [ ! -f "$APP_DIR/resources/refs/ref-arabic-saudi-24k.wav" ]; then
  needs_bootstrap=1
fi

if [ "$needs_bootstrap" -eq 1 ]; then
  echo "Restoring the Fast-VC runtime..."
  bash "$SCRIPT_DIR/bootstrap.sh"
fi

mkdir -p "$APP_DIR/logs"
if pgrep -f "fast-vc serve --config $CONFIG" >/dev/null 2>&1; then
  echo "Fast-VC-Service is already running."
else
  echo "Starting Fast-VC-Service..."
  cd "$APP_DIR"
  nohup uv run fast-vc serve --config "$CONFIG" \
    >"$LOG_FILE" 2>&1 </dev/null &
fi

echo "Waiting for Fast-VC-Service on port 8042..."
for _ in $(seq 1 180); do
  if python3 -c 'import socket; s=socket.create_connection(("127.0.0.1", 8042), 1); s.close()' 2>/dev/null; then
    echo "Fast-VC-Service is ready."
    exit 0
  fi
  if ! pgrep -f "fast-vc serve --config $CONFIG" >/dev/null 2>&1; then
    echo "Fast-VC-Service exited during startup. Recent log output:" >&2
    tail -n 50 "$LOG_FILE" >&2 || true
    exit 1
  fi
  sleep 1
done

echo "Timed out waiting for Fast-VC-Service. Recent log output:" >&2
tail -n 50 "$LOG_FILE" >&2 || true
exit 1
