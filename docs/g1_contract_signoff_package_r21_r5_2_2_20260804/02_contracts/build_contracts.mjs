import crypto from "node:crypto";
import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const scriptPath = fileURLToPath(import.meta.url);
const contractsDir = path.dirname(scriptPath);
const repoRoot = path.resolve(contractsDir, "../../../..");

const version = "xiaoge-duplex-protocol-r5.2.2";
const generatedAt = new Date().toISOString();

const files = {
  protocolSchema: "xiaoge-duplex-protocol-r5.2.2.schema.json",
  examples: "xiaoge-duplex-protocol-r5.2.2.examples.jsonl",
  closeCodes: "xiaoge-duplex-protocol-r5.2.2.close-codes.jsonl",
  registrySchema: "xiaoge-duplex-voicecmd-registry-r5.2.2.schema.json",
  sourceCheck: "xiaoge-duplex-protocol-r5.2.2.source-check.json",
  manifest: "xiaoge-duplex-protocol-r5.2.2.manifest.json",
  signoff: "xiaoge-duplex-protocol-r5.2.2.signoff.md",
};

const sourcePaths = {
  workbook: path.join(
    repoRoot,
    "outputs/xiaoge_full_duplex_20260731/xiaoge_full_duplex_requirements_design_20260731_r5_2_2_review.xlsx",
  ),
  workbookInspect: path.join(
    repoRoot,
    "outputs/xiaoge_full_duplex_20260731/xiaoge_full_duplex_requirements_design_20260731_r5_2_2_review.xlsx.inspect.ndjson",
  ),
  protocolV2: path.join(repoRoot, "docs/design/protocol-v2/PROTOCOL_V2_DESIGN.md"),
  voiceCmd: path.join(repoRoot, "docs/design/voice-cmd/VOICE_CMD_DESIGN.md"),
  generator: scriptPath,
};

const rel = (absPath) => path.relative(repoRoot, absPath).replaceAll("\\", "/");
const out = (name) => path.join(contractsDir, name);

const nonEmptyString = { type: "string", minLength: 1 };
const idString = { type: "string", minLength: 1 };
const epochMs = { type: "integer", minimum: 0 };
const openObject = { type: "object", additionalProperties: true };
const stringArray = { type: "array", items: nonEmptyString, minItems: 1 };
const capValues = ["audio", "text", "cmd", "state"];
const capArray = { type: "array", items: { enum: capValues }, minItems: 1, uniqueItems: true };
const ownerRoles = {
  clients: {
    name: "童紫薇",
    scope: "clients SDK/GUI/fake SDK/fake executor",
  },
  cloud: {
    name: "王明辉",
    scope: "Gateway/Auth/sessproto/Agent/voice-cmd/fake server/cloud replay",
  },
  protocol: {
    name: "陈强",
    scope: "Manifest hash, field, enum, error code, close code, and no-legacy dispute decisions",
  },
};
const clientExecutableDeliveries = ["data.cmd", "data.cmd after confirmation"];
const cloudReplyOnlyDeliveries = ["cloud_tool + data.reply", "cloud_knowledge + data.reply", "ask_split only"];

const strictObject = (required, properties) => ({
  type: "object",
  required,
  properties,
  additionalProperties: false,
});

const refs = [
  "createSessionRequest",
  "createSessionResponse",
  "wssAuth",
  "ctrlHello",
  "ctrlReady",
  "ctrlState",
  "ctrlFrontendState",
  "ctrlClear",
  "dataStt",
  "dataReply",
  "dataCmd",
  "dataCmdAck",
  "dataCmdResult",
  "dataError",
];

const protocolSchema = {
  $schema: "https://json-schema.org/draft/2020-12/schema",
  $id: "urn:xiaoge:duplex:protocol:r5.2.2",
  title: "Xiaoge Duplex Protocol R5.2.2 Contract",
  description:
    "Machine-checkable G1 contract manually curated from R5.2.2 workbook, protocol-v2 v1.9, and voice-cmd v4.5, then machine-reconciled against the workbook inspect tables. P1 controls ctrl.status/get/set/ack are intentionally excluded from the P0 schema. Legacy clients and legacy close codes are out of scope.",
  "x-contract-version": version,
  "x-frame-json-max-bytes": 8192,
  "x-frame-binary-max-bytes": 32768,
  oneOf: refs.map((name) => ({ $ref: `#/$defs/${name}` })),
  $defs: {
    createSessionRequest: strictObject(
      ["device_id", "credential", "caps", "audio_format", "client_version"],
      {
        device_id: idString,
        credential: { oneOf: [openObject, nonEmptyString] },
        caps: capArray,
        prefs: openObject,
        audio_format: strictObject(["sample_rate", "channels", "sample_format"], {
          sample_rate: { const: 16000 },
          channels: { const: 1 },
          sample_format: { const: "int16le" },
        }),
        client_version: nonEmptyString,
      },
    ),
    createSessionResponse: strictObject(
      [
        "type",
        "trace_id",
        "session_id",
        "access_token",
        "expires_in_ms",
        "ws_url",
        "granted_caps",
        "config_snapshot",
      ],
      {
        type: { const: "session.created" },
        trace_id: idString,
        session_id: idString,
        access_token: nonEmptyString,
        expires_in_ms: { type: "integer", minimum: 1 },
        ws_url: nonEmptyString,
        granted_caps: capArray,
        config_snapshot: {
          type: "object",
          required: ["config_version"],
          properties: { config_version: nonEmptyString },
          additionalProperties: true,
        },
      },
    ),
    wssAuth: strictObject(["method", "path", "headers"], {
      method: { const: "GET" },
      path: { const: "/ws/session" },
      headers: strictObject(["Authorization"], {
        Authorization: { type: "string", pattern: "^Bearer .+" },
      }),
    }),
    ctrlHello: strictObject(
      ["type", "trace_id", "session_id", "proto", "role", "device_id", "caps"],
      {
        type: { const: "ctrl.hello" },
        trace_id: idString,
        session_id: idString,
        proto: { const: 2 },
        role: { enum: ["device", "panel"] },
        device_id: idString,
        caps: capArray,
        prefs: openObject,
      },
    ),
    ctrlReady: strictObject(
      ["type", "trace_id", "session_id", "sample_rate", "granted_caps", "config_version"],
      {
        type: { const: "ctrl.ready" },
        trace_id: idString,
        session_id: idString,
        sample_rate: { const: 16000 },
        granted_caps: capArray,
        config_version: nonEmptyString,
      },
    ),
    ctrlState: strictObject(
      [
        "type",
        "trace_id",
        "session_id",
        "link_state",
        "interaction_mode",
        "engine_gate",
        "resource_state",
        "ts_ms",
      ],
      {
        type: { const: "ctrl.state" },
        trace_id: idString,
        session_id: idString,
        link_state: { enum: ["connecting", "connected", "reconnecting", "closed"] },
        interaction_mode: { enum: ["sleeping", "dialogue", "listening"] },
        engine_gate: { enum: ["closed", "open", "kws_only"] },
        resource_state: {
          enum: ["SleepingHot", "SleepingWarm", "ActiveAgent", "ReleasedIdle", "PendingReconnect"],
        },
        ts_ms: epochMs,
        pending_confirmation: openObject,
      },
    ),
    ctrlFrontendState: strictObject(["type", "trace_id", "session_id", "seq", "ts_ms", "ttl_ms", "trust_level"], {
      type: { const: "ctrl.frontend_state" },
      trace_id: idString,
      session_id: idString,
      seq: { type: "integer", minimum: 0 },
      ts_ms: epochMs,
      ttl_ms: { type: "integer", minimum: 1 },
      trust_level: { enum: ["observe", "hint", "authoritative"] },
      wake_event: { enum: ["local_kws", "button", "gui", "none"] },
      wake_state: { enum: ["sleeping", "awake", "unknown"] },
      vad: { enum: ["silence", "speech", "unknown"] },
      doa: { type: "number" },
      lock_mode: { type: "boolean" },
    }),
    ctrlClear: strictObject(["type", "trace_id", "session_id"], {
      type: { const: "ctrl.clear" },
      trace_id: idString,
      session_id: idString,
      utterance_id: idString,
      reason: { enum: ["barge_in", "user_stop", "system_cancel", "sleep"] },
    }),
    dataStt: strictObject(["type", "trace_id", "session_id", "utterance_id", "text", "final", "ts_ms"], {
      type: { const: "data.stt" },
      trace_id: idString,
      session_id: idString,
      utterance_id: idString,
      text: nonEmptyString,
      final: { type: "boolean" },
      ts_ms: epochMs,
    }),
    dataReply: strictObject(["type", "trace_id", "session_id", "utterance_id", "intent_type", "text", "ts_ms"], {
      type: { const: "data.reply" },
      trace_id: idString,
      session_id: idString,
      utterance_id: idString,
      intent_type: { enum: ["control_cmd", "info_query", "knowledge_qa", "chat", "config", "system"] },
      text: nonEmptyString,
      ts_ms: epochMs,
      speak_policy: { enum: ["silent", "ack", "ack_then_result", "final_only"] },
    }),
    dataCmd: strictObject(
      [
        "type",
        "trace_id",
        "session_id",
        "utterance_id",
        "cmd_id",
        "capability_id",
        "action",
        "params",
        "risk_level",
        "ack_timeout_ms",
        "result_timeout_ms",
        "issued_at_ms",
      ],
      {
        type: { const: "data.cmd" },
        trace_id: idString,
        session_id: idString,
        utterance_id: idString,
        cmd_id: idString,
        capability_id: idString,
        action: idString,
        params: openObject,
        risk_level: { enum: ["low", "medium", "high"] },
        ack_timeout_ms: { type: "integer", minimum: 1 },
        result_timeout_ms: { type: "integer", minimum: 1 },
        issued_at_ms: epochMs,
      },
    ),
    dataCmdAck: strictObject(
      ["type", "trace_id", "session_id", "utterance_id", "cmd_id", "status", "code", "received_at_ms"],
      {
        type: { const: "data.cmd_ack" },
        trace_id: idString,
        session_id: idString,
        utterance_id: idString,
        cmd_id: idString,
        status: { enum: ["accepted", "rejected", "duplicate"] },
        code: nonEmptyString,
        message: { type: "string" },
        received_at_ms: epochMs,
      },
    ),
    dataCmdResult: strictObject(
      ["type", "trace_id", "session_id", "utterance_id", "cmd_id", "status", "code"],
      {
        type: { const: "data.cmd_result" },
        trace_id: idString,
        session_id: idString,
        utterance_id: idString,
        cmd_id: idString,
        status: { enum: ["running", "succeeded", "failed", "canceled", "timeout"] },
        code: nonEmptyString,
        message: { type: "string" },
        started_at_ms: epochMs,
        finished_at_ms: epochMs,
        duration_ms: { type: "integer", minimum: 0 },
        retryable: { type: "boolean" },
      },
    ),
    dataError: strictObject(["type", "trace_id", "session_id", "code", "message", "retryable", "ts_ms"], {
      type: { const: "data.error" },
      trace_id: idString,
      session_id: idString,
      code: {
        enum: [
          "auth_failed",
          "permission_denied",
          "busy",
          "protocol_error",
          "capability_unsupported",
          "token_expired",
          "duplicate_connection",
          "resource_exhausted",
          "unknown_cmd_id",
        ],
      },
      message: nonEmptyString,
      retryable: { type: "boolean" },
      ts_ms: epochMs,
    }),
  },
};

