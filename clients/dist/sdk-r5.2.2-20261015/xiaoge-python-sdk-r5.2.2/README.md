# Xiaoge Python Client SDK R5.2.2

中文客户调用说明见 `PYTHON_SDK_README.md`。

## Install

```bash
pip install -r requirements.txt
```

`sounddevice` is needed only by `demo_mic.py`.

## Self Test

```bash
python selftest.py
```

This starts a local create_session HTTP endpoint and mock `/ws/session` server.
Expected:

```text
records=local-selftest failures=0
```

## Contract Replay

```bash
python contract_replay.py --contract-dir <path-to-r5.2.2-02_contracts>
```

Expected:

```text
records=56 failures=0
```

## SDK Usage

```python
import os

from xiaoge_client import XiaogeClient, default_ssl_context

client = XiaogeClient(
    "https://60.205.197.165:10099/create_session",
    "robot-x3-001",
    {"key_id": "dev", "signature": "mock"},
    api_key=os.environ["XIAOGE_CLOUD_API_KEY"],  # sent as create_session x-api-key header
    ssl=default_ssl_context(),
    trace_log_path="client_trace.jsonl",
)
client.on_audio = speaker.write
client.on_clear = lambda event: speaker.flush()
client.on_json = lambda payload: print("downlink", payload["type"])
await client.run()
```

The SDK performs:

```text
create_session -> WSS /ws/session + Bearer -> ctrl.hello
```

Business callbacks receive typed event objects such as `SttEvent`, `ReplyEvent`,
and `CommandEvent`. Use `on_json(payload)` when you need raw downlink JSON for
logging, debugging, or protocol audits.

`data.cmd` is delivered to `on_command(event)`. The SDK does not auto-send
`data.cmd_ack` or `data.cmd_result`, and no real robot action is called.

### Command status constants

Use the SDK enums or constants when replying to downlink commands so status
values stay within the R5.2.2 protocol set:

```python
from xiaoge_client import CmdAckStatus, CmdResultStatus, ProtocolCodec

ack = ProtocolCodec.cmd_ack(command, CmdAckStatus.ACCEPTED, "ok")
result = ProtocolCodec.cmd_result(command, CmdResultStatus.SUCCEEDED, "ok")
```

Valid raw strings remain accepted for backwards compatibility, but invalid
`cmd_ack` or `cmd_result` status values raise `XiaogeProtocolError` before any
frame is sent.

## Demos

```bash
python demo_file.py <create_session_url> <device_id> <credential-json-or-string> in.wav out.wav [--ca-cert ../certs/cloud-ca.pem]
python demo_mic.py  <create_session_url> <device_id> <credential-json-or-string> [--ca-cert ../certs/cloud-ca.pem]
```

If `--ca-cert` is omitted, the demos use the bundled `../certs/cloud-ca.pem`
when present. `api_key` defaults to the `XIAOGE_CLOUD_API_KEY` environment
variable; pass `--api-key` to override the create_session `x-api-key` header.
`--insecure` remains test-only.

`in.wav` must be 16000 Hz, mono, signed 16-bit PCM.
