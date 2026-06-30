/* 小歌全双工语音 · C 客户端 SDK(基于 libwebsockets)。
 *
 * 对接服务端 /ws/audio(服务端需 WEB_AUDIO=1)。协议见 ../PROTOCOL.md:
 * 上行 = 连续发 16kHz/单声道/16-bit 小端裸 PCM;下行 = 同格式 PCM(TTS)
 * + 文本控制 {"type":"ready"|"clear"|"busy"}。本 SDK 只做协议与收发,
 * 音频采集/播放由调用方在回调里处理(回调式、可移植、无声卡依赖)。
 *
 * 线程模型:单线程事件循环。创建后循环调用 xiaoge_service() 泵事件;
 * 回调在该线程触发。send_pcm 线程安全(内部加锁入队)。
 *
 * 依赖:libwebsockets(>=4.0)。构建见 CMakeLists.txt。
 */
#ifndef XIAOGE_CLIENT_H
#define XIAOGE_CLIENT_H

#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct xiaoge_client xiaoge_client;

/* 回调(均可为 NULL;在 xiaoge_service() 的线程触发)。user 为创建时透传的指针。 */
typedef struct {
    void (*on_ready)(int sample_rate, void *user); /* 收到握手 ready */
    void (*on_audio)(const void *pcm, size_t len, void *user); /* 一段 TTS PCM */
    void (*on_clear)(void *user);                  /* 打断:请清空本地播放 */
    void (*on_busy)(const char *message, void *user); /* 服务端忙、被拒 */
    void *user;
} xiaoge_callbacks;

/* 创建并发起连接。host 如 "192.168.1.10";tls!=0 用 wss。失败返回 NULL。 */
xiaoge_client *xiaoge_create(const char *host, int port, int tls,
                             const xiaoge_callbacks *cb);

/* 入队一段上行 PCM(16k/单声道/int16 小端)。线程安全。成功返回 0。 */
int xiaoge_send_pcm(xiaoge_client *c, const void *pcm, size_t len);

/* 泵一次事件循环,最多阻塞 timeout_ms 毫秒。需在循环里反复调用。
 * 返回 0 正常,<0 表示连接已结束(应停止循环并 destroy)。 */
int xiaoge_service(xiaoge_client *c, int timeout_ms);

/* 关闭并释放。 */
void xiaoge_destroy(xiaoge_client *c);

#ifdef __cplusplus
}
#endif

#endif /* XIAOGE_CLIENT_H */