protocolSchema.$defs.p0Message = {
  oneOf: [
    "ctrlHello",
    "ctrlReady",
    "ctrlState",
    "ctrlFrontendState",
    "ctrlClear",
    "dataStt",
    "dataReply",
    "dataCmd",
    "dataCmdAck",
    "dataCmdResult",
    "dataError",
  ].map((name) => ({ $ref: `#/$defs/${name}` })),
};

const examples = [
  {
    id: "create_session.request.valid",
    kind: "positive",
    schema_ref: "#/$defs/createSessionRequest",
    payload: {
      device_id: "robot-x3-001",
      credential: { key_id: "dev-key", signature: "hmac-signature" },
      caps: ["audio", "text", "cmd", "state"],
      prefs: { "welcome.enabled": true },
      audio_format: { sample_rate: 16000, channels: 1, sample_format: "int16le" },
      client_version: "x3-sdk-r5.2.2",
    },
    expect: { schema: "pass" },
  },
  {
    id: "create_session.response.valid",
    kind: "positive",
    schema_ref: "#/$defs/createSessionResponse",
    payload: {
      type: "session.created",
      trace_id: "trace-20260731-0001",
      session_id: "sess-0001",
      access_token: "jwt-short-lived",
      expires_in_ms: 600000,
      ws_url: "wss://host/ws/session",
      granted_caps: ["audio", "text", "cmd", "state"],
      config_snapshot: { config_version: "cfg-001" },
    },
    expect: { schema: "pass" },
  },
  {
    id: "wss.auth.valid",
    kind: "positive",
    schema_ref: "#/$defs/wssAuth",
    payload: { method: "GET", path: "/ws/session", headers: { Authorization: "Bearer jwt-short-lived" } },
    expect: { schema: "pass" },
  },
  {
    id: "ctrl.hello.valid",
    kind: "positive",
    schema_ref: "#/$defs/ctrlHello",
    payload: {
      type: "ctrl.hello",
      trace_id: "trace-20260731-0001",
      session_id: "sess-0001",
      proto: 2,
      role: "device",
      device_id: "robot-x3-001",
      caps: ["audio", "text", "cmd", "state"],
      prefs: { "welcome.enabled": true },
    },
    expect: { schema: "pass" },
  },
  {
    id: "ctrl.ready.valid",
    kind: "positive",
    schema_ref: "#/$defs/ctrlReady",
    payload: {
      type: "ctrl.ready",
      trace_id: "trace-20260731-0001",
      session_id: "sess-0001",
      sample_rate: 16000,
      granted_caps: ["audio", "text", "cmd", "state"],
      config_version: "cfg-001",
    },
    expect: { schema: "pass" },
  },
  {
    id: "ctrl.state.valid",
    kind: "positive",
    schema_ref: "#/$defs/ctrlState",
    payload: {
      type: "ctrl.state",
      trace_id: "trace-20260731-0001",
      session_id: "sess-0001",
      link_state: "connected",
      interaction_mode: "sleeping",
      engine_gate: "closed",
      resource_state: "SleepingHot",
      ts_ms: 1789000000123,
    },
    expect: { schema: "pass" },
  },
  {
    id: "ctrl.frontend_state.valid",
    kind: "positive",
    schema_ref: "#/$defs/ctrlFrontendState",
    payload: {
      type: "ctrl.frontend_state",
      trace_id: "trace-20260731-0001",
      session_id: "sess-0001",
      seq: 17,
      ts_ms: 1789000000456,
      ttl_ms: 1000,
      trust_level: "authoritative",
      wake_event: "local_kws",
      wake_state: "awake",
      vad: "speech",
      doa: 15,
      lock_mode: false,
    },
    expect: { schema: "pass" },
  },
  {
    id: "ctrl.clear.valid",
    kind: "positive",
    schema_ref: "#/$defs/ctrlClear",
    payload: {
      type: "ctrl.clear",
      trace_id: "trace-20260731-0001",
      session_id: "sess-0001",
      utterance_id: "utt-0002",
      reason: "barge_in",
    },
    expect: { schema: "pass" },
  },
  {
    id: "data.stt.valid",
    kind: "positive",
    schema_ref: "#/$defs/dataStt",
    payload: {
      type: "data.stt",
      trace_id: "trace-20260731-0001",
      session_id: "sess-0001",
      utterance_id: "utt-0001",
      text: "往前走一米",
      final: true,
      ts_ms: 1789000000100,
    },
    expect: { schema: "pass" },
  },
  {
    id: "data.reply.valid",
    kind: "positive",
    schema_ref: "#/$defs/dataReply",
    payload: {
      type: "data.reply",
      trace_id: "trace-20260731-0001",
      session_id: "sess-0001",
      utterance_id: "utt-0001",
      intent_type: "control_cmd",
      text: "好的，正在执行。",
      ts_ms: 1789000000700,
      speak_policy: "ack_then_result",
    },
    expect: { schema: "pass" },
  },
  {
    id: "data.reply.multi_command_blocked.ask_split",
    kind: "positive",
    schema_ref: "#/$defs/dataReply",
    payload: {
      type: "data.reply",
      trace_id: "trace-20260803-multi-0001",
      session_id: "sess-0001",
      utterance_id: "utt-multi-0001",
      intent_type: "control_cmd",
      text: "我听到了两个操作：往前走一米、挥手。请拆成两句，或告诉我先执行哪一个。",
      ts_ms: 1789000000800,
      speak_policy: "ack",
    },
    context: {
      utterance_text: "往前走一米再挥手",
      intent_type: "control_cmd_multi",
      state: "multi_command_blocked",
      reply_style: "ask_split",
      detected_actions: ["navigation.move", "gesture.perform"],
      source_seed: "SEED-017",
      requirements: ["FR-CMD-003"],
      forbidden_outputs: ["data.cmd", "cmd_id", "executor_side_effect"],
    },
    expect: {
      schema: "pass",
      semantic: "pass",
      contract: "multi_command_blocked_ask_split_only",
      output_types: ["data.reply"],
      forbidden_types: ["data.cmd"],
      no_cmd_id: true,
      no_side_effects: true,
      reason: "P0 multi-command detection returns ask_split only and generates no data.cmd.",
    },
  },
  {
    id: "data.cmd.valid",
    kind: "positive",
    schema_ref: "#/$defs/dataCmd",
    payload: {
      type: "data.cmd",
      trace_id: "trace-20260731-0001",
      session_id: "sess-0001",
      utterance_id: "utt-0001",
      cmd_id: "cmd-0001",
      capability_id: "motion.move",
      action: "navigation.move",
      params: { direction: "forward", distance_cm: 100 },
      risk_level: "medium",
      ack_timeout_ms: 800,
      result_timeout_ms: 5000,
      issued_at_ms: 1789000001000,
    },
    expect: { schema: "pass" },
  },
  {
    id: "data.cmd_ack.valid",
    kind: "positive",
    schema_ref: "#/$defs/dataCmdAck",
    payload: {
      type: "data.cmd_ack",
      trace_id: "trace-20260731-0001",
      session_id: "sess-0001",
      utterance_id: "utt-0001",
      cmd_id: "cmd-0001",
      status: "accepted",
      code: "sdk_received",
      message: "accepted by SDK",
      received_at_ms: 1789000001120,
    },
    expect: { schema: "pass" },
  },
  {
    id: "data.cmd_result.running.valid",
    kind: "positive",
    schema_ref: "#/$defs/dataCmdResult",
    payload: {
      type: "data.cmd_result",
      trace_id: "trace-20260731-0001",
      session_id: "sess-0001",
      utterance_id: "utt-0001",
      cmd_id: "cmd-0001",
      status: "running",
      code: "executor_started",
      message: "executing",
      started_at_ms: 1789000001200,
    },
    expect: { schema: "pass" },
  },
  {
    id: "data.cmd_result.succeeded.valid",
    kind: "positive",
    schema_ref: "#/$defs/dataCmdResult",
    payload: {
      type: "data.cmd_result",
      trace_id: "trace-20260731-0001",
      session_id: "sess-0001",
      utterance_id: "utt-0001",
      cmd_id: "cmd-0001",
      status: "succeeded",
      code: "done",
      message: "completed",
      started_at_ms: 1789000001200,
      finished_at_ms: 1789000002400,
      duration_ms: 1200,
    },
    expect: { schema: "pass" },
  },
  {
    id: "data.error.unknown_cmd_id.valid",
    kind: "positive",
    schema_ref: "#/$defs/dataError",
    payload: {
      type: "data.error",
      trace_id: "trace-20260731-0001",
      session_id: "sess-0001",
      code: "unknown_cmd_id",
      message: "ack/result references unknown cmd_id",
      retryable: false,
      ts_ms: 1789000003000,
    },
    expect: { schema: "pass" },
  },
  {
    id: "create_session.request.missing_client_version",
    kind: "negative",
    schema_ref: "#/$defs/createSessionRequest",
    payload: {
      device_id: "robot-x3-001",
      credential: { key_id: "dev-key", signature: "hmac-signature" },
      caps: ["audio", "text", "cmd", "state"],
      audio_format: { sample_rate: 16000, channels: 1, sample_format: "int16le" },
    },
    expect: { schema: "fail", reason: "client_version is required by R5.2.2 create_session.request" },
  },
  {
    id: "create_session.response.missing_config_snapshot",
    kind: "negative",
    schema_ref: "#/$defs/createSessionResponse",
    payload: {
      type: "session.created",
      trace_id: "trace-20260731-0001",
      session_id: "sess-0001",
      access_token: "jwt-short-lived",
      expires_in_ms: 600000,
      ws_url: "wss://host/ws/session",
      granted_caps: ["audio", "text", "cmd", "state"],
    },
    expect: { schema: "fail", reason: "config_snapshot is required by R5.2.2 create_session.response" },
  },
  {
    id: "ctrl.hello.with_token",
    kind: "negative",
    schema_ref: "#/$defs/ctrlHello",
    payload: {
      type: "ctrl.hello",
      trace_id: "trace-20260731-0001",
      session_id: "sess-0001",
      proto: 2,
      role: "device",
      device_id: "robot-x3-001",
      caps: ["audio", "text", "cmd", "state"],
      token: "jwt-short-lived",
    },
    expect: { schema: "fail", reason: "ctrl.hello must not carry token; WSS Authorization is the only token carrier" },
  },
  {
    id: "ctrl.hello.invalid_role",
    kind: "negative",
    schema_ref: "#/$defs/ctrlHello",
    payload: {
      type: "ctrl.hello",
      trace_id: "trace-20260731-0001",
      session_id: "sess-0001",
      proto: 2,
      role: "admin",
      device_id: "robot-x3-001",
      caps: ["audio", "text", "cmd", "state"],
    },
    expect: { schema: "fail", reason: "role enum is device/panel" },
  },
  {
    id: "ctrl.hello.unknown_cap",
    kind: "negative",
    schema_ref: "#/$defs/ctrlHello",
    payload: {
      type: "ctrl.hello",
      trace_id: "trace-20260731-0001",
      session_id: "sess-0001",
      proto: 2,
      role: "device",
      device_id: "robot-x3-001",
      caps: ["audio", "vision"],
    },
    expect: { schema: "fail", reason: "R5.2.2 caps enum is audio/text/cmd/state; unknown caps are protocol_error" },
  },
  {
    id: "ctrl.hello.duplicate_caps",
    kind: "negative",
    schema_ref: "#/$defs/ctrlHello",
    payload: {
      type: "ctrl.hello",
      trace_id: "trace-20260731-0001",
      session_id: "sess-0001",
      proto: 2,
      role: "device",
      device_id: "robot-x3-001",
      caps: ["audio", "audio"],
    },
    expect: { schema: "fail", reason: "R5.2.2 caps must be unique" },
  },
  {
    id: "ctrl.status.not_p0",
    kind: "negative",
    schema_ref: "#/$defs/p0Message",
    payload: { type: "ctrl.status", seq: 17, fields: { battery: 82 } },
    expect: { schema: "fail", reason: "ctrl.status is P1/non-P0 and is intentionally excluded from R5.2.2 P0 schema" },
  },
  {
    id: "ctrl.set.not_p0",
    kind: "negative",
    schema_ref: "#/$defs/p0Message",
    payload: { type: "ctrl.set", req_id: "r-3", set: { "cmd.ack": "off" } },
    expect: { schema: "fail", reason: "ctrl.get/set/ack are P1/non-P0 in R5.2.2" },
  },
  {
    id: "ctrl.clear.invalid_reason",
    kind: "negative",
    schema_ref: "#/$defs/ctrlClear",
    payload: {
      type: "ctrl.clear",
      trace_id: "trace-20260731-0001",
      session_id: "sess-0001",
      utterance_id: "utt-0002",
      reason: "interrupt",
    },
    expect: { schema: "fail", reason: "reason enum is barge_in/user_stop/system_cancel/sleep" },
  },
  {
    id: "data.cmd.missing_cmd_id",
    kind: "negative",
    schema_ref: "#/$defs/dataCmd",
    payload: {
      type: "data.cmd",
      trace_id: "trace-20260731-0001",
      session_id: "sess-0001",
      utterance_id: "utt-0001",
      capability_id: "motion.move",
      action: "navigation.move",
      params: { direction: "forward" },
      risk_level: "medium",
      ack_timeout_ms: 800,
      result_timeout_ms: 5000,
      issued_at_ms: 1789000001000,
    },
    expect: { schema: "fail", reason: "cmd_id is required and generated by cloud" },
  },
  {
    id: "data.cmd_ack.invalid_status_unknown",
    kind: "negative",
    schema_ref: "#/$defs/dataCmdAck",
    payload: {
      type: "data.cmd_ack",
      trace_id: "trace-20260731-0001",
      session_id: "sess-0001",
      utterance_id: "utt-0001",
      cmd_id: "cmd-404",
      status: "unknown",
      code: "unknown_cmd_id",
      received_at_ms: 1789000001120,
    },
    expect: { schema: "fail", reason: "data.cmd_ack.status enum excludes unknown; unknown cmd_id uses data.error/audit" },
  },
  {
    id: "data.error.capability_missing_removed",
    kind: "negative",
    schema_ref: "#/$defs/dataError",
    payload: {
      type: "data.error",
      trace_id: "trace-20260731-0001",
      session_id: "sess-0001",
      code: "capability_missing",
      message: "removed enum",
      retryable: false,
      ts_ms: 1789000003000,
    },
    expect: { schema: "fail", reason: "capability_unsupported replaces removed capability_missing" },
  },
  {
    id: "data.cmd_ack.unknown_cmd_id.semantic",
    kind: "negative",
    schema_ref: "#/$defs/dataCmdAck",
    payload: {
      type: "data.cmd_ack",
      trace_id: "trace-20260731-0001",
      session_id: "sess-0001",
      utterance_id: "utt-0001",
      cmd_id: "cmd-404",
      status: "accepted",
      code: "sdk_received",
      received_at_ms: 1789000001120,
    },
    context: { known_cmd_ids: ["cmd-0001"] },
    expect: { schema: "pass", semantic: "fail", reason: "unknown cmd_id must be handled as data.error/audit, not accepted ack" },
  },
  {
    id: "data.cmd_ack.duplicate_cmd_id.semantic",
    kind: "negative",
    schema_ref: "#/$defs/dataCmdAck",
    payload: {
      type: "data.cmd_ack",
      trace_id: "trace-20260731-0001",
      session_id: "sess-0001",
      utterance_id: "utt-0001",
      cmd_id: "cmd-0001",
      status: "accepted",
      code: "sdk_received",
      received_at_ms: 1789000001220,
    },
    context: { seen_ack_cmd_ids: ["cmd-0001"] },
    expect: { schema: "pass", semantic: "fail", reason: "duplicate ack/result must be deduped or audited by contract tests" },
  },
  {
    id: "data.reply.max_json_transport",
    kind: "positive",
    schema_ref: "#/$defs/dataReply",
    payload: {
      type: "data.reply",
      trace_id: "trace-20260731-0001",
      session_id: "sess-0001",
      utterance_id: "utt-0001",
      intent_type: "chat",
      text: "placeholder",
      ts_ms: 1789000000700,
    },
    context: { serialized_bytes: 8192, frame_json_max_bytes: 8192 },
    expect: { schema: "pass", transport: "pass", reason: "WSS JSON text frame at 8KB limit is accepted" },
  },
  {
    id: "data.reply.too_large_json.transport",
    kind: "negative",
    schema_ref: "#/$defs/dataReply",
    payload: {
      type: "data.reply",
      trace_id: "trace-20260731-0001",
      session_id: "sess-0001",
      utterance_id: "utt-0001",
      intent_type: "chat",
      text: "placeholder",
      ts_ms: 1789000000700,
    },
    context: { serialized_bytes: 8193, frame_json_max_bytes: 8192 },
    expect: { schema: "pass", transport: "fail", reason: "WSS JSON text frame exceeds 8KB limit" },
  },
];

