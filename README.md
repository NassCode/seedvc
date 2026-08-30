# seedvc

Remote real-time voice conversion prototype for Windows + RunPod.

Architecture:

`Windows microphone -> WebSocket -> Seed-VC GPU service -> WebSocket -> VB-CABLE -> calling app`

The first milestone intentionally keeps one active reference voice on the server. Dynamic voice-library switching will be added after the end-to-end audio path is proven.
