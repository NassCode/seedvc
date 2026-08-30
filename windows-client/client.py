"""Low-latency Windows audio client for Fast-VC-Service."""

from __future__ import annotations

import argparse
import asyncio
from collections import deque
import json
import queue
import sys
import time
import uuid

import numpy as np
import sounddevice as sd
import soxr
import websockets
from websockets.exceptions import ConnectionClosed, InvalidURI, WebSocketException

SERVER_INPUT_SR = 16_000
CHANNELS = 1
SAMPLE_WIDTH_BYTES = 2
DEFAULT_CHUNK_MS = 20
QUEUE_AUDIO_MS = 2_000


class ClientError(RuntimeError):
    """An expected, user-actionable client error."""


def status(message: str) -> None:
    print(f"[status] {message}", flush=True)


def warning(message: str) -> None:
    print(f"[warning] {message}", file=sys.stderr, flush=True)


def device_rows(direction: str | None = None) -> list[dict]:
    """Return PortAudio devices enriched with display information."""
    if direction not in (None, "input", "output"):
        raise ValueError(f"invalid device direction: {direction}")

    rows = []
    default_input, default_output = sd.default.device
    for index, raw_device in enumerate(sd.query_devices()):
        device = dict(raw_device)
        if direction == "input" and int(device["max_input_channels"]) < 1:
            continue
        if direction == "output" and int(device["max_output_channels"]) < 1:
            continue
        device.update(
            index=index,
            hostapi_name=sd.query_hostapis(device["hostapi"])["name"],
            is_default_input=index == default_input,
            is_default_output=index == default_output,
        )
        rows.append(device)
    return rows


def print_devices(direction: str | None = None) -> None:
    rows = device_rows(direction)
    if not rows:
        print(f"No {direction or 'audio'} devices found.")
        return

    print(" ID  Kind  Default rate  Host API             Name")
    print("---  ----  ------------  -------------------  ----")
    for device in rows:
        can_input = int(device["max_input_channels"]) > 0
        can_output = int(device["max_output_channels"]) > 0
        kind = "I/O" if can_input and can_output else "IN" if can_input else "OUT"
        default_marker = ""
        if device["is_default_input"] and can_input:
            default_marker += " default-in"
        if device["is_default_output"] and can_output:
            default_marker += " default-out"
        print(
            f"{device['index']:>3}  {kind:<4}  "
            f"{int(round(device['default_samplerate'])):>10} Hz  "
            f"{device['hostapi_name'][:19]:<19}  {device['name']}{default_marker}"
        )


def resolve_device(selector: str | int, direction: str) -> int:
    """Resolve a numeric ID or unambiguous case-insensitive device name."""
    rows = device_rows(direction)
    if isinstance(selector, int) or str(selector).strip().isdigit():
        index = int(selector)
        if any(device["index"] == index for device in rows):
            return index
        raise ClientError(f"device {index} is not a usable {direction} device")

    needle = str(selector).strip().casefold()
    if not needle:
        raise ClientError(f"empty {direction} device selector")

    exact = [d for d in rows if d["name"].casefold() == needle]
    matches = exact or [d for d in rows if needle in d["name"].casefold()]
    if len(matches) == 1:
        return int(matches[0]["index"])
    if not matches:
        raise ClientError(
            f"no {direction} device matches {selector!r}; run with --list-devices"
        )
    choices = ", ".join(f"{d['index']} ({d['hostapi_name']})" for d in matches)
    raise ClientError(
        f"{direction} device name {selector!r} is ambiguous: {choices}; use its numeric ID"
    )