const closeCodeCases = [
  {
    id: "https.create_session.auth_failed",
    kind: "negative",
    source_code: "auth_failed",
    route: "HTTPS",
    trigger: "invalid device credential, invalid signature, or unknown device in create_session",
    expect: {
      http_status: 401,
      ws_close_code: null,
      data_error_code: null,
      agent_allocation: "none",
      client_action: "fix_credential_or_reprovision",
    },
  },
  {
    id: "wss.auth.no_token",
    kind: "negative",
    source_code: "auth_failed",
    route: "WSS_AUTH",
    trigger: "missing Authorization bearer token",
    expect: {
      http_status: null,
      ws_close_code: 4401,
      data_error_code: null,
      agent_allocation: "none",
      client_action: "create_session_then_reconnect",
    },
  },
  {
    id: "wss.auth.invalid_token",
    kind: "negative",
    source_code: "auth_failed",
    route: "WSS_AUTH",
    trigger: "invalid bearer token signature or unknown device",
    expect: {
      http_status: null,
      ws_close_code: 4401,
      data_error_code: null,
      agent_allocation: "none",
      client_action: "create_session_then_reconnect",
    },
  },
  {
    id: "wss.auth.expired_token",
    kind: "negative",
    source_code: "token_expired",
    route: "WSS_AUTH",
    trigger: "expired access_token",
    expect: {
      http_status: null,
      ws_close_code: 4401,
      data_error_code: null,
      agent_allocation: "none",
      client_action: "create_session_then_reconnect",
    },
  },
  {
    id: "https.create_session.permission_denied",
    kind: "negative",
    source_code: "permission_denied",
    route: "HTTPS",
    trigger: "device requests disallowed capability or disallowed config scope",
    expect: {
      http_status: 403,
      ws_close_code: null,
      data_error_code: null,
      agent_allocation: "none",
      client_action: "show_permission_denied",
    },
  },
  {
    id: "wss.auth.permission_denied",
    kind: "negative",
    source_code: "permission_denied",
    route: "WSS_AUTH",
    trigger: "valid token without required WSS permission or capability scope",
    expect: {
      http_status: null,
      ws_close_code: 4403,
      data_error_code: null,
      agent_allocation: "none",
      client_action: "show_permission_denied",
    },
  },
  {
    id: "wss.message.protocol_error",
    kind: "negative",
    source_code: "protocol_error",
    route: "WSS_MESSAGE",
    trigger: "JSON missing required field, wrong type, oversize frame, or illegal ordering",
    expect: {
      http_status: null,
      ws_close_code: 4400,
      data_error_code: "protocol_error",
      agent_allocation: "existing_session",
      client_action: "log_trace_and_reconnect_if_closed",
    },
  },
  {
    id: "wss.auth.duplicate_connection",
    kind: "negative",
    source_code: "duplicate_connection",
    route: "WSS_AUTH",
    trigger: "same device/session opens duplicate connection and policy rejects it",
    expect: {
      http_status: null,
      ws_close_code: 4009,
      data_error_code: null,
      agent_allocation: "none_or_existing_only",
      client_action: "keep_or_reconnect_by_policy",
    },
  },
  {
    id: "https.create_session.resource_exhausted",
    kind: "negative",
    source_code: "resource_exhausted",
    route: "HTTPS",
    trigger: "no available Agent/Pool capacity before WSS allocation",
    expect: {
      http_status: 503,
      ws_close_code: null,
      data_error_code: null,
      agent_allocation: "none",
      client_action: "backoff_retry",
    },
  },
  {
    id: "gateway.runtime.resource_exhausted_data_error",
    kind: "negative",
    source_code: "resource_exhausted",
    route: "GATEWAY_RUNTIME",
    trigger: "existing session cannot obtain available Agent/Pool capacity",
    expect: {
      http_status: null,
      ws_close_code: null,
      data_error_code: "resource_exhausted",
      agent_allocation: "none_or_released",
      client_action: "show_busy_and_backoff",
    },
  },
  {
    id: "wss.runtime.busy",
    kind: "negative",
    source_code: "busy",
    route: "WSS_RUNTIME",
    trigger: "runtime overload or demo concurrency limit reached after session establishment",
    expect: {
      http_status: null,
      ws_close_code: 1013,
      data_error_code: "busy",
      agent_allocation: "existing_session_or_none",
      client_action: "backoff_retry",
    },
  },
];

