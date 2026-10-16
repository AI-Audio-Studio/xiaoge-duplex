/* R5.2.2 file demo.
 *
 * Preferred usage:
 *   xiaoge_demo_file <create_session_url> <device_id> <credential-json-or-string> <in.wav> [out.wav]
 *
 * Compatibility usage:
 *   xiaoge_demo_file <ws_url> <access_token> <trace_id> <session_id> <device_id> <in.wav> [out.wav]
 */
#include "xiaoge_client.h"
#include "cJSON.h"

#include <curl/curl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#define FRAME_BYTES 640
#define SESSION_FIELD_MAX 512

struct sink {
    unsigned char *buf;
    size_t len, cap;
    int ready;
};

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

static void on_ready(const xiaoge_ready_event *event, void *u) {
    struct sink *s = (struct sink *)u;
    s->ready = 1;
    printf("ready sample_rate=%d\n", event->sample_rate);
}

static void on_audio(const void *pcm, size_t n, void *u) {
    struct sink *s = (struct sink *)u;
    if (s->len + n > s->cap) {
        size_t cap = s->cap ? s->cap : 65536;
        while (cap < s->len + n) cap *= 2;
        unsigned char *nb = (unsigned char *)realloc(s->buf, cap);
        if (!nb) return;
        s->buf = nb;
        s->cap = cap;
    }
    memcpy(s->buf + s->len, pcm, n);
    s->len += n;
}

static void on_clear(const xiaoge_clear_event *event, void *u) {
    (void)event;
    struct sink *s = (struct sink *)u;
    s->len = 0;
    printf("clear\n");
}

static void on_json(const char *json, void *u) {
    (void)u;
    printf("%s\n", json);
}

static size_t read_wav(const char *path, unsigned char **out) {
    FILE *f = fopen(path, "rb");
    if (!f) return 0;
    fseek(f, 0, SEEK_END);
    long total = ftell(f);
    fseek(f, 44, SEEK_SET);
    if (total <= 44) {
        fclose(f);
        return 0;
    }
    size_t n = (size_t)(total - 44);
    *out = (unsigned char *)malloc(n);
    size_t got = fread(*out, 1, n, f);
    fclose(f);
    return got;
}

static void write_wav(const char *path, const unsigned char *pcm, uint32_t n) {
    FILE *f = fopen(path, "wb");
    if (!f) return;
    uint32_t rate = 16000, byte_rate = 32000, riff = 36 + n, sub1 = 16;
    uint16_t ch = 1, bits = 16, align = 2, fmt = 1;
    fwrite("RIFF", 1, 4, f); fwrite(&riff, 4, 1, f); fwrite("WAVE", 1, 4, f);
    fwrite("fmt ", 1, 4, f); fwrite(&sub1, 4, 1, f); fwrite(&fmt, 2, 1, f);
    fwrite(&ch, 2, 1, f); fwrite(&rate, 4, 1, f); fwrite(&byte_rate, 4, 1, f);
    fwrite(&align, 2, 1, f); fwrite(&bits, 2, 1, f);
    fwrite("data", 1, 4, f); fwrite(&n, 4, 1, f); fwrite(pcm, 1, n, f);
    fclose(f);
}

static int arg_is_value(const char *s) {
    return s && strncmp(s, "--", 2) != 0;
}

static int starts_with(const char *s, const char *prefix) {
    return s && strncmp(s, prefix, strlen(prefix)) == 0;
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
    if (getenv("XIAOGE_CURL_VERBOSE")) {
        curl_easy_setopt(curl, CURLOPT_VERBOSE, 1L);
    }
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
    if (json_get_string(resp.data, "config_version", out->config_version, sizeof(out->config_version)) != 0) {
        snprintf(out->config_version, sizeof(out->config_version), "%s", "unknown");
    }
    free(resp.data);
    return 0;
}

