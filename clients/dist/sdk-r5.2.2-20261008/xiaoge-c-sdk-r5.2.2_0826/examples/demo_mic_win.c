/* R5.2.2 Windows microphone demo.
 *
 * Default usage with cloud gateway:
 *   xiaoge_demo_mic_win [--seconds N] [--reply-wait N] [--no-playback]
 *
 * Explicit usage:
 *   xiaoge_demo_mic_win [create_session_url] [device_id] [credential-json-or-string]
 *                       [--seconds N] [--reply-wait N] [--silence-ms N]
 *                       [--no-playback] [--ca-cert path] [--insecure]
 *                       [--api-key key]
 */
#include "xiaoge_client.h"
#include "cJSON.h"

#ifndef _WIN32
#error "demo_mic_win.c requires Windows waveIn/waveOut APIs"
#endif

#include <curl/curl.h>
#include <windows.h>
#include <mmsystem.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#define DEFAULT_CREATE_SESSION_URL "https://60.205.197.165:10099/create_session"
#define DEFAULT_DEVICE_ID "robot-x3-001"
#define DEFAULT_CREDENTIAL_JSON "{\"key_id\":\"dev\",\"signature\":\"mock\"}"
#define FRAME_SAMPLES 320
#define FRAME_BYTES (FRAME_SAMPLES * 2)
#define INPUT_BUFFER_FRAMES 5
#define INPUT_BUFFER_BYTES (FRAME_BYTES * INPUT_BUFFER_FRAMES)
#define INPUT_BUFFER_COUNT 8
#define QUEUE_CAP 1000
#define DEFAULT_SILENCE_MS 1000
#define DEFAULT_REPLY_WAIT_SECONDS 8
#define SEND_BURST_FRAMES 8
#define SESSION_FIELD_MAX 512

struct http_buf {
    char *data;
    size_t len;
};

struct session_fields {
    char ws_url[SESSION_FIELD_MAX];
    char access_token[SESSION_FIELD_MAX];
    char trace_id[SESSION_FIELD_MAX];
    char session_id[SESSION_FIELD_MAX];
    char config_version[SESSION_FIELD_MAX];
};

struct pcm_queue {
    CRITICAL_SECTION lock;
    unsigned char frames[QUEUE_CAP][FRAME_BYTES];
    size_t lens[QUEUE_CAP];
    int head;
    int count;
    unsigned long dropped;
    size_t captured_bytes;
    unsigned long captured_frames;
};

struct input_audio {
    HWAVEIN handle;
    WAVEHDR headers[INPUT_BUFFER_COUNT];
    unsigned char data[INPUT_BUFFER_COUNT][INPUT_BUFFER_BYTES];
    struct pcm_queue *queue;
    volatile LONG active;
};

struct playback_item {
    WAVEHDR header;
    unsigned char *data;
    struct playback_item *next;
};

struct playback {
    HWAVEOUT handle;
    CRITICAL_SECTION lock;
    struct playback_item *items;
    int enabled;
};

struct app_state {
    volatile LONG ready;
    volatile LONG stop;
    volatile LONG sent_state;
    size_t sent_bytes;
    size_t received_bytes;
    struct playback playback;
};

struct service_ctx {
    xiaoge_client *client;
    struct app_state *state;
};

static struct app_state *g_state;

static void usage(const char *argv0) {
    fprintf(stderr,
        "usage:\n"
        "  %s [create_session_url] [device_id] [credential-json-or-string] "
        "[--seconds N] [--reply-wait N] [--silence-ms N] [--no-playback] "
        "[--ca-cert path] [--insecure] [--api-key key]\n"
        "defaults:\n"
        "  create_session_url=%s\n"
        "  device_id=%s\n"
        "  credential=%s\n"
        "  --seconds 0 captures until Ctrl-C; --reply-wait %d waits after capture stops\n",
        argv0, DEFAULT_CREATE_SESSION_URL, DEFAULT_DEVICE_ID, DEFAULT_CREDENTIAL_JSON,
        DEFAULT_REPLY_WAIT_SECONDS);
}

static int arg_is_value(const char *s) {
    return s && strncmp(s, "--", 2) != 0;
}

