import argparse
import asyncio
import json
import queue
import signal
import time
import uuid

import numpy as np
import sounddevice as sd
import websockets

INPUT_SR = 16000
OUTPUT_SR = 16000
CHANNELS = 1
SAMPLE_WIDTH_BYTES = 2


def list_devices():
    devices = sd.query_devices()
    for i, d in enumerate(devices):
        ins = int(d['max_input_channels'])
        outs = int(d['max_output_channels'])
        print(f"[{i:>2}] in={ins} out={outs}  {d['name']}")


async def run(args):
    send_q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=200)
    recv_q: queue.Queue[np.ndarray] = queue.Queue(maxsize=200)
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    blocksize = int(INPUT_SR * args.chunk_ms / 1000)

    def input_callback(indata, frames, time_info, status):
        if status:
            print(f"input status: {status}")
        pcm = np.asarray(indata[:, 0], dtype=np.float32)
        pcm = np.clip(pcm, -1.0, 1.0)
        payload = (pcm * 32767.0).astype(np.int16).tobytes()
        try:
            loop.call_soon_threadsafe(send_q.put_nowait, payload)
        except Exception:
            pass

    playback_buffer = np.zeros(0, dtype=np.float32)

    def output_callback(outdata, frames, time_info, status):
        nonlocal playback_buffer
        if status:
            print(f"output status: {status}")
        while len(playback_buffer) < frames:
            try:
                playback_buffer = np.concatenate((playback_buffer, recv_q.get_nowait()))
            except queue.Empty:
                break

        if len(playback_buffer) >= frames:
            chunk = playback_buffer[:frames]
            playback_buffer = playback_buffer[frames:]
        else:
            chunk = np.pad(playback_buffer, (0, frames - len(playback_buffer)))
            playback_buffer = np.zeros(0, dtype=np.float32)

        outdata[:, 0] = chunk

    stream_id = f"stream_{uuid.uuid4().hex[:12]}"

    async with websockets.connect(args.url, max_size=None, ping_interval=20, ping_timeout=20) as ws:
        start_signal = {
            "signal": "start",
            "stream_id": stream_id,
            "sample_rate": INPUT_SR,
            "sample_bit": 16,
        }
        await ws.send(json.dumps(start_signal))
        print(f"connected: {args.url}")
        print(f"stream:    {stream_id}")

        async def sender():
            while not stop_event.is_set():
                payload = await send_q.get()
                await ws.send(payload)

        async def receiver():
            while not stop_event.is_set():
                msg = await ws.recv()
                if isinstance(msg, bytes):
                    arr = np.frombuffer(msg, dtype=np.int16).astype(np.float32) / 32768.0
                    try:
                        recv_q.put_nowait(arr)
                    except queue.Full:
                        try:
                            recv_q.get_nowait()
                            recv_q.put_nowait(arr)
                        except queue.Empty:
                            pass
                else:
                    try:
                        data = json.loads(msg)
                    except json.JSONDecodeError:
                        print(f"server: {msg}")
                        continue
                    if data.get("status") == "failed":
                        raise RuntimeError(data.get("error_msg", "server conversion failed"))
                    if data.get("signal") == "completed":
                        stop_event.set()

        with sd.InputStream(
            device=args.input_device,
            samplerate=INPUT_SR,
            channels=CHANNELS,
            dtype="float32",
            blocksize=blocksize,
            callback=input_callback,
            latency="low",
        ), sd.OutputStream(
            device=args.output_device,
            samplerate=OUTPUT_SR,
            channels=CHANNELS,
            dtype="float32",
            blocksize=0,
            callback=output_callback,
            latency="low",
        ):
            print("voice conversion active — press Ctrl+C to stop")
            tasks = [asyncio.create_task(sender()), asyncio.create_task(receiver())]
            try:
                await stop_event.wait()
            finally:
                try:
                    await ws.send(json.dumps({"signal": "end"}))
                except Exception:
                    pass
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)


def main():
    parser = argparse.ArgumentParser(description="Minimal Seed-VC remote Windows client")
    parser.add_argument("--url", default="ws://127.0.0.1:8042/ws")
    parser.add_argument("--input-device", type=int)
    parser.add_argument("--output-device", type=int)
    parser.add_argument("--chunk-ms", type=int, default=20)
    parser.add_argument("--list-devices", action="store_true")
    args = parser.parse_args()

    if args.list_devices:
        list_devices()
        return

    if args.input_device is None or args.output_device is None:
        parser.error("--input-device and --output-device are required; use --list-devices first")

    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print("stopped")


if __name__ == "__main__":
    main()
