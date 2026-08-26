package com.xiaoge.client;

import org.json.JSONObject;
import org.junit.Test;

import java.util.Arrays;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertTrue;

public class ProtocolCodecTest {
    @Test
    public void createSessionContainsRequiredFields() throws Exception {
        XiaogeConfig cfg = new XiaogeConfig(
                "https://gateway.test/create_session",
                "robot-x3-001",
                "{\"key_id\":\"dev\",\"signature\":\"mock\"}");
        JSONObject req = new JSONObject(ProtocolCodec.createSessionRequest(cfg));
        assertEquals("robot-x3-001", req.getString("device_id"));
        assertEquals(16000, req.getJSONObject("audio_format").getInt("sample_rate"));
        assertEquals("int16le", req.getJSONObject("audio_format").getString("sample_format"));
    }

    @Test(expected = IllegalArgumentException.class)
    public void duplicateCapsFail() {
        new XiaogeConfig("https://gateway.test/create_session", "robot", "{}",
                java.util.Arrays.asList("audio", "audio"),
                "test", "{}");
    }

    @Test
    public void frameSizeBoundary() throws Exception {
        SessionInfo s = new SessionInfo("trace", "sess", "token", 1000, "ws://host/ws/session",
                Arrays.asList("audio", "text", "cmd", "state"), "cfg");
        String text = ProtocolCodec.frontendState(s, 1, "hint", "awake", "speech", 1000);
        assertTrue(text.getBytes(java.nio.charset.StandardCharsets.UTF_8).length < ProtocolCodec.JSON_MAX_BYTES);
    }

    @Test(expected = IllegalArgumentException.class)
    public void tooLargeJsonFails() throws Exception {
        JSONObject o = new JSONObject()
                .put("type", "data.reply")
                .put("trace_id", "trace")
                .put("session_id", "sess")
                .put("utterance_id", "utt")
                .put("intent_type", "chat")
                .put("ts_ms", 1)
                .put("text", repeat("x", ProtocolCodec.JSON_MAX_BYTES));
        ProtocolCodec.compact(o);
    }

    @Test
    public void parseSessionAndHelloUseGrantedCaps() throws Exception {
        String sessionJson = new JSONObject()
                .put("type", "session.created")
                .put("trace_id", "trace")
                .put("session_id", "sess")
                .put("access_token", "token")
                .put("expires_in_ms", 1000)
                .put("ws_url", "ws://host/ws/session")
                .put("granted_caps", new org.json.JSONArray(Arrays.asList("audio", "text")))
                .put("config_snapshot", new JSONObject().put("config_version", "cfg"))
                .toString();
        SessionInfo s = ProtocolCodec.parseSession(sessionJson);
        XiaogeConfig cfg = new XiaogeConfig("https://gateway.test/create_session", "robot", "{}");
        JSONObject hello = new JSONObject(ProtocolCodec.hello(cfg, s));
        assertEquals(2, hello.getJSONArray("caps").length());
        assertEquals("audio", hello.getJSONArray("caps").getString(0));
        assertEquals("text", hello.getJSONArray("caps").getString(1));
    }

    @Test(expected = IllegalArgumentException.class)
    public void parseSessionRejectsLegacyWsAudio() throws Exception {
        ProtocolCodec.parseSession(sessionJsonWithWsUrl("ws://host/ws/audio"));
    }

    @Test(expected = IllegalArgumentException.class)
    public void parseSessionRejectsPathEndingInWsSession() throws Exception {
        ProtocolCodec.parseSession(sessionJsonWithWsUrl("ws://host/foo/ws/session"));
    }

    @Test(expected = IllegalArgumentException.class)
    public void parseSessionRejectsQueryToken() throws Exception {
        ProtocolCodec.parseSession(sessionJsonWithWsUrl("ws://host/ws/session?" + "access_" + "token=token"));
    }

    private static String sessionJsonWithWsUrl(String wsUrl) throws Exception {
        return new JSONObject()
                .put("type", "session.created")
                .put("trace_id", "trace")
                .put("session_id", "sess")
                .put("access_token", "token")
                .put("expires_in_ms", 1000)
                .put("ws_url", wsUrl)
                .put("granted_caps", new org.json.JSONArray(Arrays.asList("audio", "text")))
                .put("config_snapshot", new JSONObject().put("config_version", "cfg"))
                .toString();
    }

    @Test
    public void fakeExecutorReturnsAckAndResults() throws Exception {
        JSONObject cmd = validCmd("cmd");
        String[] out = new FakeCommandExecutor().execute(cmd);
        assertEquals(3, out.length);
        assertEquals("data.cmd_ack", new JSONObject(out[0]).getString("type"));
        assertEquals(ProtocolCodec.CMD_RESULT_STATUS_SUCCEEDED, new JSONObject(out[2]).getString("status"));
    }