static const char *skip_ws(const char *p) {
    while (p && *p == ' ') p++;
    return p;
}

static int make_credential_json(const char *raw, char *out, size_t out_len) {
    const char *p = skip_ws(raw);
    if (!p || !*p) return -1;
    if (*p == '{' || *p == '[' || *p == '"') {
        int n = snprintf(out, out_len, "%s", raw);
        return (n > 0 && (size_t)n < out_len) ? 0 : -1;
    }
    char *w = out;
    size_t left = out_len;
    if (left < 3) return -1;
    *w++ = '"';
    left--;
    for (; *raw; raw++) {
        if (left < 3) return -1;
        if (*raw == '"' || *raw == '\\') {
            *w++ = '\\';
            left--;
        }
        *w++ = *raw;
        left--;
    }
    *w++ = '"';
    *w = 0;
    return 0;
}

static size_t curl_write_cb(char *ptr, size_t size, size_t nmemb, void *userdata) {
    size_t n = size * nmemb;
    struct http_buf *b = (struct http_buf *)userdata;
    char *p = (char *)realloc(b->data, b->len + n + 1);
    if (!p) return 0;
    b->data = p;
    memcpy(b->data + b->len, ptr, n);
    b->len += n;
    b->data[b->len] = 0;
    return n;
}

static int json_get_string(const char *json, const char *key, char *out, size_t out_len) {
    cJSON *root = cJSON_Parse(json);
    cJSON *item = cJSON_GetObjectItemCaseSensitive(root, key);
    const char *value = cJSON_GetStringValue(item);
    if (!value || !out || out_len == 0 || strlen(value) >= out_len) {
        cJSON_Delete(root);
        return -1;
    }
    memcpy(out, value, strlen(value) + 1);
    cJSON_Delete(root);
    return 0;
}

static int create_session_http(const char *url, const xiaoge_config *cfg,
                               int insecure, const char *ca_cert_path,
                               struct session_fields *out) {
    char body[2048];
    if (xiaoge_build_create_session_request(cfg, body, sizeof(body)) <= 0) {
        fprintf(stderr, "failed to build create_session request\n");
        return -1;
    }

    CURL *curl = curl_easy_init();
    if (!curl) return -1;
    char curl_err[CURL_ERROR_SIZE] = {0};
    struct http_buf resp = {0};
    struct curl_slist *headers = NULL;
    headers = curl_slist_append(headers, "Content-Type: application/json");
    const char *api_key = cfg->api_key ? cfg->api_key : XIAOGE_DEFAULT_API_KEY;
    if (api_key && *api_key) {
        char api_header[1024];
        snprintf(api_header, sizeof(api_header), "x-api-key: %s", api_key);
        headers = curl_slist_append(headers, api_header);
    }

    curl_easy_setopt(curl, CURLOPT_URL, url);
    curl_easy_setopt(curl, CURLOPT_HTTPHEADER, headers);
    curl_easy_setopt(curl, CURLOPT_POSTFIELDS, body);
    curl_easy_setopt(curl, CURLOPT_POSTFIELDSIZE, (long)strlen(body));
    curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, curl_write_cb);
    curl_easy_setopt(curl, CURLOPT_WRITEDATA, &resp);
    curl_easy_setopt(curl, CURLOPT_ERRORBUFFER, curl_err);
    curl_easy_setopt(curl, CURLOPT_TIMEOUT, 30L);
    curl_easy_setopt(curl, CURLOPT_IPRESOLVE, CURL_IPRESOLVE_V4);
    curl_easy_setopt(curl, CURLOPT_NOPROXY, "*");
    curl_easy_setopt(curl, CURLOPT_PROXY, "");
    if (getenv("XIAOGE_CURL_VERBOSE")) curl_easy_setopt(curl, CURLOPT_VERBOSE, 1L);
    if (insecure) {
        curl_easy_setopt(curl, CURLOPT_SSL_VERIFYPEER, 0L);
        curl_easy_setopt(curl, CURLOPT_SSL_VERIFYHOST, 0L);
    } else if (ca_cert_path && *ca_cert_path) {
        curl_easy_setopt(curl, CURLOPT_CAINFO, ca_cert_path);
    }

    CURLcode rc = curl_easy_perform(curl);
    long http_status = 0;
    curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &http_status);
    curl_slist_free_all(headers);
    curl_easy_cleanup(curl);

    if (rc != CURLE_OK || http_status < 200 || http_status >= 300) {
        fprintf(stderr, "create_session failed curl=%d error=%s http=%ld body=%s\n",
                (int)rc, curl_err, http_status, resp.data ? resp.data : "");
        free(resp.data);
        return -1;
    }
    if (json_get_string(resp.data, "ws_url", out->ws_url, sizeof(out->ws_url)) != 0 ||
        json_get_string(resp.data, "access_token", out->access_token, sizeof(out->access_token)) != 0 ||
        json_get_string(resp.data, "trace_id", out->trace_id, sizeof(out->trace_id)) != 0 ||
        json_get_string(resp.data, "session_id", out->session_id, sizeof(out->session_id)) != 0) {
        fprintf(stderr, "create_session response missing required fields: %s\n", resp.data ? resp.data : "");
        free(resp.data);
        return -1;
    }
    if (json_get_string(resp.data, "config_version", out->config_version, sizeof(out->config_version)) != 0)
        snprintf(out->config_version, sizeof(out->config_version), "%s", "unknown");
    free(resp.data);
    return 0;
}

