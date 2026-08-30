# Windows client

Minimal full-duplex PCM client for Fast-VC-Service.

## 1. Install VB-CABLE

Install VB-CABLE and reboot Windows if prompted. The playback device normally appears as `CABLE Input`; calling apps can then use `CABLE Output` as their microphone.

## 2. Install Python dependencies

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## 3. Find device IDs

```powershell
python client.py --list-devices
```

Note the ID of your physical microphone and the output device named `CABLE Input`.

## 4. Start streaming

```powershell
python client.py --url ws://YOUR_RUNPOD_HOST:8042/ws --input-device 1 --output-device 8
```

Replace the device IDs with the values from step 3.

The client captures 16 kHz mono PCM in 20 ms packets, sends them to Fast-VC-Service, receives converted PCM, and writes it to the selected Windows playback device.

## Security note

Do not expose an unauthenticated plain `ws://` endpoint directly to the public Internet for production use. The prototype should first be tested through a private tunnel or a TLS/authenticated reverse proxy.
