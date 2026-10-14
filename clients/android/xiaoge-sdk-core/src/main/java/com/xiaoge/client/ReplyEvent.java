package com.xiaoge.client;

import org.json.JSONException;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.List;
import java.util.Set;

public final class ReplyEvent {
    private static final Set<String> KNOWN_INTENT_TYPES = EventJson.set(
            "control_cmd", "info_query", "knowledge_qa", "chat", "config", "system");
    private static final Set<String> KNOWN_SPEAK_POLICIES = EventJson.set(
            "silent", "ack", "ack_then_result", "final_only");

    public final String text;
    public final boolean isFinal;
    public final String utteranceId;
    public final String intentType;
    public final String speakPolicy;
    public final String traceId;
    public final String sessionId;
    public final long tsMs;
    public final JSONObject raw;
    /** Field-level deviations observed while parsing; empty when the payload fully matches R5.2.2. */
    public final String[] parseWarnings;

    private ReplyEvent(String text, boolean isFinal, String utteranceId, String intentType,
                       String speakPolicy, String traceId, String sessionId, long tsMs,
                       JSONObject raw, String[] parseWarnings) {
        this.text = text;
        this.isFinal = isFinal;
        this.utteranceId = utteranceId;
        this.intentType = intentType;
        this.speakPolicy = speakPolicy;
        this.traceId = traceId;
        this.sessionId = sessionId;
        this.tsMs = tsMs;
        this.raw = raw;
        this.parseWarnings = parseWarnings;
    }

    public boolean isFinal() {
        return isFinal;
    }

    public static ReplyEvent fromJson(JSONObject payload) throws JSONException {
        EventJson.requireType(payload, "data.reply");
        List<String> warnings = new ArrayList<>();
        EventJson.warnUnknownKeys(payload, warnings, "type", "trace_id", "session_id", "utterance_id",
                "intent_type", "text", "ts_ms", "speak_policy");
        return new ReplyEvent(
                EventJson.string(payload, "text", warnings),
                true,
                EventJson.string(payload, "utterance_id", warnings),
                EventJson.enumValue(payload, "intent_type", KNOWN_INTENT_TYPES, warnings),
                EventJson.optionalEnum(payload, "speak_policy", KNOWN_SPEAK_POLICIES, warnings),
                EventJson.string(payload, "trace_id", warnings),
                EventJson.string(payload, "session_id", warnings),
                EventJson.longValue(payload, "ts_ms", warnings),
                EventJson.copy(payload),
                warnings.toArray(new String[0]));
    }
}
