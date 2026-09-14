#!/usr/bin/env bash
set -euo pipefail

sink_name="seedvc_virtual"
source_name="seedvc_microphone"
description="SeedVC_Microphone"

if ! command -v pactl >/dev/null 2>&1; then
    echo "error: pactl is missing; run ./setup.sh first" >&2
    exit 1
fi

if ! pactl info >/dev/null 2>&1; then
    echo "error: PipeWire/PulseAudio is not available in this desktop session" >&2
    exit 1
fi

if ! pactl list short sinks | awk '{print $2}' | grep -Fxq "$sink_name"; then
    pactl load-module module-null-sink \
        sink_name="$sink_name" \
        sink_properties=device.description=SeedVC_Virtual_Microphone >/dev/null
fi

# Browsers commonly hide monitor sources. Expose the sink monitor through a
# remapped Audio/Source so WebRTC applications enumerate it as a microphone.
if ! pactl list short sources | awk '{print $2}' | grep -Fxq "$source_name"; then
    pactl load-module module-remap-source \
        master="$sink_name.monitor" \
        source_name="$source_name" \
        source_properties=device.description=SeedVC_Microphone >/dev/null
fi

echo "SeedVC virtual microphone is ready."
echo "Select '$description' as the microphone in your calling app."