static void queue_init(struct pcm_queue *q) {
    InitializeCriticalSection(&q->lock);
    q->head = 0;
    q->count = 0;
    q->dropped = 0;
    q->captured_bytes = 0;
    q->captured_frames = 0;
}

static void queue_destroy(struct pcm_queue *q) {
    DeleteCriticalSection(&q->lock);
}

static void queue_push(struct pcm_queue *q, const unsigned char *data, size_t len) {
    if (len > FRAME_BYTES) len = FRAME_BYTES;
    EnterCriticalSection(&q->lock);
    q->captured_bytes += len;
    q->captured_frames++;
    if (q->count == QUEUE_CAP) {
        q->head = (q->head + 1) % QUEUE_CAP;
        q->count--;
        q->dropped++;
    }
    int idx = (q->head + q->count) % QUEUE_CAP;
    memcpy(q->frames[idx], data, len);
    q->lens[idx] = len;
    q->count++;
    LeaveCriticalSection(&q->lock);
}

static int queue_pop(struct pcm_queue *q, unsigned char *out, size_t *len) {
    int ok = 0;
    EnterCriticalSection(&q->lock);
    if (q->count > 0) {
        memcpy(out, q->frames[q->head], q->lens[q->head]);
        *len = q->lens[q->head];
        q->head = (q->head + 1) % QUEUE_CAP;
        q->count--;
        ok = 1;
    }
    LeaveCriticalSection(&q->lock);
    return ok;
}

static unsigned long queue_dropped(struct pcm_queue *q) {
    unsigned long dropped;
    EnterCriticalSection(&q->lock);
    dropped = q->dropped;
    LeaveCriticalSection(&q->lock);
    return dropped;
}

static int queue_discard_pending(struct pcm_queue *q) {
    int discarded;
    EnterCriticalSection(&q->lock);
    discarded = q->count;
    q->dropped += (unsigned long)q->count;
    q->head = 0;
    q->count = 0;
    LeaveCriticalSection(&q->lock);
    return discarded;
}

static void queue_stats(struct pcm_queue *q, size_t *captured_bytes, unsigned long *captured_frames,
                        unsigned long *dropped, int *pending) {
    EnterCriticalSection(&q->lock);
    *captured_bytes = q->captured_bytes;
    *captured_frames = q->captured_frames;
    *dropped = q->dropped;
    *pending = q->count;
    LeaveCriticalSection(&q->lock);
}

static DWORD WINAPI service_thread_main(void *arg) {
    struct service_ctx *ctx = (struct service_ctx *)arg;
    while (!InterlockedCompareExchange(&ctx->state->stop, 0, 0)) {
        if (xiaoge_service(ctx->client, 5) != 0) {
            InterlockedExchange(&ctx->state->stop, 1);
            break;
        }
    }
    return 0;
}

