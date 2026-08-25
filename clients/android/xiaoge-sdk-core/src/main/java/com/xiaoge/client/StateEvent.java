package com.xiaoge.client;

import org.json.JSONException;
import org.json.JSONObject;

import java.util.Set;

public final class StateEvent {
    private static final Set<String> VALID_LINK_STATES = EventJson.set(
            "connecting", "connected", "reconnecting", "closed");
    private static final Set<String> VALID_INTERACTION_MODES = EventJson.set(
            "sleeping", "dialogue", "listening");
    private static final Set<String> VALID_ENGINE_GATES = EventJson.set(
            "closed", "open", "kws_only");
    private static final Set<String> VALID_RESOURCE_STATES = EventJson.set(
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

    private StateEvent(String linkState, String interactionMode, String engineGate, String resourceState,
                       long tsMs, String traceId, String sessionId,
                       JSONObject pendingConfirmation, JSONObject raw) {
        this.linkState = linkState;
        this.interactionMode = interactionMode;
        this.engineGate = engineGate;
        this.resourceState = resourceState;
        this.tsMs = tsMs;
        this.traceId = traceId;
        this.sessionId = sessionId;
        this.pendingConfirmation = pendingConfirmation;
        this.raw = raw;
    }

    public static StateEvent fromJson(JSONObject payload) throws JSONException {
        EventJson.requireType(payload, "ctrl.state");
        JSONObject pending = null;
        if (payload.has("pending_confirmation") && !payload.isNull("pending_confirmation")) {
            pending = EventJson.copy(payload.getJSONObject("pending_confirmation"));
        }
        return new StateEvent(
                EventJson.requiredEnum(payload, "link_state", VALID_LINK_STATES),
                EventJson.requiredEnum(payload, "interaction_mode", VALID_INTERACTION_MODES),
                EventJson.requiredEnum(payload, "engine_gate", VALID_ENGINE_GATES),
                EventJson.requiredEnum(payload, "resource_state", VALID_RESOURCE_STATES),
                EventJson.requiredLong(payload, "ts_ms"),
                EventJson.requiredString(payload, "trace_id"),
                EventJson.requiredString(payload, "session_id"),
                pending,
                EventJson.copy(payload));
    }
}