const registrySchema = {
  $schema: "https://json-schema.org/draft/2020-12/schema",
  $id: "urn:xiaoge:duplex:voicecmd-registry:r5.2.2",
  title: "Xiaoge Voice Intent Seed/Registry R5.2.2 Contract",
  description:
    "Machine-checkable schema for R5.2.2 P0 voice intent seed/registry entries. It covers cloud routing intents as well as endpoint-executable control commands. Only delivery=data.cmd and delivery=data.cmd after confirmation are executable by clients/fake executor. cloud_tool + data.reply, cloud_knowledge + data.reply, and ask_split only are cloud-handled reply-only entries and must not be consumed as endpoint execution contracts. P0 parameter types are intentionally limited to enum/int.",
  "x-contract-version": version,
  "x-client-executable-deliveries": clientExecutableDeliveries,
  "x-cloud-reply-only-deliveries": cloudReplyOnlyDeliveries,
  type: "object",
  required: ["version", "entries"],
  properties: {
    version: { const: version },
    entries: {
      type: "array",
      minItems: 1,
      items: { $ref: "#/$defs/commandEntry" },
    },
  },
  additionalProperties: false,
  $defs: {
    commandEntry: {
      type: "object",
      required: [
        "action",
        "capability_id",
        "intent_type",
        "delivery",
        "params",
        "risk_level",
        "owner",
        "unsupported_behavior",
        "source_seed",
      ],
      properties: {
        action: nonEmptyString,
        capability_id: nonEmptyString,
        intent_type: {
          enum: ["control_cmd", "config", "config/control", "info_query", "knowledge_qa", "control_cmd_multi"],
        },
        delivery: {
          description:
            "Endpoint executable iff delivery is data.cmd or data.cmd after confirmation. cloud_tool + data.reply, cloud_knowledge + data.reply, and ask_split only are not executable by clients/fake executor.",
          enum: [
            "data.cmd",
            "data.cmd after confirmation",
            "ctrl.set/config API",
            "data.cmd or ctrl.set by owner",
            "cloud_tool + data.reply",
            "cloud_knowledge + data.reply",
            "ask_split only",
          ],
        },
        params: {
          type: "array",
          items: { $ref: "#/$defs/paramSpec" },
        },
        risk_level: { enum: ["low", "medium", "high"] },
        owner: nonEmptyString,
        unsupported_behavior: nonEmptyString,
        source_seed: nonEmptyString,
        positive_examples: { type: "array", items: nonEmptyString },
        negative_examples: { type: "array", items: nonEmptyString },
      },
      additionalProperties: false,
    },
    paramSpec: {
      type: "object",
      required: ["name", "type", "required"],
      properties: {
        name: nonEmptyString,
        type: { enum: ["enum", "int"] },
        required: { type: "boolean" },
        enum: { type: "array", items: nonEmptyString, minItems: 1 },
        minimum: { type: "number" },
        maximum: { type: "number" },
        unit: { type: "string" },
        default: {},
      },
      additionalProperties: false,
    },
  },
};

