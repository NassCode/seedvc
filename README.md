# seedvc

Remote real-time voice conversion prototype for Windows/Linux + RunPod.

Architecture:

`Microphone -> WebSocket -> Seed-VC GPU service -> WebSocket -> virtual microphone -> calling app`

The first milestone intentionally keeps one active reference voice on the server. Dynamic voice-library switching will be added after the end-to-end audio path is proven.

See [windows-client/README.md](windows-client/README.md) for local Windows audio
setup and testing, or [linux-client/README.md](linux-client/README.md) for the
Ubuntu/PipeWire client. See [server/README.md](server/README.md) for the pinned
Fast-VC-Service RunPod deployment and secure first-test workflow.

For normal use, run `windows-client/setup-gui.bat` once and then launch
`windows-client/run-gui.bat`. The desktop controller manages audio devices,
Fast-VC startup, the SSH tunnel, the audio stream, and optional RunPod
start/stop automation.

On Ubuntu, run `linux-client/setup.sh` once and then launch
`linux-client/run-gui.sh`. The Linux launcher creates the PipeWire virtual
microphone automatically before starting the same desktop controller.
