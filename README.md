# seedvc

Remote real-time voice conversion prototype for Windows + RunPod.

Architecture:

`Windows microphone -> WebSocket -> Seed-VC GPU service -> WebSocket -> VB-CABLE -> calling app`

The first milestone intentionally keeps one active reference voice on the server. Dynamic voice-library switching will be added after the end-to-end audio path is proven.

See [windows-client/README.md](windows-client/README.md) for local Windows audio
setup and testing. See [server/README.md](server/README.md) for the pinned
Fast-VC-Service RunPod deployment and secure first-test workflow.