static void CALLBACK wave_in_cb(HWAVEIN hwi, UINT msg, DWORD_PTR instance,
                                DWORD_PTR param1, DWORD_PTR param2) {
    (void)param2;
    if (msg != WIM_DATA) return;
    struct input_audio *in = (struct input_audio *)instance;
    WAVEHDR *hdr = (WAVEHDR *)param1;
    if (!in || !hdr) return;
    if (hdr->dwBytesRecorded > 0 && InterlockedCompareExchange(&in->active, 0, 0)) {
        unsigned char *p = (unsigned char *)hdr->lpData;
        DWORD left = hdr->dwBytesRecorded;
        while (left > 0) {
            size_t n = left < FRAME_BYTES ? (size_t)left : FRAME_BYTES;
            queue_push(in->queue, p, n);
            p += n;
            left -= (DWORD)n;
        }
    }
    if (InterlockedCompareExchange(&in->active, 0, 0)) {
        hdr->dwFlags &= ~WHDR_DONE;
        waveInAddBuffer(hwi, hdr, sizeof(*hdr));
    }
}

static int input_start(struct input_audio *in, struct pcm_queue *queue) {
    memset(in, 0, sizeof(*in));
    in->queue = queue;
    WAVEFORMATEX fmt;
    memset(&fmt, 0, sizeof(fmt));
    fmt.wFormatTag = WAVE_FORMAT_PCM;
    fmt.nChannels = 1;
    fmt.nSamplesPerSec = XIAOGE_SAMPLE_RATE;
    fmt.wBitsPerSample = 16;
    fmt.nBlockAlign = (WORD)(fmt.nChannels * fmt.wBitsPerSample / 8);
    fmt.nAvgBytesPerSec = fmt.nSamplesPerSec * fmt.nBlockAlign;

    MMRESULT mm = waveInOpen(&in->handle, WAVE_MAPPER, &fmt,
                             (DWORD_PTR)wave_in_cb, (DWORD_PTR)in, CALLBACK_FUNCTION);
    if (mm != MMSYSERR_NOERROR) {
        fprintf(stderr, "waveInOpen failed mm=%u\n", (unsigned)mm);
        return -1;
    }
    InterlockedExchange(&in->active, 1);
    for (int i = 0; i < INPUT_BUFFER_COUNT; i++) {
        in->headers[i].lpData = (LPSTR)in->data[i];
        in->headers[i].dwBufferLength = INPUT_BUFFER_BYTES;
        mm = waveInPrepareHeader(in->handle, &in->headers[i], sizeof(in->headers[i]));
        if (mm != MMSYSERR_NOERROR) {
            fprintf(stderr, "waveInPrepareHeader failed mm=%u\n", (unsigned)mm);
            return -1;
        }
        mm = waveInAddBuffer(in->handle, &in->headers[i], sizeof(in->headers[i]));
        if (mm != MMSYSERR_NOERROR) {
            fprintf(stderr, "waveInAddBuffer failed mm=%u\n", (unsigned)mm);
            return -1;
        }
    }
    mm = waveInStart(in->handle);
    if (mm != MMSYSERR_NOERROR) {
        fprintf(stderr, "waveInStart failed mm=%u\n", (unsigned)mm);
        return -1;
    }
    return 0;
}

static void input_stop(struct input_audio *in) {
    if (!in->handle) return;
    InterlockedExchange(&in->active, 0);
    waveInStop(in->handle);
    waveInReset(in->handle);
    for (int i = 0; i < INPUT_BUFFER_COUNT; i++) {
        if (in->headers[i].dwFlags & WHDR_PREPARED)
            waveInUnprepareHeader(in->handle, &in->headers[i], sizeof(in->headers[i]));
    }
    waveInClose(in->handle);
    in->handle = NULL;
}

static void playback_init(struct playback *p, int enabled) {
    memset(p, 0, sizeof(*p));
    p->enabled = enabled;
    InitializeCriticalSection(&p->lock);
}

