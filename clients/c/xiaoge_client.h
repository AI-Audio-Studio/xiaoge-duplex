/* Xiaoge R5.2.2 C client SDK.
 *
 * Main path:
 *   create_session request JSON -> caller/cloud HTTP layer
 *   -> session.created -> WSS /ws/session with Authorization: Bearer
 *   -> ctrl.hello -> JSON ctrl/data frames + binary PCM.
 *
 * The C SDK keeps libwebsockets as its only transport dependency. It builds the
 * create_session JSON request but leaves the HTTPS POST to the embedding app, so
 * embedded targets can reuse their existing HTTP/TLS stack.
 */
#ifndef XIAOGE_CLIENT_H
#define XIAOGE_CLIENT_H

#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

#define XIAOGE_SAMPLE_RATE 16000
#define XIAOGE_JSON_MAX_BYTES 8192
#define XIAOGE_BINARY_MAX_BYTES 32768
#define XIAOGE_MANIFEST_SHA256 "845F0F4125061FF37A7F4DA20E0C88BC089200A08B319F1035D6522C80B56559"
#define XIAOGE_DEFAULT_API_KEY ""
#define XIAOGE_AUTO_FAKE_CMD_DEFAULT 0
#define XIAOGE_AUTO_FAKE_CMD_DISABLED -1
#define XIAOGE_AUTO_FAKE_CMD_ENABLED 1
#define XIAOGE_CMD_ACK_STATUS_ACCEPTED "accepted"
#define XIAOGE_CMD_ACK_STATUS_REJECTED "rejected"
#define XIAOGE_CMD_ACK_STATUS_DUPLICATE "duplicate"
#define XIAOGE_CMD_RESULT_STATUS_RUNNING "running"
#define XIAOGE_CMD_RESULT_STATUS_SUCCEEDED "succeeded"
#define XIAOGE_CMD_RESULT_STATUS_FAILED "failed"
#define XIAOGE_CMD_RESULT_STATUS_CANCELED "canceled"
#define XIAOGE_CMD_RESULT_STATUS_TIMEOUT "timeout"

#define XIAOGE_ERROR_AUTH_FAILED "auth_failed"
#define XIAOGE_ERROR_PERMISSION_DENIED "permission_denied"
#define XIAOGE_ERROR_BUSY "busy"
#define XIAOGE_ERROR_PROTOCOL_ERROR "protocol_error"
#define XIAOGE_ERROR_CAPABILITY_UNSUPPORTED "capability_unsupported"
#define XIAOGE_ERROR_TOKEN_EXPIRED "token_expired"
#define XIAOGE_ERROR_DUPLICATE_CONNECTION "duplicate_connection"
#define XIAOGE_ERROR_RESOURCE_EXHAUSTED "resource_exhausted"
#define XIAOGE_ERROR_UNKNOWN_CMD_ID "unknown_cmd_id"

typedef struct xiaoge_client xiaoge_client;

typedef struct {
    const char *device_id;
    const char *credential_json; /* object JSON or quoted/string JSON */
    const char *caps_csv;        /* default: audio,text,cmd,state */
    const char *client_version;  /* default: xiaoge-c-sdk-r5.2.2 */
    const char *prefs_json;      /* optional object JSON */
    const char *api_key;         /* create_session HTTP x-api-key; default: no header */
    int auto_fake_cmd_executor;  /* disabled=-1, default/enabled: built-in fake executor */
} xiaoge_config;

typedef struct {
    const char *trace_id;
    const char *session_id;
    const char *access_token;
    const char *ws_url;          /* ws(s)://host[:port]/ws/session */
    const char *granted_caps_csv;
    const char *config_version;
} xiaoge_session;


typedef struct {
    int sample_rate;
    const char *granted_caps;
    const char *config_version;
    const char *trace_id;
    const char *session_id;
    const char *raw_json;
} xiaoge_ready_event;

typedef struct {
    const char *reason;
    const char *utterance_id;
    const char *trace_id;
    const char *session_id;
    const char *raw_json;
} xiaoge_clear_event;

typedef struct {
    const char *link_state;
    const char *interaction_mode;
    const char *engine_gate;
    const char *resource_state;
    const char *pending_confirmation_json;
    long long ts_ms;
    const char *trace_id;
    const char *session_id;
    const char *raw_json;
} xiaoge_state_event;