function resolveRef(ref, root = protocolSchema) {
  if (!ref.startsWith("#/")) throw new Error(`Only local refs are supported: ${ref}`);
  return ref
    .slice(2)
    .split("/")
    .reduce((node, part) => {
      if (node == null || !(part in node)) throw new Error(`Cannot resolve ref ${ref}`);
      return node[part];
    }, root);
}

function validate(schema, value, root = protocolSchema, pointer = "$") {
  if (schema.$ref) return validate(resolveRef(schema.$ref, root), value, root, pointer);

  if (schema.oneOf) {
    const matches = schema.oneOf.map((candidate) => validate(candidate, value, root, pointer)).filter((r) => r.ok);
    return matches.length === 1
      ? { ok: true, errors: [] }
      : { ok: false, errors: [`${pointer}: expected exactly one oneOf match, got ${matches.length}`] };
  }

  if (schema.const !== undefined && value !== schema.const) {
    return { ok: false, errors: [`${pointer}: expected const ${JSON.stringify(schema.const)}`] };
  }
  if (schema.enum && !schema.enum.includes(value)) {
    return { ok: false, errors: [`${pointer}: expected enum ${schema.enum.join("|")}`] };
  }
  if (schema.pattern && typeof value === "string" && !new RegExp(schema.pattern).test(value)) {
    return { ok: false, errors: [`${pointer}: expected pattern ${schema.pattern}`] };
  }

  if (schema.type) {
    const typeOk =
      (schema.type === "integer" && Number.isInteger(value)) ||
      (schema.type === "number" && typeof value === "number" && Number.isFinite(value)) ||
      (schema.type === "string" && typeof value === "string") ||
      (schema.type === "boolean" && typeof value === "boolean") ||
      (schema.type === "array" && Array.isArray(value)) ||
      (schema.type === "object" && value != null && typeof value === "object" && !Array.isArray(value));
    if (!typeOk) return { ok: false, errors: [`${pointer}: expected type ${schema.type}`] };
  }

  const errors = [];
  if (typeof value === "string" && schema.minLength != null && value.length < schema.minLength) {
    errors.push(`${pointer}: expected minLength ${schema.minLength}`);
  }
  if (typeof value === "number" && schema.minimum != null && value < schema.minimum) {
    errors.push(`${pointer}: expected minimum ${schema.minimum}`);
  }
  if (Array.isArray(value)) {
    if (schema.minItems != null && value.length < schema.minItems) {
      errors.push(`${pointer}: expected minItems ${schema.minItems}`);
    }
    if (schema.uniqueItems) {
      const seen = new Set(value.map((item) => JSON.stringify(item)));
      if (seen.size !== value.length) errors.push(`${pointer}: expected uniqueItems`);
    }
    if (schema.items) {
      value.forEach((item, index) => {
        const child = validate(schema.items, item, root, `${pointer}[${index}]`);
        errors.push(...child.errors);
      });
    }
  }
  if (value != null && typeof value === "object" && !Array.isArray(value)) {
    for (const key of schema.required ?? []) {
      if (!(key in value)) errors.push(`${pointer}: missing required ${key}`);
    }
    if (schema.additionalProperties === false) {
      for (const key of Object.keys(value)) {
        if (!schema.properties || !(key in schema.properties)) errors.push(`${pointer}: additional property ${key}`);
      }
    }
    for (const [key, childSchema] of Object.entries(schema.properties ?? {})) {
      if (key in value) {
        const child = validate(childSchema, value[key], root, `${pointer}.${key}`);
        errors.push(...child.errors);
      }
    }
  }

  return { ok: errors.length === 0, errors };
}

function validateExamples() {
  const summary = {
    positive: 0,
    negative_schema_fail: 0,
    negative_semantic_or_transport: 0,
  };
  const seenExampleIds = new Set();
  for (const example of examples) {
    seenExampleIds.add(example.id);
    const result = validate(resolveRef(example.schema_ref), example.payload);
    if (example.expect.schema === "pass" && !result.ok) {
      throw new Error(`${example.id} expected schema pass but failed: ${result.errors.join("; ")}`);
    }
    if (example.expect.schema === "fail" && result.ok) {
      throw new Error(`${example.id} expected schema fail but passed`);
    }
    if (example.kind === "positive") summary.positive += 1;
    if (example.expect.schema === "fail") summary.negative_schema_fail += 1;
    if (example.expect.semantic === "fail" || example.expect.transport === "fail") {
      if (!example.expect.reason) throw new Error(`${example.id} semantic/transport negative lacks reason`);
      summary.negative_semantic_or_transport += 1;
    }
  }

  for (const p1Type of ["ctrl.status", "ctrl.get", "ctrl.set", "ctrl.ack"]) {
    if (JSON.stringify(protocolSchema.$defs).includes(`"${p1Type}"`)) {
      throw new Error(`${p1Type} leaked into protocol schema defs`);
    }
  }

  for (const id of [
    "ctrl.hello.unknown_cap",
    "ctrl.hello.duplicate_caps",
    "data.reply.max_json_transport",
    "data.reply.too_large_json.transport",
    "data.reply.multi_command_blocked.ask_split",
  ]) {
    if (!seenExampleIds.has(id)) throw new Error(`missing required R5.2.2 example: ${id}`);
  }

  return summary;
}

const sourceObjectToDef = {
  "create_session.request": "createSessionRequest",
  "create_session.response": "createSessionResponse",
  "wss.auth": "wssAuth",
  "ctrl.hello": "ctrlHello",
  "ctrl.ready": "ctrlReady",
  "ctrl.state": "ctrlState",
  "ctrl.frontend_state": "ctrlFrontendState",
  "ctrl.clear": "ctrlClear",
  "data.stt": "dataStt",
  "data.reply": "dataReply",
  "data.cmd": "dataCmd",
  "data.cmd_ack": "dataCmdAck",
  "data.cmd_result": "dataCmdResult",
  "data.error": "dataError",
};

const sourceSampleToExample = {
  "create_session.request": "create_session.request.valid",
  "create_session.response": "create_session.response.valid",
  "wss.auth": "wss.auth.valid",
  "ctrl.hello": "ctrl.hello.valid",
  "ctrl.ready": "ctrl.ready.valid",
  "ctrl.state": "ctrl.state.valid",
  "ctrl.frontend_state": "ctrl.frontend_state.valid",
  "ctrl.clear": "ctrl.clear.valid",
  "data.stt": "data.stt.valid",
  "data.reply": "data.reply.valid",
  "data.cmd": "data.cmd.valid",
  "data.cmd_ack": "data.cmd_ack.valid",
  "data.cmd_result.running": "data.cmd_result.running.valid",
  "data.cmd_result.succeeded": "data.cmd_result.succeeded.valid",
  "data.error.unknown_cmd_id": "data.error.unknown_cmd_id.valid",
};

function must(condition, message) {
  if (!condition) throw new Error(message);
}

function cell(value) {
  return value == null ? "" : String(value).trim();
}

function tableRows(table, sheetName) {
  must(table, `missing inspect table: ${sheetName}`);
  const headers = table.values[2].map(cell);
  return table.values
    .slice(3)
    .filter((row) => row.some((value) => cell(value) !== ""))
    .map((row) =>
      Object.fromEntries(headers.map((header, index) => [header, row[index] == null ? "" : row[index]])),
    );
}

async function loadInspectTables(absPath) {
  const content = await fs.readFile(absPath, "utf8");
  const tables = {};
  for (const line of content.split(/\r?\n/)) {
    if (!line.trim()) continue;
    const record = JSON.parse(line);
    if (record.kind === "table") tables[record.sheet] = record;
  }
  return tables;
}

function propertySchema(defName, field) {
  if (defName === "wssAuth" && field === "Authorization") {
    return protocolSchema.$defs.wssAuth.properties.headers.properties.Authorization;
  }
  return protocolSchema.$defs[defName]?.properties?.[field];
}

