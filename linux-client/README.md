# Linux client (Ubuntu)

The Linux client uses the same tested PCM/WebSocket implementation as the
Windows client, with PipeWire providing the virtual microphone. Ubuntu 24.04
and newer with a normal desktop PipeWire session are supported.

## Install

From this directory, run:

```bash
chmod +x ./*.sh
./setup.sh
```

The setup is idempotent. It installs Python venv/Tk, PortAudio, PipeWire Pulse
utilities, OpenSSH, and Secret Service support; creates `.venv`; installs the
Python requirements; and creates the `SeedVC_Virtual_Microphone` PipeWire sink
and browser-visible `SeedVC_Microphone` input source. `sudo` is used only for
missing Ubuntu packages.

Start the controller with:

```bash
./run-gui.sh
```

In Zoom, Discord, OBS, or another calling app, select **SeedVC_Microphone** as
the microphone. In the SeedVC controller,
select the physical microphone as input and the PipeWire/Pulse output as the
output. The controller prefers the `seedvc_virtual` PulseAudio device, and the
launcher also pins its output stream to that virtual sink.

The RunPod API keys are stored through Python keyring in the desktop Secret
Service (normally GNOME Keyring or KWallet), not in `settings.json`.

## Diagnostics and command-line use

Check the complete local installation:

```bash
./check-system.sh
```

List PortAudio devices or test local audio:

```bash
./run-client.sh --list-devices
./run-client.sh --local-test --input-device 1 --output-device 3 --duration 10
```

Stream without the GUI (start the SSH tunnel separately if needed):

```bash
PULSE_SINK=seedvc_virtual ./run-client.sh \
  --url ws://127.0.0.1:8042/ws \
  --input-device 1 --output-device seedvc_virtual
```

If the virtual microphone disappears after PipeWire restarts, rerun
`./setup-virtual-mic.sh`; `run-gui.sh` does this automatically on every launch.