typedef struct {
    const char *text;
    int final;
    const char *utterance_id;
    const char *trace_id;
    const char *session_id;
    long long ts_ms;
    const char *raw_json;
} xiaoge_stt_event;

typedef struct {
    const char *text;
    int final; /* SDK-derived for R5.2.2 data.reply, always 1. */
    const char *utterance_id;
    const char *intent_type;
    const char *speak_policy; /* nullable */
    const char *trace_id;
    const char *session_id;
    long long ts_ms;
    const char *raw_json;
} xiaoge_reply_event;

typedef struct {
    const char *cmd_id;
    const char *capability_id;
    const char *action;
    const char *params_json;
    const char *risk_level;
    long long ack_timeout_ms;
    long long result_timeout_ms;
    long long issued_at_ms;
    const char *utterance_id;
    const char *trace_id;
    const char *session_id;
    const char *raw_json;
} xiaoge_command_event;

typedef struct {
    const char *code;
    const char *message;
    int retryable;
    long long ts_ms;
    const char *trace_id;
    const char *session_id;
    const char *raw_json;
} xiaoge_error_event;

typedef struct {
    size_t struct_size;
    void (*on_ready)(const xiaoge_ready_event *event, void *user);
    void (*on_audio)(const void *pcm, size_t len, void *user);
    void (*on_clear)(const xiaoge_clear_event *event, void *user);
    void (*on_state)(const xiaoge_state_event *event, void *user);
    void (*on_stt)(const xiaoge_stt_event *event, void *user);
    void (*on_reply)(const xiaoge_reply_event *event, void *user);
    void (*on_command)(const xiaoge_command_event *event, void *user);
    void (*on_error)(const xiaoge_error_event *event, void *user);
    void (*on_json)(const char *json, void *user);
    void (*on_failure)(int code, const char *message, void *user);
    void *user;
} xiaoge_callbacks;

/* Build a compact create_session request JSON. Returns bytes written excluding
 * NUL, or -1 if the buffer is too small or config is invalid. */
int xiaoge_build_create_session_request(const xiaoge_config *cfg, char *out, size_t out_len);

/* Connect to session.ws_url using Authorization: Bearer <access_token>. */
xiaoge_client *xiaoge_create_from_session(const xiaoge_config *cfg,
                                          const xiaoge_session *session,
                                          int insecure,
                                          const xiaoge_callbacks *cb);

/* Connect with an explicit PEM CA bundle for WSS server verification.
 * ca_cert_path may be NULL to use the platform/libwebsockets default trust
 * store. insecure is still supported for test-only environments. */
xiaoge_client *xiaoge_create_from_session_with_ca(const xiaoge_config *cfg,
                                                  const xiaoge_session *session,
                                                  const char *ca_cert_path,
                                                  int insecure,
                                                  const xiaoge_callbacks *cb);

int xiaoge_send_pcm(xiaoge_client *c, const void *pcm, size_t len);
int xiaoge_send_frontend_state(xiaoge_client *c, const char *trust_level,
                               const char *wake_state, const char *vad, int ttl_ms);

/* Send command acknowledgement/result frames derived from a downlink data.cmd JSON.
 * cmd_json must contain trace_id, session_id, utterance_id, and cmd_id. */
int xiaoge_send_cmd_ack(xiaoge_client *c, const char *cmd_json,
                        const char *status, const char *code, const char *message);
int xiaoge_send_cmd_result(xiaoge_client *c, const char *cmd_json,
                           const char *status, const char *code, const char *message,
                           int retryable);
int xiaoge_send_cmd_result_ex(xiaoge_client *c, const char *cmd_json,
                              const char *status, const char *code, const char *message,
                              int retryable, long long started_at_ms,
                              long long finished_at_ms, long long duration_ms);
int xiaoge_send_cmd_ack_event(xiaoge_client *c, const xiaoge_command_event *event,
                              const char *status, const char *code, const char *message);
int xiaoge_send_cmd_result_event(xiaoge_client *c, const xiaoge_command_event *event,
                                 const char *status, const char *code, const char *message,
                                 int retryable);
int xiaoge_service(xiaoge_client *c, int timeout_ms);
void xiaoge_destroy(xiaoge_client *c);

#ifdef __cplusplus
}
#endif

#endif
