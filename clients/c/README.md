# 小歌 C 客户端 SDK(libwebsockets)

对接小歌 `/ws/audio`(服务端需 `WEB_AUDIO=1`)。协议见 [../PROTOCOL.md](../PROTOCOL.md)。
回调式、可移植,只做协议与收发;音频采集/播放由你在回调里处理。

> ✅ **CI 已验证**:GitHub Actions(ubuntu + `libwebsockets-dev`)每次自动**编译**(job `C SDK build`)
> **并跑真机端到端**——用编译好的二进制连 `wss://60.205.197.165:10099`(自签),握手 `ready`、
> 收到欢迎语 TTS(实测发 1s 静音收到约 120KB 音频)。含 `ws`/`wss`(自签)完整支持。
> 调试:设环境变量 `XIAOGE_DEBUG=1` 打开 libwebsockets 详细连接/TLS 日志。

## 依赖:libwebsockets ≥ 4.0(**需带 TLS**,连 wss 必需)
```bash
# Ubuntu/Debian(自带 OpenSSL 支持)
sudo apt install libwebsockets-dev cmake build-essential
# macOS
brew install libwebsockets cmake
# Windows(vcpkg;如需 TLS 用 libwebsockets[ssl])
vcpkg install libwebsockets[core,ssl]
```
> 发行版的 libwebsockets 一般已带 OpenSSL;若自行编译,需 `-DLWS_WITH_SSL=ON` 才能连 wss。

## 构建
```bash
cmake -S . -B build [-DCMAKE_TOOLCHAIN_FILE=<vcpkg>/scripts/buildsystems/vcpkg.cmake]
cmake --build build
```

## 运行文件 demo(自测/验收)
```bash
# in.wav 须 16kHz/单声道/16-bit PCM
# 当前部署是 wss 自签,加 --tls --insecure:
./build/xiaoge_demo_file 60.205.197.165 10099 in.wav out.wav --tls --insecure
# 明文 ws 部署则去掉这两个开关。期望:打印 ready、收到字节数 > 0,out.wav 为小歌回复音频。
```

## API(`xiaoge_client.h`)
```c
xiaoge_callbacks cb = { on_ready, on_audio, on_clear, on_busy, user_ptr };
// tls=1 用 wss;insecure=1 允许自签(仅测试/内网)
xiaoge_client *c = xiaoge_create("60.205.197.165", 10099, /*tls=*/1, /*insecure=*/1, &cb);
while (xiaoge_service(c, 20) == 0) {     // 单线程泵事件循环
    xiaoge_send_pcm(c, pcm, len);        // 上行 16k/单声道/int16 小端;线程安全
}
xiaoge_destroy(c);
```
- `on_audio(pcm,len,user)`:一段 TTS PCM(同格式)→ 送你的播放器。
- `on_clear(user)`:打断 → 立即清空本地播放。
- `on_busy(msg,user)`:服务端忙、连接被拒。

**音频格式(必须严格匹配)**:16000 Hz、单声道、16-bit 有符号小端、裸 PCM。
