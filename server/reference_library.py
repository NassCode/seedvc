#!/usr/bin/env python3
"""Persistent reference-voice metadata for the SeedVC RunPod deployment."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import sys
import uuid


ROOT = Path(os.environ.get("SEEDVC_ROOT", "/workspace/seedvc"))
LIBRARY_DIR = ROOT / "voices"
STATE_PATH = ROOT / "voice-library.json"


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"version": 1, "active_id": None, "voices": []}
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Could not read voice library: {exc}") from exc
    if state.get("version") != 1 or not isinstance(state.get("voices"), list):
        raise SystemExit("Voice library metadata has an unsupported format")
    return state


def save_state(state: dict) -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
    temporary = STATE_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(STATE_PATH)


def present_voices(state: dict) -> list[dict]:
    result = []
    for voice in state["voices"]:
        path = LIBRARY_DIR / str(voice.get("file", ""))
        if path.is_file():
            result.append(voice)
    return result


def store(source: Path, name: str, activate: bool) -> dict:
    if not source.is_file():
        raise SystemExit(f"Reference source does not exist: {source}")
    display_name = Path(name.strip()).name.strip()
    if not display_name:
        raise SystemExit("Reference voice name is empty")
    if len(display_name) > 120:
        display_name = display_name[:120]

    state = load_state()
    voice_id = uuid.uuid4().hex
    filename = f"{voice_id}.wav"
    LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
    destination = LIBRARY_DIR / filename
    shutil.copy2(source, destination)
    record = {
        "id": voice_id,
        "name": display_name,
        "file": filename,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    state["voices"].append(record)
    if activate or not state.get("active_id"):
        state["active_id"] = voice_id
    try:
        save_state(state)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return record


def find_voice(state: dict, voice_id: str) -> dict:
    if not voice_id or any(character not in "0123456789abcdef" for character in voice_id):
        raise SystemExit("Invalid stored voice ID")
    voice = next((item for item in state["voices"] if item.get("id") == voice_id), None)
    if voice is None:
        raise SystemExit(f"Stored voice was not found: {voice_id}")
    path = LIBRARY_DIR / str(voice.get("file", ""))
    if not path.is_file():
        raise SystemExit(f"Stored voice audio is missing: {voice.get('name', voice_id)}")
    return voice


def command_list() -> None:
    state = load_state()
    voices = present_voices(state)
    active = next(
        (voice for voice in voices if voice.get("id") == state.get("active_id")),
        None,
    )
    print(
        json.dumps(
            {
                "active_id": active.get("id") if active else None,
                "active_name": active.get("name") if active else None,
                "voices": [
                    {"id": voice["id"], "name": voice["name"]} for voice in voices
                ],
            },
            ensure_ascii=False,
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("list")
    subparsers.add_parser("active-path")

    store_parser = subparsers.add_parser("store")
    store_parser.add_argument("source", type=Path)
    store_parser.add_argument("name")
    store_parser.add_argument("--activate", action="store_true")

    seed_parser = subparsers.add_parser("seed")
    seed_parser.add_argument("source", type=Path)
    seed_parser.add_argument("name")

    path_parser = subparsers.add_parser("path")
    path_parser.add_argument("voice_id")

    activate_parser = subparsers.add_parser("activate")
    activate_parser.add_argument("voice_id")

    args = parser.parse_args()
    if args.command == "list":
        command_list()
    elif args.command == "active-path":
        state = load_state()
        active_id = state.get("active_id")
        if not active_id:
            raise SystemExit("Voice library has no active voice")
        voice = find_voice(state, active_id)
        print(LIBRARY_DIR / voice["file"])
    elif args.command == "store":
        print(store(args.source, args.name, args.activate)["id"])
    elif args.command == "seed":
        state = load_state()
        existing = next(
            (voice for voice in present_voices(state) if voice.get("name") == args.name),
            None,
        )
        print((existing or store(args.source, args.name, not state.get("active_id")))["id"])
    elif args.command == "path":
        voice = find_voice(load_state(), args.voice_id)
        print(LIBRARY_DIR / voice["file"])
    elif args.command == "activate":
        state = load_state()
        find_voice(state, args.voice_id)
        state["active_id"] = args.voice_id
        save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
