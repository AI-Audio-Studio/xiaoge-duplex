# Xiaoge C Client SDK R5.2.2

This SDK implements the R5.2.2 WSS session path with libwebsockets:

```text
create_session request JSON -> caller HTTP layer
session.created -> WSS /ws/session + Authorization: Bearer -> ctrl.hello
-> PCM + ctrl/data JSON -> fake cmd_ack/cmd_result
```

The SDK does not connect to real robot actions by default. `data.cmd` is
acknowledged and completed through the built-in fake executor behavior.
Set `cfg.auto_fake_cmd_executor = XIAOGE_AUTO_FAKE_CMD_DISABLED` when an
embedding application wants to execute commands itself and report real status.

## Build

```bash
cmake -S . -B build
cmake --build build
ctest --test-dir build --output-on-failure
```

Install `libwebsockets` with TLS support, `libcurl`, and `cmake` first. On
Linux, `libwebsockets-dev`, `libcurl4-openssl-dev`, and `cmake` are sufficient
on most distributions.

For Android cross builds, `ctest` does not execute the Android ELF on the host.
The `xiaoge_c_codec_test` target is registered through an adb wrapper:

```text
cmake -P clients/c/cmake/run_adb_test.cmake
```

At configure time CMake finds `adb` from `ANDROID_HOME`, `ANDROID_SDK_ROOT`, or
`PATH`, then `ctest` pushes the test binary to `/data/local/tmp`, marks it
executable, and runs it on the connected device.

## API

```c
xiaoge_config cfg = {
    "robot-x3-001",
    "{\"key_id\":\"dev\",\"signature\":\"mock\"}",
    "audio,text,cmd,state",
    "xiaoge-c-sdk-r5.2.2",
    "{}",
    XIAOGE_CLOUD_API_KEY environment variable
};
char body[2048];
xiaoge_build_create_session_request(&cfg, body, sizeof(body));
```

The SDK receives downlink `data.cmd` as a typed `xiaoge_command_event`. For a
real executor, disable the built-in fake executor and send command status
explicitly from `on_command` or your worker thread:

```c
static xiaoge_client *g_client;

static void on_command(const xiaoge_command_event *event, void *user) {
    (void)user;
    xiaoge_send_cmd_ack_event(g_client, event, XIAOGE_CMD_ACK_STATUS_ACCEPTED, "ok", "queued");
    xiaoge_send_cmd_result_event(g_client, event, XIAOGE_CMD_RESULT_STATUS_SUCCEEDED, "ok", "completed", 0);
}

xiaoge_config cfg = {
    "robot-x3-001",
    "{\"key_id\":\"dev\",\"signature\":\"mock\"}",
    "audio,text,cmd,state",
    "xiaoge-c-sdk-r5.2.2",
    "{}",
    NULL,
    XIAOGE_AUTO_FAKE_CMD_DISABLED
};
```

Use the `XIAOGE_CMD_ACK_STATUS_*` and `XIAOGE_CMD_RESULT_STATUS_*` macros
when sending command status. The event helper functions derive `trace_id`,
`session_id`, `utterance_id`, and `cmd_id` from the typed command event.

Typical command handling flow:

```c
static void on_command(const xiaoge_command_event *event, void *user) {
    (void)user;

    /* 1. Tell the cloud that this command was accepted and queued. */
    if (xiaoge_send_cmd_ack_event(g_client, event, XIAOGE_CMD_ACK_STATUS_ACCEPTED,
                                  "ok", "queued") != 0) {
        return;
    }

    if (run_robot_action(event->params_json) == 0) {
        /* 2. Terminal success result. */
        xiaoge_send_cmd_result_event(g_client, event, XIAOGE_CMD_RESULT_STATUS_SUCCEEDED,
                                     "ok", "completed", 0);
    } else {
        /* 2. Terminal failure result. */
        xiaoge_send_cmd_result_event(g_client, event, XIAOGE_CMD_RESULT_STATUS_FAILED,
                                     "action_failed", "robot action failed", 0);
    }
}
```

