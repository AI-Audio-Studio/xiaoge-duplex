#include "xiaoge_client.h"

#include <libwebsockets.h>
#include <pthread.h>
#include <ctype.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <stddef.h>
#include "cJSON.h"
#ifdef _WIN32
#include <winsock2.h>
#endif

enum out_kind { OUT_BINARY, OUT_TEXT };

struct out_buf {
    struct out_buf *next;
    enum out_kind kind;
    size_t len;
    unsigned char data[];
};

struct parsed_url {
    int tls;
    char host[256];
    int port;
    char path[256];
};

struct xiaoge_client {
    struct lws_context *ctx;
    struct lws *wsi;
    xiaoge_config cfg;
    xiaoge_session session;
    xiaoge_callbacks cb;
    pthread_mutex_t lock;
    struct out_buf *head, *tail;
    unsigned char *rx;
    size_t rx_len, rx_cap;
    int finished;
    int seq;
    struct parsed_url url;
    char host_header[320];
    char seen_cmd_ids[32][128];
    int seen_cmd_count;
    int auto_fake_cmd_executor;
};

static long long now_ms(void) {
    return (long long)time(NULL) * 1000LL;
}

static const char *nn(const char *s, const char *fallback) {
    return s && *s ? s : fallback;
}

static int copy_string(char *out, size_t out_len, const char *s) {
    if (!out || out_len == 0 || !s) return -1;
    size_t n = strlen(s);
    if (n >= out_len) return -1;
    memcpy(out, s, n + 1);
    return 0;
}

static cJSON *json_parse_object(const char *s) {
    if (!s) return NULL;
    cJSON *root = cJSON_Parse(s);
    if (!cJSON_IsObject(root)) {
        cJSON_Delete(root);
        return NULL;
    }
    return root;
}

static cJSON *json_parse_value(const char *s) {
    if (!s) return NULL;
    return cJSON_Parse(s);
}

static int json_get_string(const cJSON *obj, const char *key, char *out, size_t out_len) {
    cJSON *item = cJSON_GetObjectItemCaseSensitive(obj, key);
    const char *value = cJSON_GetStringValue(item);
    return value ? copy_string(out, out_len, value) : -1;
}

static int json_string_is(const cJSON *obj, const char *key, const char *want) {
    char got[128];
    return json_get_string(obj, key, got, sizeof(got)) == 0 && strcmp(got, want) == 0;
}

static int json_type_is(const cJSON *obj, const char *want) {
    return json_string_is(obj, "type", want);
}

static int json_get_ll(const cJSON *obj, const char *key, long long *out) {
    cJSON *item = cJSON_GetObjectItemCaseSensitive(obj, key);
    if (!cJSON_IsNumber(item) || !out) return -1;
    *out = (long long)item->valuedouble;
    return 0;
}

static int json_get_bool(const cJSON *obj, const char *key, int *out) {
    cJSON *item = cJSON_GetObjectItemCaseSensitive(obj, key);
    if (!out || !cJSON_IsBool(item)) return -1;
    *out = cJSON_IsTrue(item) ? 1 : 0;
    return 0;
}

static int json_has_object(const cJSON *obj, const char *key) {
    return cJSON_IsObject(cJSON_GetObjectItemCaseSensitive(obj, key));
}

static int json_copy_value(const cJSON *obj, const char *key, char *out, size_t out_len) {
    cJSON *item = cJSON_GetObjectItemCaseSensitive(obj, key);
    if (!item || !out || out_len == 0) return -1;
    char *printed = cJSON_PrintUnformatted(item);
    if (!printed) return -1;
    int rc = copy_string(out, out_len, printed);
    cJSON_free(printed);
    return rc;
}

static int json_print_checked(const cJSON *obj, char *out, size_t out_len) {
    if (!obj || !out || out_len == 0) return -1;
    char *printed = cJSON_PrintUnformatted(obj);
    if (!printed) return -1;
    size_t n = strlen(printed);
    int rc = (n < out_len && n <= XIAOGE_JSON_MAX_BYTES) ? 0 : -1;
    if (rc == 0) memcpy(out, printed, n + 1);
    cJSON_free(printed);
    return rc;
}

static int json_add_string(cJSON *obj, const char *key, const char *value) {
    return cJSON_AddStringToObject(obj, key, nn(value, "")) != NULL ? 0 : -1;
}

static int json_add_number_ll(cJSON *obj, const char *key, long long value) {
    return cJSON_AddNumberToObject(obj, key, (double)value) != NULL ? 0 : -1;
}

static int json_add_optional_ll(cJSON *obj, const char *key, long long value) {
    return value < 0 ? 0 : json_add_number_ll(obj, key, value);
}

static int cap_valid(const char *cap);

static int caps_csv_to_json_array(const char *csv, cJSON *arr) {
    char tmp[256];
    snprintf(tmp, sizeof(tmp), "%s", nn(csv, "audio,text,cmd,state"));
    char used[4][16];
    int used_count = 0;
    char *tok = strtok(tmp, ",");
    while (tok) {
        while (*tok == ' ') tok++;
        char *end = tok + strlen(tok);
        while (end > tok && end[-1] == ' ') *--end = 0;
        if (!cap_valid(tok) || used_count >= 4) return -1;
        for (int i = 0; i < used_count; i++) {
            if (strcmp(used[i], tok) == 0) return -1;
        }
        snprintf(used[used_count++], sizeof(used[0]), "%s", tok);
        if (!cJSON_AddItemToArray(arr, cJSON_CreateString(tok))) return -1;
        tok = strtok(NULL, ",");
    }
    return used_count == 0 ? -1 : 0;
}

static int parse_url(const char *url, struct parsed_url *out) {
    memset(out, 0, sizeof(*out));
    const char *p = NULL;
    if (strncmp(url, "wss://", 6) == 0) {
        out->tls = 1; out->port = 443; p = url + 6;
    } else if (strncmp(url, "ws://", 5) == 0) {
        out->tls = 0; out->port = 80; p = url + 5;
    } else {
        return -1;
    }
    const char *slash = strchr(p, '/');
    if (!slash) return -1;
    const char *colon = memchr(p, ':', (size_t)(slash - p));
    size_t host_len = colon ? (size_t)(colon - p) : (size_t)(slash - p);
    if (host_len == 0 || host_len >= sizeof(out->host)) return -1;
    memcpy(out->host, p, host_len);
    out->host[host_len] = 0;
    if (colon) out->port = atoi(colon + 1);
    snprintf(out->path, sizeof(out->path), "%s", slash);
    return strcmp(out->path, "/ws/session") == 0 ? 0 : -1;
}