    @Test
    public void fakeExecutorRejectsInvalidDuplicateAndLateCommands() throws Exception {
        FakeCommandExecutor executor = new FakeCommandExecutor();
        JSONObject cmd = validCmd("cmd");
        assertEquals(ProtocolCodec.CMD_ACK_STATUS_ACCEPTED, new JSONObject(executor.execute(cmd)[0]).getString("status"));
        assertEquals(ProtocolCodec.CMD_ACK_STATUS_DUPLICATE, new JSONObject(executor.execute(cmd)[0]).getString("status"));

        JSONObject unsupported = validCmd("cmd-unsupported").put("capability_id", "robot.unknown");
        JSONObject unsupportedAck = new JSONObject(executor.execute(unsupported)[0]);
        assertEquals(ProtocolCodec.CMD_ACK_STATUS_REJECTED, unsupportedAck.getString("status"));
        assertEquals("capability_unsupported", unsupportedAck.getString("code"));

        JSONObject badParams = validCmd("cmd-bad-params")
                .put("params", new JSONObject().put("direction", "sideways").put("distance_cm", 1));
        JSONObject badParamsAck = new JSONObject(executor.execute(badParams)[0]);
        assertEquals(ProtocolCodec.CMD_ACK_STATUS_REJECTED, badParamsAck.getString("status"));
        assertEquals("invalid_params", badParamsAck.getString("code"));

        JSONObject late = validCmd("cmd-late").put("issued_at_ms", 1);
        JSONObject lateAck = new JSONObject(executor.execute(late)[0]);
        assertEquals(ProtocolCodec.CMD_ACK_STATUS_REJECTED, lateAck.getString("status"));
        assertEquals("late_cmd", lateAck.getString("code"));
    }

    @Test
    public void cmdAckAndResultStatusEnumsAreGuarded() throws Exception {
        JSONObject cmd = validCmd("cmd-status");
        for (String status : Arrays.asList(
                ProtocolCodec.CMD_ACK_STATUS_ACCEPTED,
                ProtocolCodec.CMD_ACK_STATUS_REJECTED,
                ProtocolCodec.CMD_ACK_STATUS_DUPLICATE)) {
            assertEquals(status, new JSONObject(ProtocolCodec.cmdAck(cmd, status, "ok")).getString("status"));
        }
        for (String status : Arrays.asList(
                ProtocolCodec.CMD_RESULT_STATUS_RUNNING,
                ProtocolCodec.CMD_RESULT_STATUS_SUCCEEDED,
                ProtocolCodec.CMD_RESULT_STATUS_FAILED,
                ProtocolCodec.CMD_RESULT_STATUS_CANCELED,
                ProtocolCodec.CMD_RESULT_STATUS_TIMEOUT)) {
            assertEquals(status, new JSONObject(ProtocolCodec.cmdResult(cmd, status, "ok")).getString("status"));
        }
    }

    @Test(expected = IllegalArgumentException.class)
    public void invalidCmdAckStatusFails() throws Exception {
        ProtocolCodec.cmdAck(validCmd("cmd-bad-ack-status"), "unknown", "bad");
    }

    @Test(expected = IllegalArgumentException.class)
    public void invalidCmdResultStatusFails() throws Exception {
        ProtocolCodec.cmdResult(validCmd("cmd-bad-result-status"), "done", "bad");
    }

    @Test(expected = org.json.JSONException.class)
    public void readyRejectsWrongSampleRate() throws Exception {
        ReadyEvent.fromJson(validReady().put("sample_rate", 8000));
    }

    @Test(expected = org.json.JSONException.class)
    public void readyRejectsExtraField() throws Exception {
        ReadyEvent.fromJson(validReady().put("extra", "forbidden"));
    }

    @Test(expected = org.json.JSONException.class)
    public void commandRejectsZeroAckTimeout() throws Exception {
        CommandEvent.fromJson(validCmd("cmd-zero-ack").put("ack_timeout_ms", 0));
    }

    @Test(expected = org.json.JSONException.class)
    public void commandRejectsZeroResultTimeout() throws Exception {
        CommandEvent.fromJson(validCmd("cmd-zero-result").put("result_timeout_ms", 0));
    }

    @Test(expected = org.json.JSONException.class)
    public void commandRejectsNegativeIssuedAt() throws Exception {
        CommandEvent.fromJson(validCmd("cmd-negative-issued").put("issued_at_ms", -1));
    }

    @Test(expected = org.json.JSONException.class)
    public void sttRejectsExtraField() throws Exception {
        SttEvent.fromJson(new JSONObject()
                .put("type", "data.stt")
                .put("trace_id", "trace")
                .put("session_id", "sess")
                .put("utterance_id", "utt")
                .put("text", "hello")
                .put("final", true)
                .put("ts_ms", 1)
                .put("extra", "forbidden"));
    }

    @Test(expected = org.json.JSONException.class)
    public void parseSessionRejectsExtraField() throws Exception {
        ProtocolCodec.parseSession(new JSONObject(sessionJsonWithWsUrl("ws://host/ws/session"))
                .put("extra", "forbidden")
                .toString());
    }

    private static JSONObject validReady() throws Exception {
        return new JSONObject()
                .put("type", "ctrl.ready")
                .put("trace_id", "trace")
                .put("session_id", "sess")
                .put("sample_rate", ProtocolCodec.SAMPLE_RATE)
                .put("granted_caps", new org.json.JSONArray(Arrays.asList("audio", "text")))
                .put("config_version", "cfg");
    }

    private static JSONObject validCmd(String cmdId) throws Exception {
        return new JSONObject()
                .put("type", "data.cmd")
                .put("trace_id", "trace")
                .put("session_id", "sess")
                .put("utterance_id", "utt")
                .put("cmd_id", cmdId)
                .put("capability_id", "motion.move")
                .put("action", "navigation.move")
                .put("params", new JSONObject().put("direction", "forward").put("distance_cm", 1))
                .put("risk_level", "medium")
                .put("ack_timeout_ms", 800)
                .put("result_timeout_ms", 5000)
                .put("issued_at_ms", System.currentTimeMillis());
    }

    private static String repeat(String value, int count) {
        StringBuilder out = new StringBuilder(value.length() * count);
        for (int i = 0; i < count; i++) {
            out.append(value);
        }
        return out.toString();
    }
}
