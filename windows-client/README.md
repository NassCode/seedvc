# Windows client

Low-latency full-duplex PCM client for Fast-VC-Service. The client opens each
Windows device at its native rate, resamples microphone audio to the server's
16 kHz input rate, and asks the server to return audio at the selected output
device's native rate.

## 1. Install VB-CABLE

Install VB-CABLE and reboot Windows if prompted. The playback device normally appears as `CABLE Input`; calling apps can then use `CABLE Output` as their microphone.

## 2. Install Python dependencies

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

## 3. Find device IDs

```powershell
python client.py --list-devices
```

The table shows the host API and native rate for every device. Prefer the
`Windows WASAPI` entry for your microphone and `CABLE Input` when available.
The same physical device can appear multiple times through different host APIs,
so numeric IDs are the least ambiguous selectors.

If `CABLE Input` is not listed, VB-CABLE is not installed, disabled, or Windows
has not been restarted since installation.

## 4. Test local capture and playback

Test the selected path without a server first:

```powershell
python client.py --local-test --input-device 15 --output-device 13 --duration 10
```

Speak during the test. The final peak should normally be above `-60 dBFS`. A
near-silent result points to the selected device, mute/level settings, or
Windows microphone privacy rather than the WebSocket path. Omit `--duration`
to run until Ctrl+C.

## 5. Start streaming

```powershell
python client.py --url ws://YOUR_RUNPOD_HOST:8042/ws --input-device 1 --output-device 8
```

Replace the device IDs with the values from step 3. Device selectors may also
be unique name fragments, for example:

```powershell
python client.py --url wss://YOUR_HOST/ws --input-device 15 --output-device "CABLE Input"
```

If device arguments are omitted in an interactive terminal, the client prints
the relevant devices and prompts for each ID.

The client captures mono PCM in 20 ms packets, resamples it to 16 kHz for
Fast-VC-Service, receives converted PCM at the negotiated output-device rate,
and writes it to the selected Windows playback device. It drops the oldest
queued audio if a stalled network or device would otherwise cause latency to
grow without bound.

Clear `[status]`, `[warning]`, and `[error]` messages identify connection,
device, server, underrun, and timeout failures.

## 6. Run automated checks

```powershell
python -m unittest -v
```

The suite includes a local WebSocket server that verifies the exact
Fast-VC-Service simple-protocol start, binary PCM, converted PCM, completion,
and end-message exchange. The protocol was checked against upstream
Fast-VC-Service commit `27eced5` (2026-08-18).

## Security note

Do not expose an unauthenticated plain `ws://` endpoint directly to the public Internet for production use. The prototype should first be tested through a private tunnel or a TLS/authenticated reverse proxy.
