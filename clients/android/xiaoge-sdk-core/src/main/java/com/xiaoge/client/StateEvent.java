package com.xiaoge.client;

import org.json.JSONException;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.List;
import java.util.Set;

public final class StateEvent {
    private static final Set<String> KNOWN_LINK_STATES = EventJson.set(
            "connecting", "connected", "reconnecting", "closed");
    private static final Set<String> KNOWN_INTERACTION_MODES = EventJson.set(
            "sleeping", "dialogue", "listening");
    private static final Set<String> KNOWN_ENGINE_GATES = EventJson.set(
            "closed", "open", "kws_only");
    private static final Set<String> KNOWN_RESOURCE_STATES = EventJson.set(
            "SleepingHot", "SleepingWarm", "ActiveAgent", "ReleasedIdle", "PendingReconnect");

    public final String linkState;
    public final String interactionMode;
    public final String engineGate;
    public final String resourceState;
    public final long tsMs;
    public final String traceId;
    public final String sessionId;
    public final JSONObject pendingConfirmation;
    public final JSONObject raw;
    /** Field-level deviations observed while parsing; empty when the payload fully matches R5.2.2. */
    public final String[] parseWarnings;

    private StateEvent(String linkState, String interactionMode, String engineGate, String resourceState,
                       long tsMs, String traceId, String sessionId,
                       JSONObject pendingConfirmation, JSONObject raw, String[] parseWarnings) {
        this.linkState = linkState;
        this.interactionMode = interactionMode;
        this.engineGate = engineGate;
        this.resourceState = resourceState;
        this.tsMs = tsMs;
        this.traceId = traceId;
        this.sessionId = sessionId;
        this.pendingConfirmation = pendingConfirmation;
        this.raw = raw;
        this.parseWarnings = parseWarnings;
    }

    public static StateEvent fromJson(JSONObject payload) throws JSONException {
        EventJson.requireType(payload, "ctrl.state");
        List<String> warnings = new ArrayList<>();
        EventJson.warnUnknownKeys(payload, warnings, "type", "trace_id", "session_id", "link_state",
                "interaction_mode", "engine_gate", "resource_state", "ts_ms", "pending_confirmation");
        return new StateEvent(
                EventJson.enumValue(payload, "link_state", KNOWN_LINK_STATES, warnings),
                EventJson.enumValue(payload, "interaction_mode", KNOWN_INTERACTION_MODES, warnings),
                EventJson.enumValue(payload, "engine_gate", KNOWN_ENGINE_GATES, warnings),
                EventJson.enumValue(payload, "resource_state", KNOWN_RESOURCE_STATES, warnings),
                EventJson.longValue(payload, "ts_ms", warnings),
                EventJson.string(payload, "trace_id", warnings),
                EventJson.string(payload, "session_id", warnings),
                EventJson.optionalObject(payload, "pending_confirmation", warnings),
                EventJson.copy(payload),
                warnings.toArray(new String[0]));
    }
}
