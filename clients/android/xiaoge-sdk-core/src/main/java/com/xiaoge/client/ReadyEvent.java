package com.xiaoge.client;

import org.json.JSONException;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.List;

public final class ReadyEvent {
    public final int sampleRate;
    public final String[] grantedCaps;
    public final String configVersion;
    public final String traceId;
    public final String sessionId;
    public final JSONObject raw;
    /** Field-level deviations observed while parsing; empty when the payload fully matches R5.2.2. */
    public final String[] parseWarnings;

    private ReadyEvent(int sampleRate, String[] grantedCaps, String configVersion,
                       String traceId, String sessionId, JSONObject raw, String[] parseWarnings) {
        this.sampleRate = sampleRate;
        this.grantedCaps = grantedCaps;
        this.configVersion = configVersion;
        this.traceId = traceId;
        this.sessionId = sessionId;
        this.raw = raw;
        this.parseWarnings = parseWarnings;
    }

    public static ReadyEvent fromJson(JSONObject payload) throws JSONException {
        EventJson.requireType(payload, "ctrl.ready");
        List<String> warnings = new ArrayList<>();
        EventJson.warnUnknownKeys(payload, warnings,
                "type", "trace_id", "session_id", "sample_rate", "granted_caps", "config_version");
        int sampleRate = EventJson.intOrDefault(payload, "sample_rate", ProtocolCodec.SAMPLE_RATE, warnings);
        if (sampleRate != ProtocolCodec.SAMPLE_RATE) {
            warnings.add("sample_rate is " + sampleRate + ", expected " + ProtocolCodec.SAMPLE_RATE);
        }
        return new ReadyEvent(
                sampleRate,
                EventJson.stringArray(payload, "granted_caps", warnings),
                EventJson.string(payload, "config_version", warnings),
                EventJson.string(payload, "trace_id", warnings),
                EventJson.string(payload, "session_id", warnings),
                EventJson.copy(payload),
                warnings.toArray(new String[0]));
    }
}
