"""Validate the Seed-VC server config and reference audio before startup."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
import wave

import yaml


VERIFIED_FAST_VC_REF = "27eced54047fba4cdb42c41589345b5cbb3d6801"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def validate(root: Path, config_path: Path) -> None:
    root = root.resolve()
    config_path = config_path if config_path.is_absolute() else root / config_path
    config_path = config_path.resolve()
    require(config_path.is_file(), f"config not found: {config_path}")

    with config_path.open("r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle)
    require(isinstance(config, dict), "config must contain a YAML mapping")

    app = config.get("app", {})
    realtime = config.get("realtime_vc", {})
    models = config.get("models", {})
    require(app.get("host") == "0.0.0.0", "app.host must be 0.0.0.0")
    require(app.get("port") == 8042, "app.port must be 8042")
    require(app.get("workers") == 1, "use one worker for one configured GPU")
    require(realtime.get("device") == ["cuda:0"], "device must be ['cuda:0']")
    require(realtime.get("SAMPLERATE_IN") == 16_000, "input rate must be 16000")
    require(realtime.get("SAMPLERATE_OUT") == 22_050, "model output rate must be 22050")
    require(realtime.get("BIT_DEPTH") == 16, "bit depth must be 16")
    require(realtime.get("FRAMERATE") == 50, "frame rate must be 50")
    require(
        realtime.get("extra_time_ce", 0) > realtime.get("extra_time_dit", 0),
        "extra_time_ce must exceed extra_time_dit",
    )
    require(realtime.get("sola_buffer_time", 1) <= 0.08, "SOLA buffer is too large")
    require(0.1 <= realtime.get("block_time", 0) <= 1.0, "block_time is unreasonable")
    require(
        models.get("defalut_original_model") in {"tiny", "small", "base"},
        "invalid model size",
    )

    reference_value = realtime.get("reference_wav_path")
    require(isinstance(reference_value, str), "reference_wav_path is missing")
    reference_path = Path(reference_value)
    if not reference_path.is_absolute():
        reference_path = root / reference_path
    reference_path = reference_path.resolve()
    require(reference_path.is_file(), f"reference WAV not found: {reference_path}")

    with wave.open(str(reference_path), "rb") as wav:
        require(wav.getnchannels() == 1, "reference WAV must be mono")
        require(wav.getsampwidth() == 2, "reference WAV must be 16-bit PCM")
        require(wav.getframerate() == 24_000, "reference WAV must be 24 kHz")
        duration = wav.getnframes() / wav.getframerate()
        require(
            duration >= realtime.get("max_prompt_length", 5.0),
            "reference WAV is too short",
        )

    print(f"OK config:    {config_path}")
    print(f"OK reference: {reference_path} ({duration:.2f}s, mono, 24 kHz, PCM16)")
    print(f"OK upstream:  {VERIFIED_FAST_VC_REF}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True, help="Fast-VC application root")
    parser.add_argument("--config", type=Path, required=True, help="config path, relative to root")
    args = parser.parse_args()
    try:
        validate(args.root, args.config)
    except (OSError, ValueError, yaml.YAMLError, wave.Error) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
