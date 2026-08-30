#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(dirname -- "$0")"
SCRIPT_DIR="$(cd -- "$SCRIPT_DIR" && pwd)"
APP_DIR="${APP_DIR:-/workspace/fast-vc-service}"
CONFIG="${CONFIG:-configs/seedvc-saudi.yaml}"
INPUT="${1:-/workspace/seedvc/reference-upload}"
ACTIVE_REFERENCE="$APP_DIR/resources/refs/ref-arabic-saudi-24k.wav"
BACKUP_DIR="/workspace/seedvc/backups/references"
TEMP_REFERENCE="$APP_DIR/resources/refs/.reference-24k.tmp.wav"
ROLLBACK_REFERENCE="$APP_DIR/resources/refs/.reference-rollback.wav"
SERVICE_PATTERN="fast-vc serve --config $CONFIG"

if [ ! -f "$INPUT" ]; then
  echo "Reference upload not found: $INPUT" >&2
  exit 1
fi
if [ ! -f "$ACTIVE_REFERENCE" ]; then
  echo "Active reference not found: $ACTIVE_REFERENCE" >&2
  exit 1
fi
if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "FFmpeg is unavailable; run server/bootstrap.sh first." >&2
  exit 1
fi

mkdir -p "$(dirname -- "$ACTIVE_REFERENCE")" "$BACKUP_DIR"
rm -f "$TEMP_REFERENCE" "$ROLLBACK_REFERENCE"
trap 'rm -f "$TEMP_REFERENCE" "$ROLLBACK_REFERENCE"' EXIT

echo "Converting uploaded audio to mono 24 kHz PCM16..."
ffmpeg -nostdin -hide_banner -loglevel error -y \
  -i "$INPUT" -vn -ac 1 -ar 24000 -c:a pcm_s16le "$TEMP_REFERENCE"

python3 - "$TEMP_REFERENCE" <<'PY'
from array import array
from pathlib import Path
import sys
import wave

path = Path(sys.argv[1])
with wave.open(str(path), "rb") as wav:
    if wav.getnchannels() != 1 or wav.getsampwidth() != 2 or wav.getframerate() != 24000:
        raise SystemExit("Converted reference is not mono 24 kHz PCM16")
    frames = wav.getnframes()
    duration = frames / wav.getframerate()
    if duration < 5.0:
        raise SystemExit(f"Reference is too short ({duration:.2f}s); provide at least 5 seconds")
    if duration > 120.0:
        raise SystemExit(f"Reference is too long ({duration:.2f}s); limit it to 120 seconds")
    samples = array("h", wav.readframes(frames))
    peak = max((abs(value) for value in samples), default=0)
    if peak < 64:
        raise SystemExit("Reference is effectively silent")
print(f"Validated reference: {duration:.2f}s, mono, 24 kHz, PCM16, peak={peak}")
PY

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup="$BACKUP_DIR/reference-$stamp.wav"
cp "$ACTIVE_REFERENCE" "$ROLLBACK_REFERENCE"
cp "$ACTIVE_REFERENCE" "$backup"
mv "$TEMP_REFERENCE" "$ACTIVE_REFERENCE"

echo "Stopping Fast-VC-Service to activate the new reference..."
pkill -TERM -f "$SERVICE_PATTERN" >/dev/null 2>&1 || true
for _ in $(seq 1 30); do
  if ! pgrep -f "$SERVICE_PATTERN" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
if pgrep -f "$SERVICE_PATTERN" >/dev/null 2>&1; then
  echo "Fast-VC-Service did not stop within 30 seconds." >&2
  mv "$ROLLBACK_REFERENCE" "$ACTIVE_REFERENCE"
  exit 1
fi

if bash "$SCRIPT_DIR/start.sh"; then
  rm -f "$INPUT"
  # Keep only the five most recent rollback copies.
  find "$BACKUP_DIR" -maxdepth 1 -type f -name 'reference-*.wav' -printf '%T@ %p\n' \
    | sort -nr | awk 'NR > 5 {sub(/^[^ ]+ /, ""); print}' \
    | while IFS= read -r old_backup; do rm -f -- "$old_backup"; done
  echo "New reference voice is active. Backup: $backup"
  exit 0
fi

echo "New reference failed to start; restoring the previous reference..." >&2
mv "$ROLLBACK_REFERENCE" "$ACTIVE_REFERENCE"
bash "$SCRIPT_DIR/start.sh" || true
exit 1
