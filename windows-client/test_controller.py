import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import controller
import gui


class SettingsTests(unittest.TestCase):
    def test_settings_round_trip_does_not_include_api_key(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            expected = controller.Settings(
                input_device=25,
                output_device=22,
                pod_id="pod123",
                manage_pod=True,
                ssh_host="203.0.113.1",
                ssh_port=12345,
                ssh_pod_id="pod123",
                ssh_key="keyfile",
                network_volume_id="vol123",
            )
            controller.save_settings(expected, path)
            stored = json.loads(path.read_text(encoding="utf-8"))

            self.assertEqual(controller.load_settings(path), expected)
            self.assertNotIn("api_key", stored)
            self.assertNotIn("gemini_api_key", stored)

    def test_autopilot_settings_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            expected = controller.Settings(
                gemini_autopilot_enabled=True,
                gemini_cli_path="gemini.cmd",
                gpu_vram_min_gb=16,
                gpu_vram_max_gb=48,
                replacement_policy="delete_old_after_verified",
            )

            controller.save_settings(expected, path)

            self.assertEqual(controller.load_settings(path), expected)


class PodTests(unittest.TestCase):
    def test_pod_connection_reads_ssh_mapping(self):
        result = controller.pod_connection(
            {"publicIp": "213.173.108.134", "portMappings": {"22": 18121}}
        )
        self.assertEqual(
            result, controller.PodConnection("213.173.108.134", 18121)
        )

    def test_pod_connection_returns_none_while_starting(self):
        self.assertIsNone(controller.pod_connection({"portMappings": {}}))

    @mock.patch("controller.tcp_open", side_effect=(False, True))
    def test_wait_for_ssh_requires_reachable_port_and_tracks_remapping(self, tcp_open):
        api = controller.RunPodAPI("secret", base_url="https://example.test")
        api.get_pod = mock.Mock(
            side_effect=(
                {"status": "RUNNING", "publicIp": "203.0.113.1", "portMappings": {"22": 18122}},
                {"status": "RUNNING", "publicIp": "203.0.113.2", "portMappings": {"22": 18123}},
            )
        )

        result = api.wait_for_ssh("pod123", timeout=1, interval=0)

        self.assertEqual(result, controller.PodConnection("203.0.113.2", 18123))
        self.assertEqual(tcp_open.call_count, 2)

    @mock.patch.object(controller.RunPodAPI, "_graphql")
    def test_runtime_ssh_connection_selects_tcp_not_udp(self, graphql):
        graphql.return_value = {
            "pod": {
                "runtime": {
                    "ports": [
                        {
                            "ip": "203.0.113.1",
                            "privatePort": 22,
                            "publicPort": 38744,
                            "type": "udp",
                        },
                        {
                            "ip": "203.0.113.1",
                            "privatePort": 22,
                            "publicPort": 38743,
                            "type": "tcp",
                        },
                    ]
                }
            }
        }
        api = controller.RunPodAPI("secret")

        connection = api.runtime_ssh_connection("pod123")

        self.assertEqual(
            connection,
            controller.PodConnection("203.0.113.1", 38743),
        )

    @mock.patch("controller.request.urlopen")
    def test_runpod_api_uses_bearer_auth(self, urlopen):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"id":"pod123"}'
        urlopen.return_value = response

        result = controller.RunPodAPI("secret", base_url="https://example.test").start_pod("pod123")

        req = urlopen.call_args.args[0]
        self.assertEqual(result["id"], "pod123")
        self.assertEqual(req.full_url, "https://example.test/pods/pod123/start")
        self.assertEqual(req.method, "POST")
        self.assertEqual(req.headers["Authorization"], "Bearer secret")
        self.assertIn("Mozilla/5.0", req.headers["User-agent"])

    @mock.patch.object(controller.RunPodAPI, "_request", return_value={})
    def test_restart_pod_uses_restart_endpoint(self, request):
        api = controller.RunPodAPI("secret", base_url="https://example.test")

        api.restart_pod("pod123")

        request.assert_called_once_with("POST", "/pods/pod123/restart")

    @mock.patch.object(controller.RunPodAPI, "_request", return_value={})
    def test_delete_pod_uses_delete_endpoint(self, request):
        api = controller.RunPodAPI("secret", base_url="https://example.test")

        api.delete_pod("pod123")

        request.assert_called_once_with("DELETE", "/pods/pod123")

    @mock.patch.object(controller.RunPodAPI, "_request", return_value={"id": "newpod"})
    def test_create_pod_uses_create_endpoint(self, request):
        api = controller.RunPodAPI("secret", base_url="https://example.test")
        payload = {"name": "seedvc", "gpuTypeIds": ["NVIDIA RTX 4090"]}

        result = api.create_pod(payload)

        self.assertEqual(result["id"], "newpod")
        request.assert_called_once_with("POST", "/pods", payload)

    @mock.patch.object(controller.RunPodAPI, "_request")
    def test_pods_for_network_volume_filters_pods(self, request):
        request.return_value = [
            {"id": "one", "networkVolumeId": "vol123"},
            {"id": "two", "networkVolumeId": "other"},
        ]
        api = controller.RunPodAPI("secret", base_url="https://example.test")

        pods = api.pods_for_network_volume("vol123")

        self.assertEqual([pod["id"] for pod in pods], ["one"])
        request.assert_called_once_with("GET", "/pods")

    @mock.patch.object(controller.RunPodAPI, "_request")
    def test_preferred_pod_for_network_volume_prefers_running(self, request):
        request.return_value = [
            {"id": "old", "networkVolumeId": "vol123", "desiredStatus": "EXITED"},
            {"id": "active", "networkVolumeId": "vol123", "desiredStatus": "RUNNING"},
        ]
        api = controller.RunPodAPI("secret", base_url="https://example.test")

        pod = api.preferred_pod_for_network_volume("vol123")

        self.assertEqual(pod["id"], "active")

    @mock.patch.object(controller.RunPodAPI, "_request")
    def test_preferred_pod_for_network_volume_uses_newest_exited_pod(self, request):
        request.return_value = [
            {
                "id": "old",
                "networkVolumeId": "vol123",
                "desiredStatus": "EXITED",
                "createdAt": "2026-08-30 12:44:58.1 +0000 UTC",
            },
            {
                "id": "new",
                "networkVolumeId": "vol123",
                "desiredStatus": "EXITED",
                "createdAt": "2026-08-31 20:54:28.651 +0000 UTC",
            },
        ]
        api = controller.RunPodAPI("secret", base_url="https://example.test")

        pod = api.preferred_pod_for_network_volume("vol123")

        self.assertEqual(pod["id"], "new")

    @mock.patch.object(controller.RunPodAPI, "_graphql")
    @mock.patch.object(controller.RunPodAPI, "_request")
    def test_preferred_pod_for_network_volume_applies_vram_policy(self, request, graphql):
        request.return_value = [
            {
                "id": "new-too-large",
                "networkVolumeId": "vol123",
                "desiredStatus": "EXITED",
                "createdAt": "2026-09-11 05:30:13.783 +0000 UTC",
            },
            {
                "id": "older-allowed",
                "networkVolumeId": "vol123",
                "desiredStatus": "EXITED",
                "createdAt": "2026-08-31 20:54:28.651 +0000 UTC",
            },
        ]
        graphql.return_value = {
            "myself": {
                "pods": [
                    {
                        "id": "new-too-large",
                        "machine": {"gpuType": {"memoryInGb": 80}},
                    },
                    {
                        "id": "older-allowed",
                        "machine": {"gpuType": {"memoryInGb": 24}},
                    },
                ]
            }
        }
        api = controller.RunPodAPI("secret", base_url="https://example.test")

        pod = api.preferred_pod_for_network_volume("vol123", 16, 48)

        self.assertEqual(pod["id"], "older-allowed")

    @mock.patch.object(controller.RunPodAPI, "_request")
    def test_configure_public_key_preserves_environment(self, request):
        request.side_effect = (
            {"env": {"JUPYTER_PASSWORD": "keep-me", "PUBLIC_KEY": ""}},
            {},
        )
        api = controller.RunPodAPI("secret", base_url="https://example.test")

        api.configure_public_key("pod123", "ssh-ed25519 AAAA test")

        request.assert_has_calls(
            [
                mock.call("GET", "/pods/pod123"),
                mock.call(
                    "POST",
                    "/pods/pod123/update",
                    {
                        "env": {
                            "JUPYTER_PASSWORD": "keep-me",
                            "PUBLIC_KEY": "ssh-ed25519 AAAA test",
                        }
                    },
                ),
            ]
        )