int main(int argc, char **argv) {
    curl_global_init(CURL_GLOBAL_DEFAULT);
    if (argc < 2) {
        fprintf(stderr,
            "usage:\n"
            "  %s <create_session_url> <device_id> <credential-json-or-string> <in.wav> [out.wav] [--ca-cert path] [--insecure] [--api-key key]\n"
            "  %s <ws_url> <access_token> <trace_id> <session_id> <device_id> <in.wav> [out.wav] [--ca-cert path] [--insecure]\n",
            argv[0],
            argv[0]);
        return 2;
    }
    int http_mode = starts_with(argv[1], "http://") || starts_with(argv[1], "https://");
    if ((http_mode && argc < 5) || (!http_mode && argc < 7)) {
        fprintf(stderr, "invalid arguments\n");
        return 2;
    }

    const char *ca_cert_path = NULL;
    const char *api_key = getenv("XIAOGE_CLOUD_API_KEY");
    if (!api_key) api_key = XIAOGE_DEFAULT_API_KEY;
    int insecure = 0;
    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--insecure") == 0) insecure = 1;
        if (strcmp(argv[i], "--ca-cert") == 0 && i + 1 < argc && arg_is_value(argv[i + 1]))
            ca_cert_path = argv[++i];
        if (strcmp(argv[i], "--api-key") == 0 && i + 1 < argc && arg_is_value(argv[i + 1]))
            api_key = argv[++i];
    }

    const char *device_id = http_mode ? argv[2] : argv[5];
    const char *in_path = http_mode ? argv[4] : argv[6];
    const char *out_path = http_mode
        ? ((argc >= 6 && arg_is_value(argv[5])) ? argv[5] : "xiaoge_reply.wav")
        : ((argc >= 8 && arg_is_value(argv[7])) ? argv[7] : "xiaoge_reply.wav");

    unsigned char *pcm = NULL;
    size_t pcm_len = read_wav(in_path, &pcm);
    if (!pcm_len) {
        fprintf(stderr, "failed to read wav, must be 16k/mono/16-bit PCM: %s\n", in_path);
        return 1;
    }

    struct sink sink = {0};
    char credential_json[1024];
    if (http_mode && make_credential_json(argv[3], credential_json, sizeof(credential_json)) != 0) {
        fprintf(stderr, "invalid credential JSON/string\n");
        free(pcm);
        return 1;
    }
    xiaoge_config cfg = {
        device_id,
        http_mode ? credential_json : "{\"type\":\"external\"}",
        "audio,text,cmd,state",
        "xiaoge-c-demo-r5.2.2",
        "{}",
        api_key,
    };
    struct session_fields created = {0};
    if (http_mode) {
        if (create_session_http(argv[1], &cfg, insecure, ca_cert_path, &created) != 0) {
            free(pcm);
            return 1;
        }
        printf("session trace_id=%s session_id=%s ws_url=%s\n",
               created.trace_id, created.session_id, created.ws_url);
    }
    xiaoge_session sess = {
        http_mode ? created.trace_id : argv[3],
        http_mode ? created.session_id : argv[4],
        http_mode ? created.access_token : argv[2],
        http_mode ? created.ws_url : argv[1],
        "audio,text,cmd,state",
        http_mode ? created.config_version : "cfg-external",
    };
    xiaoge_callbacks cb = {
        .struct_size = sizeof(cb),
        .on_ready = on_ready,
        .on_audio = on_audio,
        .on_clear = on_clear,
        .on_json = on_json,
        .user = &sink,
    };
    xiaoge_client *c = xiaoge_create_from_session_with_ca(&cfg, &sess, ca_cert_path, insecure, &cb);
    if (!c) {
        fprintf(stderr, "failed to connect\n");
        free(pcm);
        return 1;
    }

    size_t sent = 0;
    time_t deadline = 0;
    int sent_state = 0;
    while (xiaoge_service(c, 50) == 0) {
        if (sink.ready && !sent_state) {
            xiaoge_send_frontend_state(c, "hint", "awake", "speech", 1000);
            sent_state = 1;
        }
        if (sink.ready && sent < pcm_len) {
            size_t chunk = pcm_len - sent < FRAME_BYTES ? pcm_len - sent : FRAME_BYTES;
            xiaoge_send_pcm(c, pcm + sent, chunk);
            sent += chunk;
        } else {
            if (deadline == 0) deadline = time(NULL) + 5;
            if (time(NULL) >= deadline) break;
        }
    }

    write_wav(out_path, sink.buf, (uint32_t)sink.len);
    printf("sent=%zu received=%zu out=%s\n", sent, sink.len, out_path);
    xiaoge_destroy(c);
    free(pcm);
    free(sink.buf);
    return sink.ready && sent == pcm_len ? 0 : 1;
}
