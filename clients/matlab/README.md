# Xiaoge MATLAB / Simulink Client R5.2.2

MATLAB uses the Python TCP bridge as the main path. The bridge performs
`create_session`, WSS Bearer auth, `ctrl.hello`, JSON event dispatch, and fake
command ack/result. MATLAB sends/receives PCM through local TCP.

## Start Bridge

```bash
python bridge/xiaoge_bridge.py <create_session_url> <device_id> <credential-json-or-string> --up 5001 --down 5002 --events 5003 --api-key <cloud-api-key> [--ca-cert ../certs/cloud-ca.pem]
```

If `--ca-cert` is omitted, the bridge uses the bundled cloud CA at
`../certs/cloud-ca.pem` when present. `--api-key` sets the create_session
`x-api-key` header; this key is not forwarded through local TCP or `ctrl.hello`.
`--insecure` remains test-only.

Ports:

| Port | Direction |
| --- | --- |
| 5001 | MATLAB -> bridge PCM |
| 5002 | bridge -> MATLAB TTS PCM |
| 5003 | bridge -> MATLAB JSONL events |

## File Demo

```matlab
addpath(pwd)
demo_file('127.0.0.1', 'in.wav', 'out.wav')
```

`in.wav` must be 16000 Hz, mono, signed 16-bit PCM.

## Programmatic Client

```matlab
c = xiaoge.Client('127.0.0.1', 5001, 5002, 5003);
c.OnEvent = @(ev) disp(ev.type);
c.sendPcm(int16Frame);
tts = c.readAudio(320);
events = c.readEvents();
```

## Simulink

```matlab
addpath(pwd)
build_xiaoge_demo
open_system('xiaoge_demo')
```

Validation requires a MATLAB/Simulink R2022b host. The repository environment
does not include MATLAB.