class SSHCommandTests(unittest.TestCase):
    def test_parses_runpod_ssh_command(self):
        connection, key = controller.parse_ssh_command(
            "ssh root@203.0.113.4 -p 17644 -i ~/.ssh/id_ed25519"
        )

        self.assertEqual(connection, controller.PodConnection("203.0.113.4", 17644))
        self.assertEqual(Path(key), Path.home() / ".ssh" / "id_ed25519")

    def test_tunnel_command_is_argument_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            key = Path(directory) / "key with spaces"
            key.touch()
            command = controller.tunnel_command("example.test", 1234, str(key), 8042)

        self.assertEqual(command[0], "ssh")
        self.assertIn("8042:127.0.0.1:8042", command)
        self.assertIn(str(key), command)
        self.assertEqual(command[-1], "root@example.test")

    def test_reference_upload_command_uses_fixed_remote_name(self):
        with tempfile.TemporaryDirectory() as directory:
            key = Path(directory) / "key"
            key.touch()
            reference = Path(directory) / "voice sample.mp3"
            reference.write_bytes(b"not-real-audio-but-nonempty")

            command = controller.scp_upload_command(
                "example.test", 1234, str(key), reference
            )

        self.assertEqual(command[0], "scp")
        self.assertIn(str(reference), command)
        self.assertEqual(
            command[-1],
            "root@example.test:/workspace/seedvc/reference-upload",
        )


