# RunPod Seed-VC server

This deployment uses Fast-VC-Service commit `27eced5` with the checked-in Saudi
Arabic reference voice. The bootstrap copies the config and WAV into the
upstream checkout, installs its locked environment, validates the audio/config,
and reports the visible NVIDIA GPU.

## Pod setup

Use a current RunPod PyTorch Pod with one NVIDIA GPU and enough persistent
`/workspace` storage for model downloads. Connect through the Pod's web terminal
or SSH, then run:

```bash
cd /workspace
git clone https://github.com/NassCode/seedvc.git
cd seedvc
bash server/bootstrap.sh
```

The initial install and first service start download large Python/model assets.
Keep `/workspace` persistent so they survive Pod restarts.

Start or verify the service with the idempotent launcher:

```bash
bash /workspace/seedvc/server/start.sh
```

The launcher restores the container-layer runtime after a Pod restart when
needed, preserves the cached environment and models under `/workspace`, starts
Fast-VC-Service in the background, and waits until port 8042 is ready. It is
safe to run again when the service is already running.

To start the service in the foreground for diagnostics instead:

```bash
cd /workspace/fast-vc-service
uv run fast-vc serve --config configs/seedvc-saudi.yaml
```

Wait for model initialization to finish and confirm the service is listening on
`0.0.0.0:8042`. Leave this terminal open while testing.

## Recommended first remote test: SSH tunnel

Fast-VC-Service's simple protocol has no real authentication. Do not publish
port 8042 as an unauthenticated plain-WebSocket service. From Windows, use the
SSH command shown by RunPod's Connect panel and add local forwarding:

```powershell
ssh -N -L 8042:127.0.0.1:8042 root@RUNPOD_IP -p RUNPOD_SSH_PORT -i $HOME\.ssh\id_ed25519
```

Then point the Windows client at `ws://127.0.0.1:8042/ws`. The WebSocket crosses
the internet inside the encrypted SSH connection without exposing the service.

## RunPod HTTP proxy alternative

If port 8042 is exposed as an HTTP port, the proxy URL is:

```text
wss://POD_ID-8042.proxy.runpod.net/ws
```

The proxy provides TLS but the endpoint remains public and unauthenticated.
RunPod also documents a 100-second HTTP-proxy connection limit, so this is only
appropriate for short smoke tests. Persistent WebSockets are better suited to
direct TCP, but direct TCP requires adding TLS and authentication before public
use.

## Validation and troubleshooting

Re-run the staged config/reference check inside the Pod:

```bash
cd /workspace/fast-vc-service
uv run python /workspace/seedvc/server/validate_deployment.py \
  --root /workspace/fast-vc-service \
  --config configs/seedvc-saudi.yaml
```

Expected reference properties are mono PCM16, 24 kHz, and approximately 35.47
seconds. The service uses one worker on `cuda:0`, accepts 16 kHz PCM input, and
internally generates 22.05 kHz audio; the client requests resampling to the
selected Windows output device's native rate.
