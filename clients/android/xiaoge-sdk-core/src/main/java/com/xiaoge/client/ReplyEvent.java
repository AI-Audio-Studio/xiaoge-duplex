package com.xiaoge.client;

import org.json.JSONException;
import org.json.JSONObject;

import java.util.Set;

public final class ReplyEvent {
    private static final Set<String> VALID_INTENT_TYPES = EventJson.set(
            "control_cmd", "info_query", "knowledge_qa", "chat", "config", "system");
    private static final Set<String> VALID_SPEAK_POLICIES = EventJson.set(
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

    private ReplyEvent(String text, boolean isFinal, String utteranceId, String intentType,
                       String speakPolicy, String traceId, String sessionId, long tsMs, JSONObject raw) {
        this.text = text;
        this.isFinal = isFinal;
        this.utteranceId = utteranceId;
        this.intentType = intentType;
        this.speakPolicy = speakPolicy;
        this.traceId = traceId;
        this.sessionId = sessionId;
        this.tsMs = tsMs;
        this.raw = raw;
    }

    public boolean isFinal() {
        return isFinal;
    }

    public static ReplyEvent fromJson(JSONObject payload) throws JSONException {
        EventJson.requireType(payload, "data.reply");
        EventJson.requireOnlyKeys(payload, "type", "trace_id", "session_id", "utterance_id", "intent_type", "text",
                "ts_ms", "speak_policy");
        return new ReplyEvent(
                EventJson.requiredString(payload, "text"),
                true,
                EventJson.requiredString(payload, "utterance_id"),
                EventJson.requiredEnum(payload, "intent_type", VALID_INTENT_TYPES),
                EventJson.optionalEnum(payload, "speak_policy", VALID_SPEAK_POLICIES),
                EventJson.requiredString(payload, "trace_id"),
                EventJson.requiredString(payload, "session_id"),
                EventJson.requiredLong(payload, "ts_ms"),
                EventJson.copy(payload));
    }
}
