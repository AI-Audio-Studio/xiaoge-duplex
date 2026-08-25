package com.xiaoge.client;

import org.json.JSONException;
import org.json.JSONObject;

import java.util.HashSet;
import java.util.Set;

public final class FakeCommandExecutor {
    private static final long COMMAND_MAX_AGE_GRACE_MS = 1000;
    private final Set<String> seen = new HashSet<>();

    public String[] execute(JSONObject cmd) throws JSONException {
        String cmdId = cmd.optString("cmd_id", "");
        if (cmdId.isEmpty()) {
            JSONObject error = new JSONObject()
                    .put("type", "data.error")
                    .put("trace_id", cmd.optString("trace_id"))
                    .put("session_id", cmd.optString("session_id"))
                    .put("code", "unknown_cmd_id")
                    .put("message", "missing cmd_id")
                    .put("retryable", false)
                    .put("ts_ms", System.currentTimeMillis());
            return new String[]{ProtocolCodec.compact(error)};
        }
        if (!seen.add(cmdId)) {
            return new String[]{ProtocolCodec.cmdAck(
                    cmd,
                    ProtocolCodec.CMD_ACK_STATUS_DUPLICATE,
                    "duplicate_cmd_id")};
        }
        String rejectCode = rejectionCode(cmd);
        if (rejectCode != null) {
            return new String[]{ProtocolCodec.cmdAck(cmd, ProtocolCodec.CMD_ACK_STATUS_REJECTED, rejectCode)};
        }
        return new String[]{
                ProtocolCodec.cmdAck(cmd, ProtocolCodec.CMD_ACK_STATUS_ACCEPTED, "ok"),
                ProtocolCodec.cmdResult(cmd, ProtocolCodec.CMD_RESULT_STATUS_RUNNING, "ok"),
                ProtocolCodec.cmdResult(cmd, ProtocolCodec.CMD_RESULT_STATUS_SUCCEEDED, "ok")
        };
    }

    private String rejectionCode(JSONObject cmd) {
        String[] requiredStrings = {
                "type", "trace_id", "session_id", "utterance_id", "cmd_id", "capability_id", "action"
        };
        for (String key : requiredStrings) {
            if (cmd.optString(key, "").isEmpty()) {
                return "invalid_cmd_schema";
            }
        }
        if (!cmd.optString("type").equals("data.cmd") || !cmd.has("params")) {
            return "invalid_cmd_schema";
        }
        if (!(cmd.opt("params") instanceof JSONObject)) {
            return "invalid_cmd_schema";
        }
        String risk = cmd.optString("risk_level", "");
        if (!risk.equals("low") && !risk.equals("medium") && !risk.equals("high")) {
            return "invalid_cmd_schema";
        }
        if (cmd.optLong("ack_timeout_ms", 0) < 1
                || cmd.optLong("result_timeout_ms", 0) < 1
                || cmd.optLong("issued_at_ms", -1) < 0) {
            return "invalid_cmd_schema";
        }
        long expiresAt = cmd.optLong("issued_at_ms")
                + cmd.optLong("ack_timeout_ms")
                + cmd.optLong("result_timeout_ms");
        if (System.currentTimeMillis() > expiresAt + COMMAND_MAX_AGE_GRACE_MS) {
            return "late_cmd";
        }
        String capability = cmd.optString("capability_id");
        String action = cmd.optString("action");
        if (!capability.equals("motion.move")) {
            return "capability_unsupported";
        }
        if (!action.equals("navigation.move")) {
            return "action_unsupported";
        }
        JSONObject params = cmd.optJSONObject("params");
        String direction = params == null ? "" : params.optString("direction", "");
        int distance = params == null ? 0 : params.optInt("distance_cm", 0);
        boolean directionOk = direction.equals("forward")
                || direction.equals("backward")
                || direction.equals("left")
                || direction.equals("right");
        if (!directionOk || distance < 1 || distance > 10000) {
            return "invalid_params";
        }
        return null;
    }
}