static int rx_append(struct xiaoge_client *c, const void *p, size_t n) {
    if (c->rx_len + n > c->rx_cap) {
        size_t cap = c->rx_cap ? c->rx_cap : 4096;
        while (cap < c->rx_len + n) cap *= 2;
        unsigned char *nb = (unsigned char *)realloc(c->rx, cap + 1);
        if (!nb) return -1;
        c->rx = nb;
        c->rx_cap = cap;
    }
    memcpy(c->rx + c->rx_len, p, n);
    c->rx_len += n;
    c->rx[c->rx_len] = 0;
    return 0;
}

static int str_in(const char *s, const char *a, const char *b, const char *c) {
    return s && ((a && strcmp(s, a) == 0) || (b && strcmp(s, b) == 0) || (c && strcmp(s, c) == 0));
}

static int str_in4(const char *s, const char *a, const char *b, const char *c, const char *d) {
    return str_in(s, a, b, c) || (d && strcmp(s, d) == 0);
}

static int intent_valid(const char *s) {
    return str_in(s, "control_cmd", "info_query", "knowledge_qa") ||
           str_in(s, "chat", "config", "system");
}

static int speak_policy_valid(const char *s) {
    return str_in4(s, "silent", "ack", "ack_then_result", "final_only");
}

static int error_code_valid(const char *s) {
    return str_in(s, XIAOGE_ERROR_AUTH_FAILED, XIAOGE_ERROR_PERMISSION_DENIED, XIAOGE_ERROR_BUSY) ||
           str_in(s, XIAOGE_ERROR_PROTOCOL_ERROR, XIAOGE_ERROR_CAPABILITY_UNSUPPORTED, XIAOGE_ERROR_TOKEN_EXPIRED) ||
           str_in(s, XIAOGE_ERROR_DUPLICATE_CONNECTION, XIAOGE_ERROR_RESOURCE_EXHAUSTED, XIAOGE_ERROR_UNKNOWN_CMD_ID);
}

static void failure(struct xiaoge_client *c, const char *message) {
    if (c && c->cb.on_failure) c->cb.on_failure(-1, message, c->cb.user);
}

static void observe_json(struct xiaoge_client *c, const char *s) {
    if (c && c->cb.on_json) c->cb.on_json(s, c->cb.user);
}

static int cap_valid(const char *cap) {
    return strcmp(cap, "audio") == 0 || strcmp(cap, "text") == 0 ||
           strcmp(cap, "cmd") == 0 || strcmp(cap, "state") == 0;
}

static int seen_cmd(struct xiaoge_client *c, const char *cmd_id) {
    for (int i = 0; i < c->seen_cmd_count; i++) {
        if (strcmp(c->seen_cmd_ids[i], cmd_id) == 0) return 1;
    }
    return 0;
}

static void remember_cmd(struct xiaoge_client *c, const char *cmd_id) {
    if (c->seen_cmd_count < 32) {
        snprintf(c->seen_cmd_ids[c->seen_cmd_count++], sizeof(c->seen_cmd_ids[0]), "%s", cmd_id);
    }
}

static int enqueue(struct xiaoge_client *c, enum out_kind kind, const void *data, size_t len) {
    if (!c || !data || !len) return -1;
    if (kind == OUT_TEXT && len > XIAOGE_JSON_MAX_BYTES) return -1;
    if (kind == OUT_BINARY && len > XIAOGE_BINARY_MAX_BYTES) return -1;
    struct out_buf *b = (struct out_buf *)malloc(sizeof(*b) + LWS_PRE + len);
    if (!b) return -1;
    b->next = NULL;
    b->kind = kind;
    b->len = len;
    memcpy(b->data + LWS_PRE, data, len);
    pthread_mutex_lock(&c->lock);
    if (c->tail) c->tail->next = b; else c->head = b;
    c->tail = b;
    pthread_mutex_unlock(&c->lock);
    if (c->wsi) lws_callback_on_writable(c->wsi);
    if (c->ctx) lws_cancel_service(c->ctx);
    return 0;
}

static int send_text(struct xiaoge_client *c, const char *text) {
    return enqueue(c, OUT_TEXT, text, strlen(text));
}

static int cmd_ack_status_valid(const char *status) {
    return status && (strcmp(status, XIAOGE_CMD_ACK_STATUS_ACCEPTED) == 0 ||
                      strcmp(status, XIAOGE_CMD_ACK_STATUS_REJECTED) == 0 ||
                      strcmp(status, XIAOGE_CMD_ACK_STATUS_DUPLICATE) == 0);
}

static int cmd_result_status_valid(const char *status) {
    return status && (strcmp(status, XIAOGE_CMD_RESULT_STATUS_RUNNING) == 0 ||
                      strcmp(status, XIAOGE_CMD_RESULT_STATUS_SUCCEEDED) == 0 ||
                      strcmp(status, XIAOGE_CMD_RESULT_STATUS_FAILED) == 0 ||
                      strcmp(status, XIAOGE_CMD_RESULT_STATUS_CANCELED) == 0 ||
                      strcmp(status, XIAOGE_CMD_RESULT_STATUS_TIMEOUT) == 0);
}

static int build_cmd_ids_from_root(const cJSON *root, char *trace_id, size_t trace_len,
                                   char *session_id, size_t session_len, char *utterance_id,
                                   size_t utterance_len, char *cmd_id, size_t cmd_len) {
    if (!root) return -1;
    if (json_get_string(root, "trace_id", trace_id, trace_len) != 0 || !trace_id[0]) return -1;
    if (json_get_string(root, "session_id", session_id, session_len) != 0 || !session_id[0]) return -1;
    if (json_get_string(root, "utterance_id", utterance_id, utterance_len) != 0 || !utterance_id[0]) return -1;
    if (json_get_string(root, "cmd_id", cmd_id, cmd_len) != 0 || !cmd_id[0]) return -1;
    return 0;
}

static int build_cmd_ids(const char *cmd_json, char *trace_id, size_t trace_len,
                         char *session_id, size_t session_len, char *utterance_id,
                         size_t utterance_len, char *cmd_id, size_t cmd_len) {
    cJSON *root = json_parse_object(cmd_json);
    int rc = build_cmd_ids_from_root(root, trace_id, trace_len, session_id, session_len,
                                     utterance_id, utterance_len, cmd_id, cmd_len);
    cJSON_Delete(root);
    return rc;
}