class ReferenceValidationTests(unittest.TestCase):
    def test_accepts_supported_nonempty_audio_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "voice.WAV"
            path.write_bytes(b"audio")
            self.assertEqual(controller.validate_reference_file(path), path.resolve())

    def test_rejects_unsupported_or_empty_file(self):
        with tempfile.TemporaryDirectory() as directory:
            unsupported = Path(directory) / "voice.txt"
            unsupported.write_bytes(b"audio")
            empty = Path(directory) / "voice.wav"
            empty.touch()
            with self.assertRaisesRegex(controller.ControllerError, "unsupported"):
                controller.validate_reference_file(unsupported)
            with self.assertRaisesRegex(controller.ControllerError, "empty"):
                controller.validate_reference_file(empty)

    def test_parses_voice_library_and_builds_safe_activation_commands(self):
        voice_id = "a" * 32
        voices, active_id = controller.parse_voice_library(
            json.dumps(
                {
                    "active_id": voice_id,
                    "voices": [{"id": voice_id, "name": "Arabic female"}],
                }
            )
        )

        self.assertEqual(voices, [controller.VoiceReference(voice_id, "Arabic female")])
        self.assertEqual(active_id, voice_id)
        self.assertIn("'voice; name.mp3'", controller.reference_upload_activate_command("voice; name.mp3"))
        self.assertTrue(controller.reference_stored_activate_command(voice_id).endswith(voice_id))


class AutopilotPolicyTests(unittest.TestCase):
    def test_gpu_candidate_allowed_enforces_vram_range(self):
        self.assertFalse(controller.gpu_candidate_allowed({"memoryInGb": 12}))
        self.assertTrue(controller.gpu_candidate_allowed({"memoryInGb": 24}))
        self.assertFalse(controller.gpu_candidate_allowed({"memoryInGb": 80}))
        self.assertFalse(controller.gpu_candidate_allowed({"name": "unknown"}))

    def test_pod_system_memory_is_not_treated_as_gpu_vram(self):
        pod = {"networkVolumeId": "vol123", "memoryInGb": 46}

        self.assertFalse(controller.gpu_candidate_allowed(pod, 16, 48))
        pod["gpuMemoryInGb"] = 24
        self.assertTrue(controller.gpu_candidate_allowed(pod, 16, 48))

    def test_gpu_candidates_sort_by_cheapest_known_price(self):
        candidates = [
            {"id": "expensive", "memoryInGb": 24, "securePrice": "1.2"},
            {"id": "unknown", "memoryInGb": 24},
            {"id": "cheap", "memoryInGb": 24, "securePrice": "0.45"},
        ]

        ordered = controller.sort_gpu_candidates(candidates)

        self.assertEqual([item["id"] for item in ordered], ["cheap", "expensive", "unknown"])

    def test_blackwell_requires_cuda_12_8_runtime(self):
        candidate = {"id": "NVIDIA RTX PRO 4500 Blackwell"}

        self.assertFalse(
            controller.gpu_runtime_compatible(
                candidate,
                "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04",
            )
        )
        self.assertTrue(
            controller.gpu_runtime_compatible(
                candidate,
                "runpod/pytorch:2.7.0-py3.11-cuda12.8.1-devel-ubuntu22.04",
            )
        )

    def test_redact_secrets_omits_short_values(self):
        text = "Bearer runpod-secret and Gemini gemini-secret and short abc"

        redacted = controller.redact_secrets(text, "runpod-secret", "gemini-secret", "abc")

        self.assertEqual(redacted, "Bearer [redacted] and Gemini [redacted] and short abc")