def choose_device(selector: str | None, direction: str) -> int:
    if selector is not None:
        return resolve_device(selector, direction)

    rows = device_rows(direction)
    if not rows:
        raise ClientError(f"no usable {direction} devices were found")
    if not sys.stdin.isatty():
        default_index = sd.default.device[0 if direction == "input" else 1]
        if default_index >= 0:
            status(f"using default {direction} device {default_index}")
            return resolve_device(default_index, direction)
        raise ClientError(f"--{direction}-device is required in non-interactive mode")

    print(f"\nAvailable {direction} devices:")
    print_devices(direction)
    while True:
        try:
            answer = input(f"Select {direction} device ID: ").strip()
        except EOFError as exc:
            raise ClientError(f"no {direction} device selected") from exc
        try:
            return resolve_device(answer, direction)
        except ClientError as exc:
            warning(str(exc))


def describe_device(index: int) -> str:
    device = sd.query_devices(index)
    hostapi = sd.query_hostapis(device["hostapi"])["name"]
    return f"{index}: {device['name']} [{hostapi}]"


def default_sample_rate(device: int) -> int:
    return int(round(float(sd.query_devices(device)["default_samplerate"])))


def validate_stream_settings(
    input_device: int,
    output_device: int,
    capture_rate: int,
    output_rate: int,
) -> None:
    try:
        sd.check_input_settings(
            device=input_device,
            channels=CHANNELS,
            dtype="int16",
            samplerate=capture_rate,
        )
    except sd.PortAudioError as exc:
        raise ClientError(
            f"input device cannot capture at {capture_rate} Hz: {exc}"
        ) from exc
    try:
        sd.check_output_settings(
            device=output_device,
            channels=CHANNELS,
            dtype="int16",
            samplerate=output_rate,
        )
    except sd.PortAudioError as exc:
        raise ClientError(
            f"output device cannot play at {output_rate} Hz: {exc}"
        ) from exc


class LatestQueue:
    """Bounded queue that drops oldest audio to prevent runaway latency."""

    def __init__(self, maxsize: int):
        self.queue: queue.Queue[np.ndarray | bytes] = queue.Queue(maxsize=maxsize)
        self.dropped = 0

    def put(self, item: np.ndarray | bytes) -> None:
        try:
            self.queue.put_nowait(item)
            return
        except queue.Full:
            pass
        try:
            self.queue.get_nowait()
        except queue.Empty:
            pass
        self.dropped += 1
        try:
            self.queue.put_nowait(item)
        except queue.Full:
            self.dropped += 1


class PlaybackBuffer:
    """Allocation-light PCM chunk reader used by the PortAudio callback."""

    def __init__(self, source: LatestQueue):
        self.source = source
        self.chunks: deque[np.ndarray] = deque()
        self.offset = 0
        self.underflows = 0

    def fill(self, outdata: np.ndarray, frames: int) -> None:
        outdata.fill(0)
        written = 0
        while written < frames:
            if not self.chunks:
                try:
                    item = self.source.queue.get_nowait()
                except queue.Empty:
                    self.underflows += 1
                    break
                assert isinstance(item, np.ndarray)
                self.chunks.append(item)
                self.offset = 0

            chunk = self.chunks[0]
            available = len(chunk) - self.offset
            count = min(frames - written, available)
            outdata[written : written + count, 0] = chunk[
                self.offset : self.offset + count
            ]
            written += count
            self.offset += count
            if self.offset == len(chunk):
                self.chunks.popleft()
                self.offset = 0


def make_resampler(input_rate: int, output_rate: int) -> soxr.ResampleStream | None:
    if input_rate == output_rate:
        return None
    return soxr.ResampleStream(
        input_rate,
        output_rate,
        CHANNELS,
        dtype="int16",
        quality="LQ",
    )


def resample_chunk(
    resampler: soxr.ResampleStream | None, samples: np.ndarray
) -> np.ndarray:
    mono = np.ascontiguousarray(samples[:, 0])
    if resampler is None:
        return mono.copy()
    return resampler.resample_chunk(mono)