function propertyRequired(defName, field) {
  if (defName === "wssAuth" && field === "Authorization") {
    return (
      protocolSchema.$defs.wssAuth.required.includes("headers") &&
      protocolSchema.$defs.wssAuth.properties.headers.required.includes("Authorization")
    );
  }
  return protocolSchema.$defs[defName]?.required?.includes(field) ?? false;
}

function enumTokens(text) {
  const match = cell(text).match(/[A-Za-z0-9_.+-]+(?:\/[A-Za-z0-9_.+-]+)+/);
  return match ? match[0].split("/") : [];
}

function deepEqual(left, right) {
  if (left === right) return true;
  if (Array.isArray(left) || Array.isArray(right)) {
    return (
      Array.isArray(left) &&
      Array.isArray(right) &&
      left.length === right.length &&
      left.every((item, index) => deepEqual(item, right[index]))
    );
  }
  if (left && right && typeof left === "object" && typeof right === "object") {
    const leftKeys = Object.keys(left).sort();
    const rightKeys = Object.keys(right).sort();
    return (
      deepEqual(leftKeys, rightKeys) &&
      leftKeys.every((key) => deepEqual(left[key], right[key]))
    );
  }
  return false;
}

function parseSourceSample(name, raw) {
  if (name === "wss.auth") {
    must(cell(raw).includes("Authorization: Bearer jwt-short-lived"), "wss.auth sample lacks bearer header");
    return { method: "GET", path: "/ws/session", headers: { Authorization: "Bearer jwt-short-lived" } };
  }
  return JSON.parse(cell(raw));
}

function reconcileGlobalField(field) {
  const traceAndSessionDefs = [
    "createSessionResponse",
    "ctrlHello",
    "ctrlReady",
    "ctrlState",
    "ctrlFrontendState",
    "ctrlClear",
    "dataStt",
    "dataReply",
    "dataCmd",
    "dataCmdAck",
    "dataCmdResult",
    "dataError",
  ];
  const utteranceDefs = ["ctrlClear", "dataStt", "dataReply", "dataCmd", "dataCmdAck", "dataCmdResult"];

  if (field === "trace_id" || field === "session_id") {
    for (const defName of traceAndSessionDefs) {
      must(propertySchema(defName, field), `${field} missing in ${defName}`);
    }
    return traceAndSessionDefs.length;
  }

  if (field === "utterance_id") {
    for (const defName of utteranceDefs) {
      must(propertySchema(defName, field), `${field} missing in ${defName}`);
    }
    return utteranceDefs.length;
  }

  throw new Error(`unknown global schema field in workbook: ${field}`);
}

function reconcileSchemaRows(rows) {
  let fieldsChecked = 0;
  let requiredChecked = 0;
  let enumChecked = 0;
  let constChecked = 0;
  const checkedObjects = new Set();

  for (const row of rows) {
    const objectName = cell(row["对象"]);
    const field = cell(row["字段"]);
    const type = cell(row["类型/枚举"]);
    const required = cell(row["必填"]);
    const description = cell(row["说明"]);
    const sample = cell(row["示例"]);

    if (objectName === "全局") {
      fieldsChecked += reconcileGlobalField(field);
      continue;
    }

    const defName = sourceObjectToDef[objectName];
    must(defName, `schema source object has no contract mapping: ${objectName}`);
    const fieldSchema = propertySchema(defName, field);
    must(fieldSchema, `${objectName}.${field} missing in protocol schema`);
    fieldsChecked += 1;
    checkedObjects.add(objectName);

    if (required === "必填") {
      must(propertyRequired(defName, field), `${objectName}.${field} is required in workbook but not schema`);
      requiredChecked += 1;
    }

    if (type === "const") {
      must(fieldSchema.const !== undefined, `${objectName}.${field} is const in workbook but not schema`);
      must(String(fieldSchema.const) === sample, `${objectName}.${field} const mismatch`);
      constChecked += 1;
    }

    const expectedEnums = enumTokens(description);
    const enumSchema = fieldSchema.enum ? fieldSchema : fieldSchema.items?.enum ? fieldSchema.items : null;
    if (enumSchema && expectedEnums.length > 0) {
      for (const value of expectedEnums) {
        must(enumSchema.enum.includes(value), `${objectName}.${field} enum missing ${value}`);
      }
      enumChecked += expectedEnums.length;
    }
  }

  return {
    result: "PASS",
    source_rows_checked: rows.length,
    fields_checked: fieldsChecked,
    required_fields_checked: requiredChecked,
    enum_values_checked: enumChecked,
    const_fields_checked: constChecked,
    source_objects_checked: [...checkedObjects].sort(),
  };
}

function reconcileSampleRows(rows) {
  let checked = 0;
  for (const row of rows) {
    const sampleName = cell(row["样例"]);
    const sourcePayload = parseSourceSample(sampleName, row["JSON"]);
    const exampleId = sourceSampleToExample[sampleName];
    must(exampleId, `source sample lacks generated example mapping: ${sampleName}`);
    const example = examples.find((item) => item.id === exampleId);
    must(example, `generated examples missing ${exampleId}`);
    must(deepEqual(example.payload, sourcePayload), `generated example differs from source sample: ${sampleName}`);

    const result = validate(resolveRef(example.schema_ref), sourcePayload);
    must(result.ok, `source sample fails protocol schema: ${sampleName}: ${result.errors.join("; ")}`);
    checked += 1;
  }

  return {
    result: "PASS",
    source_samples_checked: checked,
    generated_positive_examples_checked: checked,
  };
}

function reconcileErrorRows(rows) {
  const schemaErrorCodes = protocolSchema.$defs.dataError.properties.code.enum;
  const sourceCodes = {};
  const closeCodesFromSource = new Set();
  const httpStatusesFromSource = new Set();

  for (const row of rows) {
    const code = cell(row.code);
    const behavior = cell(row["协议表现"]);
    must(code, "error/close code row lacks code");
    sourceCodes[code] = {
      plane: cell(row["协议面"]),
      trigger: cell(row["触发条件"]),
      behavior,
      client_gui_behavior: cell(row["客户端/GUI表现"]),
      requirement: cell(row["关联需求"]),
    };

    for (const match of behavior.matchAll(/\bclose\s+(\d{4})\b/g)) {
      closeCodesFromSource.add(Number(match[1]));
    }
    for (const match of behavior.matchAll(/\b(401|403|503)\b/g)) {
      httpStatusesFromSource.add(Number(match[1]));
    }

    must(schemaErrorCodes.includes(code), `${code} exists in source table but not data.error enum`);
  }

  return {
    result: "PASS",
    source_error_rows_checked: rows.length,
    source_codes: sourceCodes,
    data_error_enum_checked: schemaErrorCodes.length,
    close_codes_from_source: [...closeCodesFromSource].sort((a, b) => a - b),
    http_statuses_from_source: [...httpStatusesFromSource].sort((a, b) => a - b),
    legacy_close_only_codes: [],
  };
}

function validateCloseCodeCases(sourceCodes) {
  const requiredCaseIds = [
    "https.create_session.auth_failed",
    "wss.auth.no_token",
    "wss.auth.invalid_token",
    "wss.auth.expired_token",
    "https.create_session.permission_denied",
    "wss.auth.permission_denied",
    "wss.message.protocol_error",
    "wss.auth.duplicate_connection",
    "https.create_session.resource_exhausted",
    "gateway.runtime.resource_exhausted_data_error",
    "wss.runtime.busy",
  ];
  const requiredCloseCodes = [4400, 4401, 4403, 4009, 1013];
  const requiredHttpStatuses = [401, 403, 503];
  const schemaErrorCodes = protocolSchema.$defs.dataError.properties.code.enum;
  const coveredCloseCodes = new Set();
  const coveredHttpStatuses = new Set();
  const coveredDataErrorCodes = new Set();

  for (const caseId of requiredCaseIds) {
    must(closeCodeCases.some((item) => item.id === caseId), `close-code case missing: ${caseId}`);
  }

  for (const item of closeCodeCases) {
    must(item.kind === "negative", `${item.id} must be a negative replay case`);
    must(sourceCodes[item.source_code], `${item.id} references unknown source code ${item.source_code}`);
    must(item.route, `${item.id} lacks route`);
    must(item.trigger, `${item.id} lacks trigger`);
    must(item.expect, `${item.id} lacks expect`);

    const sourceBehavior = sourceCodes[item.source_code].behavior;
    const { http_status: httpStatus, ws_close_code: closeCode, data_error_code: dataErrorCode } = item.expect;

    if (httpStatus != null) {
      must(sourceBehavior.includes(String(httpStatus)), `${item.id} HTTP ${httpStatus} not in source behavior`);
      coveredHttpStatuses.add(httpStatus);
    }
    if (closeCode != null) {
      must(sourceBehavior.includes(String(closeCode)), `${item.id} close ${closeCode} not in source behavior`);
      coveredCloseCodes.add(closeCode);
    }
    if (dataErrorCode != null) {
      must(schemaErrorCodes.includes(dataErrorCode), `${item.id} data_error_code not in schema enum`);
      must(
        sourceBehavior.includes("data.error") || item.source_code === "busy",
        `${item.id} data.error path not backed by source behavior`,
      );
      coveredDataErrorCodes.add(dataErrorCode);
    }
  }

  for (const code of requiredCloseCodes) {
    must(coveredCloseCodes.has(code), `required close code not covered: ${code}`);
  }
  for (const status of requiredHttpStatuses) {
    must(coveredHttpStatuses.has(status), `required HTTP status not covered: ${status}`);
  }

  return {
    result: "PASS",
    cases_checked: closeCodeCases.length,
    required_cases_checked: requiredCaseIds.length,
    close_codes_covered: [...coveredCloseCodes].sort((a, b) => a - b),
    http_statuses_covered: [...coveredHttpStatuses].sort((a, b) => a - b),
    data_error_codes_covered: [...coveredDataErrorCodes].sort(),
  };
}