static int playback_open(struct playback *p) {
    if (!p->enabled) return 0;
    WAVEFORMATEX fmt;
    memset(&fmt, 0, sizeof(fmt));
    fmt.wFormatTag = WAVE_FORMAT_PCM;
    fmt.nChannels = 1;
    fmt.nSamplesPerSec = XIAOGE_SAMPLE_RATE;
    fmt.wBitsPerSample = 16;
    fmt.nBlockAlign = (WORD)(fmt.nChannels * fmt.wBitsPerSample / 8);
    fmt.nAvgBytesPerSec = fmt.nSamplesPerSec * fmt.nBlockAlign;
    MMRESULT mm = waveOutOpen(&p->handle, WAVE_MAPPER, &fmt, 0, 0, CALLBACK_NULL);
    if (mm != MMSYSERR_NOERROR) {
        fprintf(stderr, "waveOutOpen failed mm=%u; continuing without playback\n", (unsigned)mm);
        p->enabled = 0;
        return -1;
    }
    return 0;
}

static void playback_collect_done(struct playback *p) {
    EnterCriticalSection(&p->lock);
    struct playback_item **cur = &p->items;
    while (*cur) {
        struct playback_item *item = *cur;
        if (item->header.dwFlags & WHDR_DONE) {
            *cur = item->next;
            waveOutUnprepareHeader(p->handle, &item->header, sizeof(item->header));
            free(item->data);
            free(item);
        } else {
            cur = &item->next;
        }
    }
    LeaveCriticalSection(&p->lock);
}

static void playback_push(struct playback *p, const void *pcm, size_t len) {
    if (!p->enabled || !p->handle || !pcm || !len) return;
    playback_collect_done(p);
    struct playback_item *item = (struct playback_item *)calloc(1, sizeof(*item));
    if (!item) return;
    item->data = (unsigned char *)malloc(len);
    if (!item->data) {
        free(item);
        return;
    }
    memcpy(item->data, pcm, len);
    item->header.lpData = (LPSTR)item->data;
    item->header.dwBufferLength = (DWORD)len;
    if (waveOutPrepareHeader(p->handle, &item->header, sizeof(item->header)) != MMSYSERR_NOERROR ||
        waveOutWrite(p->handle, &item->header, sizeof(item->header)) != MMSYSERR_NOERROR) {
        free(item->data);
        free(item);
        return;
    }
    EnterCriticalSection(&p->lock);
    item->next = p->items;
    p->items = item;
    LeaveCriticalSection(&p->lock);
}

static void playback_clear(struct playback *p) {
    if (!p->handle) return;
    waveOutReset(p->handle);
    playback_collect_done(p);
}

static void playback_close(struct playback *p) {
    if (p->handle) {
        waveOutReset(p->handle);
        playback_collect_done(p);
        waveOutClose(p->handle);
        p->handle = NULL;
    }
    DeleteCriticalSection(&p->lock);
}

static void on_ready(const xiaoge_ready_event *event, void *u) {
    struct app_state *s = (struct app_state *)u;
    InterlockedExchange(&s->ready, 1);
    printf("ready sample_rate=%d\n", event->sample_rate);
}

static void on_audio(const void *pcm, size_t n, void *u) {
    struct app_state *s = (struct app_state *)u;
    s->received_bytes += n;
    playback_push(&s->playback, pcm, n);
}

static void on_clear(const xiaoge_clear_event *event, void *u) {
    (void)event;
    struct app_state *s = (struct app_state *)u;
    playback_clear(&s->playback);
    printf("clear\n");
}

static void on_json(const char *json, void *u) {
    (void)u;
    printf("%s\n", json);
}

static BOOL WINAPI console_handler(DWORD type) {
    if (type == CTRL_C_EVENT || type == CTRL_BREAK_EVENT || type == CTRL_CLOSE_EVENT) {
        if (g_state) InterlockedExchange(&g_state->stop, 1);
        return TRUE;
    }
    return FALSE;
}