Reject a command at ack time when the app will not execute it, for example when
capability or parameters are unsupported:

```c
xiaoge_send_cmd_ack_event(g_client, event, XIAOGE_CMD_ACK_STATUS_REJECTED,
                          "capability_unsupported", "unsupported capability");
```

`xiaoge_send_cmd_ack_event` accepts `accepted`, `rejected`, or `duplicate`.
`xiaoge_send_cmd_result_event` accepts `running`, `succeeded`, `failed`,
`canceled`, or `timeout`. Lower-level raw JSON helpers remain available for
compatibility, but new command handlers should use the event helpers.

Submit `body` to the cloud fake server or test Gateway create_session endpoint.
Then pass the returned `session.created` fields to the SDK:

```c
xiaoge_session s = {
    trace_id,
    session_id,
    access_token,
    ws_url,              /* must be ws(s)://host[:port]/ws/session */
    "audio,text,cmd,state",
    config_version
};
xiaoge_client *c = xiaoge_create_from_session_with_ca(
    &cfg,
    &s,
    "../certs/cloud-ca.pem",
    0,
    &callbacks
);
xiaoge_send_frontend_state(c, "hint", "awake", "speech", 1000);
xiaoge_send_pcm(c, pcm, len);
while (xiaoge_service(c, 20) == 0) {}
xiaoge_destroy(c);
```

## Event callbacks

The C SDK exposes a single `xiaoge_callbacks` struct. Business callbacks receive
typed event structs, matching the Python SDK callback model. Raw downlink JSON is
available only through `on_json(const char *json, void *user)` for logging,
auditing, or inspecting fields that are not yet modeled.

Typed downlink events:

- `on_ready(const xiaoge_ready_event *)`: `sample_rate`, `granted_caps_csv`,
  `config_version`, IDs, `raw_json`.
- `on_clear(const xiaoge_clear_event *)`: optional `reason` and `utterance_id`,
  IDs, `raw_json`.
- `on_state(const xiaoge_state_event *)`: link/interaction/gate/resource state,
  optional `pending_confirmation_json`, IDs, `ts_ms`, `raw_json`.
- `on_stt(const xiaoge_stt_event *)`: `text`, `final`, `utterance_id`,
  `trace_id`, `session_id`, `ts_ms`, `raw_json`.
- `on_reply(const xiaoge_reply_event *)`: `text`, SDK-derived `final == 1`,
  `intent_type`, optional `speak_policy`, IDs, `ts_ms`, `raw_json`. R5.2.2
  `data.reply` has no protocol `final` field.
- `on_command(const xiaoge_command_event *)`: `cmd_id`, `capability_id`,
  `action`, `params_json`, `risk_level`, timeouts, IDs, `raw_json`.
- `on_error(const xiaoge_error_event *)`: server `data.error` fields `code`,
  `message`, `retryable`, `ts_ms`, IDs, `raw_json`.
- `on_failure(int code, const char *message, void *user)`: local/protocol
  validation failures before a typed business callback can be delivered.

All event pointers and all `const char *` fields are valid only until the
callback returns. Copy strings with `strdup` or application-owned storage before
handing an event to another thread. `raw_json` and `on_json` are for
logging/audit/debugging; normal command execution should use `on_command` and the
event helpers:

```c
static void on_command(const xiaoge_command_event *event, void *user) {
    xiaoge_client *client = user;
    xiaoge_send_cmd_ack_event(client, event, XIAOGE_CMD_ACK_STATUS_ACCEPTED,
                              "ok", "queued");
    xiaoge_send_cmd_result_event(client, event, XIAOGE_CMD_RESULT_STATUS_SUCCEEDED,
                                 "ok", "completed", 0);
}

xiaoge_callbacks callbacks = {
    .struct_size = sizeof(callbacks),
    .on_command = on_command,
    .on_json = log_raw_json,
    .user = client_or_app_state,
};
```

When `on_command` is registered and `cfg.auto_fake_cmd_executor` is left at
`XIAOGE_AUTO_FAKE_CMD_DEFAULT`, the built-in fake command executor is not run for
that command. Set `XIAOGE_AUTO_FAKE_CMD_ENABLED` only for explicit fake-executor
tests.

