package com.xiaoge.client;

import org.json.JSONException;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.List;
import java.util.Set;

public final class ErrorEvent {
    private static final Set<String> KNOWN_CODES = EventJson.set(
            "auth_failed", "permission_denied", "busy", "protocol_error", "capability_unsupported",
            "token_expired", "duplicate_connection", "resource_exhausted", "unknown_cmd_id");

    public final String code;
    public final String message;
    public final boolean retryable;
    public final long tsMs;
    public final String traceId;
    public final String sessionId;
    public final JSONObject raw;
    /** Field-level deviations observed while parsing; empty when the payload fully matches R5.2.2. */
    public final String[] parseWarnings;

    private ErrorEvent(String code, String message, boolean retryable, long tsMs,
                       String traceId, String sessionId, JSONObject raw, String[] parseWarnings) {
        this.code = code;
        this.message = message;
        this.retryable = retryable;
        this.tsMs = tsMs;
        this.traceId = traceId;
        this.sessionId = sessionId;
        this.raw = raw;
        this.parseWarnings = parseWarnings;
    }

    public static ErrorEvent fromJson(JSONObject payload) throws JSONException {
        EventJson.requireType(payload, "data.error");
        List<String> warnings = new ArrayList<>();
        EventJson.warnUnknownKeys(payload, warnings,
                "type", "trace_id", "session_id", "code", "message", "retryable", "ts_ms");
        return new ErrorEvent(
                EventJson.enumValue(payload, "code", KNOWN_CODES, warnings),
                EventJson.string(payload, "message", warnings),
                EventJson.bool(payload, "retryable", warnings),
                EventJson.longValue(payload, "ts_ms", warnings),
                EventJson.string(payload, "trace_id", warnings),
                EventJson.string(payload, "session_id", warnings),
                EventJson.copy(payload),
                warnings.toArray(new String[0]));
    }
}
