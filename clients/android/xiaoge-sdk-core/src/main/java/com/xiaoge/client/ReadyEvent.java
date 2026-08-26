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
        EventJson.requireOnlyKeys(payload, "type", "trace_id", "session_id", "sample_rate", "granted_caps", "config_version");
        String[] grantedCaps = EventJson.requiredStringArray(payload, "granted_caps");
        ProtocolCodec.validateCaps(java.util.Arrays.asList(grantedCaps));
        return new ReadyEvent(
                EventJson.requiredIntEquals(payload, "sample_rate", ProtocolCodec.SAMPLE_RATE),
                grantedCaps,
                EventJson.requiredString(payload, "config_version"),
                EventJson.requiredString(payload, "trace_id"),
                EventJson.requiredString(payload, "session_id"),
                EventJson.copy(payload));
    }
}
