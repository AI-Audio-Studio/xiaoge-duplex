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
    public void duplexStateEventParsesFieldsAndWarnsOnMissingEnabled() throws Exception {
        JSONObject payload = new JSONObject()
                .put("type", "ctrl.duplex_state")
                .put("trace_id", "trace")
                .put("session_id", "sess")
                .put("enabled", false)
                .put("ts_ms", 1790000001000L);
        DuplexStateEvent event = DuplexStateEvent.fromJson(payload);
        assertFalse(event.enabled);
        assertEquals(1790000001000L, event.tsMs);
        assertEquals("trace", event.traceId);
        assertEquals("sess", event.sessionId);
        assertEquals(0, event.parseWarnings.length);

        DuplexStateEvent missing = DuplexStateEvent.fromJson(
                new JSONObject("{\"type\":\"ctrl.duplex_state\",\"trace_id\":\"trace\","
                        + "\"session_id\":\"sess\",\"ts_ms\":1}"));
        assertFalse(missing.enabled);
        assertTrue(contains(missing.parseWarnings, "missing field: enabled"));
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
        assertFalse(result.has("result"));

        JSONObject payload = new JSONObject("{" + "\"type\":\"cap\",\"value\":40,\"gauge_id\":1" + "}");
        JSONObject withResult = new JSONObject(ProtocolCodec.cmdResult(
                event,
                ProtocolCodec.CMD_RESULT_STATUS_SUCCEEDED,
                "ok",
                "completed",
                false,
                payload));
        assertEquals(40, withResult.getJSONObject("result").getInt("value"));
        assertEquals("cap", withResult.getJSONObject("result").getString("type"));
        assertEquals(1, withResult.getJSONObject("result").getInt("gauge_id"));
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

    @Test
    public void stateEventFieldsMatchWireConstants() throws Exception {
        StateEvent event = StateEvent.fromJson(validState());
        assertEquals(ProtocolCodec.LINK_STATE_CONNECTED, event.linkState);
        assertEquals(ProtocolCodec.INTERACTION_MODE_DIALOGUE, event.interactionMode);
        assertEquals(ProtocolCodec.ENGINE_GATE_OPEN, event.engineGate);
        assertEquals(ProtocolCodec.RESOURCE_STATE_ACTIVE_AGENT, event.resourceState);
        assertEquals(0, event.parseWarnings.length);
    }

    @Test
    public void stateEventUnknownEnumPassesThrough() throws Exception {
        StateEvent event = StateEvent.fromJson(validState().put("interaction_mode", "dozing"));
        assertEquals("dozing", event.interactionMode);
        assertTrue(contains(event.parseWarnings, "unknown interaction_mode: dozing"));
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

    private static JSONObject validState() throws Exception {
        return new JSONObject()
                .put("type", "ctrl.state")
                .put("trace_id", "trace")
                .put("session_id", "sess")
                .put("link_state", "connected")
                .put("interaction_mode", "dialogue")
                .put("engine_gate", "open")
                .put("resource_state", "ActiveAgent")
                .put("ts_ms", 1790000000000L);
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