function reconcileRegistryRows(rows) {
  const commandEntry = registrySchema.$defs.commandEntry;
  const intentEnums = commandEntry.properties.intent_type.enum;
  const deliveryEnums = commandEntry.properties.delivery.enum;
  const riskEnums = commandEntry.properties.risk_level.enum;
  const paramTypeEnums = registrySchema.$defs.paramSpec.properties.type.enum;
  const actions = new Set();

  for (const row of rows) {
    const action = cell(row.action);
    must(action, "registry row lacks action");
    actions.add(action);
    must(cell(row.capability_id), `${action} lacks capability_id`);
    must(intentEnums.includes(cell(row.intent_type)), `${action} intent_type not allowed: ${row.intent_type}`);
    must(deliveryEnums.includes(cell(row["投递方式"])), `${action} delivery not allowed: ${row["投递方式"]}`);
    must(cell(row["参数"]), `${action} lacks param name`);
    must(paramTypeEnums.includes(cell(row["类型"])), `${action} param type not allowed: ${row["类型"]}`);
    must(riskEnums.includes(cell(row.risk_level)), `${action} risk_level not allowed: ${row.risk_level}`);
    must(cell(row.owner), `${action} lacks owner`);
    must(cell(row["unsupported/错误行为"]), `${action} lacks unsupported/error behavior`);
    must(cell(row["来源seed"]), `${action} lacks source seed`);
  }

  return {
    result: "PASS",
    source_rows_checked: rows.length,
    distinct_actions_checked: actions.size,
    intent_enums_checked: intentEnums,
    delivery_enums_checked: deliveryEnums,
    param_type_enums_checked: paramTypeEnums,
    risk_enums_checked: riskEnums,
  };
}

function reconcileMultiCommandBlocked(seedRows, commandStateRows) {
  const exampleId = "data.reply.multi_command_blocked.ask_split";
  const example = examples.find((item) => item.id === exampleId);
  must(example, `generated examples missing ${exampleId}`);
  must(example.payload.type === "data.reply", `${exampleId} must output data.reply`);
  must(!("cmd_id" in example.payload), `${exampleId} payload must not carry cmd_id`);
  must(example.context?.utterance_text === "往前走一米再挥手", `${exampleId} utterance mismatch`);
  must(example.context?.intent_type === "control_cmd_multi", `${exampleId} must trace control_cmd_multi`);
  must(example.context?.state === "multi_command_blocked", `${exampleId} must trace multi_command_blocked`);
  must(example.context?.reply_style === "ask_split", `${exampleId} must trace ask_split`);
  must(example.context?.source_seed === "SEED-017", `${exampleId} must trace SEED-017`);
  must(example.context?.requirements?.includes("FR-CMD-003"), `${exampleId} must trace FR-CMD-003`);
  must(example.context?.forbidden_outputs?.includes("data.cmd"), `${exampleId} must forbid data.cmd`);
  must(example.context?.forbidden_outputs?.includes("cmd_id"), `${exampleId} must forbid cmd_id`);
  must(
    example.context?.forbidden_outputs?.includes("executor_side_effect"),
    `${exampleId} must forbid endpoint executor side effects`,
  );
  must(example.expect?.output_types?.length === 1, `${exampleId} must define a single output type`);
  must(example.expect.output_types[0] === "data.reply", `${exampleId} output type must be data.reply`);
  must(example.expect?.forbidden_types?.includes("data.cmd"), `${exampleId} must machine-forbid data.cmd`);
  must(example.expect?.no_cmd_id === true, `${exampleId} must machine-forbid cmd_id`);
  must(example.expect?.no_side_effects === true, `${exampleId} must machine-forbid side effects`);

  const seed = seedRows.find((row) => cell(row["Seed ID"]) === "SEED-017");
  must(seed, "P0 seed table missing SEED-017");
  must(cell(seed.intent_type) === "control_cmd_multi", "SEED-017 intent_type must be control_cmd_multi");
  must(cell(seed["用户话术"]) === example.context.utterance_text, "SEED-017 utterance must match contract example");
  must(cell(seed["是否下发data.cmd"]) === "否", "SEED-017 must not dispatch data.cmd");
  must(cell(seed["P0负例行为"]).includes("ask_split"), "SEED-017 must specify ask_split behavior");
  must(cell(seed["关联需求ID"]).includes("FR-CMD-003"), "SEED-017 must trace FR-CMD-003");

  const stateRow = commandStateRows.find(
    (row) =>
      cell(row["终点"]) === "multi_command_blocked" &&
      cell(row["触发"]).includes("多个控制动作") &&
      cell(row["动作"]).includes("不生成 data.cmd") &&
      cell(row["动作"]).includes("data.reply") &&
      cell(row["关联需求"]).includes("FR-CMD-003"),
  );
  must(stateRow, "command state table lacks multi_command_blocked ask_split no-data.cmd trace");

  return {
    result: "PASS",
    example_id: exampleId,
    source_seed: "SEED-017",
    requirement_id: "FR-CMD-003",
    utterance_text: example.context.utterance_text,
    expected_state: example.context.state,
    expected_reply_style: example.context.reply_style,
    expected_output_types: example.expect.output_types,
    forbidden_outputs: example.context.forbidden_outputs,
    workbook_seed_checked: true,
    command_state_checked: true,
  };
}

async function reconcileSources(absInspectPath, exampleValidation) {
  const tables = await loadInspectTables(absInspectPath);
  const schemaRows = tableRows(tables["权威JSON Schema"], "权威JSON Schema");
  const sampleRows = tableRows(tables["Schema样例帧"], "Schema样例帧");
  const errorRows = tableRows(tables["错误码与关闭码"], "错误码与关闭码");
  const registryRows = tableRows(tables["P0 registry schema"], "P0 registry schema");
  const seedRows = tableRows(tables["P0 seed命令表"], "P0 seed命令表");
  const commandStateRows = tableRows(tables["命令状态机"], "命令状态机");

  const schemaTable = reconcileSchemaRows(schemaRows);
  const sampleTable = reconcileSampleRows(sampleRows);
  const errorTable = reconcileErrorRows(errorRows);
  const closeCodeTable = validateCloseCodeCases(errorTable.source_codes);
  const registryTable = reconcileRegistryRows(registryRows);
  const multiCommandBlockedTable = reconcileMultiCommandBlocked(seedRows, commandStateRows);

  return {
    version,
    generated_at: generatedAt,
    result: "PASS",
    mode: "manual_curated_contract_with_machine_reconciliation",
    workbook_inspect_path: rel(absInspectPath),
    tables_checked: {
      authoritative_json_schema: schemaTable,
      schema_sample_frames: sampleTable,
      error_and_close_codes: errorTable,
      close_code_replay_cases: closeCodeTable,
      p0_registry_schema: registryTable,
      multi_command_blocked_contract: multiCommandBlockedTable,
      examples_jsonl: {
        result: "PASS",
        positive_examples: exampleValidation.positive,
        negative_schema_fail_examples: exampleValidation.negative_schema_fail,
        negative_semantic_or_transport_examples: exampleValidation.negative_semantic_or_transport,
      },
      p1_control_exclusion: {
        result: "PASS",
        excluded_types: ["ctrl.status", "ctrl.get", "ctrl.set", "ctrl.ack"],
      },
    },
  };
}

async function sha256(absPath) {
  const bytes = await fs.readFile(absPath);
  return crypto.createHash("sha256").update(bytes).digest("hex");
}

async function fileExists(absPath) {
  try {
    await fs.access(absPath);
    return true;
  } catch {
    return false;
  }
}

