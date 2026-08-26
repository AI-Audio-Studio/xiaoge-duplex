package com.xiaoge.client;

import org.json.JSONException;
import org.json.JSONObject;

import java.util.Set;

public final class CommandEvent {
    private static final Set<String> VALID_RISK_LEVELS = EventJson.set("low", "medium", "high");

    public final String cmdId;
    public final String capabilityId;
    public final String action;
    public final JSONObject params;
    public final String riskLevel;
    public final int ackTimeoutMs;
    public final int resultTimeoutMs;
    public final long issuedAtMs;
    public final String utteranceId;
    public final String traceId;
    public final String sessionId;
    public final JSONObject raw;

    private CommandEvent(String cmdId, String capabilityId, String action, JSONObject params,
                         String riskLevel, int ackTimeoutMs, int resultTimeoutMs, long issuedAtMs,
                         String utteranceId, String traceId, String sessionId, JSONObject raw) {
        this.cmdId = cmdId;
        this.capabilityId = capabilityId;
        this.action = action;
        this.params = params;
        this.riskLevel = riskLevel;
        this.ackTimeoutMs = ackTimeoutMs;
        this.resultTimeoutMs = resultTimeoutMs;
        this.issuedAtMs = issuedAtMs;
        this.utteranceId = utteranceId;
        this.traceId = traceId;
        this.sessionId = sessionId;
        this.raw = raw;
    }

    public static CommandEvent fromJson(JSONObject payload) throws JSONException {
        EventJson.requireType(payload, "data.cmd");
        EventJson.requireOnlyKeys(payload, "type", "trace_id", "session_id", "utterance_id", "cmd_id",
                "capability_id", "action", "params", "risk_level", "ack_timeout_ms", "result_timeout_ms",
                "issued_at_ms");
        return new CommandEvent(
                EventJson.requiredString(payload, "cmd_id"),
                EventJson.requiredString(payload, "capability_id"),
                EventJson.requiredString(payload, "action"),
                EventJson.requiredObject(payload, "params"),
                EventJson.requiredEnum(payload, "risk_level", VALID_RISK_LEVELS),
                EventJson.requiredIntAtLeast(payload, "ack_timeout_ms", 1),
                EventJson.requiredIntAtLeast(payload, "result_timeout_ms", 1),
                EventJson.requiredLongAtLeast(payload, "issued_at_ms", 0),
                EventJson.requiredString(payload, "utterance_id"),
                EventJson.requiredString(payload, "trace_id"),
                EventJson.requiredString(payload, "session_id"),
                EventJson.copy(payload));
    }
}
