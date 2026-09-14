"""Small platform-specific labels shared by the desktop client."""

from __future__ import annotations

import os


IS_WINDOWS = os.name == "nt"
PLATFORM_NAME = "Windows" if IS_WINDOWS else "Linux"
VIRTUAL_OUTPUT_LABEL = "VB-CABLE playback" if IS_WINDOWS else "PipeWire virtual microphone"
VIRTUAL_MIC_NAME = "CABLE Output" if IS_WINDOWS else "SeedVC_Microphone"
ROUTE_DESCRIPTION = (
    "Microphone → RunPod Seed-VC → VB-CABLE"
    if IS_WINDOWS
    else "Microphone → RunPod Seed-VC → PipeWire virtual microphone"
)
CREDENTIAL_STORE_NAME = "Windows Credential Manager" if IS_WINDOWS else "the system keyring"
UI_FONT = "Segoe UI" if IS_WINDOWS else "DejaVu Sans"
MONO_FONT = "Cascadia Mono" if IS_WINDOWS else "DejaVu Sans Mono"
PREFERRED_OUTPUT_NAME = "cable input" if IS_WINDOWS else "seedvc_virtual"
PREFERRED_AUDIO_API = "wasapi" if IS_WINDOWS else "pulseaudio"
PREFERRED_INPUT_NAME = "microphone" if IS_WINDOWS else "default source"
PREFERRED_INPUT_API = "wasapi" if IS_WINDOWS else "pulseaudio"


def is_virtual_output_name(name: str) -> bool:
    normalized = name.casefold()
    if IS_WINDOWS:
        return "cable input" in normalized
    return "seedvc" in normalized and "virtual" in normalized
