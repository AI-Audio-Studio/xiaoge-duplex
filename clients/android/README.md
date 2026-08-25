# Xiaoge Android Java Core SDK R5.2.2

This module implements the approved R5.2.2 client path:

```text
HTTPS create_session -> WSS /ws/session + Authorization: Bearer -> ctrl.hello
-> PCM + ctrl/data JSON -> event callbacks
```

Real robot actions are not connected. Downlink JSON messages are exposed through
typed event callbacks such as `onStt`, `onReply`, and `onCommand`. Use
`onJson(JSONObject)` as the single raw observer for logging, debugging, or
protocol audits.

## Build

```bash
cd clients/android
.\gradlew.bat :xiaoge-sdk-core:testDebugUnitTest
.\gradlew.bat :xiaoge-sdk-core:assembleRelease
.\gradlew.bat :demo:assembleDebug
```

The wrapper is pinned to `gradle-8.9-bin.zip` in
`gradle/wrapper/gradle-wrapper.properties`.

## Android SDK delivery package

Generate the customer delivery package from the `clients` directory:

```bash
cd clients
python package_android_sdk.py
```

Outputs:

```text
dist/xiaoge-android-sdk-r5.2.2/
dist/xiaoge-android-sdk-r5.2.2.zip
```

The package includes the release AAR, Demo source, Chinese SDK guide, protocol
file, and `SHA256SUMS.txt`. The packaging script excludes local build
artifacts and secrets such as `local.properties`, `.gradle/`, `build/`, and
`.idea/`.

Chinese integration guide: `docs/ANDROID_SDK_README.md`.

## Usage

```java
XiaogeConfig cfg = new XiaogeConfig(
    createSessionUrl,
    "robot-x3-001",
    "{\"key_id\":\"dev\",\"signature\":\"mock\"}",
    apiKey,
    Arrays.asList("audio", "text", "cmd", "state"),
    "xiaoge-android-sdk-r5.2.2",
    "{}"
);
XiaogeClient client = new XiaogeClient(cfg, new XiaogeClient.Listener() {});
client.start();
client.sendFrontendState("hint", "awake", "speech", 1000);
client.sendPcm(frame);
```

When handling downlink `data.cmd` yourself, reply with the SDK status constants
instead of handwritten status strings:

```java
client.sendCmdAck(event, ProtocolCodec.CMD_ACK_STATUS_ACCEPTED, "ok", "queued");
client.sendCmdResult(event, ProtocolCodec.CMD_RESULT_STATUS_RUNNING, "ok", "started", false);
client.sendCmdResult(event, ProtocolCodec.CMD_RESULT_STATUS_SUCCEEDED, "ok", "completed", false);
```

Use `CMD_ACK_STATUS_REJECTED` when refusing a command, or
`CMD_RESULT_STATUS_FAILED` / `CMD_RESULT_STATUS_CANCELED` /
`CMD_RESULT_STATUS_TIMEOUT` for terminal execution results. Invalid status
strings are rejected locally with `IllegalArgumentException`.

For the cloud TLS certificate bundled in `:xiaoge-sdk-core` as
`res/raw/xiaoge_cloud_ca.pem`, construct the client with the CA-backed OkHttp
instance:

```java
XiaogeClient client = new XiaogeClient(
    cfg,
    new XiaogeClient.Listener() {},
    XiaogeTls.cloudClient(context)
);
```

`XiaogeConfig` sends `x-api-key` only on create_session HTTP when `apiKey` is
non-empty. WebSocket session authentication uses only
`Authorization: Bearer <access_token>`.

The demo app reads its create_session `x-api-key` from `XIAOGE_API_KEY` in
`clients/android/local.properties`; do not hard-code cloud keys in app source.
Example:

```properties
XIAOGE_API_KEY=your-cloud-api-key
```

Use `XiaogeVoiceSession` from an Android app to let the SDK manage
`AudioRecord`, `AudioTrack`, audio focus, reconnect, and high-level callbacks.
Use `XiaogeClient` directly only when the app wants to manage audio itself.

## Demo App

The `:demo` module is a minimal Android app that calls `:xiaoge-sdk-core`
directly:

- POST `create_session`.
- Open WSS `/ws/session` with `Authorization: Bearer`.
- Send `ctrl.hello` and one `ctrl.frontend_state`.
- Stream microphone PCM using `AudioRecord`.
- Play downlink TTS PCM using `AudioTrack`.
- Show `ctrl.state`, `data.stt`, `data.reply`, `data.cmd`, and `data.error` logs.

Build:

```bash
.\gradlew.bat :demo:assembleDebug
```

Install:

```bash
adb install -r demo/build/outputs/apk/debug/demo-debug.apk
```

For an emulator, use `http://10.0.2.2:<port>/create_session` to reach a fake
server on the host machine. The demo does not connect to real robot actions;
`data.cmd` is handled in `onCommand(CommandEvent)` and replies with explicit
ack/result frames through the event-based helpers.
