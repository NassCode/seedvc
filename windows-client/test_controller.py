import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import controller


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
                ssh_key="keyfile",
            )
            controller.save_settings(expected, path)
            stored = json.loads(path.read_text(encoding="utf-8"))

            self.assertEqual(controller.load_settings(path), expected)
            self.assertNotIn("api_key", stored)


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

    @mock.patch.object(controller.RunPodAPI, "_request", return_value={})
    def test_restart_pod_uses_restart_endpoint(self, request):
        api = controller.RunPodAPI("secret", base_url="https://example.test")

        api.restart_pod("pod123")

        request.assert_called_once_with("POST", "/pods/pod123/restart")


class SSHCommandTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
