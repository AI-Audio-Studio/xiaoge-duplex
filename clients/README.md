# Xiaoge Client SDKs R5.2.2

This directory contains the approved client-side implementation area for G3.
Only files under `clients` are changed.

Main protocol: [PROTOCOL.md](PROTOCOL.md)

## SDKs

| Directory | Purpose | Status |
| --- | --- | --- |
| `python/` | Reference R5.2.2 SDK, local mock selftest, contract replay, file/mic demos | Implemented and locally tested |
| `c/` | libwebsockets R5.2.2 session transport, embedded-friendly API | Implemented; build on target toolchain |
| `matlab/` | MATLAB/Simulink through Python TCP bridge | Bridge self-testable; MATLAB host validation required |
| `android/` | Android Java Core SDK with OkHttp and AudioRecord/AudioTrack helper | Scaffold implemented; Android Gradle host required |

## Required Path

```text
create_session -> /ws/session + Bearer -> ctrl.hello -> ctrl/data.* + PCM
```

`/ws/audio` is historical only. It is not a default demo path and not a forward
compatibility target.

## Cloud TLS CA

The cloud-provided PEM certificate is bundled at `certs/cloud-ca.pem` for
client-side HTTPS/WSS verification. Production client paths should trust this
CA or an updated cloud CA bundle instead of using `--insecure`.

Current bundled CA fingerprint:

```text
sha256=460e09d5d59b91df0e2eb6fe2d47d28db1229cdf561b3e2e2623ae8a0ac6fabf
subject=CN=60.205.197.165
not_after=2027-05-28 10:56 Asia/Shanghai
```



## Safety Boundary

- No real robot action is connected.
- Built-in command execution is fake executor only.
- Frozen R5.2.2 schema/examples/close-code/registry/manifest files are read-only
  external inputs and are not copied into this implementation tree.
- Any real Gateway or robot-action联调 requires the separate G3 gate described
  by the review package.