async function optionalFileHash(absPath) {
  return (await fileExists(absPath)) ? await sha256(absPath) : null;
}

function json(data) {
  return `${JSON.stringify(data, null, 2)}\n`;
}

function jsonl(rows) {
  return `${rows.map((row) => JSON.stringify(row)).join("\n")}\n`;
}

function signoffMarkdown() {
  return `# Xiaoge Duplex Protocol R5.2.2 G1 Signoff

Status: DRAFT - owners assigned, signatures pending
Contract version: ${version}
Generated at: ${generatedAt}

This signoff record is a G1 contract-signoff material. It does not authorize SDK/Gateway/Agent implementation changes and does not authorize G2 mock/test coding before review approval.

## Contract Files

- Protocol schema: \`${files.protocolSchema}\`
- Examples JSONL: \`${files.examples}\`
- Close/error code replay cases: \`${files.closeCodes}\`
- Voice-cmd registry schema: \`${files.registrySchema}\`
- Source reconciliation report: \`${files.sourceCheck}\`
- Manifest: \`${files.manifest}\`

## Accountable Owners

| Role | Accountable person or unique role | Scope | Signoff |
| --- | --- | --- | --- |
| Clients owner | ${ownerRoles.clients.name} | ${ownerRoles.clients.scope} | Pending |
| Cloud owner | ${ownerRoles.cloud.name} | ${ownerRoles.cloud.scope} | Pending |
| Protocol arbiter | ${ownerRoles.protocol.name} | ${ownerRoles.protocol.scope} | Pending |

The accountable people above are assigned by the product owner. G1 is still not closed until all three sign the same manifest hash.

## Pending G1 Signing Condition

This file intentionally remains DRAFT because named owners have not yet signed the same generated manifest hash in this artifact.

## Mock Responsibility Split

| Item | Owner |
| --- | --- |
| fake server / cloud replay | ${ownerRoles.cloud.name} |
| fake SDK / fake executor | ${ownerRoles.clients.name} |
| shared examples, manifest, replay report signoff | ${ownerRoles.clients.name} + ${ownerRoles.cloud.name} + ${ownerRoles.protocol.name} |

## Gate Statement

- G1 may sign only after the owners above approve the same manifest hash.
- G2 may only write mock/test code after G1 is signed and reviewed.
- Real SDK/Gateway/Agent implementation remains blocked until G1/G2/G3 and explicit owner approval.
`;
}

async function main() {
  const validation = validateExamples();
  const sourceReconciliation = await reconcileSources(sourcePaths.workbookInspect, validation);

  await fs.mkdir(contractsDir, { recursive: true });
  await fs.writeFile(out(files.protocolSchema), json(protocolSchema), "utf8");
  await fs.writeFile(out(files.examples), jsonl(examples), "utf8");
  await fs.writeFile(out(files.closeCodes), jsonl(closeCodeCases), "utf8");
  await fs.writeFile(out(files.registrySchema), json(registrySchema), "utf8");
  await fs.writeFile(out(files.sourceCheck), json(sourceReconciliation), "utf8");
  await fs.writeFile(out(files.signoff), signoffMarkdown(), "utf8");

  const generatedPaths = {
    protocolSchema: out(files.protocolSchema),
    examples: out(files.examples),
    closeCodes: out(files.closeCodes),
    registrySchema: out(files.registrySchema),
    sourceCheck: out(files.sourceCheck),
    signoff: out(files.signoff),
  };

  const manifest = {
    version,
    generated_at: generatedAt,
    status: "G1_CONTRACT_PACKAGE_DRAFT_NOT_SIGNED",
    accountable_owners: {
      clients_owner: ownerRoles.clients,
      cloud_owner: ownerRoles.cloud,
      protocol_arbiter: ownerRoles.protocol,
    },
    contract_generation_mode: {
      mode: "manual_curated_contract_with_machine_reconciliation",
      statement:
        "The schema/examples/registry are curated contract artifacts. build_contracts.mjs parses the R5.2.2 workbook inspect tables and fails generation if the source tables drift from the contract.",
    },
    gate: {
      g1: "contract package/signoff only",
      g2: "blocked until G1 review; only mock/test code when approved",
      implementation: "blocked until G1/G2/G3 and explicit owner approval",
    },
    sources: {
      workbook: {
        path: rel(sourcePaths.workbook),
        sha256: await optionalFileHash(sourcePaths.workbook),
      },
      workbook_inspect: {
        path: rel(sourcePaths.workbookInspect),
        sha256: await optionalFileHash(sourcePaths.workbookInspect),
      },
      protocol_v2: {
        path: rel(sourcePaths.protocolV2),
        version: "v1.9",
        sha256: await optionalFileHash(sourcePaths.protocolV2),
      },
      voice_cmd: {
        path: rel(sourcePaths.voiceCmd),
        version: "v4.5",
        sha256: await optionalFileHash(sourcePaths.voiceCmd),
      },
      generator: {
        path: rel(sourcePaths.generator),
        sha256: await optionalFileHash(sourcePaths.generator),
      },
    },
    generated_files: {
      protocol_schema: {
        path: rel(generatedPaths.protocolSchema),
        sha256: await sha256(generatedPaths.protocolSchema),
      },
      examples_jsonl: {
        path: rel(generatedPaths.examples),
        sha256: await sha256(generatedPaths.examples),
      },
      close_code_cases_jsonl: {
        path: rel(generatedPaths.closeCodes),
        sha256: await sha256(generatedPaths.closeCodes),
      },
      voicecmd_registry_schema: {
        path: rel(generatedPaths.registrySchema),
        sha256: await sha256(generatedPaths.registrySchema),
      },
      source_reconciliation_report: {
        path: rel(generatedPaths.sourceCheck),
        sha256: await sha256(generatedPaths.sourceCheck),
      },
      signoff: {
        path: rel(generatedPaths.signoff),
        sha256: await sha256(generatedPaths.signoff),
      },
    },
    validation: {
      result: "PASS",
      schema_sample_field_check: "PASS",
      p1_control_not_in_p0_schema: "PASS",
      positive_examples: validation.positive,
      negative_schema_fail_examples: validation.negative_schema_fail,
      negative_semantic_or_transport_examples: validation.negative_semantic_or_transport,
      source_reconciliation: {
        result: sourceReconciliation.result,
        mode: sourceReconciliation.mode,
        workbook_schema_rows_checked:
          sourceReconciliation.tables_checked.authoritative_json_schema.source_rows_checked,
        workbook_schema_fields_checked:
          sourceReconciliation.tables_checked.authoritative_json_schema.fields_checked,
        workbook_sample_frames_checked:
          sourceReconciliation.tables_checked.schema_sample_frames.source_samples_checked,
        registry_rows_checked: sourceReconciliation.tables_checked.p0_registry_schema.source_rows_checked,
        error_close_code_rows_checked:
          sourceReconciliation.tables_checked.error_and_close_codes.source_error_rows_checked,
        close_code_cases_checked:
          sourceReconciliation.tables_checked.close_code_replay_cases.cases_checked,
        close_codes_covered:
          sourceReconciliation.tables_checked.close_code_replay_cases.close_codes_covered,
        http_statuses_covered:
          sourceReconciliation.tables_checked.close_code_replay_cases.http_statuses_covered,
        data_error_codes_covered:
          sourceReconciliation.tables_checked.close_code_replay_cases.data_error_codes_covered,
      },
      p1_controls_excluded: ["ctrl.status", "ctrl.get", "ctrl.set", "ctrl.ack"],
      error_code_contract: {
        protocol_error: 4400,
        token_expired: 4401,
        permission_denied: 4403,
        duplicate_connection: 4009,
        resource_exhausted_or_busy: 1013,
      },
    },
  };

  await fs.writeFile(out(files.manifest), json(manifest), "utf8");

  console.log(`CONTRACT_PACKAGE: ${version}`);
  console.log(`SCHEMA_SAMPLE_FIELD_CHECK: ${manifest.validation.schema_sample_field_check}`);
  console.log(`P1_CONTROL_NOT_IN_P0_SCHEMA: ${manifest.validation.p1_control_not_in_p0_schema}`);
  console.log(`POSITIVE_EXAMPLES: ${validation.positive}`);
  console.log(`NEGATIVE_SCHEMA_FAIL_EXAMPLES: ${validation.negative_schema_fail}`);
  console.log(`NEGATIVE_SEMANTIC_OR_TRANSPORT_EXAMPLES: ${validation.negative_semantic_or_transport}`);
  console.log(`SOURCE_RECONCILIATION: ${sourceReconciliation.result}`);
  console.log(
    `CLOSE_CODE_CASES: ${sourceReconciliation.tables_checked.close_code_replay_cases.cases_checked}`,
  );
  console.log(rel(out(files.manifest)));
}

await main();
