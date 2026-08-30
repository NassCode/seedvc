"""Process, settings, and RunPod helpers for the SeedVC desktop controller."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import socket
import time
from typing import Callable
from urllib import error, request


APP_NAME = "SeedVC"
CREDENTIAL_SERVICE = "SeedVC RunPod"
CREDENTIAL_USERNAME = "api-key"
RUNPOD_API_BASE = "https://rest.runpod.io/v1"
MAX_REFERENCE_BYTES = 100 * 1024 * 1024
REFERENCE_EXTENSIONS = {
    ".aac",
    ".flac",
    ".m4a",
    ".mp3",
    ".mp4",
    ".ogg",
    ".opus",
    ".wav",
    ".wma",
}
REMOTE_REFERENCE_UPLOAD = "/workspace/seedvc/reference-upload"


class ControllerError(RuntimeError):
    """An expected, user-actionable controller error."""


@dataclass
class Settings:
    input_device: int | None = None
    output_device: int | None = None
    pod_id: str = ""
    manage_pod: bool = False
    ssh_host: str = ""
    ssh_port: int = 22
    ssh_key: str = ""
    local_port: int = 8042
    stop_pod_on_exit: bool = False
    reference_file: str = ""
    active_reference: str = "Saudi Arabic (bundled)"


@dataclass(frozen=True)
class PodConnection:
    host: str
    ssh_port: int


def default_settings_path() -> Path:
    base = os.environ.get("APPDATA")
    if base:
        return Path(base) / APP_NAME / "settings.json"
    return Path.home() / f".{APP_NAME.casefold()}" / "settings.json"


def load_settings(path: Path | None = None) -> Settings:
    settings_path = path or default_settings_path()
    if not settings_path.exists():
        return Settings()
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
        allowed = Settings.__dataclass_fields__.keys()
        return Settings(**{key: value for key, value in data.items() if key in allowed})
    except (OSError, ValueError, TypeError) as exc:
        raise ControllerError(f"could not read settings: {exc}") from exc


def save_settings(settings: Settings, path: Path | None = None) -> None:
    settings_path = path or default_settings_path()
    try:
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = settings_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(asdict(settings), indent=2), encoding="utf-8")
        temporary.replace(settings_path)
    except OSError as exc:
        raise ControllerError(f"could not save settings: {exc}") from exc


def get_api_key() -> str:
    try:
        import keyring

        return keyring.get_password(CREDENTIAL_SERVICE, CREDENTIAL_USERNAME) or ""
    except Exception as exc:  # platform credential backends vary
        raise ControllerError(f"could not read Windows Credential Manager: {exc}") from exc


def set_api_key(api_key: str) -> None:
    if not api_key.strip():
        raise ControllerError("RunPod API key is empty")
    try:
        import keyring

        keyring.set_password(CREDENTIAL_SERVICE, CREDENTIAL_USERNAME, api_key.strip())
    except Exception as exc:
        raise ControllerError(f"could not save to Windows Credential Manager: {exc}") from exc


class RunPodAPI:
    """Small client for the supported RunPod REST Pod endpoints."""

    def __init__(self, api_key: str, base_url: str = RUNPOD_API_BASE, timeout: float = 20):
        if not api_key.strip():
            raise ControllerError("RunPod API key is required for pod automation")
        self.api_key = api_key.strip()
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _request(self, method: str, path: str) -> dict:
        req = request.Request(
            f"{self.base_url}{path}",
            method=method,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "application/json",
            },
        )
        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                body = response.read()
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            raise ControllerError(f"RunPod API returned HTTP {exc.code}: {detail}") from exc
        except (error.URLError, TimeoutError, OSError) as exc:
            raise ControllerError(f"could not reach RunPod API: {exc}") from exc
        if not body:
            return {}
        try:
            result = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ControllerError("RunPod API returned invalid JSON") from exc
        if not isinstance(result, dict):
            raise ControllerError("RunPod API returned an unexpected response")
        return result

    def get_pod(self, pod_id: str) -> dict:
        return self._request("GET", f"/pods/{pod_id}")

    def start_pod(self, pod_id: str) -> dict:
        return self._request("POST", f"/pods/{pod_id}/start")

    def stop_pod(self, pod_id: str) -> dict:
        return self._request("POST", f"/pods/{pod_id}/stop")

    def restart_pod(self, pod_id: str) -> dict:
        return self._request("POST", f"/pods/{pod_id}/restart")

    def wait_for_ssh(
        self,
        pod_id: str,
        timeout: float = 300,
        interval: float = 5,
        progress: Callable[[str], None] | None = None,
    ) -> PodConnection:
        deadline = time.monotonic() + timeout
        last_status = "starting"
        while time.monotonic() < deadline:
            pod = self.get_pod(pod_id)
            last_status = str(pod.get("desiredStatus") or pod.get("status") or "starting")
            connection = pod_connection(pod)
            if connection and tcp_open(connection.host, connection.ssh_port, timeout=1.0):
                if progress:
                    progress(
                        f"SSH is ready at {connection.host}:{connection.ssh_port}"
                    )
                return connection
            if progress:
                detail = "waiting for an SSH address"
                if connection:
                    detail = (
                        f"waiting for SSH at {connection.host}:"
                        f"{connection.ssh_port}"
                    )
                progress(f"RunPod status: {last_status}; {detail}")
            time.sleep(interval)
        raise ControllerError(
            f"pod SSH did not become reachable within {timeout:g} seconds "
            f"(last status: {last_status})"
        )


def pod_connection(pod: dict) -> PodConnection | None:
    host = pod.get("publicIp")
    mappings = pod.get("portMappings") or {}
    port = mappings.get("22") or mappings.get(22)
    if not host or not port:
        return None
    try:
        return PodConnection(str(host), int(port))
    except (TypeError, ValueError):
        return None


def tcp_open(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def wait_for_port(
    host: str,
    port: int,
    timeout: float,
    process_alive: Callable[[], bool] | None = None,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if tcp_open(host, port):
            return
        if process_alive and not process_alive():
            raise ControllerError("SSH tunnel exited before opening its local port")
        time.sleep(0.2)
    raise ControllerError(f"timed out waiting for {host}:{port}")


def ssh_base_command(host: str, port: int, key_path: str) -> list[str]:
    if not host.strip():
        raise ControllerError("SSH host is required")
    if port <= 0 or port > 65535:
        raise ControllerError("SSH port must be between 1 and 65535")
    key = Path(key_path).expanduser()
    if not key.is_file():
        raise ControllerError(f"SSH private key does not exist: {key}")
    return [
        "ssh",
        "-p",
        str(port),
        "-i",
        str(key),
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        f"root@{host.strip()}",
    ]


def tunnel_command(host: str, port: int, key_path: str, local_port: int) -> list[str]:
    command = ssh_base_command(host, port, key_path)
    command[1:1] = [
        "-N",
        "-L",
        f"{local_port}:127.0.0.1:8042",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=3",
    ]
    return command


def validate_reference_file(path: str | Path) -> Path:
    reference = Path(path).expanduser()
    if not reference.is_file():
        raise ControllerError(f"reference audio file does not exist: {reference}")
    if reference.suffix.casefold() not in REFERENCE_EXTENSIONS:
        supported = ", ".join(sorted(REFERENCE_EXTENSIONS))
        raise ControllerError(f"unsupported reference format; choose one of: {supported}")
    try:
        size = reference.stat().st_size
    except OSError as exc:
        raise ControllerError(f"could not inspect reference audio: {exc}") from exc
    if size == 0:
        raise ControllerError("reference audio file is empty")
    if size > MAX_REFERENCE_BYTES:
        raise ControllerError("reference audio exceeds the 100 MB upload limit")
    return reference.resolve()


def scp_upload_command(
    host: str,
    port: int,
    key_path: str,
    local_path: str | Path,
    remote_path: str = REMOTE_REFERENCE_UPLOAD,
) -> list[str]:
    # Reuse SSH validation and options, but translate OpenSSH's port flag for SCP.
    ssh = ssh_base_command(host, port, key_path)
    reference = validate_reference_file(local_path)
    return [
        "scp",
        "-P",
        str(port),
        "-i",
        ssh[ssh.index("-i") + 1],
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        str(reference),
        f"root@{host.strip()}:{remote_path}",
    ]


REMOTE_START_COMMAND = "bash /workspace/seedvc/server/start.sh"
REMOTE_ACTIVATE_REFERENCE_COMMAND = (
    "bash /workspace/seedvc/server/activate-reference.sh "
    f"{REMOTE_REFERENCE_UPLOAD}"
)
