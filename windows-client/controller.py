"""Process, settings, and RunPod helpers for the SeedVC desktop controller."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import shlex
import socket
import time
import re
from typing import Callable
from urllib import error, request


APP_NAME = "SeedVC"
CREDENTIAL_SERVICE = "SeedVC RunPod"
CREDENTIAL_USERNAME = "api-key"
GEMINI_CREDENTIAL_SERVICE = "SeedVC Gemini"
GEMINI_CREDENTIAL_USERNAME = "api-key"
RUNPOD_API_BASE = "https://rest.runpod.io/v1"
RUNPOD_GRAPHQL_URL = "https://api.runpod.io/graphql"
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
REMOTE_LIBRARY_SCRIPT = "/workspace/seedvc/server/reference_library.py"


class ControllerError(RuntimeError):
    """An expected, user-actionable controller error."""


@dataclass
class Settings:
    input_device: int | None = None
    output_device: int | None = None
    input_device_name: str = ""
    input_hostapi: str = ""
    output_device_name: str = ""
    output_hostapi: str = ""
    pod_id: str = ""
    manage_pod: bool = True
    ssh_host: str = ""
    ssh_port: int = 22
    ssh_pod_id: str = ""
    ssh_key: str = ""
    network_volume_id: str = ""
    local_port: int = 8042
    stop_pod_on_exit: bool = False
    reference_file: str = ""
    active_reference: str = "Saudi Arabic (bundled)"
    gemini_autopilot_enabled: bool = True
    gemini_cli_path: str = "gemini"
    gpu_vram_min_gb: int = 16
    gpu_vram_max_gb: int = 48
    gpu_selection: str = "cheapest"
    replacement_policy: str = "delete_old_after_verified"
    terminal_output_mode: str = "summarized"


@dataclass(frozen=True)
class PodConnection:
    host: str
    ssh_port: int


@dataclass(frozen=True)
class VoiceReference:
    id: str
    name: str


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


def get_gemini_api_key() -> str:
    try:
        import keyring

        return keyring.get_password(GEMINI_CREDENTIAL_SERVICE, GEMINI_CREDENTIAL_USERNAME) or ""
    except Exception as exc:  # platform credential backends vary
        raise ControllerError(f"could not read Windows Credential Manager: {exc}") from exc


def set_gemini_api_key(api_key: str) -> None:
    if not api_key.strip():
        raise ControllerError("Gemini API key is empty")
    try:
        import keyring

        keyring.set_password(
            GEMINI_CREDENTIAL_SERVICE, GEMINI_CREDENTIAL_USERNAME, api_key.strip()
        )
    except Exception as exc:
        raise ControllerError(f"could not save to Windows Credential Manager: {exc}") from exc


def redact_secrets(text: str, *secrets: str) -> str:
    redacted = text
    for secret in secrets:
        value = secret.strip()
        if len(value) >= 6:
            redacted = redacted.replace(value, "[redacted]")
    return redacted


def gpu_candidate_allowed(candidate: dict, min_vram_gb: int = 16, max_vram_gb: int = 48) -> bool:
    gpu_type = candidate.get("gpuType") or {}
    machine = candidate.get("machine") or {}
    machine_gpu_type = machine.get("gpuType") or {} if isinstance(machine, dict) else {}
    raw_vram = candidate.get("gpuMemoryInGb")
    if raw_vram is None and isinstance(gpu_type, dict):
        raw_vram = gpu_type.get("memoryInGb")
    if raw_vram is None and isinstance(machine_gpu_type, dict):
        raw_vram = machine_gpu_type.get("memoryInGb")
    if raw_vram is None and "networkVolumeId" not in candidate:
        raw_vram = (
            candidate.get("memoryInGb")
            or candidate.get("vramGb")
            or candidate.get("vram_gb")
            or candidate.get("memory")
        )
    try:
        vram = float(raw_vram)
    except (TypeError, ValueError):
        return False
    return min_vram_gb <= vram <= max_vram_gb


def sort_gpu_candidates(candidates: list[dict]) -> list[dict]:
    def key(candidate: dict) -> tuple[float, str]:
        lowest_price = candidate.get("lowestPrice") or {}
        raw_price = (
            candidate.get("securePrice")
            or candidate.get("communityPrice")
            or candidate.get("price")
            or candidate.get("costPerHr")
            or (
                lowest_price.get("uninterruptablePrice")
                if isinstance(lowest_price, dict)
                else None
            )
        )
        try:
            price = float(raw_price)
        except (TypeError, ValueError):
            price = float("inf")
        return price, str(candidate.get("id") or candidate.get("name") or "")

    return sorted(candidates, key=key)


def gpu_runtime_compatible(candidate: dict, image_name: str) -> bool:
    gpu_id = str(candidate.get("id") or candidate.get("gpuTypeId") or "").casefold()
    requires_cuda_128 = "blackwell" in gpu_id or "rtx 50" in gpu_id
    if not requires_cuda_128:
        return True
    match = re.search(r"cuda(\d+)\.(\d+)", image_name.casefold())
    if not match:
        return False
    return (int(match.group(1)), int(match.group(2))) >= (12, 8)


class RunPodAPI:
    """Small client for the supported RunPod REST Pod endpoints."""

    def __init__(self, api_key: str, base_url: str = RUNPOD_API_BASE, timeout: float = 20):
        if not api_key.strip():
            raise ControllerError("RunPod API key is required for pod automation")
        self.api_key = api_key.strip()
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 SeedVC-Windows-Client/1.0",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        req = request.Request(
            f"{self.base_url}{path}",
            method=method,
            headers=headers,
            data=body,
        )
        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                body = response.read()
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            if "cloudflare" in detail.casefold() or "error 1010" in detail.casefold():
                raise ControllerError(
                    "RunPod blocked the API request at Cloudflare. "
                    "The app now sends a browser-like user agent; try again."
                ) from exc
            raise ControllerError(f"RunPod API returned HTTP {exc.code}: {detail}") from exc
        except (error.URLError, TimeoutError, OSError) as exc:
            raise ControllerError(f"could not reach RunPod API: {exc}") from exc
        if not body:
            return {}
        try:
            result = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ControllerError("RunPod API returned invalid JSON") from exc
        if not isinstance(result, (dict, list)):
            raise ControllerError("RunPod API returned an unexpected response")
        return result

    def _graphql(self, query: str) -> dict:
        body = json.dumps({"query": query}).encode("utf-8")
        req = request.Request(
            RUNPOD_GRAPHQL_URL,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0 SeedVC-Windows-Client/1.0",
            },
            data=body,
        )
        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                result = json.loads(response.read())
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            raise ControllerError(
                f"RunPod GraphQL returned HTTP {exc.code}: {detail}"
            ) from exc
        except (error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise ControllerError(f"could not query RunPod GPU metadata: {exc}") from exc
        if not isinstance(result, dict) or result.get("errors"):
            raise ControllerError(
                f"RunPod GraphQL returned an error: {json.dumps(result.get('errors'))}"
            )
        data = result.get("data")
        if not isinstance(data, dict):
            raise ControllerError("RunPod GraphQL returned an unexpected response")
        return data

    def pod_gpu_details(self) -> dict[str, dict]:
        data = self._graphql(
            "query { myself { pods { id machine { secureCloud gpuTypeId "
            "gpuDisplayName gpuType { id displayName memoryInGb } } } } }"
        )
        myself = data.get("myself") or {}
        pods = myself.get("pods") or [] if isinstance(myself, dict) else []
        details: dict[str, dict] = {}
        for pod in pods:
            if not isinstance(pod, dict) or not pod.get("id"):
                continue
            machine = pod.get("machine") or {}
            gpu_type = machine.get("gpuType") or {} if isinstance(machine, dict) else {}
            details[str(pod["id"])] = {
                "gpuTypeId": machine.get("gpuTypeId") if isinstance(machine, dict) else None,
                "gpuDisplayName": machine.get("gpuDisplayName") if isinstance(machine, dict) else None,
                "gpuMemoryInGb": gpu_type.get("memoryInGb") if isinstance(gpu_type, dict) else None,
                "secureCloud": machine.get("secureCloud") if isinstance(machine, dict) else None,
            }
        return details

    def gpu_types(self) -> list[dict]:
        data = self._graphql(
            "query { gpuTypes { id displayName memoryInGb secureCloud communityCloud "
            "lowestPrice(input: {gpuCount: 1}) { uninterruptablePrice } } }"
        )
        gpu_types = data.get("gpuTypes") or []
        if not isinstance(gpu_types, list):
            raise ControllerError("RunPod returned an unexpected GPU catalog")
        return [item for item in gpu_types if isinstance(item, dict)]

    def get_network_volume(self, network_volume_id: str) -> dict:
        return self._request("GET", f"/networkvolumes/{network_volume_id}")

    def runtime_ssh_connection(self, pod_id: str) -> PodConnection | None:
        escaped_pod_id = json.dumps(pod_id)
        query = (
            "query { pod(input: {podId: "
            + escaped_pod_id
            + "}) { runtime { ports { ip isIpPublic privatePort publicPort type } } } }"
        )
        data = self._graphql(query)
        pod = data.get("pod") or {}
        runtime = pod.get("runtime") or {} if isinstance(pod, dict) else {}
        ports = runtime.get("ports") or [] if isinstance(runtime, dict) else []
        for port in ports:
            if not isinstance(port, dict):
                continue
            if (
                port.get("privatePort") == 22
                and str(port.get("type") or "").casefold() == "tcp"
                and port.get("ip")
                and port.get("publicPort")
            ):
                return PodConnection(str(port["ip"]), int(port["publicPort"]))
        return None

    def get_pod(self, pod_id: str) -> dict:
        return self._request("GET", f"/pods/{pod_id}")

    def list_pods(self) -> list[dict]:
        result = self._request("GET", "/pods")
        if isinstance(result, dict):
            pods = result.get("pods") or result.get("data") or []
        else:
            pods = result
        if not isinstance(pods, list):
            raise ControllerError("RunPod API returned an unexpected pod list")
        return [pod for pod in pods if isinstance(pod, dict)]

    def start_pod(self, pod_id: str) -> dict:
        return self._request("POST", f"/pods/{pod_id}/start")

    def stop_pod(self, pod_id: str) -> dict:
        return self._request("POST", f"/pods/{pod_id}/stop")

    def restart_pod(self, pod_id: str) -> dict:
        return self._request("POST", f"/pods/{pod_id}/restart")

    def delete_pod(self, pod_id: str) -> dict:
        return self._request("DELETE", f"/pods/{pod_id}")

    def create_pod(self, payload: dict) -> dict:
        return self._request("POST", "/pods", payload)

    def pods_for_network_volume(self, network_volume_id: str) -> list[dict]:
        volume_id = network_volume_id.strip()
        if not volume_id:
            raise ControllerError("RunPod network volume ID is required")
        return [
            pod
            for pod in self.list_pods()
            if str(pod.get("networkVolumeId") or "") == volume_id
        ]

    def preferred_pod_for_network_volume(
        self,
        network_volume_id: str,
        min_vram_gb: int | None = None,
        max_vram_gb: int | None = None,
    ) -> dict:
        pods = self.pods_for_network_volume(network_volume_id)
        if min_vram_gb is not None and max_vram_gb is not None:
            gpu_details = self.pod_gpu_details()
            pods = [dict(pod, **gpu_details.get(str(pod.get("id") or ""), {})) for pod in pods]
            pods = [
                pod
                for pod in pods
                if gpu_candidate_allowed(pod, min_vram_gb, max_vram_gb)
            ]
        if not pods:
            if min_vram_gb is not None and max_vram_gb is not None:
                raise ControllerError(
                    f"no compatible RunPod pod attached to network volume "
                    f"{network_volume_id} has {min_vram_gb}-{max_vram_gb} GB VRAM"
                )
            raise ControllerError(
                f"no RunPod pod is attached to network volume {network_volume_id}"
            )
        statuses = {"RUNNING": 0, "STARTING": 1, "EXITED": 2, "STOPPED": 3}

        def created_timestamp(pod: dict) -> float:
            raw = str(pod.get("createdAt") or "")
            for suffix in (" UTC", ""):
                value = raw.removesuffix(suffix)
                try:
                    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S.%f %z").timestamp()
                except ValueError:
                    pass
            return 0.0

        def key(pod: dict) -> tuple[int, float]:
            status = str(pod.get("desiredStatus") or pod.get("status") or "").upper()
            return statuses.get(status, 9), -created_timestamp(pod)

        return sorted(pods, key=key)[0]

    def configure_public_key(self, pod_id: str, public_key: str) -> dict:
        key = public_key.strip()
        if not key.startswith(("ssh-ed25519 ", "ssh-rsa ", "ecdsa-sha2-")):
            raise ControllerError("SSH public key has an unsupported format")
        pod = self.get_pod(pod_id)
        environment = dict(pod.get("env") or {})
        environment["PUBLIC_KEY"] = key
        return self._request("POST", f"/pods/{pod_id}/update", {"env": environment})

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
            if "22/udp" in (pod.get("ports") or []):
                runtime_connection = self.runtime_ssh_connection(pod_id)
                if runtime_connection is not None:
                    connection = runtime_connection
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


def parse_ssh_command(command: str) -> tuple[PodConnection, str]:
    """Extract host, port, and identity path from a RunPod SSH command."""
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError as exc:
        raise ControllerError(f"could not parse SSH command: {exc}") from exc
    if not tokens or Path(tokens[0]).name.casefold() not in {"ssh", "ssh.exe"}:
        raise ControllerError("SSH command must start with 'ssh'")
    host = ""
    port = 22
    key_path = ""
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token in {"-p", "-i"}:
            if index + 1 >= len(tokens):
                raise ControllerError(f"SSH option {token} is missing its value")
            value = tokens[index + 1]
            if token == "-p":
                try:
                    port = int(value)
                except ValueError as exc:
                    raise ControllerError("SSH command port must be a number") from exc
            else:
                key_path = value
            index += 2
            continue
        if not token.startswith("-") and "@" in token:
            host = token.rsplit("@", 1)[1]
        index += 1
    if not host or not key_path:
        raise ControllerError("SSH command must include root@HOST and -i KEY_PATH")
    expanded_key = str(Path(key_path).expanduser())
    return PodConnection(host, port), expanded_key


def public_key_for_private(key_path: str | Path) -> str:
    private_key = Path(key_path).expanduser()
    public_key = Path(f"{private_key}.pub")
    if not private_key.is_file():
        raise ControllerError(f"SSH private key does not exist: {private_key}")
    if not public_key.is_file():
        raise ControllerError(f"SSH public key does not exist: {public_key}")
    try:
        value = public_key.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ControllerError(f"could not read SSH public key: {exc}") from exc
    if not value.startswith(("ssh-ed25519 ", "ssh-rsa ", "ecdsa-sha2-")):
        raise ControllerError("SSH public key has an unsupported format")
    return value


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


def parse_voice_library(output: str) -> tuple[list[VoiceReference], str | None]:
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as exc:
        raise ControllerError("pod returned an invalid voice-library response") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("voices"), list):
        raise ControllerError("pod returned an unexpected voice-library response")
    voices: list[VoiceReference] = []
    for item in payload["voices"]:
        if not isinstance(item, dict):
            raise ControllerError("pod returned an invalid stored voice")
        voice_id = str(item.get("id", ""))
        name = str(item.get("name", "")).strip()
        if len(voice_id) != 32 or any(character not in "0123456789abcdef" for character in voice_id):
            raise ControllerError("pod returned an invalid stored voice ID")
        if not name:
            raise ControllerError("pod returned a stored voice without a name")
        voices.append(VoiceReference(voice_id, name))
    active_id = payload.get("active_id")
    if active_id is not None and active_id not in {voice.id for voice in voices}:
        active_id = None
    return voices, active_id


def reference_upload_activate_command(display_name: str) -> str:
    name = Path(display_name).name.strip() or "Uploaded voice"
    return (
        "bash /workspace/seedvc/server/activate-reference.sh "
        f"{REMOTE_REFERENCE_UPLOAD} {shlex.quote(name)}"
    )


def reference_stored_activate_command(voice_id: str) -> str:
    if len(voice_id) != 32 or any(character not in "0123456789abcdef" for character in voice_id):
        raise ControllerError("invalid stored voice ID")
    return f"bash /workspace/seedvc/server/activate-reference.sh --stored {voice_id}"


REMOTE_START_COMMAND = "bash /workspace/seedvc/server/start.sh"
REMOTE_LIST_REFERENCES_COMMAND = f"python3 {REMOTE_LIBRARY_SCRIPT} list"