def queue_capacity(chunk_ms: int) -> int:
    return max(4, QUEUE_AUDIO_MS // chunk_ms)


def build_start_signal(stream_id: str, output_rate: int) -> dict:
    """Build Fast-VC-Service's supported simple-protocol configuration."""
    return {
        "signal": "start",
        "stream_id": stream_id,
        "sample_rate": SERVER_INPUT_SR,
        "sample_rate_out": output_rate,
        "sample_bit": 16,
        "encoding": "PCM",
    }


def run_local(args: argparse.Namespace, input_device: int, output_device: int) -> None:
    capture_rate = args.capture_sample_rate or default_sample_rate(input_device)
    output_rate = args.output_sample_rate or default_sample_rate(output_device)
    validate_stream_settings(input_device, output_device, capture_rate, output_rate)

    chunks = LatestQueue(queue_capacity(args.chunk_ms))
    playback = PlaybackBuffer(chunks)
    resampler = make_resampler(capture_rate, output_rate)
    callback_messages: queue.SimpleQueue[str] = queue.SimpleQueue()
    blocksize_in = max(1, round(capture_rate * args.chunk_ms / 1000))
    blocksize_out = max(1, round(output_rate * args.chunk_ms / 1000))
    peak = 0

    def input_callback(indata, frames, time_info, callback_status):
        nonlocal peak
        if callback_status:
            callback_messages.put(f"input: {callback_status}")
        converted = resample_chunk(resampler, indata)
        if len(converted):
            peak = max(peak, int(np.max(np.abs(converted.astype(np.int32)))))
            chunks.put(converted)

    def output_callback(outdata, frames, time_info, callback_status):
        if callback_status:
            callback_messages.put(f"output: {callback_status}")
        playback.fill(outdata, frames)

    status(f"input:  {describe_device(input_device)} at {capture_rate} Hz")
    status(f"output: {describe_device(output_device)} at {output_rate} Hz")
    status("local monitor starting (microphone is played directly to the output)")
    if args.duration:
        status(f"test will stop after {args.duration:g} seconds")
    else:
        status("press Ctrl+C to stop")

    started = time.monotonic()
    try:
        with sd.InputStream(
            device=input_device,
            samplerate=capture_rate,
            channels=CHANNELS,
            dtype="int16",
            blocksize=blocksize_in,
            callback=input_callback,
            latency="low",
        ), sd.OutputStream(
            device=output_device,
            samplerate=output_rate,
            channels=CHANNELS,
            dtype="int16",
            blocksize=blocksize_out,
            callback=output_callback,
            latency="low",
        ):
            while args.duration is None or time.monotonic() - started < args.duration:
                try:
                    message = callback_messages.get(timeout=0.25)
                    warning(message)
                except queue.Empty:
                    pass
    except sd.PortAudioError as exc:
        raise ClientError(f"could not run local audio streams: {exc}") from exc

    elapsed = time.monotonic() - started
    level_dbfs = -96.0 if peak == 0 else 20 * np.log10(peak / 32768.0)
    status(
        f"local audio test completed: {elapsed:.1f}s, peak {level_dbfs:.1f} dBFS, "
        f"dropped {chunks.dropped} chunk(s)"
    )
    if level_dbfs < -60:
        warning(
            "microphone signal was effectively silent; check Windows microphone "
            "privacy, mute, level, and the selected input device"
        )


async def run_remote(
    args: argparse.Namespace, input_device: int, output_device: int
) -> None:
    capture_rate = args.capture_sample_rate or default_sample_rate(input_device)
    output_rate = args.output_sample_rate or default_sample_rate(output_device)
    validate_stream_settings(input_device, output_device, capture_rate, output_rate)

    send_chunks = LatestQueue(queue_capacity(args.chunk_ms))
    # Fast-VC-Service normally returns 500 ms blocks. Four blocks cap queued
    # playback near two seconds if the output device or network falls behind.
    recv_chunks = LatestQueue(4)
    playback = PlaybackBuffer(recv_chunks)
    input_resampler = make_resampler(capture_rate, SERVER_INPUT_SR)
    callback_messages: queue.SimpleQueue[str] = queue.SimpleQueue()
    stop_event = asyncio.Event()
    blocksize_in = max(1, round(capture_rate * args.chunk_ms / 1000))
    blocksize_out = max(1, round(output_rate * args.chunk_ms / 1000))
    stream_id = f"stream_{uuid.uuid4().hex[:12]}"

    def input_callback(indata, frames, time_info, callback_status):
        if callback_status:
            callback_messages.put(f"input: {callback_status}")
        converted = resample_chunk(input_resampler, indata)
        if len(converted):
            send_chunks.put(converted.tobytes())

    def output_callback(outdata, frames, time_info, callback_status):
        if callback_status:
            callback_messages.put(f"output: {callback_status}")
        playback.fill(outdata, frames)

    status(f"input:  {describe_device(input_device)} at {capture_rate} Hz")
    status(f"output: {describe_device(output_device)} at {output_rate} Hz")
    status(f"connecting to {args.url}")

    tasks: set[asyncio.Task] = set()
    received_audio = False
    try:
        async with websockets.connect(
            args.url,
            max_size=None,
            open_timeout=args.connect_timeout,
            ping_interval=20,
            ping_timeout=20,
            compression=None,
        ) as websocket:
            start_signal = build_start_signal(stream_id, output_rate)
            await websocket.send(json.dumps(start_signal))
            status(f"connected; stream {stream_id}")

            async def sender() -> None:
                while not stop_event.is_set():
                    try:
                        payload = send_chunks.queue.get_nowait()
                    except queue.Empty:
                        await asyncio.sleep(0.002)
                        continue
                    assert isinstance(payload, bytes)
                    await websocket.send(payload)

            async def receiver() -> None:
                nonlocal received_audio
                while not stop_event.is_set():
                    message = await websocket.recv()
                    if isinstance(message, bytes):
                        if len(message) % SAMPLE_WIDTH_BYTES:
                            raise ClientError(
                                f"server returned an odd-sized PCM frame ({len(message)} bytes)"
                            )
                        recv_chunks.put(np.frombuffer(message, dtype="<i2").copy())
                        if not received_audio:
                            received_audio = True
                            status("receiving converted audio")
                        continue
                    try:
                        data = json.loads(message)
                    except json.JSONDecodeError:
                        warning(f"unrecognized server message: {message}")
                        continue
                    if data.get("status") == "failed" or data.get("type") == "error":
                        raise ClientError(
                            data.get("error_msg")
                            or data.get("message")
                            or "server conversion failed"
                        )
                    if data.get("signal") == "completed" or data.get("type") == "complete":
                        status("server completed the stream")
                        stop_event.set()
                    else:
                        status(f"server: {data}")

            async def report_callback_status() -> None:
                while not stop_event.is_set():
                    try:
                        message = callback_messages.get_nowait()
                    except queue.Empty:
                        await asyncio.sleep(0.1)
                        continue
                    warning(message)

            async def control_stdin() -> None:
                """Allow a parent GUI to request a protocol-clean shutdown."""
                while not stop_event.is_set():
                    command = await asyncio.to_thread(sys.stdin.readline)
                    if not command or command.strip().casefold() in {"stop", "quit", "exit"}:
                        status("stop requested by controller")
                        stop_event.set()
                        return

            try:
                with sd.InputStream(
                    device=input_device,
                    samplerate=capture_rate,
                    channels=CHANNELS,
                    dtype="int16",
                    blocksize=blocksize_in,
                    callback=input_callback,
                    latency="low",
                ), sd.OutputStream(
                    device=output_device,
                    samplerate=output_rate,
                    channels=CHANNELS,
                    dtype="int16",
                    blocksize=blocksize_out,
                    callback=output_callback,
                    latency="low",
                ):
                    status("voice conversion active; press Ctrl+C to stop")
                    tasks = {
                        asyncio.create_task(sender(), name="sender"),
                        asyncio.create_task(receiver(), name="receiver"),
                        asyncio.create_task(report_callback_status(), name="audio-status"),
                    }
                    if args.control_stdin:
                        tasks.add(asyncio.create_task(control_stdin(), name="controller"))
                    done, pending = await asyncio.wait(
                        tasks, return_when=asyncio.FIRST_EXCEPTION
                    )
                    for task in done:
                        exception = task.exception()
                        if exception:
                            raise exception
                    if stop_event.is_set():
                        for task in pending:
                            task.cancel()
            except sd.PortAudioError as exc:
                raise ClientError(f"could not run audio streams: {exc}") from exc
            finally:
                stop_event.set()
                try:
                    await websocket.send(json.dumps({"signal": "end"}))
                except (ConnectionClosed, RuntimeError):
                    pass
                for task in tasks:
                    task.cancel()
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
    except TimeoutError as exc:
        raise ClientError(
            f"connection timed out after {args.connect_timeout:g}s: {args.url}"
        ) from exc
    except InvalidURI as exc:
        raise ClientError(f"invalid WebSocket URL {args.url!r}: {exc}") from exc
    except ConnectionClosed as exc:
        raise ClientError(
            f"server connection closed (code {exc.code}, reason: {exc.reason or 'none'})"
        ) from exc
    except OSError as exc:
        raise ClientError(f"could not connect to {args.url}: {exc}") from exc
    except WebSocketException as exc:
        raise ClientError(f"WebSocket error: {exc}") from exc
    finally:
        if send_chunks.dropped or recv_chunks.dropped:
            warning(
                f"latency protection dropped {send_chunks.dropped} input and "
                f"{recv_chunks.dropped} output chunk(s)"
            )


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Windows microphone client for Fast-VC-Service"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--local-test",
        action="store_true",
        help="monitor microphone directly to output without a server",
    )
    mode.add_argument(
        "--list-devices", action="store_true", help="list audio devices and exit"
    )
    parser.add_argument("--url", default="ws://127.0.0.1:8042/ws")
    parser.add_argument(
        "--input-device", help="input device numeric ID or unique name substring"
    )
    parser.add_argument(
        "--output-device", help="output device numeric ID or unique name substring"
    )
    parser.add_argument(
        "--capture-sample-rate",
        type=positive_int,
        help="hardware capture rate (default: selected device's native rate)",
    )
    parser.add_argument(
        "--output-sample-rate",
        type=positive_int,
        help="hardware output rate (default: selected device's native rate)",
    )
    parser.add_argument("--chunk-ms", type=positive_int, default=DEFAULT_CHUNK_MS)
    parser.add_argument(
        "--duration",
        type=float,
        help="local-test duration in seconds (default: until Ctrl+C)",
    )
    parser.add_argument("--connect-timeout", type=float, default=10.0)
    parser.add_argument(
        "--control-stdin",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args(argv)
    if args.duration is not None and args.duration <= 0:
        parser.error("--duration must be greater than zero")
    if args.duration is not None and not args.local_test:
        parser.error("--duration is only valid with --local-test")
    if args.chunk_ms > 100:
        parser.error("--chunk-ms must not exceed 100 ms")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.list_devices:
        print_devices()
        cable_present = any(
            "cable input" in device["name"].casefold()
            for device in device_rows("output")
        )
        if not cable_present:
            warning("CABLE Input (VB-Audio Virtual Cable) is not currently installed or visible")
        return 0

    try:
        input_device = choose_device(args.input_device, "input")
        output_device = choose_device(args.output_device, "output")
        if args.local_test:
            run_local(args, input_device, output_device)
        else:
            asyncio.run(run_remote(args, input_device, output_device))
        return 0
    except KeyboardInterrupt:
        status("stopped")
        return 130
    except ClientError as exc:
        print(f"[error] {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
