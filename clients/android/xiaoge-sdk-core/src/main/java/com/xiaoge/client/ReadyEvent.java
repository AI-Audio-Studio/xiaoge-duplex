package com.xiaoge.client;

import org.json.JSONException;
import org.json.JSONObject;

public final class ReadyEvent {
    public final int sampleRate;
    public final String[] grantedCaps;
    public final String configVersion;
    public final String traceId;
    public final String sessionId;
    public final JSONObject raw;

    private ReadyEvent(int sampleRate, String[] grantedCaps, String configVersion,
                       String traceId, String sessionId, JSONObject raw) {
        this.sampleRate = sampleRate;
        this.grantedCaps = grantedCaps;
        this.configVersion = configVersion;
        this.traceId = traceId;
        this.sessionId = sessionId;
        this.raw = raw;
    }

    public static ReadyEvent fromJson(JSONObject payload) throws JSONException {
        EventJson.requireType(payload, "ctrl.ready");
        return new ReadyEvent(
                EventJson.requiredInt(payload, "sample_rate"),
                EventJson.optionalStringArray(payload, "granted_caps"),
                EventJson.optionalString(payload, "config_version"),
                EventJson.requiredString(payload, "trace_id"),
                EventJson.requiredString(payload, "session_id"),
                EventJson.copy(payload));
    }
}
