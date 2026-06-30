# 小歌 Python 客户端 SDK

对接小歌服务端的 `/ws/audio`(服务端需 `WEB_AUDIO=1`)。协议见 [../PROTOCOL.md](../PROTOCOL.md)。

## 安装
```bash
pip install -r requirements.txt   # websockets(核心)+ sounddevice(仅 demo_mic 需要)
```

## SDK 用法(`xiaoge_client.py`)
```python
import asyncio
from xiaoge_client import XiaogeClient

async def main():
    c = XiaogeClient("192.168.1.10", 8787)   # tls=True 用 wss
    c.on_ready = lambda sr: print("已就绪", sr)
    c.on_audio = lambda pcm: my_speaker(pcm)  # 播放 TTS(16k/单声道/int16 小端)
    c.on_clear = lambda: my_speaker_flush()   # 打断:清空播放
    c.on_busy  = lambda m: print("忙:", m)

    async def feed():
        while True:
            await c.send_pcm(my_mic_read())    # 上行麦克风 PCM(同格式)

    await asyncio.gather(c.run(), feed())

asyncio.run(main())
```
**音频格式(必须严格匹配)**:16000 Hz、单声道、16-bit 有符号小端、裸 PCM。

## 示例
```bash
python demo_mic.py  <host> <port>            # 实时麦克风↔扬声器(需 sounddevice)
python demo_file.py <host> <port> in.wav [out.wav]   # 无声卡:发 wav、存回复 wav(in.wav 须 16k/单声道/16-bit)
```

## 自测(冒烟)
```bash
python selftest.py    # 本地 mock 服务验证握手/收发/clear/busy,退出码 0=通过
```
