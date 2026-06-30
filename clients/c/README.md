# 小歌 C 客户端 SDK(libwebsockets)

对接小歌 `/ws/audio`(服务端需 `WEB_AUDIO=1`)。协议见 [../PROTOCOL.md](../PROTOCOL.md)。
回调式、可移植,只做协议与收发;音频采集/播放由你在回调里处理。

> ⚠️ **状态**:本目录代码在交付环境(无 C 工具链)**未编译**,需你在自己的工具链上
> build + 跑 `xiaoge_demo_file` 验证。若有编译/运行问题反馈给我即修。

## 依赖:libwebsockets ≥ 4.0
```bash
# Ubuntu/Debian
sudo apt install libwebsockets-dev cmake build-essential
# macOS
brew install libwebsockets cmake
# Windows(vcpkg)
vcpkg install libwebsockets
```

## 构建
```bash
cmake -S . -B build [-DCMAKE_TOOLCHAIN_FILE=<vcpkg>/scripts/buildsystems/vcpkg.cmake]
cmake --build build
```

## 运行文件 demo(自测/验收)
```bash
# in.wav 须 16kHz/单声道/16-bit PCM
./build/xiaoge_demo_file <host> <port> in.wav out.wav
# 期望:打印 ready、收到字节数 > 0,out.wav 为小歌回复音频
```

## API(`xiaoge_client.h`)
```c
xiaoge_callbacks cb = { on_ready, on_audio, on_clear, on_busy, user_ptr };
xiaoge_client *c = xiaoge_create("192.168.1.10", 8787, /*tls=*/0, &cb);
while (xiaoge_service(c, 20) == 0) {     // 单线程泵事件循环
    xiaoge_send_pcm(c, pcm, len);        // 上行 16k/单声道/int16 小端;线程安全
}
xiaoge_destroy(c);
```
- `on_audio(pcm,len,user)`:一段 TTS PCM(同格式)→ 送你的播放器。
- `on_clear(user)`:打断 → 立即清空本地播放。
- `on_busy(msg,user)`:服务端忙、连接被拒。

**音频格式(必须严格匹配)**:16000 Hz、单声道、16-bit 有符号小端、裸 PCM。
