package com.xiaoge.client;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.net.URI;
import java.net.URISyntaxException;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Set;

public final class ProtocolCodec {
    public static final int SAMPLE_RATE = 16000;
    public static final int JSON_MAX_BYTES = 8192;
    public static final int BINARY_MAX_BYTES = 32768;
    public static final String MANIFEST_SHA256 =
            "845F0F4125061FF37A7F4DA20E0C88BC089200A08B319F1035D6522C80B56559";
    public static final String CMD_ACK_STATUS_ACCEPTED = "accepted";
    public static final String CMD_ACK_STATUS_REJECTED = "rejected";
    public static final String CMD_ACK_STATUS_DUPLICATE = "duplicate";
    public static final String CMD_RESULT_STATUS_RUNNING = "running";
    public static final String CMD_RESULT_STATUS_SUCCEEDED = "succeeded";
    public static final String CMD_RESULT_STATUS_FAILED = "failed";
    public static final String CMD_RESULT_STATUS_CANCELED = "canceled";
    public static final String CMD_RESULT_STATUS_TIMEOUT = "timeout";

    private ProtocolCodec() {}

    public static void validateCaps(List<String> caps) {
        Set<String> seen = new HashSet<>();
        for (String cap : caps) {
            if (!cap.equals("audio") && !cap.equals("text") && !cap.equals("cmd") && !cap.equals("state")) {
                throw new IllegalArgumentException("unknown cap: " + cap);
            }
            if (!seen.add(cap)) {
                throw new IllegalArgumentException("duplicate cap: " + cap);
            }
        }
        if (seen.isEmpty()) {
            throw new IllegalArgumentException("caps must not be empty");
        }
    }

    public static String createSessionRequest(XiaogeConfig cfg) throws JSONException {
        JSONObject audio = new JSONObject()
                .put("sample_rate", SAMPLE_RATE)
                .put("channels", 1)
                .put("sample_format", "int16le");
        return compact(new JSONObject()
                .put("device_id", cfg.deviceId)
                .put("credential", parseJsonValue(cfg.credentialJson))
                .put("caps", new JSONArray(cfg.caps))
                .put("prefs", new JSONObject(cfg.prefsJson))
                .put("audio_format", audio)
                .put("client_version", cfg.clientVersion));
    }

    public static SessionInfo parseSession(String json) throws JSONException {
        JSONObject o = new JSONObject(json);
        EventJson.requireOnlyKeys(o, "type", "trace_id", "session_id", "access_token", "expires_in_ms", "ws_url",
                "granted_caps", "config_snapshot");
        if (!o.getString("type").equals("session.created")) {
            throw new IllegalArgumentException("create_session response type must be session.created");
        }
        long expiresInMs = o.getLong("expires_in_ms");
        if (expiresInMs <= 0) {
            throw new IllegalArgumentException("expires_in_ms must be positive");
        }
        String wsUrl = o.getString("ws_url");
        URI wsUri;
        try {
            wsUri = new URI(wsUrl);
        } catch (URISyntaxException e) {
            throw new IllegalArgumentException("ws_url must be a valid URI", e);
        }
        if (!"/ws/session".equals(wsUri.getPath()) || wsUri.getQuery() != null || wsUri.getFragment() != null) {
            throw new IllegalArgumentException("ws_url must point exactly to /ws/session without query or fragment");
        }
        List<String> grantedCaps = jsonArrayToList(o.getJSONArray("granted_caps"));
        validateCaps(grantedCaps);
        JSONObject snapshot = o.getJSONObject("config_snapshot");
        return new SessionInfo(
                o.getString("trace_id"),
                o.getString("session_id"),
                o.getString("access_token"),
                expiresInMs,
                wsUrl,
                grantedCaps,
                snapshot.getString("config_version"));
    }

    public static String hello(XiaogeConfig cfg, SessionInfo s) throws JSONException {
        return compact(new JSONObject()
                .put("type", "ctrl.hello")
                .put("trace_id", s.traceId)
                .put("session_id", s.sessionId)
                .put("proto", 2)
                .put("role", "device")
                .put("device_id", cfg.deviceId)
                .put("caps", new JSONArray(s.grantedCaps)));
    }

