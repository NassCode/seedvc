#!/usr/bin/env bash
set -euo pipefail

client_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
missing=0

for package in python3-venv python3-tk libportaudio2 pulseaudio-utils openssh-client libsecret-1-0; do
    if dpkg-query -W -f='${Status}' "$package" 2>/dev/null | grep -Fq 'install ok installed'; then
        printf 'ok:      %s\n' "$package"
    else
        printf 'missing: %s\n' "$package"
        missing=1
    fi
done

for service in pipewire pipewire-pulse; do
    if systemctl --user is-active --quiet "$service"; then
        printf 'ok:      %s user service\n' "$service"
    else
        printf 'missing: active %s user service\n' "$service"
        missing=1
    fi
done

if [[ -x "$client_dir/.venv/bin/python" ]]; then
    if "$client_dir/.venv/bin/python" -c 'import tkinter, keyring, numpy, sounddevice, soxr, websockets'; then
        echo "ok:      Python client dependencies"
    else
        echo "missing: one or more Python client dependencies"
        missing=1
    fi
else
    echo "missing: $client_dir/.venv (run ./setup.sh)"
    missing=1
fi

if command -v pactl >/dev/null 2>&1 \
    && pactl list short sinks 2>/dev/null | awk '{print $2}' | grep -Fxq seedvc_virtual \
    && pactl list short sources 2>/dev/null | awk '{print $2}' | grep -Fxq seedvc_microphone; then
    echo "ok:      SeedVC PipeWire virtual sink and microphone source"
else
    echo "missing: SeedVC PipeWire virtual sink/source (run ./setup-virtual-mic.sh)"
    missing=1
fi

exit "$missing"