static int build_cmd_status_json(const char *type, const char *trace, const char *session,
                                 const char *utt, const char *cmd_id, const char *status,
                                 const char *code, const char *message, int has_retryable,
                                 int retryable, long long received_at_ms, long long started_at_ms,
                                 long long finished_at_ms, long long duration_ms, char *out,
                                 size_t out_len) {
    cJSON *root = cJSON_CreateObject();
    if (!root) return -1;
    int ok = json_add_string(root, "type", type) == 0 &&
             json_add_string(root, "trace_id", trace) == 0 &&
             json_add_string(root, "session_id", session) == 0 &&
             json_add_string(root, "utterance_id", utt) == 0 &&
             json_add_string(root, "cmd_id", cmd_id) == 0 &&
             json_add_string(root, "status", status) == 0 &&
             json_add_string(root, "code", code) == 0 &&
             json_add_string(root, "message", nn(message, "")) == 0;
    if (ok && has_retryable) ok = cJSON_AddBoolToObject(root, "retryable", retryable) != NULL;
    if (ok && received_at_ms >= 0) ok = json_add_number_ll(root, "received_at_ms", received_at_ms) == 0;
    if (ok) ok = json_add_optional_ll(root, "started_at_ms", started_at_ms) == 0;
    if (ok) ok = json_add_optional_ll(root, "finished_at_ms", finished_at_ms) == 0;
    if (ok) ok = json_add_optional_ll(root, "duration_ms", duration_ms) == 0;
    int rc = ok ? json_print_checked(root, out, out_len) : -1;
    cJSON_Delete(root);
    return rc;
}

static int build_cmd_ack_json(const char *cmd_json, const char *status, const char *code,
                              const char *message, char *out, size_t out_len) {
    if (!out || !cmd_ack_status_valid(status) || !code || !*code) return -1;
    char trace[128] = "", session[128] = "", utt[128] = "", cmd_id[128] = "";
    if (build_cmd_ids(cmd_json, trace, sizeof(trace), session, sizeof(session), utt, sizeof(utt),
                      cmd_id, sizeof(cmd_id)) != 0)
        return -1;
    return build_cmd_status_json("data.cmd_ack", trace, session, utt, cmd_id, status, code,
                                 message, 0, 0, now_ms(), -1, -1, -1, out, out_len);
}

static int build_cmd_ids_from_event(const xiaoge_command_event *event, char *trace_id, size_t trace_len,
                                    char *session_id, size_t session_len, char *utterance_id,
                                    size_t utterance_len, char *cmd_id, size_t cmd_len) {
    if (!event || !event->trace_id || !event->session_id || !event->utterance_id || !event->cmd_id) return -1;
    if (copy_string(trace_id, trace_len, event->trace_id) != 0) return -1;
    if (copy_string(session_id, session_len, event->session_id) != 0) return -1;
    if (copy_string(utterance_id, utterance_len, event->utterance_id) != 0) return -1;
    if (copy_string(cmd_id, cmd_len, event->cmd_id) != 0) return -1;
    return trace_id[0] && session_id[0] && utterance_id[0] && cmd_id[0] ? 0 : -1;
}

static int build_cmd_ack_event_json(const xiaoge_command_event *event, const char *status, const char *code,
                                    const char *message, char *out, size_t out_len) {
    if (!out || !cmd_ack_status_valid(status) || !code || !*code) return -1;
    char trace[128] = "", session[128] = "", utt[128] = "", cmd_id[128] = "";
    if (build_cmd_ids_from_event(event, trace, sizeof(trace), session, sizeof(session), utt, sizeof(utt),
                                 cmd_id, sizeof(cmd_id)) != 0)
        return -1;
    return build_cmd_status_json("data.cmd_ack", trace, session, utt, cmd_id, status, code,
                                 message, 0, 0, now_ms(), -1, -1, -1, out, out_len);
}

static int build_cmd_result_event_json(const xiaoge_command_event *event, const char *status, const char *code,
                                       const char *message, int retryable, char *out, size_t out_len) {
    if (!out || !cmd_result_status_valid(status) || !code || !*code) return -1;
    char trace[128] = "", session[128] = "", utt[128] = "", cmd_id[128] = "";
    if (build_cmd_ids_from_event(event, trace, sizeof(trace), session, sizeof(session), utt, sizeof(utt),
                                 cmd_id, sizeof(cmd_id)) != 0)
        return -1;
    return build_cmd_status_json("data.cmd_result", trace, session, utt, cmd_id, status, code,
                                 message, 1, retryable, -1, -1, -1, -1, out, out_len);
}

static int build_cmd_result_json(const char *cmd_json, const char *status, const char *code,
                                 const char *message, int retryable, long long started_at_ms,
                                 long long finished_at_ms, long long duration_ms, char *out,
                                 size_t out_len) {
    if (!out || !cmd_result_status_valid(status) || !code || !*code) return -1;
    char trace[128] = "", session[128] = "", utt[128] = "", cmd_id[128] = "";
    if (build_cmd_ids(cmd_json, trace, sizeof(trace), session, sizeof(session), utt, sizeof(utt),
                      cmd_id, sizeof(cmd_id)) != 0)
        return -1;
    return build_cmd_status_json("data.cmd_result", trace, session, utt, cmd_id, status, code,
                                 message, 1, retryable, -1, started_at_ms, finished_at_ms,
                                 duration_ms, out, out_len);
}

int xiaoge_send_cmd_ack(xiaoge_client *c, const char *cmd_json,
                        const char *status, const char *code, const char *message) {
    char buf[1024];
    if (build_cmd_ack_json(cmd_json, status, code, message, buf, sizeof(buf)) != 0) return -1;
    return send_text(c, buf);
}

int xiaoge_send_cmd_result_ex(xiaoge_client *c, const char *cmd_json,
                              const char *status, const char *code, const char *message,
                              int retryable, long long started_at_ms,
                              long long finished_at_ms, long long duration_ms) {
    char buf[1024];
    if (build_cmd_result_json(cmd_json, status, code, message, retryable, started_at_ms,
                              finished_at_ms, duration_ms, buf, sizeof(buf)) != 0)
        return -1;
    return send_text(c, buf);
}

int xiaoge_send_cmd_result(xiaoge_client *c, const char *cmd_json,
                           const char *status, const char *code, const char *message,
                           int retryable) {
    return xiaoge_send_cmd_result_ex(c, cmd_json, status, code, message, retryable, -1, -1, -1);
}

int xiaoge_send_cmd_ack_event(xiaoge_client *c, const xiaoge_command_event *event,
                              const char *status, const char *code, const char *message) {
    char buf[1024];
    if (build_cmd_ack_event_json(event, status, code, message, buf, sizeof(buf)) != 0) return -1;
    return send_text(c, buf);
}

int xiaoge_send_cmd_result_event(xiaoge_client *c, const xiaoge_command_event *event,
                                 const char *status, const char *code, const char *message,
                                 int retryable) {
    char buf[1024];
    if (build_cmd_result_event_json(event, status, code, message, retryable, buf, sizeof(buf)) != 0) return -1;
    return send_text(c, buf);
}


static void copy_callbacks(struct xiaoge_client *c, const xiaoge_callbacks *cb) {
    if (!c || !cb || cb->struct_size < offsetof(xiaoge_callbacks, on_ready)) return;
    size_t n = cb->struct_size;
    if (n > sizeof(c->cb)) n = sizeof(c->cb);
    memcpy(&c->cb, cb, n);
    c->cb.struct_size = n;
}

