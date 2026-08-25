# Xiaoge Duplex Client Protocol R5.2.2

Contract version: `xiaoge-duplex-protocol-r5.2.2`

Manifest SHA256:

```text
845F0F4125061FF37A7F4DA20E0C88BC089200A08B319F1035D6522C80B56559
```

## Main Path

```text
HTTPS create_session
  -> x-api-key header
  -> session.created(trace_id, session_id, access_token, expires_in_ms, ws_url, granted_caps, config_snapshot)
WSS /ws/session
  -> Authorization: Bearer <access_token>
  -> ctrl.hello
  -> ctrl.ready / ctrl.state / ctrl.clear / data.* + binary PCM
  -> data.cmd_ack / data.cmd_result / data.error
```

The API key and token are carried only by transport headers. `ctrl.hello` must
not contain token or API-key fields.

## Audio

| Field | Value |
| --- | --- |
| Sample rate | 16000 Hz |
| Channels | mono |
| Sample format | signed 16-bit little-endian |
| Container | raw PCM over WebSocket binary frames |
| Binary frame max | 32768 bytes |

Recommended upstream frame size is 10-20 ms: 320-640 bytes.

## JSON Transport

JSON text frames are compact UTF-8 bytes. `8192` bytes pass; `8193` bytes fail.
Clients must not add fields, enums, delivery values, error codes, or close codes
outside the frozen R5.2.2 contract.

## Client Responsibilities

- Build `create_session` request with `device_id`, `credential`, `caps`,
  `audio_format={16000,1,int16le}`, and `client_version`.
- Send `x-api-key` only on create_session HTTP when explicitly configured; WebSocket session authentication uses only `Authorization: Bearer <access_token>`.
- Connect to `ws_url` using `Authorization: Bearer <access_token>`.
- Send `ctrl.hello` with `proto=2`, `role=device`, `device_id`, and granted caps.
- Send optional `ctrl.frontend_state` with monotonic `seq`, `ttl_ms`, and
  `trust_level` in `authoritative/hint/observe`.
- Send PCM as binary frames and handle downlink TTS PCM.
- Handle `ctrl.clear` by clearing local playback buffers.
- Consume only `data.cmd` and `data.cmd after confirmation` as executable
  deliveries. Before high-risk confirmation, cancel, or timeout, no command
  reaches the executor.
- Return `data.cmd_ack` status `accepted/rejected/duplicate`; unknown `cmd_id`
  uses `data.error` or audit, never `data.cmd_ack.status=unknown`.

## Error And Close Handling

Clients log and recover according to R5.2.2 close-code cases:

| Case | Client action |
| --- | --- |
| HTTP 401 create_session | fix credential or reprovision |
| WSS 4401 token/auth | create_session then reconnect |
| HTTP/WSS 403 permission | show permission denied |
| WSS 4400 protocol_error | log trace and reconnect if closed |
| WSS 4009 duplicate_connection | keep or reconnect by policy |
| HTTP 503/resource_exhausted | backoff retry |
| runtime `data.error=busy/resource_exhausted` or WSS 1013 | show busy and backoff |

Legacy close code `4001` is not part of R5.2.2.

## Historical Reference

Old `/ws/audio` clients were a pre-R5.2.2 audio-only reference. They are not the
current client main path and must not be used for new SDK demos, replay, or
forward-compatibility work.
