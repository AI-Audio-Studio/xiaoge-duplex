/* 小歌 C 客户端 SDK 实现(libwebsockets)。见 xiaoge_client.h。
 *
 * 注意:本文件需 libwebsockets 头/库,**未在交付环境编译**;请按 ../c/README.md
 * 在你的工具链上 build + 跑 demo_file 验证(有编译/运行问题反馈即修)。
 */
#include "xiaoge_client.h"

#include <libwebsockets.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* 单条待发 PCM(已在前部预留 LWS_PRE)。 */
struct out_buf {
    struct out_buf *next;
    size_t len;             /* 有效负载长度(不含 LWS_PRE) */
    unsigned char data[];   /* [LWS_PRE | payload] */
};

struct xiaoge_client {
    struct lws_context *ctx;
    struct lws *wsi;
    xiaoge_callbacks cb;
    /* 发送队列(send_pcm 入队;WRITEABLE 出队) */
    pthread_mutex_t lock;
    struct out_buf *head, *tail;
    /* 接收分片重组缓冲 */
    unsigned char *rx;
    size_t rx_len, rx_cap;
    int connected;
    int finished;           /* 连接结束(错误/关闭) */
};

static void rx_reset(struct xiaoge_client *c) {
    c->rx_len = 0;
}

static int rx_append(struct xiaoge_client *c, const void *p, size_t n) {
    if (c->rx_len + n > c->rx_cap) {
        size_t cap = c->rx_cap ? c->rx_cap : 4096;
        while (cap < c->rx_len + n) cap *= 2;
        unsigned char *nb = (unsigned char *)realloc(c->rx, cap);
        if (!nb) return -1;
        c->rx = nb;
        c->rx_cap = cap;
    }
    memcpy(c->rx + c->rx_len, p, n);
    c->rx_len += n;
    return 0;
}

/* 简易提取 JSON 文本里的 "type":"xxx"(避免引入 JSON 库)。 */
static int json_has_type(const char *s, size_t n, const char *want) {
    const char *key = "\"type\"";
    for (size_t i = 0; i + 6 <= n; i++) {
        if (memcmp(s + i, key, 6) != 0) continue;
        const char *p = s + i + 6, *end = s + n;
        while (p < end && (*p == ' ' || *p == ':' || *p == '"')) p++;
        size_t wl = strlen(want);
        return (size_t)(end - p) >= wl && memcmp(p, want, wl) == 0;
    }
    return 0;
}

static void dispatch_text(struct xiaoge_client *c, const char *s, size_t n) {
    if (json_has_type(s, n, "ready")) {
        if (c->cb.on_ready) c->cb.on_ready(16000, c->cb.user);
    } else if (json_has_type(s, n, "clear")) {
        if (c->cb.on_clear) c->cb.on_clear(c->cb.user);
    } else if (json_has_type(s, n, "busy")) {
        if (c->cb.on_busy) c->cb.on_busy("server busy", c->cb.user);
    }
}

static int cb_lws(struct lws *wsi, enum lws_callback_reasons reason,
                  void *user, void *in, size_t len) {
    struct xiaoge_client *c = (struct xiaoge_client *)lws_context_user(lws_get_context(wsi));
    (void)user;
    switch (reason) {
    case LWS_CALLBACK_CLIENT_ESTABLISHED:
        c->connected = 1;
        break;
    case LWS_CALLBACK_CLIENT_RECEIVE: {
        int binary = lws_frame_is_binary(wsi);
        if (rx_append(c, in, len) != 0) break;
        if (lws_is_final_fragment(wsi) && lws_remaining_packet_payload(wsi) == 0) {
            if (binary) {
                if (c->cb.on_audio) c->cb.on_audio(c->rx, c->rx_len, c->cb.user);
            } else {
                dispatch_text(c, (const char *)c->rx, c->rx_len);
            }
            rx_reset(c);
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
            lws_write(wsi, b->data + LWS_PRE, b->len, LWS_WRITE_BINARY);
            free(b);
            pthread_mutex_lock(&c->lock);
            int more = c->head != NULL;
            pthread_mutex_unlock(&c->lock);
            if (more) lws_callback_on_writable(wsi);
        }
        break;
    }
    case LWS_CALLBACK_CLIENT_CONNECTION_ERROR:
        fprintf(stderr, "xiaoge: connection error: %.*s\n", in ? (int)len : 0,
                in ? (char *)in : "");
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
    {"xiaoge-audio", cb_lws, 0, 0, 0, NULL, 0},
    LWS_PROTOCOL_LIST_TERM,
};

xiaoge_client *xiaoge_create(const char *host, int port, int tls, int insecure,
                             const xiaoge_callbacks *cb) {
    struct xiaoge_client *c = (struct xiaoge_client *)calloc(1, sizeof(*c));
    if (!c) return NULL;
    if (cb) c->cb = *cb;
    pthread_mutex_init(&c->lock, NULL);

    struct lws_context_creation_info info;
    memset(&info, 0, sizeof(info));
    info.port = CONTEXT_PORT_NO_LISTEN;
    info.protocols = protocols;
    info.user = c;
    if (tls) info.options |= LWS_SERVER_OPTION_DO_SSL_GLOBAL_INIT;
    c->ctx = lws_create_context(&info);
    if (!c->ctx) {
        free(c);
        return NULL;
    }

    struct lws_client_connect_info ci;
    memset(&ci, 0, sizeof(ci));
    ci.context = c->ctx;
    ci.address = host;
    ci.port = port;
    ci.path = "/ws/audio";
    ci.host = host;
    ci.origin = host;
    ci.protocol = NULL; /* 不请求子协议(与 Python 客户端一致);服务端未回子协议头会致握手失败 */
    ci.pwsi = &c->wsi;
    if (tls) {
        ci.ssl_connection = LCCSCF_USE_SSL;
        if (insecure) {
            /* 自签/内网测试:接受自签证书、跳过主机名与证书链校验 */
            ci.ssl_connection |= LCCSCF_ALLOW_SELFSIGNED
                               | LCCSCF_SKIP_SERVER_CERT_HOSTNAME_CHECK
                               | LCCSCF_ALLOW_INSECURE;
        }
    }
    if (!lws_client_connect_via_info(&ci)) {
        lws_context_destroy(c->ctx);
        free(c);
        return NULL;
    }
    return c;
}

int xiaoge_send_pcm(xiaoge_client *c, const void *pcm, size_t len) {
    if (!c || !pcm || len == 0) return -1;
    struct out_buf *b = (struct out_buf *)malloc(sizeof(*b) + LWS_PRE + len);
    if (!b) return -1;
    b->next = NULL;
    b->len = len;
    memcpy(b->data + LWS_PRE, pcm, len);
    pthread_mutex_lock(&c->lock);
    if (c->tail) c->tail->next = b;
    else c->head = b;
    c->tail = b;
    pthread_mutex_unlock(&c->lock);
    if (c->wsi) lws_callback_on_writable(c->wsi);
    return 0;
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
