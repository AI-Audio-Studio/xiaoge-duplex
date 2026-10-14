package com.xiaoge.client;

import org.json.JSONObject;
import org.junit.Test;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

public class EventCodecTest {
    @Test
    public void sttEventCopiesPayloadBeforeRawMutation() throws Exception {
        JSONObject payload = new JSONObject()
                .put("type", "data.stt")
                .put("trace_id", "trace")
                .put("session_id", "sess")
                .put("utterance_id", "utt")
                .put("text", "往前走一米")
                .put("final", true)
                .put("ts_ms", 1789000000200L);
        SttEvent event = SttEvent.fromJson(payload);
        payload.put("text", "mutated");
        assertEquals("往前走一米", event.text);
        assertEquals("往前走一米", event.raw.getString("text"));
        assertTrue(event.isFinal);
    }

    @Test
    public void replyEventDerivesFinalWithoutMutatingRaw() throws Exception {
        JSONObject payload = new JSONObject()
                .put("type", "data.reply")
                .put("trace_id", "trace")
                .put("session_id", "sess")
                .put("utterance_id", "utt")
                .put("intent_type", "control_cmd")
                .put("speak_policy", "ack_then_result")
                .put("text", "好的，正在执行。")
                .put("ts_ms", 1789000000300L);
        ReplyEvent event = ReplyEvent.fromJson(payload);
        assertTrue(event.isFinal);
        assertFalse(event.raw.has("final"));
        assertEquals("control_cmd", event.intentType);
        assertEquals("ack_then_result", event.speakPolicy);
    }

    @Test
    public void commandEventCanBuildAckAndResult() throws Exception {
        CommandEvent event = CommandEvent.fromJson(validCommand());
        JSONObject ack = new JSONObject(ProtocolCodec.cmdAck(
                event,
                ProtocolCodec.CMD_ACK_STATUS_ACCEPTED,
                "ok",
                "queued"));
        assertEquals("data.cmd_ack", ack.getString("type"));
        assertEquals("cmd-1", ack.getString("cmd_id"));
        assertEquals("queued", ack.getString("message"));

        JSONObject result = new JSONObject(ProtocolCodec.cmdResult(
                event,
                ProtocolCodec.CMD_RESULT_STATUS_SUCCEEDED,
                "ok",
                "completed",
                false));
        assertEquals("data.cmd_result", result.getString("type"));
        assertEquals("cmd-1", result.getString("cmd_id"));
        assertFalse(result.getBoolean("retryable"));
    }

    @Test
    public void sttCoercesStringFinalValue() throws Exception {
        SttEvent event = SttEvent.fromJson(validStt()
                .put("final", "true"));
        assertTrue(event.isFinal);
        assertTrue(contains(event.parseWarnings, "field is coerced from string: final"));
    }

    @Test
    public void compliantPayloadHasNoParseWarnings() throws Exception {
        ReplyEvent event = ReplyEvent.fromJson(validReply());
        assertEquals(0, event.parseWarnings.length);
        SttEvent stt = SttEvent.fromJson(validStt());
        assertEquals(0, stt.parseWarnings.length);
    }

    @Test
    public void missingFieldsFallBackToDefaults() throws Exception {
        SttEvent event = SttEvent.fromJson(new JSONObject()
                .put("type", "data.stt")
                .put("trace_id", "trace"));
        assertEquals("", event.text);
        assertFalse(event.isFinal);
        assertEquals("", event.utteranceId);
        assertEquals(0L, event.tsMs);
        assertTrue(contains(event.parseWarnings, "missing field: text"));
        assertTrue(contains(event.parseWarnings, "missing field: final"));
        assertTrue(contains(event.parseWarnings, "missing field: session_id"));
    }

    @Test
    public void unknownIntentTypePassesThrough() throws Exception {
        ReplyEvent event = ReplyEvent.fromJson(validReply().put("intent_type", "control_cmd_multi"));
        assertEquals("control_cmd_multi", event.intentType);
        assertTrue(contains(event.parseWarnings, "unknown intent_type: control_cmd_multi"));
    }

    @Test
    public void stringTsMsIsCoerced() throws Exception {
        SttEvent event = SttEvent.fromJson(validStt().put("ts_ms", "1789000000200"));
        assertEquals(1789000000200L, event.tsMs);
        assertTrue(contains(event.parseWarnings, "field is coerced from string: ts_ms"));
    }

    @Test
    public void missingParamsFallsBackToEmptyObject() throws Exception {
        JSONObject payload = validCommand();
        payload.remove("params");
        CommandEvent event = CommandEvent.fromJson(payload);
        assertEquals(0, event.params.length());
        assertTrue(contains(event.parseWarnings, "missing field: params"));
    }

    @Test
    public void missingGrantedCapsFallsBackToEmptyArray() throws Exception {
        JSONObject payload = validReady();
        payload.remove("granted_caps");
        ReadyEvent event = ReadyEvent.fromJson(payload);
        assertEquals(0, event.grantedCaps.length);
        assertTrue(contains(event.parseWarnings, "missing field: granted_caps"));
    }

    private static JSONObject validStt() throws Exception {
        return new JSONObject()
                .put("type", "data.stt")
                .put("trace_id", "trace")
                .put("session_id", "sess")
                .put("utterance_id", "utt")
                .put("text", "往前走一米")
                .put("final", true)
                .put("ts_ms", 1789000000200L);
    }

    private static JSONObject validReply() throws Exception {
        return new JSONObject()
                .put("type", "data.reply")
                .put("trace_id", "trace")
                .put("session_id", "sess")
                .put("utterance_id", "utt")
                .put("intent_type", "control_cmd")
                .put("speak_policy", "ack_then_result")
                .put("text", "好的，正在执行。")
                .put("ts_ms", 1789000000300L);
    }

    private static JSONObject validReady() throws Exception {
        return new JSONObject()
                .put("type", "ctrl.ready")
                .put("trace_id", "trace")
                .put("session_id", "sess")
                .put("sample_rate", ProtocolCodec.SAMPLE_RATE)
                .put("granted_caps", new org.json.JSONArray(java.util.Arrays.asList("audio", "text")))
                .put("config_version", "cfg");
    }

    private static boolean contains(String[] values, String expected) {
        for (String value : values) {
            if (expected.equals(value)) {
                return true;
            }
        }
        return false;
    }

    private static JSONObject validCommand() throws Exception {
        return new JSONObject()
                .put("type", "data.cmd")
                .put("trace_id", "trace")
                .put("session_id", "sess")
                .put("utterance_id", "utt")
                .put("cmd_id", "cmd-1")
                .put("capability_id", "motion.move")
                .put("action", "navigation.move")
                .put("params", new JSONObject().put("direction", "forward").put("distance_cm", 1))
                .put("risk_level", "medium")
                .put("ack_timeout_ms", 800)
                .put("result_timeout_ms", 5000)
                .put("issued_at_ms", 1789000000100L);
    }
}
