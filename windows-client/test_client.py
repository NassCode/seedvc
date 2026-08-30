import unittest
from unittest import mock

import numpy as np

import client


DEVICES = [
    {
        "name": "Physical Microphone",
        "hostapi": 0,
        "max_input_channels": 1,
        "max_output_channels": 0,
        "default_samplerate": 48000.0,
    },
    {
        "name": "CABLE Input (VB-Audio Virtual Cable)",
        "hostapi": 0,
        "max_input_channels": 0,
        "max_output_channels": 2,
        "default_samplerate": 48000.0,
    },
    {
        "name": "Physical Microphone (duplicate API)",
        "hostapi": 1,
        "max_input_channels": 1,
        "max_output_channels": 0,
        "default_samplerate": 44100.0,
    },
]


class ClientTests(unittest.TestCase):
    def setUp(self):
        query_devices = mock.patch.object(
            client.sd,
            "query_devices",
            side_effect=lambda index=None: DEVICES if index is None else DEVICES[index],
        )
        query_hostapis = mock.patch.object(
            client.sd,
            "query_hostapis",
            side_effect=lambda index=None: (
                [{"name": "Windows WASAPI"}, {"name": "MME"}]
                if index is None
                else [{"name": "Windows WASAPI"}, {"name": "MME"}][index]
            ),
        )
        default_device = mock.patch.object(client.sd.default, "device", (0, 1))
        self.addCleanup(query_devices.stop)
        self.addCleanup(query_hostapis.stop)
        self.addCleanup(default_device.stop)
        query_devices.start()
        query_hostapis.start()
        default_device.start()

    def test_resolve_device_by_id_and_name(self):
        self.assertEqual(client.resolve_device("0", "input"), 0)
        self.assertEqual(client.resolve_device("CABLE Input", "output"), 1)

    def test_resolve_device_rejects_wrong_direction(self):
        with self.assertRaisesRegex(client.ClientError, "not a usable output"):
            client.resolve_device("0", "output")

    def test_resolve_device_reports_missing_name(self):
        with self.assertRaisesRegex(client.ClientError, "no output device matches"):
            client.resolve_device("not installed", "output")

    def test_playback_buffer_spans_chunks_and_zero_fills(self):
        source = client.LatestQueue(maxsize=4)
        source.put(np.array([1, 2], dtype=np.int16))
        source.put(np.array([3], dtype=np.int16))
        playback = client.PlaybackBuffer(source)
        output = np.empty((5, 1), dtype=np.int16)

        playback.fill(output, 5)

        np.testing.assert_array_equal(output[:, 0], [1, 2, 3, 0, 0])
        self.assertEqual(playback.underflows, 1)

    def test_latest_queue_drops_oldest(self):
        source = client.LatestQueue(maxsize=2)
        source.put(b"first")
        source.put(b"second")
        source.put(b"third")

        self.assertEqual(source.queue.get_nowait(), b"second")
        self.assertEqual(source.queue.get_nowait(), b"third")
        self.assertEqual(source.dropped, 1)

    def test_start_signal_sample_rates_match_protocol(self):
        capture_rate = 48_000
        output_rate = 48_000
        signal = {
            "signal": "start",
            "sample_rate": client.SERVER_INPUT_SR,
            "sample_rate_out": output_rate,
            "sample_bit": 16,
        }
        self.assertNotEqual(capture_rate, signal["sample_rate"])
        self.assertEqual(signal["sample_rate"], 16_000)
        self.assertEqual(signal["sample_rate_out"], output_rate)

    def test_streaming_resampler_converts_native_rate_to_server_rate(self):
        resampler = client.make_resampler(48_000, client.SERVER_INPUT_SR)
        output = []
        for _ in range(50):
            chunk = np.zeros((960, 1), dtype=np.int16)
            output.append(client.resample_chunk(resampler, chunk))
        output.append(resampler.resample_chunk(np.zeros(0, dtype=np.int16), last=True))

        self.assertEqual(sum(map(len, output)), client.SERVER_INPUT_SR)


if __name__ == "__main__":
    unittest.main()
