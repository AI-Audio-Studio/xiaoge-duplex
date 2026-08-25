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

    @Test(expected = org.json.JSONException.class)
    public void invalidSttFinalDoesNotBuildEvent() throws Exception {
        SttEvent.fromJson(new JSONObject()
                .put("type", "data.stt")
                .put("trace_id", "trace")
                .put("session_id", "sess")
                .put("utterance_id", "utt")
                .put("text", "bad")
                .put("final", "true")
                .put("ts_ms", 1));
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
