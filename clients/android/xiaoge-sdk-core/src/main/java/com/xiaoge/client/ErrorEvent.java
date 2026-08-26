package com.xiaoge.client;

import org.json.JSONException;
import org.json.JSONObject;

import java.util.Set;

public final class ErrorEvent {
    private static final Set<String> VALID_CODES = EventJson.set(
            "auth_failed", "permission_denied", "busy", "protocol_error", "capability_unsupported",
            "token_expired", "duplicate_connection", "resource_exhausted", "unknown_cmd_id");

    public final String code;
    public final String message;
    public final boolean retryable;
    public final long tsMs;
    public final String traceId;
    public final String sessionId;
    public final JSONObject raw;

    private ErrorEvent(String code, String message, boolean retryable, long tsMs,
                       String traceId, String sessionId, JSONObject raw) {
        this.code = code;
        this.message = message;
        this.retryable = retryable;
        this.tsMs = tsMs;
        this.traceId = traceId;
        this.sessionId = sessionId;
        this.raw = raw;
    }

    public static ErrorEvent fromJson(JSONObject payload) throws JSONException {
        EventJson.requireType(payload, "data.error");
        EventJson.requireOnlyKeys(payload, "type", "trace_id", "session_id", "code", "message", "retryable", "ts_ms");
        return new ErrorEvent(
                EventJson.requiredEnum(payload, "code", VALID_CODES),
                EventJson.requiredString(payload, "message"),
                EventJson.requiredBoolean(payload, "retryable"),
                EventJson.requiredLong(payload, "ts_ms"),
                EventJson.requiredString(payload, "trace_id"),
                EventJson.requiredString(payload, "session_id"),
                EventJson.copy(payload));
    }
}
