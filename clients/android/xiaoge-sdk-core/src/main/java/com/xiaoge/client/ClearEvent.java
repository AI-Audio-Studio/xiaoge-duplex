package com.xiaoge.client;

import org.json.JSONException;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.List;
import java.util.Set;

public final class ClearEvent {
    private static final Set<String> KNOWN_REASONS = EventJson.set(
            "barge_in", "user_stop", "system_cancel", "sleep");

    public final String traceId;
    public final String sessionId;
    public final String reason;
    public final String utteranceId;
    public final JSONObject raw;
    /** Field-level deviations observed while parsing; empty when the payload fully matches R5.2.2. */
    public final String[] parseWarnings;

    private ClearEvent(String traceId, String sessionId, String reason, String utteranceId,
                       JSONObject raw, String[] parseWarnings) {
        this.traceId = traceId;
        this.sessionId = sessionId;
        this.reason = reason;
        this.utteranceId = utteranceId;
        this.raw = raw;
        this.parseWarnings = parseWarnings;
    }

    public static ClearEvent fromJson(JSONObject payload) throws JSONException {
        EventJson.requireType(payload, "ctrl.clear");
        List<String> warnings = new ArrayList<>();
        EventJson.warnUnknownKeys(payload, warnings,
                "type", "trace_id", "session_id", "utterance_id", "reason");
        return new ClearEvent(
                EventJson.string(payload, "trace_id", warnings),
                EventJson.string(payload, "session_id", warnings),
                EventJson.optionalEnum(payload, "reason", KNOWN_REASONS, warnings),
                EventJson.optionalString(payload, "utterance_id", warnings),
                EventJson.copy(payload),
                warnings.toArray(new String[0]));
    }
}