static int build_ready_event(const char *s, const cJSON *root, xiaoge_ready_event *ev, char caps[128], char config[128],
                             char trace[128], char session[128]) {
    long long sample_rate = -1;
    if (!json_type_is(root, "ctrl.ready") ||
        json_get_ll(root, "sample_rate", &sample_rate) != 0 || sample_rate != XIAOGE_SAMPLE_RATE ||
        !cJSON_IsArray(cJSON_GetObjectItemCaseSensitive(root, "granted_caps")) ||
        json_get_string(root, "config_version", config, 128) != 0 || !config[0] ||
        json_get_string(root, "trace_id", trace, 128) != 0 || !trace[0] ||
        json_get_string(root, "session_id", session, 128) != 0 || !session[0]) {
        return -1;
    }
    ev->sample_rate = XIAOGE_SAMPLE_RATE;
    ev->granted_caps = caps;
    ev->config_version = config;
    ev->trace_id = trace;
    ev->session_id = session;
    ev->raw_json = s;
    return 0;
}

static int build_stt_event(const char *s, const cJSON *root, xiaoge_stt_event *ev, char text[512], char utt[128],
                           char trace[128], char session[128]) {
    int final = 0;
    long long ts = -1;
    if (json_get_string(root, "text", text, 512) != 0 || !text[0] ||
        json_get_bool(root, "final", &final) != 0 ||
        json_get_string(root, "utterance_id", utt, 128) != 0 || !utt[0] ||
        json_get_string(root, "trace_id", trace, 128) != 0 || !trace[0] ||
        json_get_string(root, "session_id", session, 128) != 0 || !session[0] ||
        json_get_ll(root, "ts_ms", &ts) != 0 || ts < 0) {
        return -1;
    }
    ev->text = text;
    ev->final = final;
    ev->utterance_id = utt;
    ev->trace_id = trace;
    ev->session_id = session;
    ev->ts_ms = ts;
    ev->raw_json = s;
    return 0;
}

static int build_reply_event(const char *s, const cJSON *root, xiaoge_reply_event *ev, char text[512], char utt[128],
                             char intent[64], char speak[64], char trace[128], char session[128]) {
    long long ts = -1;
    if (json_get_string(root, "text", text, 512) != 0 || !text[0] ||
        json_get_string(root, "utterance_id", utt, 128) != 0 || !utt[0] ||
        json_get_string(root, "intent_type", intent, 64) != 0 || !intent_valid(intent) ||
        json_get_string(root, "trace_id", trace, 128) != 0 || !trace[0] ||
        json_get_string(root, "session_id", session, 128) != 0 || !session[0] ||
        json_get_ll(root, "ts_ms", &ts) != 0 || ts < 0) {
        return -1;
    }
    speak[0] = 0;
    if (json_get_string(root, "speak_policy", speak, 64) == 0 && !speak_policy_valid(speak)) return -1;
    ev->text = text;
    ev->final = 1;
    ev->utterance_id = utt;
    ev->intent_type = intent;
    ev->speak_policy = speak[0] ? speak : NULL;
    ev->trace_id = trace;
    ev->session_id = session;
    ev->ts_ms = ts;
    ev->raw_json = s;
    return 0;
}

static int clear_reason_valid(const char *s) {
    return str_in4(s, "barge_in", "user_stop", "system_cancel", "sleep");
}

static int link_state_valid(const char *s) {
    return str_in4(s, "connecting", "connected", "reconnecting", "closed");
}

static int interaction_mode_valid(const char *s) {
    return str_in(s, "sleeping", "dialogue", "listening");
}

static int engine_gate_valid(const char *s) {
    return str_in(s, "closed", "open", "kws_only");
}

static int resource_state_valid(const char *s) {
    return str_in(s, "SleepingHot", "SleepingWarm", "ActiveAgent") ||
           str_in(s, "ReleasedIdle", "PendingReconnect", NULL);
}

static int build_clear_event(const char *s, const cJSON *root, xiaoge_clear_event *ev, char reason[64], char utt[128],
                             char trace[128], char session[128]) {
    if (json_get_string(root, "trace_id", trace, 128) != 0 || !trace[0] ||
        json_get_string(root, "session_id", session, 128) != 0 || !session[0]) {
        return -1;
    }
    reason[0] = 0;
    if (json_get_string(root, "reason", reason, 64) == 0 && !clear_reason_valid(reason)) return -1;
    utt[0] = 0;
    if (json_get_string(root, "utterance_id", utt, 128) == 0 && !utt[0]) return -1;
    ev->reason = reason[0] ? reason : NULL;
    ev->utterance_id = utt[0] ? utt : NULL;
    ev->trace_id = trace;
    ev->session_id = session;
    ev->raw_json = s;
    return 0;
}

static int build_state_event(const char *s, const cJSON *root, xiaoge_state_event *ev, char link[64], char mode[64],
                             char gate[64], char resource[64], char pending[1024], char trace[128],
                             char session[128]) {
    long long ts = -1;
    if (json_get_string(root, "link_state", link, 64) != 0 || !link_state_valid(link) ||
        json_get_string(root, "interaction_mode", mode, 64) != 0 || !interaction_mode_valid(mode) ||
        json_get_string(root, "engine_gate", gate, 64) != 0 || !engine_gate_valid(gate) ||
        json_get_string(root, "resource_state", resource, 64) != 0 || !resource_state_valid(resource) ||
        json_get_ll(root, "ts_ms", &ts) != 0 || ts < 0 ||
        json_get_string(root, "trace_id", trace, 128) != 0 || !trace[0] ||
        json_get_string(root, "session_id", session, 128) != 0 || !session[0]) {
        return -1;
    }
    pending[0] = 0;
    json_copy_value(root, "pending_confirmation", pending, 1024);
    ev->link_state = link;
    ev->interaction_mode = mode;
    ev->engine_gate = gate;
    ev->resource_state = resource;
    ev->pending_confirmation_json = pending[0] ? pending : NULL;
    ev->ts_ms = ts;
    ev->trace_id = trace;
    ev->session_id = session;
    ev->raw_json = s;
    return 0;
}

