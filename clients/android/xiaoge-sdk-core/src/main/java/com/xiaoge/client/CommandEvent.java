package com.xiaoge.client;

import org.json.JSONException;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.List;
import java.util.Set;

public final class CommandEvent {
    private static final Set<String> KNOWN_RISK_LEVELS = EventJson.set("low", "medium", "high");

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
    /** Field-level deviations observed while parsing; empty when the payload fully matches R5.2.2. */
    public final String[] parseWarnings;

    private CommandEvent(String cmdId, String capabilityId, String action, JSONObject params,
                         String riskLevel, int ackTimeoutMs, int resultTimeoutMs, long issuedAtMs,
                         String utteranceId, String traceId, String sessionId, JSONObject raw,
                         String[] parseWarnings) {
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
        this.parseWarnings = parseWarnings;
    }

    public static CommandEvent fromJson(JSONObject payload) throws JSONException {
        EventJson.requireType(payload, "data.cmd");
        List<String> warnings = new ArrayList<>();
        EventJson.warnUnknownKeys(payload, warnings, "type", "trace_id", "session_id", "utterance_id", "cmd_id",
                "capability_id", "action", "params", "risk_level", "ack_timeout_ms", "result_timeout_ms",
                "issued_at_ms");
        return new CommandEvent(
                EventJson.string(payload, "cmd_id", warnings),
                EventJson.string(payload, "capability_id", warnings),
                EventJson.string(payload, "action", warnings),
                EventJson.object(payload, "params", warnings),
                EventJson.enumValue(payload, "risk_level", KNOWN_RISK_LEVELS, warnings),
                EventJson.intOrDefault(payload, "ack_timeout_ms", 0, warnings),
                EventJson.intOrDefault(payload, "result_timeout_ms", 0, warnings),
                EventJson.longValue(payload, "issued_at_ms", warnings),
                EventJson.string(payload, "utterance_id", warnings),
                EventJson.string(payload, "trace_id", warnings),
                EventJson.string(payload, "session_id", warnings),
                EventJson.copy(payload),
                warnings.toArray(new String[0]));
    }
}
