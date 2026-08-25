# R5.2.2 data.cmd Sample Supplement

generated_at: 2026-08-05 16:45:40

## 1. Evidence Scope

This log supplements the cross-language e2e evidence with the jointly submitted cloud-side command sample. It records the expected R5.2.2 loop shape only:

```text
data.stt("往前走") -> data.cmd(motion.move/navigation.move/forward/50cm)
-> client data.cmd_ack -> client data.cmd_result
```

Evidence directory:

```text
E:\Project\Project2026\AIAudioCloudPlatform\xiaogeV2\xiaoge-duplex\xiaoge-duplex\clients\e2e\evidence\20260805_164437
```

## 2. Cloud Input

STT event:

```json
{
  "type": "data.stt",
  "trace_id": "trace-webpanel",
  "session_id": "sess-r522-1785919629099-82573d",
  "utterance_id": "utt-1785919637178",
  "text": "往前走",
  "final": false,
  "ts_ms": 1785919637178
}
```

Command event:

```json
{
  "type": "data.cmd",
  "trace_id": "trace-g3-1785919637216",
  "session_id": "119e452b",
  "utterance_id": "utt-g3-1785919637216",
  "cmd_id": "cmd-g3-387709",
  "capability_id": "motion.move",
  "action": "navigation.move",
  "params": {
    "direction": "forward",
    "distance_cm": 50,
    "distance_hint": "default_step"
  },
  "risk_level": "medium",
  "ack_timeout_ms": 800,
  "result_timeout_ms": 5000,
  "issued_at_ms": 1785919637216
}
```

## 3. Sample Acceptance Criteria

Scope note: this `cmd-g3-387709` record is a sample supplement. It is not evidence that the same `cmd_id` was replayed through real cloud delivery, client ack/result, and cloud-side archival in this e2e run. Closing that joint端云 item requires a later rerun that preserves the same `cmd_id` across cloud downlink, client uplink ack/result, and cloud archive logs.

The current clients treat this command as an accepted fake-executor command when the WSS session has granted `cmd` capability:

| Field | Expected value | Client handling |
| --- | --- | --- |
| `type` | `data.cmd` | Recognized as command frame. |
| `capability_id` | `motion.move` | Supported capability. |
| `action` | `navigation.move` | Supported action. |
| `params.direction` | `forward` | Supported direction. |
| `params.distance_cm` | `50` | Valid positive distance. |
| `ack_timeout_ms` | `800` | Within timeout semantics for immediate ack. |
| `result_timeout_ms` | `5000` | Within timeout semantics for fake completion. |

Expected client responses for this sample shape:

```json
{"type":"data.cmd_ack","cmd_id":"cmd-g3-387709","status":"accepted"}
```

```json
{"type":"data.cmd_result","cmd_id":"cmd-g3-387709","status":"running"}
```

```json
{"type":"data.cmd_result","cmd_id":"cmd-g3-387709","status":"succeeded"}
```

## 4. Cross-Language Closure Evidence

The e2e run used the R5.2.2 fake Gateway and the shared test wav:

```text
fake Gateway: http://127.0.0.1:18082/create_session
wav: E:\Project\Project2026\AIAudioCloudPlatform\xiaogeV2\xiaoge-duplex\xiaoge-duplex\tests\test_realtime\hello_world.wav
```

Result summary:

| Target | ExitCode | Evidence |
| --- | ---: | --- |
| C | 0 | `c.log`, `c\trace.jsonl` |
| Python | 0 | `python.log`, `python\trace.jsonl` |
| Android | 0 | `android.log` |
| MATLAB bridge | 0 | `matlab_bridge.log`, `matlab_bridge\trace.jsonl` |

Closure note:

- Python/C/Android/MATLAB-bridge all connected through `/ws/session`.
- All targets sent the shared wav input and completed without client-side protocol failures.
- Command execution remained inside the G3 client fake-executor boundary; no real robot motion was triggered.
- The submitted `data.cmd` shape is captured as a sample supplement for review. It must not be described as a completed same-`cmd_id` cloud archive closure until a future rerun records `cmd-g3-387709` or another agreed `cmd_id` end to end.
