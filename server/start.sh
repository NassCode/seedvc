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
PID_FILE="${PID_FILE:-/tmp/seedvc-fast-vc-8042.pid}"
service_pid=""

port_ready() {
  python3 -c 'import socket; s=socket.create_connection(("127.0.0.1", 8042), 1); s.close()' 2>/dev/null
}

service_pid_matches() {
  local pid="${1:-}"
  [ -n "$pid" ] && [ "$pid" -gt 0 ] 2>/dev/null &&
    [ -r "/proc/$pid/cmdline" ] &&
    tr '\0' ' ' <"/proc/$pid/cmdline" | grep -Fq "fast-vc serve --config $CONFIG"
}

if port_ready; then
  echo "Fast-VC-Service is already running."
  exit 0
fi

if [ -f "$PID_FILE" ]; then
  read -r service_pid <"$PID_FILE" || service_pid=""
fi

if ! service_pid_matches "$service_pid"; then
  rm -f "$PID_FILE"
  echo "Starting Fast-VC-Service..."
  cd "$APP_DIR"
  nohup "$APP_DIR/.venv/bin/fast-vc" serve --config "$CONFIG" \
    >"$LOG_FILE" 2>&1 </dev/null &
  service_pid=$!
  printf '%s\n' "$service_pid" >"$PID_FILE"
else
  echo "Fast-VC-Service is still starting (PID $service_pid)."
fi

echo "Waiting for Fast-VC-Service on port 8042..."
for elapsed in $(seq 1 240); do
  if port_ready; then
    echo "Fast-VC-Service is ready."
    exit 0
  fi
  if ! service_pid_matches "$service_pid"; then
    echo "Fast-VC-Service exited during startup. Recent log output:" >&2
    tail -n 50 "$LOG_FILE" >&2 || true
    rm -f "$PID_FILE"
    exit 1
  fi
  if [ $((elapsed % 10)) -eq 0 ]; then
    echo "Fast-VC models are loading (${elapsed}s elapsed)..."
  fi
  sleep 1
done

echo "Timed out waiting for Fast-VC-Service. Recent log output:" >&2
tail -n 50 "$LOG_FILE" >&2 || true
exit 1