## Demo

```bash
./build/xiaoge_demo_file <create_session_url> <device_id> <credential-json-or-string> in.wav out.wav [--ca-cert ../certs/cloud-ca.pem]
```

The demo performs `create_session` internally through libcurl, then connects to
the returned `ws_url`.

Compatibility mode is still available when your test harness has already called
`create_session`:

```bash
./build/xiaoge_demo_file <ws_url> <access_token> <trace_id> <session_id> <device_id> in.wav out.wav [--ca-cert ../certs/cloud-ca.pem]
```

`in.wav` must be 16000 Hz, mono, signed 16-bit PCM.

`--insecure` remains available for isolated test environments only.
The C SDK sends only `Authorization: Bearer <access_token>` on WSS upgrade
requests. When an embedding HTTP layer performs create_session itself, it must
send the configured cloud key as the create_session `x-api-key` header.

## Local proxy

If a local firewall blocks the C demo process from connecting directly to the
remote gateway, run the local Python proxy and point the demo at `127.0.0.1`.
The demo connects to the proxy over local cleartext HTTP/WS; the proxy connects
upstream over HTTPS/WSS and rewrites the `create_session` response `ws_url` to
`ws://127.0.0.1:10097/ws/session`.

The proxy uses `aiohttp`. If your environment does not already have it, install
it into the environment you use to run project scripts:

```bash
uv run python -m pip install aiohttp
```

Start the proxy from the repository root:

```bash
uv run python clients/c/local_proxy.py
```

Then run the demo against the cloud `create_session` URL:

```bash
./clients/c/build/xiaoge_demo_file https://60.205.197.165:10099/create_session \
  robot-x3-001 '{"key_id":"dev","signature":"mock"}' in.wav out.wav --insecure
```

When running from `clients/c`, use `./build/xiaoge_demo_file` for the executable
path. Keep the proxy running while the demo is active. The local URL must use
`http://`; the websocket URL returned to the demo will use local `ws://`. The
proxy handles upstream `https://` and `wss://`.

Optional settings can be placed next to the script in `local_proxy_config.json`:

```json
{
  "server_host": "60.205.197.165",
  "server_port": 10097,
  "local_host": "127.0.0.1",
  "local_port": 10097,
  "verify_ssl": false
}
```

## Windows microphone demo

`xiaoge_demo_mic_win` is a Windows-only realtime microphone demo. It mirrors the
Python `demo_mic.py` flow: capture 16 kHz mono int16 microphone frames, send them
to the Xiaoge session, print downlink JSON, and play received PCM through the
system speaker unless playback is disabled.

Start the local proxy first:

```bash
python clients/c/local_proxy.py
```

Then run the microphone demo. With no positional arguments it defaults to the
cloud Gateway URL, device `robot-x3-001`, and the mock credential:

```bash
PATH="/d/msys64/mingw64/bin:$PATH" ./clients/c/build-mingw64/xiaoge_demo_mic_win.exe --seconds 20
```

When running from `clients/c`:

```bash
PATH="/d/msys64/mingw64/bin:$PATH" ./build-mingw64/xiaoge_demo_mic_win.exe --seconds 20
```

Explicit form:

```bash
./build-mingw64/xiaoge_demo_mic_win.exe https://60.205.197.165:10099/create_session \
  robot-x3-001 '{"key_id":"dev","signature":"mock"}' --seconds 20
```

Options:

- `--seconds N`: run for N seconds; `0` means run until Ctrl-C.
- `--no-playback`: keep streaming microphone audio but skip speaker playback.
- `--ca-cert path`, `--insecure`, `--api-key key`: same meaning as the file demo.

The demo uses Windows `waveIn`/`waveOut`, so Windows microphone permission and a
working default input device are required. In the current MSYS build environment,
adding `/d/msys64/mingw64/bin` to `PATH` may be required so runtime DLLs such as
`libuv-1.dll` can be found.
