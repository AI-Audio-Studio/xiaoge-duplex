#include "xiaoge_client.h"

#include <stdio.h>
#include <string.h>

int xiaoge_test_json_cloud_cmd_compatible(const char *cmd_json);
int xiaoge_test_build_cmd_ack_json(const char *cmd_json, const char *status, const char *code,
                                   const char *message, char *out, size_t out_len);
int xiaoge_test_build_cmd_result_json(const char *cmd_json, const char *status, const char *code,
                                      const char *message, int retryable, long long started_at_ms,
                                      long long finished_at_ms, long long duration_ms, char *out,
                                      size_t out_len);
int xiaoge_test_auto_fake_cmd_enabled(int value);
int xiaoge_test_build_stt_event(const char *json, xiaoge_stt_event *event, char *text, char *utt,
                                char *trace, char *session);
int xiaoge_test_build_reply_event(const char *json, xiaoge_reply_event *event, char *text, char *utt,
                                  char *intent, char *speak, char *trace, char *session);
int xiaoge_test_build_error_event(const char *json, xiaoge_error_event *event, char *code, char *message,
                                  char *trace, char *session);
int xiaoge_test_build_cmd_ack_event_json(const xiaoge_command_event *event, const char *status, const char *code,
                                         const char *message, char *out, size_t out_len);

static void test_ready_callback(const xiaoge_ready_event *event, void *user) {
    (void)event;
    (void)user;
}

static void test_audio_callback(const void *pcm, size_t len, void *user) {
    (void)pcm;
    (void)len;
    (void)user;
}

static void test_clear_callback(const xiaoge_clear_event *event, void *user) {
    (void)event;
    (void)user;
}

static void test_state_callback(const xiaoge_state_event *event, void *user) {
    (void)event;
    (void)user;
}

static void test_stt_callback(const xiaoge_stt_event *event, void *user) {
    (void)event;
    (void)user;
}

static void test_reply_callback(const xiaoge_reply_event *event, void *user) {
    (void)event;
    (void)user;
}

static void test_command_callback(const xiaoge_command_event *event, void *user) {
    (void)event;
    (void)user;
}

static void test_error_callback(const xiaoge_error_event *event, void *user) {
    (void)event;
    (void)user;
}

static void test_json_callback(const char *json, void *user) {
    (void)json;
    (void)user;
}

static void test_failure_callback(int code, const char *message, void *user) {
    (void)code;
    (void)message;
    (void)user;
}

static int must(int ok, const char *message) {
    if (!ok) {
        fprintf(stderr, "FAIL %s\n", message);
        return 1;
    }
    return 0;
}

