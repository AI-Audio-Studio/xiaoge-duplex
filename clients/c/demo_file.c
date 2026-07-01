/* 文件 demo(无声卡):把一个 wav 发给小歌,把收到的音频(TTS)存成 wav。
 *
 * 用法:  xiaoge_demo_file <host> <port> <in.wav> [out.wav]
 * in.wav 须为 16kHz/单声道/16-bit PCM。仅依赖标准 C + 本 SDK。
 *
 * 注意:需 libwebsockets 才能链接;**未在交付环境编译**,请按 README.md 构建验证。
 */
#include "xiaoge_client.h"

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define FRAME_BYTES 640 /* 20ms @ 16k/mono/16bit */

struct sink {
    unsigned char *buf;
    size_t len, cap;
};

static void on_ready(int sr, void *u) {
    (void)u;
    printf("ready, sample_rate=%d\n", sr);
}

static void on_audio(const void *pcm, size_t n, void *u) {
    struct sink *s = (struct sink *)u;
    if (s->len + n > s->cap) {
        size_t cap = s->cap ? s->cap : 65536;
        while (cap < s->len + n) cap *= 2;
        s->buf = (unsigned char *)realloc(s->buf, cap);
        s->cap = cap;
    }
    memcpy(s->buf + s->len, pcm, n);
    s->len += n;
}

static void on_clear(void *u) {
    struct sink *s = (struct sink *)u;
    s->len = 0; /* 打断:丢弃已收 */
    printf("clear\n");
}

/* 读取 16k/mono/16bit 的 wav data 块到 *out(malloc)。返回字节数,失败 0。 */
static size_t read_wav(const char *path, unsigned char **out) {
    FILE *f = fopen(path, "rb");
    if (!f) return 0;
    fseek(f, 0, SEEK_END);
    long total = ftell(f);
    fseek(f, 44, SEEK_SET); /* 跳过标准 44 字节头 */
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
    uint32_t rate = 16000, byte_rate = 32000;
    uint16_t ch = 1, bits = 16, align = 2, fmt = 1;
    uint32_t riff = 36 + n, sub1 = 16;
    fwrite("RIFF", 1, 4, f); fwrite(&riff, 4, 1, f); fwrite("WAVE", 1, 4, f);
    fwrite("fmt ", 1, 4, f); fwrite(&sub1, 4, 1, f); fwrite(&fmt, 2, 1, f);
    fwrite(&ch, 2, 1, f); fwrite(&rate, 4, 1, f); fwrite(&byte_rate, 4, 1, f);
    fwrite(&align, 2, 1, f); fwrite(&bits, 2, 1, f);
    fwrite("data", 1, 4, f); fwrite(&n, 4, 1, f); fwrite(pcm, 1, n, f);
    fclose(f);
}

int main(int argc, char **argv) {
    const char *host = NULL, *port_s = NULL, *in_path = NULL;
    const char *out_path = "xiaoge_reply.wav";
    int tls = 0, insecure = 0, pos = 0;
    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--tls") == 0) {
            tls = 1;
        } else if (strcmp(argv[i], "--insecure") == 0) {
            tls = 1;
            insecure = 1; /* 自签测试 */
        } else if (pos == 0) {
            host = argv[i];
            pos++;
        } else if (pos == 1) {
            port_s = argv[i];
            pos++;
        } else if (pos == 2) {
            in_path = argv[i];
            pos++;
        } else {
            out_path = argv[i];
            pos++;
        }
    }
    if (pos < 3) {
        fprintf(stderr, "用法: %s <host> <port> <in.wav> [out.wav] [--tls] [--insecure]\n", argv[0]);
        return 2;
    }

    unsigned char *pcm = NULL;
    size_t pcm_len = read_wav(in_path, &pcm);
    if (!pcm_len) {
        fprintf(stderr, "读取 wav 失败(须 16k/单声道/16-bit): %s\n", in_path);
        return 1;
    }

    struct sink sink = {0};
    xiaoge_callbacks cb = {on_ready, on_audio, on_clear, NULL, &sink};
    xiaoge_client *c = xiaoge_create(host, atoi(port_s), tls, insecure, &cb);
    if (!c) {
        fprintf(stderr, "连接失败\n");
        free(pcm);
        return 1;
    }

    size_t sent = 0;
    int tail = 150; /* 发完后再泵 ~3s 收尾巴 */
    while (xiaoge_service(c, 20) == 0) {
        if (sent < pcm_len) {
            size_t chunk = pcm_len - sent < FRAME_BYTES ? pcm_len - sent : FRAME_BYTES;
            xiaoge_send_pcm(c, pcm + sent, chunk);
            sent += chunk;
        } else if (--tail <= 0) {
            break;
        }
    }

    write_wav(out_path, sink.buf, (uint32_t)sink.len);
    printf("已发送 %zu 字节,收到 %zu 字节 → %s\n", pcm_len, sink.len, out_path);
    xiaoge_destroy(c);
    free(pcm);
    free(sink.buf);
    return 0;
}
