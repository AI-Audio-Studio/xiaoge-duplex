package com.xiaoge.client;

import org.json.JSONException;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.List;

/** Ack for {@code ctrl.duplex}: the switch update was scheduled, not necessarily applied yet. */
public final class DuplexStateEvent {
    public final boolean enabled;
    public final long tsMs;
    public final String traceId;
    public final String sessionId;
    public final JSONObject raw;
    /** Field-level deviations observed while parsing; empty when the payload fully matches. */
    public final String[] parseWarnings;

    private DuplexStateEvent(boolean enabled, long tsMs, String traceId, String sessionId,
                             JSONObject raw, String[] parseWarnings) {
        this.enabled = enabled;
        this.tsMs = tsMs;
        this.traceId = traceId;
        this.sessionId = sessionId;
        this.raw = raw;
        this.parseWarnings = parseWarnings;
    }

    public static DuplexStateEvent fromJson(JSONObject payload) throws JSONException {
        EventJson.requireType(payload, "ctrl.duplex_state");
        List<String> warnings = new ArrayList<>();
        EventJson.warnUnknownKeys(payload, warnings, "type", "trace_id", "session_id", "enabled", "ts_ms");
        return new DuplexStateEvent(
                EventJson.bool(payload, "enabled", warnings),
                EventJson.longValue(payload, "ts_ms", warnings),
                EventJson.string(payload, "trace_id", warnings),
                EventJson.string(payload, "session_id", warnings),
                EventJson.copy(payload),
                warnings.toArray(new String[0]));
    }
}