int main(void) {
    char body[2048];
    xiaoge_config cfg = {
        "robot-test",
        "{\"type\":\"mock\",\"value\":\"credential\"}",
        "audio,text,cmd,state",
        "xiaoge-c-test-r5.2.2",
        "{}",
    };

    xiaoge_callbacks callbacks = {
        .struct_size = sizeof(callbacks),
        .on_ready = test_ready_callback,
        .on_audio = test_audio_callback,
        .on_clear = test_clear_callback,
        .on_state = test_state_callback,
        .on_stt = test_stt_callback,
        .on_reply = test_reply_callback,
        .on_command = test_command_callback,
        .on_error = test_error_callback,
        .on_json = test_json_callback,
        .on_failure = test_failure_callback,
        .user = &callbacks,
    };
    if (must(callbacks.struct_size == sizeof(callbacks), "event callback struct initializes")) return 1;

    int n = xiaoge_build_create_session_request(&cfg, body, sizeof(body));
    if (must(n > 0, "create_session request builds")) return 1;
    if (must(strstr(body, "\"caps\":[\"audio\",\"text\",\"cmd\",\"state\"]") != NULL, "caps serialized")) return 1;
    if (must(strstr(body, "\"sample_rate\":16000") != NULL, "sample rate serialized")) return 1;

    xiaoge_config dup_caps = cfg;
    dup_caps.caps_csv = "audio,audio";
    if (must(xiaoge_build_create_session_request(&dup_caps, body, sizeof(body)) == -1, "duplicate caps rejected")) {
        return 1;
    }

    xiaoge_config bad_caps = cfg;
    bad_caps.caps_csv = "audio,legacy";
    if (must(xiaoge_build_create_session_request(&bad_caps, body, sizeof(body)) == -1, "unknown caps rejected")) {
        return 1;
    }

    xiaoge_session bad_path = {
        "trace",
        "sess",
        "token",
        "ws://127.0.0.1/ws/audio",
        "audio,text",
        "cfg",
    };
    if (must(xiaoge_create_from_session(&cfg, &bad_path, 0, NULL) == NULL, "legacy ws path rejected")) return 1;

    if (must(xiaoge_send_pcm(NULL, "x", 1) == -1, "null pcm client rejected")) return 1;
    if (must(xiaoge_send_frontend_state(NULL, "bad", "awake", "speech", 1000) == -1,
             "null frontend_state client rejected")) {
        return 1;
    }

    const char *cloud_json =
        "{"
        "\"type\": \"data.cmd\", "
        "\"trace_id\": \"trace\", "
        "\"session_id\": \"sess\", "
        "\"utterance_id\": \"utt-cloud-json\", "
        "\"cmd_id\": \"cmd-cloud-json\", "
        "\"capability_id\": \"motion.move\", "
        "\"action\": \"navigation.move\", "
        "\"params\": {\"direction\": \"forward\", \"distance_cm\": 100}, "
        "\"risk_level\": \"medium\", "
        "\"ack_timeout_ms\": 800, "
        "\"result_timeout_ms\": 5000, "
        "\"issued_at_ms\": 1789000001000"
        "}";
    if (must(xiaoge_test_json_cloud_cmd_compatible(cloud_json) == 1,
             "cloud default JSON spacing accepted")) {
        return 1;
    }

    char frame[1024];
    if (must(xiaoge_test_build_cmd_ack_json(cloud_json, XIAOGE_CMD_ACK_STATUS_ACCEPTED, "ok", "accepted", frame,
                                            sizeof(frame)) == 0,
             "cmd_ack builds")) {
        return 1;
    }
    if (must(strstr(frame, "\"type\":\"data.cmd_ack\"") != NULL, "cmd_ack type serialized")) return 1;
    if (must(strstr(frame, "\"status\":\"accepted\"") != NULL, "cmd_ack status serialized")) return 1;
    if (must(strstr(frame, "\"received_at_ms\":") != NULL, "cmd_ack timestamp serialized")) return 1;
    if (must(xiaoge_test_build_cmd_ack_json(cloud_json, "unknown", "bad", "bad", frame,
                                            sizeof(frame)) == -1,
             "invalid cmd_ack status rejected")) {
        return 1;
    }

    if (must(xiaoge_test_build_cmd_result_json(cloud_json, XIAOGE_CMD_RESULT_STATUS_SUCCEEDED, "ok", "done", 0, 11, 12,
                                               1, frame, sizeof(frame)) == 0,
             "cmd_result builds")) {
        return 1;
    }
    if (must(strstr(frame, "\"type\":\"data.cmd_result\"") != NULL, "cmd_result type serialized")) return 1;
    if (must(strstr(frame, "\"retryable\":false") != NULL, "cmd_result retryable serialized")) return 1;
    if (must(strstr(frame, "\"started_at_ms\":11") != NULL, "cmd_result start serialized")) return 1;
    if (must(strstr(frame, "\"finished_at_ms\":12") != NULL, "cmd_result finish serialized")) return 1;
    if (must(strstr(frame, "\"duration_ms\":1") != NULL, "cmd_result duration serialized")) return 1;
    if (must(xiaoge_test_build_cmd_result_json(cloud_json, "accepted", "bad", "bad", 0, -1,
                                               -1, -1, frame, sizeof(frame)) == -1,
             "invalid cmd_result status rejected")) {
        return 1;
    }

    const char *missing_cmd_id =
        "{\"type\":\"data.cmd\",\"trace_id\":\"trace\",\"session_id\":\"sess\","
        "\"utterance_id\":\"utt\"}";
    if (must(xiaoge_test_build_cmd_ack_json(missing_cmd_id, XIAOGE_CMD_ACK_STATUS_ACCEPTED, "ok", "", frame,
                                            sizeof(frame)) == -1,
             "cmd_ack missing cmd_id rejected")) {
        return 1;
    }
    if (must(xiaoge_test_build_cmd_result_json(missing_cmd_id, XIAOGE_CMD_RESULT_STATUS_RUNNING, "ok", "", 0, -1,
                                               -1, -1, frame, sizeof(frame)) == -1,
             "cmd_result missing cmd_id rejected")) {
        return 1;
    }

    const char *stt_json =
        "{\"type\":\"data.stt\",\"trace_id\":\"trace\",\"session_id\":\"sess\","
        "\"utterance_id\":\"utt-0001\",\"text\":\"往前走一米\",\"final\":true,\"ts_ms\":1789000000100}";
    xiaoge_stt_event stt_event;
    char stt_text[512], stt_utt[128], stt_trace[128], stt_session[128];
    if (must(xiaoge_test_build_stt_event(stt_json, &stt_event, stt_text, stt_utt, stt_trace, stt_session) == 0,
             "stt event builds")) {
        return 1;
    }
    if (must(strcmp(stt_event.text, "往前走一米") == 0 && stt_event.final == 1,
             "stt event exposes text/final")) {
        return 1;
    }

    const char *reply_json =
        "{\"type\":\"data.reply\",\"trace_id\":\"trace\",\"session_id\":\"sess\","
        "\"utterance_id\":\"utt-0001\",\"intent_type\":\"control_cmd\",\"text\":\"好的\","
        "\"speak_policy\":\"ack_then_result\",\"ts_ms\":1789000000700}";
    xiaoge_reply_event reply_event;
    char reply_text[512], reply_utt[128], reply_intent[64], reply_speak[64], reply_trace[128], reply_session[128];
    if (must(xiaoge_test_build_reply_event(reply_json, &reply_event, reply_text, reply_utt, reply_intent,
                                           reply_speak, reply_trace, reply_session) == 0,
             "reply event builds")) {
        return 1;
    }
    if (must(reply_event.final == 1 && strcmp(reply_event.intent_type, "control_cmd") == 0 &&
             strcmp(reply_event.speak_policy, "ack_then_result") == 0,
             "reply event exposes derived final and metadata")) {
        return 1;
    }
    const char *bad_reply_json =
        "{\"type\":\"data.reply\",\"trace_id\":\"trace\",\"session_id\":\"sess\","
        "\"utterance_id\":\"utt\",\"intent_type\":\"bad\",\"text\":\"x\",\"ts_ms\":1}";
    if (must(xiaoge_test_build_reply_event(bad_reply_json, &reply_event, reply_text, reply_utt, reply_intent,
                                           reply_speak, reply_trace, reply_session) == -1,
             "invalid reply intent rejected")) {
        return 1;
    }

    xiaoge_command_event cmd_event = {
        "cmd-cloud-json", "motion.move", "navigation.move", "{\"direction\":\"forward\"}", "medium",
        800, 5000, 1789000001000LL, "utt-cloud-json", "trace", "sess", cloud_json,
    };
    if (must(xiaoge_test_build_cmd_ack_event_json(&cmd_event, XIAOGE_CMD_ACK_STATUS_ACCEPTED, "ok", "accepted",
                                                 frame, sizeof(frame)) == 0,
             "cmd_ack event builds")) {
        return 1;
    }
    if (must(strstr(frame, "\"cmd_id\":\"cmd-cloud-json\"") != NULL, "cmd_ack event uses command event ids")) return 1;

    const char *error_json =
        "{\"type\":\"data.error\",\"trace_id\":\"trace\",\"session_id\":\"sess\","
        "\"code\":\"unknown_cmd_id\",\"message\":\"missing cmd\",\"retryable\":false,\"ts_ms\":1789000000900}";
    xiaoge_error_event error_event;
    char err_code[64], err_message[512], err_trace[128], err_session[128];
    if (must(xiaoge_test_build_error_event(error_json, &error_event, err_code, err_message, err_trace, err_session) == 0,
             "error event builds")) {
        return 1;
    }
    if (must(strcmp(error_event.code, XIAOGE_ERROR_UNKNOWN_CMD_ID) == 0 && error_event.retryable == 0,
             "error event exposes code/retryable")) {
        return 1;
    }

    if (must(xiaoge_test_auto_fake_cmd_enabled(XIAOGE_AUTO_FAKE_CMD_DEFAULT) == 1,
             "default fake executor enabled")) {
        return 1;
    }
    if (must(xiaoge_test_auto_fake_cmd_enabled(XIAOGE_AUTO_FAKE_CMD_ENABLED) == 1,
             "explicit fake executor enabled")) {
        return 1;
    }
    if (must(xiaoge_test_auto_fake_cmd_enabled(XIAOGE_AUTO_FAKE_CMD_DISABLED) == 0,
             "fake executor disabled")) {
        return 1;
    }

    printf("records=c-codec-smoke failures=0\n");
    return 0;
}