static int build_command_event(const char *s, const cJSON *root, xiaoge_command_event *ev, char cmd[128], char cap[128],
                               char action[128], char params[1024], char risk[32], char utt[128],
                               char trace[128], char session[128]) {
    long long ack = 0, result = 0, issued = -1;
    if (json_get_string(root, "cmd_id", cmd, 128) != 0 || !cmd[0] ||
        json_get_string(root, "capability_id", cap, 128) != 0 || !cap[0] ||
        json_get_string(root, "action", action, 128) != 0 || !action[0] ||
        json_copy_value(root, "params", params, 1024) != 0 ||
        json_get_string(root, "risk_level", risk, 32) != 0 || !str_in(risk, "low", "medium", "high") ||
        json_get_ll(root, "ack_timeout_ms", &ack) != 0 || ack < 1 ||
        json_get_ll(root, "result_timeout_ms", &result) != 0 || result < 1 ||
        json_get_ll(root, "issued_at_ms", &issued) != 0 || issued < 0 ||
        json_get_string(root, "utterance_id", utt, 128) != 0 || !utt[0] ||
        json_get_string(root, "trace_id", trace, 128) != 0 || !trace[0] ||
        json_get_string(root, "session_id", session, 128) != 0 || !session[0]) {
        return -1;
    }
    ev->cmd_id = cmd;
    ev->capability_id = cap;
    ev->action = action;
    ev->params_json = params;
    ev->risk_level = risk;
    ev->ack_timeout_ms = ack;
    ev->result_timeout_ms = result;
    ev->issued_at_ms = issued;
    ev->utterance_id = utt;
    ev->trace_id = trace;
    ev->session_id = session;
    ev->raw_json = s;
    return 0;
}

static int build_error_event(const char *s, const cJSON *root, xiaoge_error_event *ev, char code[64], char message[512],
                             char trace[128], char session[128]) {
    int retryable = 0;
    long long ts = -1;
    if (json_get_string(root, "code", code, 64) != 0 || !error_code_valid(code) ||
        json_get_string(root, "message", message, 512) != 0 || !message[0] ||
        json_get_bool(root, "retryable", &retryable) != 0 ||
        json_get_ll(root, "ts_ms", &ts) != 0 || ts < 0 ||
        json_get_string(root, "trace_id", trace, 128) != 0 || !trace[0] ||
        json_get_string(root, "session_id", session, 128) != 0 || !session[0]) {
        return -1;
    }
    ev->code = code;
    ev->message = message;
    ev->retryable = retryable;
    ev->ts_ms = ts;
    ev->trace_id = trace;
    ev->session_id = session;
    ev->raw_json = s;
    return 0;
}

static int auto_fake_cmd_enabled(const xiaoge_config *cfg) {
    return !cfg || cfg->auto_fake_cmd_executor != XIAOGE_AUTO_FAKE_CMD_DISABLED;
}

static int send_hello(struct xiaoge_client *c) {
    char buf[2048];
    cJSON *root = cJSON_CreateObject();
    cJSON *caps = cJSON_CreateArray();
    if (!root || !caps) {
        cJSON_Delete(root);
        cJSON_Delete(caps);
        return -1;
    }
    int ok = json_add_string(root, "type", "ctrl.hello") == 0 &&
             json_add_string(root, "trace_id", c->session.trace_id) == 0 &&
             json_add_string(root, "session_id", c->session.session_id) == 0 &&
             json_add_number_ll(root, "proto", 2) == 0 &&
             json_add_string(root, "role", "device") == 0 &&
             json_add_string(root, "device_id", c->cfg.device_id) == 0 &&
             caps_csv_to_json_array(c->session.granted_caps_csv, caps) == 0;
    if (ok) {
        ok = cJSON_AddItemToObject(root, "caps", caps);
        if (ok) caps = NULL;
    }
    int rc = ok && json_print_checked(root, buf, sizeof(buf)) == 0 ? send_text(c, buf) : -1;
    cJSON_Delete(caps);
    cJSON_Delete(root);
    return rc;
}

static int send_error_json(struct xiaoge_client *c, const char *code, const char *message) {
    char err[1024];
    cJSON *root = cJSON_CreateObject();
    if (!root) return -1;
    int ok = json_add_string(root, "type", "data.error") == 0 &&
             json_add_string(root, "trace_id", c->session.trace_id) == 0 &&
             json_add_string(root, "session_id", c->session.session_id) == 0 &&
             json_add_string(root, "code", code) == 0 &&
             json_add_string(root, "message", message) == 0 &&
             cJSON_AddBoolToObject(root, "retryable", 0) != NULL &&
             json_add_number_ll(root, "ts_ms", now_ms()) == 0;
    int rc = ok && json_print_checked(root, err, sizeof(err)) == 0 ? send_text(c, err) : -1;
    cJSON_Delete(root);
    return rc;
}

static int direction_valid(const cJSON *root) {
    cJSON *params = cJSON_GetObjectItemCaseSensitive(root, "params");
    return cJSON_IsObject(params) &&
           (json_string_is(params, "direction", "forward") ||
            json_string_is(params, "direction", "backward") ||
            json_string_is(params, "direction", "left") ||
            json_string_is(params, "direction", "right"));
}

static int send_ack_result(struct xiaoge_client *c, const char *cmd_json) {
    cJSON *root = json_parse_object(cmd_json);
    char cmd_id[128] = "", utt[128] = "", cap[128] = "", action[128] = "", risk[32] = "";
    if (root) {
        json_get_string(root, "cmd_id", cmd_id, sizeof(cmd_id));
        json_get_string(root, "utterance_id", utt, sizeof(utt));
        json_get_string(root, "capability_id", cap, sizeof(cap));
        json_get_string(root, "action", action, sizeof(action));
        json_get_string(root, "risk_level", risk, sizeof(risk));
    }
    if (!cmd_id[0]) {
        cJSON_Delete(root);
        return send_error_json(c, "unknown_cmd_id", "missing cmd_id");
    }
    const char *ack_status = XIAOGE_CMD_ACK_STATUS_ACCEPTED;
    const char *ack_code = "ok";
    if (seen_cmd(c, cmd_id)) {
        ack_status = XIAOGE_CMD_ACK_STATUS_DUPLICATE;
        ack_code = "duplicate_cmd_id";
    } else {
        long long ack_timeout = 0, result_timeout = 0, issued_at = -1;
        if (!json_type_is(root, "data.cmd") || !utt[0] || !cap[0] || !action[0] ||
            !risk[0] || !json_has_object(root, "params") ||
            json_get_ll(root, "ack_timeout_ms", &ack_timeout) != 0 ||
            json_get_ll(root, "result_timeout_ms", &result_timeout) != 0 ||
            json_get_ll(root, "issued_at_ms", &issued_at) != 0 ||
            ack_timeout < 1 || result_timeout < 1 || issued_at < 0 ||
            (strcmp(risk, "low") != 0 && strcmp(risk, "medium") != 0 && strcmp(risk, "high") != 0)) {
            ack_status = XIAOGE_CMD_ACK_STATUS_REJECTED;
            ack_code = "invalid_cmd_schema";
        } else if (now_ms() > issued_at + ack_timeout + result_timeout + 1000) {
            ack_status = XIAOGE_CMD_ACK_STATUS_REJECTED;
            ack_code = "late_cmd";
        } else if (strcmp(cap, "motion.move") != 0) {
            ack_status = XIAOGE_CMD_ACK_STATUS_REJECTED;
            ack_code = "capability_unsupported";
        } else if (strcmp(action, "navigation.move") != 0) {
            ack_status = XIAOGE_CMD_ACK_STATUS_REJECTED;
            ack_code = "action_unsupported";
        } else if (!direction_valid(root)) {
            ack_status = XIAOGE_CMD_ACK_STATUS_REJECTED;
            ack_code = "invalid_params";
        }
        remember_cmd(c, cmd_id);
    }
    cJSON_Delete(root);
    if (xiaoge_send_cmd_ack(c, cmd_json, ack_status, ack_code, ack_status) != 0) return -1;
    if (strcmp(ack_status, XIAOGE_CMD_ACK_STATUS_ACCEPTED) != 0) return 0;
    long long start = now_ms();
    if (xiaoge_send_cmd_result_ex(c, cmd_json, XIAOGE_CMD_RESULT_STATUS_RUNNING, "ok", "fake executor started", 0,
                                  start, -1, -1) != 0)
        return -1;
    return xiaoge_send_cmd_result_ex(c, cmd_json, XIAOGE_CMD_RESULT_STATUS_SUCCEEDED, "ok", "fake executor completed", 0,
                                     start, start + 1, 1);
}

