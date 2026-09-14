#!/usr/bin/env bash
set -euo pipefail

client_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ ! -x "$client_dir/.venv/bin/python" ]]; then
    echo "error: Linux client is not installed; run $client_dir/setup.sh first" >&2
    exit 1
fi

"$client_dir/setup-virtual-mic.sh"
export PULSE_SINK=seedvc_virtual
exec "$client_dir/.venv/bin/python" "$client_dir/gui.py"
