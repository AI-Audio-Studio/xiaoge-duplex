package com.xiaoge.client;

import org.json.JSONException;
import org.json.JSONObject;

import java.util.Set;

public final class ClearEvent {
    private static final Set<String> VALID_REASONS = EventJson.set(
            "barge_in", "user_stop", "system_cancel", "sleep");

    public final String traceId;
    public final String sessionId;
    public final String reason;
    public final String utteranceId;
    public final JSONObject raw;

    private ClearEvent(String traceId, String sessionId, String reason, String utteranceId, JSONObject raw) {
        this.traceId = traceId;
        this.sessionId = sessionId;
        this.reason = reason;
        this.utteranceId = utteranceId;
        this.raw = raw;
    }

    public static ClearEvent fromJson(JSONObject payload) throws JSONException {
        EventJson.requireType(payload, "ctrl.clear");
        EventJson.requireOnlyKeys(payload, "type", "trace_id", "session_id", "utterance_id", "reason");
        return new ClearEvent(
                EventJson.requiredString(payload, "trace_id"),
                EventJson.requiredString(payload, "session_id"),
                EventJson.optionalEnum(payload, "reason", VALID_REASONS),
                EventJson.optionalString(payload, "utterance_id"),
                EventJson.copy(payload));
    }
}