static void dispatch_ready(struct xiaoge_client *c, const char *s, const cJSON *root) {
    xiaoge_ready_event ev;
    char caps[128], config[128], trace[128], session[128];
    snprintf(caps, sizeof(caps), "%s", nn(c->session.granted_caps_csv, "audio,text,cmd,state"));
    if (build_ready_event(s, root, &ev, caps, config, trace, session) != 0) {
        observe_json(c, s);
        failure(c, "invalid ctrl.ready payload");
        return;
    }
    if (c->cb.on_ready) c->cb.on_ready(&ev, c->cb.user);
    observe_json(c, s);
}

static void dispatch_stt(struct xiaoge_client *c, const char *s, const cJSON *root) {
    xiaoge_stt_event ev;
    char text[512], utt[128], trace[128], session[128];
    if (build_stt_event(s, root, &ev, text, utt, trace, session) != 0) {
        observe_json(c, s);
        failure(c, "invalid data.stt payload");
        return;
    }
    if (c->cb.on_stt) c->cb.on_stt(&ev, c->cb.user);
    observe_json(c, s);
}

static void dispatch_reply(struct xiaoge_client *c, const char *s, const cJSON *root) {
    xiaoge_reply_event ev;
    char text[512], utt[128], intent[64], speak[64], trace[128], session[128];
    if (build_reply_event(s, root, &ev, text, utt, intent, speak, trace, session) != 0) {
        observe_json(c, s);
        failure(c, "invalid data.reply payload");
        return;
    }
    if (c->cb.on_reply) c->cb.on_reply(&ev, c->cb.user);
    observe_json(c, s);
}

static void dispatch_command(struct xiaoge_client *c, const char *s, const cJSON *root) {
    xiaoge_command_event ev;
    char cmd[128], cap[128], action[128], params[1024], risk[32], utt[128], trace[128], session[128];
    if (build_command_event(s, root, &ev, cmd, cap, action, params, risk, utt, trace, session) != 0) {
        observe_json(c, s);
        failure(c, "invalid data.cmd payload");
        return;
    }
    if (c->cb.on_command) c->cb.on_command(&ev, c->cb.user);
    observe_json(c, s);
    if (c->auto_fake_cmd_executor && !c->cb.on_command) send_ack_result(c, s);
}

static void dispatch_error(struct xiaoge_client *c, const char *s, const cJSON *root) {
    xiaoge_error_event ev;
    char code[64], message[512], trace[128], session[128];
    if (build_error_event(s, root, &ev, code, message, trace, session) != 0) {
        observe_json(c, s);
        failure(c, "invalid data.error payload");
        return;
    }
    if (c->cb.on_error) c->cb.on_error(&ev, c->cb.user);
    observe_json(c, s);
}

static void dispatch_text(struct xiaoge_client *c, const char *s) {
    cJSON *root = json_parse_object(s);
    if (!root) {
        observe_json(c, s);
        return;
    }
    if (json_type_is(root, "ctrl.ready")) {
        dispatch_ready(c, s, root);
    } else if (json_type_is(root, "ctrl.clear")) {
        xiaoge_clear_event ev;
        char reason[64], utt[128], trace[128], session[128];
        if (build_clear_event(s, root, &ev, reason, utt, trace, session) != 0) {
            observe_json(c, s);
            failure(c, "invalid ctrl.clear payload");
            cJSON_Delete(root);
            return;
        }
        if (c->cb.on_clear) c->cb.on_clear(&ev, c->cb.user);
        observe_json(c, s);
    } else if (json_type_is(root, "ctrl.state")) {
        xiaoge_state_event ev;
        char link[64], mode[64], gate[64], resource[64], pending[1024], trace[128], session[128];
        if (build_state_event(s, root, &ev, link, mode, gate, resource, pending, trace, session) != 0) {
            observe_json(c, s);
            failure(c, "invalid ctrl.state payload");
            cJSON_Delete(root);
            return;
        }
        if (c->cb.on_state) c->cb.on_state(&ev, c->cb.user);
        observe_json(c, s);
    } else if (json_type_is(root, "data.stt")) {
        dispatch_stt(c, s, root);
    } else if (json_type_is(root, "data.reply")) {
        dispatch_reply(c, s, root);
    } else if (json_type_is(root, "data.cmd")) {
        dispatch_command(c, s, root);
    } else if (json_type_is(root, "data.error")) {
        dispatch_error(c, s, root);
    } else {
        observe_json(c, s);
    }
    cJSON_Delete(root);
}

#ifdef XIAOGE_ENABLE_TEST_HOOKS

int xiaoge_test_build_stt_event(const char *json, xiaoge_stt_event *event, char *text, char *utt,
                                char *trace, char *session) {
    cJSON *root = json_parse_object(json);
    int rc = build_stt_event(json, root, event, text, utt, trace, session);
    cJSON_Delete(root);
    return rc;
}

int xiaoge_test_build_ready_event(const char *json, xiaoge_ready_event *event, char *caps,
                                  char *config, char *trace, char *session) {
    cJSON *root = json_parse_object(json);
    int rc = build_ready_event(json, root, event, caps, config, trace, session);
    cJSON_Delete(root);
    return rc;
}

int xiaoge_test_parse_session_url(const char *url) {
    struct parsed_url parsed;
    return parse_url(url, &parsed);
}

int xiaoge_test_build_reply_event(const char *json, xiaoge_reply_event *event, char *text, char *utt,
                                  char *intent, char *speak, char *trace, char *session) {
    cJSON *root = json_parse_object(json);
    int rc = build_reply_event(json, root, event, text, utt, intent, speak, trace, session);
    cJSON_Delete(root);
    return rc;
}

