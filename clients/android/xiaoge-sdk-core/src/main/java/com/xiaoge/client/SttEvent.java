package com.xiaoge.client;

import org.json.JSONException;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.List;

public final class SttEvent {
    public final String text;
    public final boolean isFinal;
    public final String utteranceId;
    public final String traceId;
    public final String sessionId;
    public final long tsMs;
    public final JSONObject raw;
    /** Field-level deviations observed while parsing; empty when the payload fully matches R5.2.2. */
    public final String[] parseWarnings;

    private SttEvent(String text, boolean isFinal, String utteranceId,
                     String traceId, String sessionId, long tsMs, JSONObject raw, String[] parseWarnings) {
        this.text = text;
        this.isFinal = isFinal;
        this.utteranceId = utteranceId;
        this.traceId = traceId;
        this.sessionId = sessionId;
        this.tsMs = tsMs;
        this.raw = raw;
        this.parseWarnings = parseWarnings;
    }

    public boolean isFinal() {
        return isFinal;
    }

    public static SttEvent fromJson(JSONObject payload) throws JSONException {
        EventJson.requireType(payload, "data.stt");
        List<String> warnings = new ArrayList<>();
        EventJson.warnUnknownKeys(payload, warnings,
                "type", "trace_id", "session_id", "utterance_id", "text", "final", "ts_ms");
        return new SttEvent(
                EventJson.string(payload, "text", warnings),
                EventJson.bool(payload, "final", warnings),
                EventJson.string(payload, "utterance_id", warnings),
                EventJson.string(payload, "trace_id", warnings),
                EventJson.string(payload, "session_id", warnings),
                EventJson.longValue(payload, "ts_ms", warnings),
                EventJson.copy(payload),
                warnings.toArray(new String[0]));
    }
}