int main(int argc, char **argv) {
    SetConsoleOutputCP(CP_UTF8);
    SetConsoleCP(CP_UTF8);
    const char *create_session_url = DEFAULT_CREATE_SESSION_URL;
    const char *device_id = DEFAULT_DEVICE_ID;
    const char *credential_raw = DEFAULT_CREDENTIAL_JSON;
    const char *ca_cert_path = NULL;
    const char *api_key = getenv("XIAOGE_CLOUD_API_KEY");
    if (!api_key) api_key = XIAOGE_DEFAULT_API_KEY;
    int insecure = 0;
    int seconds = 0;
    int reply_wait_seconds = DEFAULT_REPLY_WAIT_SECONDS;
    int silence_ms = DEFAULT_SILENCE_MS;
    int playback_enabled = 1;
    int positional = 0;

    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--help") == 0 || strcmp(argv[i], "-h") == 0) {
            usage(argv[0]);
            return 0;
        } else if (strcmp(argv[i], "--insecure") == 0) {
            insecure = 1;
        } else if (strcmp(argv[i], "--no-playback") == 0) {
            playback_enabled = 0;
        } else if (strcmp(argv[i], "--seconds") == 0 && i + 1 < argc && arg_is_value(argv[i + 1])) {
            seconds = atoi(argv[++i]);
            if (seconds < 0) {
                fprintf(stderr, "--seconds must be >= 0\n");
                return 2;
            }
        } else if (strcmp(argv[i], "--reply-wait") == 0 && i + 1 < argc && arg_is_value(argv[i + 1])) {
            reply_wait_seconds = atoi(argv[++i]);
            if (reply_wait_seconds < 0) {
                fprintf(stderr, "--reply-wait must be >= 0\n");
                return 2;
            }
        } else if (strcmp(argv[i], "--silence-ms") == 0 && i + 1 < argc && arg_is_value(argv[i + 1])) {
            silence_ms = atoi(argv[++i]);
            if (silence_ms < 0) {
                fprintf(stderr, "--silence-ms must be >= 0\n");
                return 2;
            }
        } else if (strcmp(argv[i], "--ca-cert") == 0 && i + 1 < argc && arg_is_value(argv[i + 1])) {
            ca_cert_path = argv[++i];
        } else if (strcmp(argv[i], "--api-key") == 0 && i + 1 < argc && arg_is_value(argv[i + 1])) {
            api_key = argv[++i];
        } else if (strncmp(argv[i], "--", 2) == 0) {
            fprintf(stderr, "unknown option: %s\n", argv[i]);
            usage(argv[0]);
            return 2;
        } else {
            if (positional == 0) create_session_url = argv[i];
            else if (positional == 1) device_id = argv[i];
            else if (positional == 2) credential_raw = argv[i];
            else {
                fprintf(stderr, "too many positional arguments\n");
                usage(argv[0]);
                return 2;
            }
            positional++;
        }
    }

    curl_global_init(CURL_GLOBAL_DEFAULT);
    char credential_json[1024];
    if (make_credential_json(credential_raw, credential_json, sizeof(credential_json)) != 0) {
        fprintf(stderr, "invalid credential JSON/string\n");
        return 1;
    }
    xiaoge_config cfg = {
        device_id,
        credential_json,
        "audio,text,cmd,state",
        "xiaoge-c-mic-demo-r5.2.2",
        "{}",
        api_key,
    };
    struct session_fields created = {0};
    if (create_session_http(create_session_url, &cfg, insecure, ca_cert_path, &created) != 0) {
        return 1;
    }
    printf("session trace_id=%s session_id=%s ws_url=%s\n",
           created.trace_id, created.session_id, created.ws_url);

    xiaoge_session sess = {
        created.trace_id,
        created.session_id,
        created.access_token,
        created.ws_url,
        "audio,text,cmd,state",
        created.config_version,
    };

    struct app_state state;
    memset(&state, 0, sizeof(state));
    playback_init(&state.playback, playback_enabled);
    playback_open(&state.playback);
    g_state = &state;
    SetConsoleCtrlHandler(console_handler, TRUE);

    xiaoge_callbacks cb = {
        .struct_size = sizeof(cb),
        .on_ready = on_ready,
        .on_audio = on_audio,
        .on_clear = on_clear,
        .on_json = on_json,
        .user = &state,
    };
    xiaoge_client *c = xiaoge_create_from_session_with_ca(&cfg, &sess, ca_cert_path, insecure, &cb);
    if (!c) {
        fprintf(stderr, "failed to connect\n");
        playback_close(&state.playback);
        return 1;
    }

    struct service_ctx svc = {c, &state};
    HANDLE svc_thread = CreateThread(NULL, 0, service_thread_main, &svc, 0, NULL);
    if (!svc_thread) {
        fprintf(stderr, "failed to start service thread\n");
        xiaoge_destroy(c);
        playback_close(&state.playback);
        return 1;
    }

    struct pcm_queue q;
    struct input_audio input;
    int input_started = 0;
    queue_init(&q);
    memset(&input, 0, sizeof(input));
    printf("waiting for ready; microphone starts after ready\n");

    unsigned char frame[FRAME_BYTES];
    size_t frame_len = 0;
    DWORD started = GetTickCount();
    DWORD reply_wait_started = 0;
    unsigned long last_dropped = 0;
    int capture_done = 0;
    while (!InterlockedCompareExchange(&state.stop, 0, 0)) {
        if (InterlockedCompareExchange(&state.ready, 0, 0) &&
            !InterlockedCompareExchange(&state.sent_state, 1, 1)) {
            xiaoge_send_frontend_state(c, "hint", "awake", "speech", 1000);
            InterlockedExchange(&state.sent_state, 1);
            if (!input_started) {
                if (input_start(&input, &q) != 0) {
                    InterlockedExchange(&state.stop, 1);
                    break;
                }
                input_started = 1;
                started = GetTickCount();
                printf("talking; press Ctrl-C to exit\n");
            }
        }
        int sent_count = 0;
        while (input_started && sent_count < SEND_BURST_FRAMES && queue_pop(&q, frame, &frame_len)) {
            if (xiaoge_send_pcm(c, frame, frame_len) == 0) {
                state.sent_bytes += frame_len;
                sent_count++;
            }
        }
        playback_collect_done(&state.playback);
        unsigned long dropped = queue_dropped(&q);
        if (dropped != last_dropped && (dropped < 50 || dropped % 50 == 0)) {
            fprintf(stderr, "warning: dropped %lu mic frames\n", dropped);
            last_dropped = dropped;
        }
        if (!capture_done && seconds > 0 && input_started &&
            (GetTickCount() - started) >= (DWORD)seconds * 1000U) {
            input_stop(&input);
            input_started = 0;
            capture_done = 1;
            int discarded = queue_discard_pending(&q);
            xiaoge_send_frontend_state(c, "hint", "awake", "silence", 1000);
            printf("capture stopped; discarded %d stale queued mic frames, sent vad=silence, then sending %d ms silence and waiting %d seconds for reply\n",
                   discarded, silence_ms, reply_wait_seconds);
            if (silence_ms > 0) {
                memset(frame, 0, sizeof(frame));
                int silence_frames = (silence_ms + 19) / 20;
                for (int i = 0; i < silence_frames; i++) {
                    if (xiaoge_send_pcm(c, frame, FRAME_BYTES) == 0) state.sent_bytes += FRAME_BYTES;
                    Sleep(20);
                }
            }
            reply_wait_started = GetTickCount();
            if (reply_wait_seconds == 0) break;
        }
        if (capture_done && reply_wait_seconds > 0 &&
            (GetTickCount() - reply_wait_started) >= (DWORD)reply_wait_seconds * 1000U) break;
        Sleep(sent_count > 0 ? 1 : 2);
    }

    if (input_started) input_stop(&input);
    InterlockedExchange(&state.stop, 1);
    WaitForSingleObject(svc_thread, 2000);
    CloseHandle(svc_thread);
    size_t captured_bytes = 0;
    unsigned long captured_frames = 0;
    unsigned long dropped = 0;
    int pending = 0;
    queue_stats(&q, &captured_bytes, &captured_frames, &dropped, &pending);
    printf("captured=%zu captured_frames=%lu sent=%zu received=%zu dropped=%lu pending=%d\n",
           captured_bytes, captured_frames, state.sent_bytes, state.received_bytes, dropped, pending);
    xiaoge_destroy(c);
    playback_close(&state.playback);
    queue_destroy(&q);
    SetConsoleCtrlHandler(console_handler, FALSE);
    curl_global_cleanup();
    return InterlockedCompareExchange(&state.ready, 0, 0) ? 0 : 1;
}