int xiaoge_test_build_error_event(const char *json, xiaoge_error_event *event, char *code, char *message,
                                  char *trace, char *session) {
    cJSON *root = json_parse_object(json);
    int rc = build_error_event(json, root, event, code, message, trace, session);
    cJSON_Delete(root);
    return rc;
}

int xiaoge_test_build_cmd_ack_event_json(const xiaoge_command_event *event, const char *status, const char *code,
                                         const char *message, char *out, size_t out_len) {
    return build_cmd_ack_event_json(event, status, code, message, out, out_len);
}
int xiaoge_test_build_cmd_ack_json(const char *cmd_json, const char *status, const char *code,
                                   const char *message, char *out, size_t out_len) {
    return build_cmd_ack_json(cmd_json, status, code, message, out, out_len);
}

int xiaoge_test_build_cmd_result_json(const char *cmd_json, const char *status, const char *code,
                                      const char *message, int retryable, long long started_at_ms,
                                      long long finished_at_ms, long long duration_ms, char *out,
                                      size_t out_len) {
    return build_cmd_result_json(cmd_json, status, code, message, retryable, started_at_ms,
                                 finished_at_ms, duration_ms, out, out_len);
}

int xiaoge_test_auto_fake_cmd_enabled(int value) {
    xiaoge_config cfg;
    memset(&cfg, 0, sizeof(cfg));
    cfg.auto_fake_cmd_executor = value;
    return auto_fake_cmd_enabled(&cfg);
}

int xiaoge_test_json_cloud_cmd_compatible(const char *cmd_json) {
    cJSON *root = json_parse_object(cmd_json);
    cJSON *params = cJSON_GetObjectItemCaseSensitive(root, "params");
    char cmd_id[128] = "", utt[128] = "", cap[128] = "", action[128] = "", risk[32] = "";
    long long ack_timeout = 0, result_timeout = 0, issued_at = -1;
    int ok = root && json_type_is(root, "data.cmd") &&
             json_get_string(root, "cmd_id", cmd_id, sizeof(cmd_id)) == 0 && strcmp(cmd_id, "cmd-cloud-json") == 0 &&
             json_get_string(root, "utterance_id", utt, sizeof(utt)) == 0 && strcmp(utt, "utt-cloud-json") == 0 &&
             json_get_string(root, "capability_id", cap, sizeof(cap)) == 0 && strcmp(cap, "motion.move") == 0 &&
             json_get_string(root, "action", action, sizeof(action)) == 0 && strcmp(action, "navigation.move") == 0 &&
             json_get_string(root, "risk_level", risk, sizeof(risk)) == 0 && strcmp(risk, "medium") == 0 &&
             cJSON_IsObject(params) && json_string_is(params, "direction", "forward") &&
             json_get_ll(root, "ack_timeout_ms", &ack_timeout) == 0 && ack_timeout == 800 &&
             json_get_ll(root, "result_timeout_ms", &result_timeout) == 0 && result_timeout == 5000 &&
             json_get_ll(root, "issued_at_ms", &issued_at) == 0 && issued_at == 1789000001000LL;
    cJSON_Delete(root);
    return ok ? 1 : 0;
}
#endif

static int cb_lws(struct lws *wsi, enum lws_callback_reasons reason,
                  void *user, void *in, size_t len) {
    struct xiaoge_client *c = (struct xiaoge_client *)lws_context_user(lws_get_context(wsi));
    (void)user;
    switch (reason) {
    case LWS_CALLBACK_CLIENT_APPEND_HANDSHAKE_HEADER: {
        unsigned char **p = (unsigned char **)in;
        unsigned char *end = (*p) + len;
        char auth[1024];
        snprintf(auth, sizeof(auth), "Bearer %s", nn(c->session.access_token, ""));
        if (lws_add_http_header_by_name(wsi, (const unsigned char *)"Authorization:",
                                        (const unsigned char *)auth, (int)strlen(auth), p, end))
            return -1;
        break;
    }
    case LWS_CALLBACK_CLIENT_ESTABLISHED:
        send_hello(c);
        break;
    case LWS_CALLBACK_CLIENT_RECEIVE: {
        int binary = lws_frame_is_binary(wsi);
        if (rx_append(c, in, len) != 0) break;
        if (lws_is_final_fragment(wsi) && lws_remaining_packet_payload(wsi) == 0) {
            if (binary) {
                if (c->cb.on_audio) c->cb.on_audio(c->rx, c->rx_len, c->cb.user);
            } else {
                dispatch_text(c, (const char *)c->rx);
            }
            c->rx_len = 0;
        }
        break;
    }
    case LWS_CALLBACK_CLIENT_WRITEABLE: {
        pthread_mutex_lock(&c->lock);
        struct out_buf *b = c->head;
        if (b) {
            c->head = b->next;
            if (!c->head) c->tail = NULL;
        }
        pthread_mutex_unlock(&c->lock);
        if (b) {
            enum lws_write_protocol proto = b->kind == OUT_TEXT ? LWS_WRITE_TEXT : LWS_WRITE_BINARY;
            lws_write(wsi, b->data + LWS_PRE, b->len, proto);
            free(b);
            pthread_mutex_lock(&c->lock);
            int more = c->head != NULL;
            pthread_mutex_unlock(&c->lock);
            if (more) lws_callback_on_writable(wsi);
        }
        break;
    }
    case LWS_CALLBACK_CLIENT_CONNECTION_ERROR:
        fprintf(stderr, "lws connection error: %s\n", in ? (const char *)in : "(no detail)");
        c->finished = 1;
        break;
    case LWS_CALLBACK_CLIENT_CLOSED:
        c->finished = 1;
        break;
    default:
        break;
    }
    return 0;
}

static const struct lws_protocols protocols[] = {
    {"default", cb_lws, 0, 8192, 0, NULL, 0},
    {NULL, NULL, 0, 0, 0, NULL, 0},
};