class GuiAutopilotTests(unittest.TestCase):
    def test_stale_settings_widget_does_not_abort_callback(self):
        class StaleWidget:
            def winfo_exists(self):
                return True

            def configure(self, **options):
                raise gui.tk.TclError("invalid command name")

        gui.SeedVCApp._configure_widget_if_alive(
            StaleWidget(), state="disabled"
        )

    def test_autopilot_triggers_on_capacity_failure(self):
        app = object.__new__(gui.SeedVCApp)
        settings = controller.Settings(gemini_autopilot_enabled=True)

        self.assertTrue(
            app._should_run_autopilot(
                settings,
                "RunPod API returned HTTP 500: not enough free GPUs on the host machine",
            )
        )

    def test_gemini_command_uses_noninteractive_yolo_prompt(self):
        app = object.__new__(gui.SeedVCApp)
        settings = controller.Settings(
            gemini_cli_path="gemini.cmd",
            network_volume_id="vol123",
            ssh_key="keyfile",
        )

        command = app._gemini_command(settings)

        self.assertTrue(command[0].endswith("gemini.cmd"))
        self.assertIn("--model", command)
        self.assertIn(gui.GEMINI_AUTOPILOT_MODEL, command)
        self.assertIn("--approval-mode", command)
        self.assertIn("yolo", command)
        self.assertIn("--prompt", command)
        self.assertIn("16-48 GB VRAM", command[-1])

    def test_parse_autopilot_decision_accepts_selected_gpu(self):
        output = 'status line\n{"selected_gpu_type_id":"NVIDIA L4"}\n'

        self.assertEqual(
            gui.SeedVCApp._parse_autopilot_decision(output),
            ["NVIDIA L4"],
        )

    def test_replacement_payload_keeps_only_tcp_ssh_port(self):
        payload = gui.SeedVCApp._replacement_payload(
            {
                "imageName": "image",
                "ports": ["8888/http", "22/udp", "22/tcp"],
            },
            {"id": "vol123", "dataCenterId": "EU-RO-1"},
            ["NVIDIA L4"],
            "ssh-ed25519 AAAA test",
        )

        self.assertIn("22/tcp", payload["ports"])
        self.assertNotIn("22/udp", payload["ports"])

    def test_prepare_gemini_auth_env_forces_api_key_auth(self):
        app = object.__new__(gui.SeedVCApp)
        with tempfile.TemporaryDirectory() as directory:
            original_appdata = os.environ.get("APPDATA")
            os.environ["APPDATA"] = directory
            try:
                env = {
                    "GOOGLE_GENAI_USE_GCA": "true",
                    "GOOGLE_CLOUD_PROJECT": "old-project",
                }
                app._prepare_gemini_auth_env(env)
                settings_path = Path(env["USERPROFILE"]) / ".gemini" / "settings.json"
                stored = json.loads(settings_path.read_text(encoding="utf-8"))

                self.assertEqual(stored["selectedAuthType"], "gemini-api-key")
                self.assertEqual(stored["model"], gui.GEMINI_AUTOPILOT_MODEL)
                self.assertEqual(stored["coreTools"], [])
                self.assertEqual(env["GEMINI_DEFAULT_AUTH_TYPE"], "gemini-api-key")
                self.assertNotIn("GOOGLE_GENAI_USE_GCA", env)
                self.assertNotIn("GOOGLE_CLOUD_PROJECT", env)
            finally:
                if original_appdata is None:
                    os.environ.pop("APPDATA", None)
                else:
                    os.environ["APPDATA"] = original_appdata


if __name__ == "__main__":
    unittest.main()