    public static String frontendState(SessionInfo s, int seq, String trustLevel,
                                       String wakeState, String vad, int ttlMs) throws JSONException {
        if (seq < 0) {
            throw new IllegalArgumentException("seq must be >= 0");
        }
        if (ttlMs <= 0) {
            throw new IllegalArgumentException("ttl_ms must be positive");
        }
        if (!trustLevel.equals("authoritative") && !trustLevel.equals("hint") && !trustLevel.equals("observe")) {
            throw new IllegalArgumentException("invalid trust_level");
        }
        return compact(new JSONObject()
                .put("type", "ctrl.frontend_state")
                .put("trace_id", s.traceId)
                .put("session_id", s.sessionId)
                .put("seq", seq)
                .put("ts_ms", System.currentTimeMillis())
                .put("ttl_ms", ttlMs)
                .put("trust_level", trustLevel)
                .put("wake_state", wakeState == null ? "unknown" : wakeState)
                .put("vad", vad == null ? "unknown" : vad));
    }

    public static String cmdAck(JSONObject cmd, String status, String code) throws JSONException {
        return cmdAck(cmd, status, code, status);
    }

    public static String cmdAck(JSONObject cmd, String status, String code, String message) throws JSONException {
        if (!status.equals(CMD_ACK_STATUS_ACCEPTED)
                && !status.equals(CMD_ACK_STATUS_REJECTED)
                && !status.equals(CMD_ACK_STATUS_DUPLICATE)) {
            throw new IllegalArgumentException("invalid cmd_ack status");
        }
        return compact(commandBase(cmd)
                .put("type", "data.cmd_ack")
                .put("status", status)
                .put("code", code)
                .put("message", message)
                .put("received_at_ms", System.currentTimeMillis()));
    }

    public static String cmdAck(CommandEvent event, String status, String code, String message) throws JSONException {
        return cmdAck(commandContext(event), status, code, message);
    }

    public static String cmdResult(JSONObject cmd, String status, String code) throws JSONException {
        return cmdResult(cmd, status, code, status, null);
    }

    public static String cmdResult(JSONObject cmd, String status, String code, String message, Boolean retryable)
            throws JSONException {
        if (!status.equals(CMD_RESULT_STATUS_RUNNING)
                && !status.equals(CMD_RESULT_STATUS_SUCCEEDED)
                && !status.equals(CMD_RESULT_STATUS_FAILED)
                && !status.equals(CMD_RESULT_STATUS_CANCELED)
                && !status.equals(CMD_RESULT_STATUS_TIMEOUT)) {
            throw new IllegalArgumentException("invalid cmd_result status");
        }
        boolean shouldRetry = retryable != null
                ? retryable
                : status.equals(CMD_RESULT_STATUS_FAILED) || status.equals(CMD_RESULT_STATUS_TIMEOUT);
        return compact(commandBase(cmd)
                .put("type", "data.cmd_result")
                .put("status", status)
                .put("code", code)
                .put("message", message)
                .put("retryable", shouldRetry));
    }

    public static String cmdResult(CommandEvent event, String status, String code, String message, Boolean retryable)
            throws JSONException {
        return cmdResult(commandContext(event), status, code, message, retryable);
    }

    private static JSONObject commandBase(JSONObject cmd) throws JSONException {
        return new JSONObject()
                .put("trace_id", cmd.getString("trace_id"))
                .put("session_id", cmd.getString("session_id"))
                .put("utterance_id", cmd.getString("utterance_id"))
                .put("cmd_id", cmd.getString("cmd_id"));
    }

    private static JSONObject commandContext(CommandEvent event) throws JSONException {
        return new JSONObject()
                .put("trace_id", event.traceId)
                .put("session_id", event.sessionId)
                .put("utterance_id", event.utteranceId)
                .put("cmd_id", event.cmdId);
    }

    public static String compact(JSONObject object) {
        String text = object.toString();
        int size = text.getBytes(StandardCharsets.UTF_8).length;
        if (size > JSON_MAX_BYTES) {
            throw new IllegalArgumentException("JSON frame exceeds 8192 bytes");
        }
        return text;
    }

    private static Object parseJsonValue(String raw) throws JSONException {
        String text = raw == null ? "" : raw.trim();
        if (text.startsWith("{")) {
            return new JSONObject(text);
        }
        if (text.startsWith("[")) {
            return new JSONArray(text);
        }
        return text;
    }

    private static List<String> jsonArrayToList(JSONArray array) throws JSONException {
        List<String> out = new ArrayList<>();
        for (int i = 0; i < array.length(); i++) {
            out.add(array.getString(i));
        }
        return out;
    }
}
