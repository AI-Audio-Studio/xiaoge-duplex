package com.xiaoge.client;

import org.json.JSONException;
import org.json.JSONObject;

public final class SttEvent {
    public final String text;
    public final boolean isFinal;
    public final String utteranceId;
    public final String traceId;
    public final String sessionId;
    public final long tsMs;
    public final JSONObject raw;

    private SttEvent(String text, boolean isFinal, String utteranceId,
                     String traceId, String sessionId, long tsMs, JSONObject raw) {
        this.text = text;
        this.isFinal = isFinal;
        this.utteranceId = utteranceId;
        this.traceId = traceId;
        this.sessionId = sessionId;
        this.tsMs = tsMs;
        this.raw = raw;
    }

    public boolean isFinal() {
        return isFinal;
    }

    public static SttEvent fromJson(JSONObject payload) throws JSONException {
        EventJson.requireType(payload, "data.stt");
        EventJson.requireOnlyKeys(payload, "type", "trace_id", "session_id", "utterance_id", "text", "final", "ts_ms");
        return new SttEvent(
                EventJson.requiredString(payload, "text"),
                EventJson.requiredBoolean(payload, "final"),
                EventJson.requiredString(payload, "utterance_id"),
                EventJson.requiredString(payload, "trace_id"),
                EventJson.requiredString(payload, "session_id"),
                EventJson.requiredLong(payload, "ts_ms"),
                EventJson.copy(payload));
    }
}