int xiaoge_build_create_session_request(const xiaoge_config *cfg, char *out, size_t out_len) {
    if (!cfg || !out || !cfg->device_id || !cfg->credential_json) return -1;
    cJSON *root = cJSON_CreateObject();
    cJSON *credential = json_parse_value(cfg->credential_json);
    cJSON *prefs = json_parse_value(nn(cfg->prefs_json, "{}"));
    cJSON *caps = cJSON_CreateArray();
    cJSON *audio_format = cJSON_CreateObject();
    if (!root || !credential || !prefs || !cJSON_IsObject(prefs) || !caps || !audio_format) {
        cJSON_Delete(root);
        cJSON_Delete(credential);
        cJSON_Delete(prefs);
        cJSON_Delete(caps);
        cJSON_Delete(audio_format);
        return -1;
    }
    const char *ver = nn(cfg->client_version, "xiaoge-c-sdk-r5.2.2");
    int ok = json_add_string(root, "device_id", cfg->device_id) == 0;
    if (ok) {
        ok = cJSON_AddItemToObject(root, "credential", credential);
        if (ok) credential = NULL;
    }
    if (ok) ok = caps_csv_to_json_array(cfg->caps_csv, caps) == 0;
    if (ok) {
        ok = cJSON_AddItemToObject(root, "caps", caps);
        if (ok) caps = NULL;
    }
    if (ok) {
        ok = cJSON_AddItemToObject(root, "prefs", prefs);
        if (ok) prefs = NULL;
    }
    if (ok) ok = json_add_number_ll(audio_format, "sample_rate", 16000) == 0;
    if (ok) ok = json_add_number_ll(audio_format, "channels", 1) == 0;
    if (ok) ok = json_add_string(audio_format, "sample_format", "int16le") == 0;
    if (ok) {
        ok = cJSON_AddItemToObject(root, "audio_format", audio_format);
        if (ok) audio_format = NULL;
    }
    if (ok) ok = json_add_string(root, "client_version", ver) == 0;
    int rc = ok && json_print_checked(root, out, out_len) == 0 ? (int)strlen(out) : -1;
    cJSON_Delete(audio_format);
    cJSON_Delete(caps);
    cJSON_Delete(prefs);
    cJSON_Delete(credential);
    cJSON_Delete(root);
    return rc;
}

xiaoge_client *xiaoge_create_from_session(const xiaoge_config *cfg,
                                          const xiaoge_session *session,
                                          int insecure,
                                          const xiaoge_callbacks *cb) {
    return xiaoge_create_from_session_with_ca(cfg, session, NULL, insecure, cb);
}

xiaoge_client *xiaoge_create_from_session_with_ca(const xiaoge_config *cfg,
                                                  const xiaoge_session *session,
                                                  const char *ca_cert_path,
                                                  int insecure,
                                                  const xiaoge_callbacks *cb) {
    if (!cfg || !session || !session->ws_url || parse_url(session->ws_url, &(struct parsed_url){0}) != 0)
        return NULL;
    struct xiaoge_client *c = (struct xiaoge_client *)calloc(1, sizeof(*c));
    if (!c) return NULL;
    c->cfg = *cfg;
    c->session = *session;
    c->auto_fake_cmd_executor = auto_fake_cmd_enabled(cfg);
    copy_callbacks(c, cb);
    if (c->cb.on_command && cfg && cfg->auto_fake_cmd_executor == XIAOGE_AUTO_FAKE_CMD_DEFAULT) {
        c->auto_fake_cmd_executor = 0;
    }
    pthread_mutex_init(&c->lock, NULL);
    if (parse_url(session->ws_url, &c->url) != 0) {
        xiaoge_destroy(c);
        return NULL;
    }
#ifdef _WIN32
    WSADATA wsa;
    WSAStartup(MAKEWORD(2, 2), &wsa);
#endif
    struct lws_context_creation_info info;
    memset(&info, 0, sizeof(info));
    info.port = CONTEXT_PORT_NO_LISTEN;
    info.protocols = protocols;
    info.user = c;
    info.vhost_name = "xiaoge-client";
    if (c->url.tls) info.options |= LWS_SERVER_OPTION_DO_SSL_GLOBAL_INIT;
    if (c->url.tls && ca_cert_path && *ca_cert_path && !insecure)
        info.client_ssl_ca_filepath = ca_cert_path;
    c->ctx = lws_create_context(&info);
    if (!c->ctx) {
        xiaoge_destroy(c);
        return NULL;
    }
    struct lws_client_connect_info ci;
    memset(&ci, 0, sizeof(ci));
    ci.context = c->ctx;
    ci.address = c->url.host;
    ci.port = c->url.port;
    ci.path = c->url.path;
    snprintf(c->host_header, sizeof(c->host_header), "%s", c->url.host);
    ci.host = c->host_header;
    ci.origin = c->host_header;
    ci.local_protocol_name = protocols[0].name;
    ci.protocol = NULL;
    ci.alpn = "http/1.1";
    ci.ietf_version_or_minus_one = -1;
    ci.pwsi = &c->wsi;
    if (c->url.tls) {
        ci.ssl_connection = LCCSCF_USE_SSL;
        if (insecure)
            ci.ssl_connection |= LCCSCF_ALLOW_SELFSIGNED |
                                 LCCSCF_SKIP_SERVER_CERT_HOSTNAME_CHECK |
                                 LCCSCF_ALLOW_INSECURE;
    }
    if (!lws_client_connect_via_info(&ci)) {
        xiaoge_destroy(c);
        return NULL;
    }
    return c;
}

int xiaoge_send_pcm(xiaoge_client *c, const void *pcm, size_t len) {
    return enqueue(c, OUT_BINARY, pcm, len);
}

int xiaoge_send_frontend_state(xiaoge_client *c, const char *trust_level,
                               const char *wake_state, const char *vad, int ttl_ms) {
    if (!c) return -1;
    if (!trust_level) trust_level = "hint";
    if (strcmp(trust_level, "authoritative") != 0 &&
        strcmp(trust_level, "hint") != 0 &&
        strcmp(trust_level, "observe") != 0)
        return -1;
    c->seq++;
    char buf[1024];
    cJSON *root = cJSON_CreateObject();
    if (!root) return -1;
    int ok = json_add_string(root, "type", "ctrl.frontend_state") == 0 &&
             json_add_string(root, "trace_id", c->session.trace_id) == 0 &&
             json_add_string(root, "session_id", c->session.session_id) == 0 &&
             json_add_number_ll(root, "seq", c->seq) == 0 &&
             json_add_number_ll(root, "ts_ms", now_ms()) == 0 &&
             json_add_number_ll(root, "ttl_ms", ttl_ms > 0 ? ttl_ms : 1000) == 0 &&
             json_add_string(root, "trust_level", trust_level) == 0 &&
             json_add_string(root, "wake_state", nn(wake_state, "unknown")) == 0 &&
             json_add_string(root, "vad", nn(vad, "unknown")) == 0;
    int rc = ok && json_print_checked(root, buf, sizeof(buf)) == 0 ? send_text(c, buf) : -1;
    cJSON_Delete(root);
    return rc;
}

int xiaoge_service(xiaoge_client *c, int timeout_ms) {
    if (!c || c->finished) return -1;
    lws_service(c->ctx, timeout_ms);
    return c->finished ? -1 : 0;
}

void xiaoge_destroy(xiaoge_client *c) {
    if (!c) return;
    if (c->ctx) lws_context_destroy(c->ctx);
    struct out_buf *b = c->head;
    while (b) {
        struct out_buf *n = b->next;
        free(b);
        b = n;
    }
    free(c->rx);
    pthread_mutex_destroy(&c->lock);
    free(c);
}
