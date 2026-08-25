from __future__ import annotations

import audioop
import shutil
import sys
import wave
from pathlib import Path


TARGET_RATE = 16000
TARGET_CHANNELS = 1
TARGET_WIDTH = 2


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: prepare_wav_16k.py <input.wav> <output.wav>", file=sys.stderr)
        return 2
    src = Path(sys.argv[1])
    dst = Path(sys.argv[2])
    dst.parent.mkdir(parents=True, exist_ok=True)

    with wave.open(str(src), "rb") as reader:
        channels = reader.getnchannels()
        width = reader.getsampwidth()
        rate = reader.getframerate()
        pcm = reader.readframes(reader.getnframes())

    if width != TARGET_WIDTH:
        raise SystemExit(f"expected 16-bit PCM WAV, got sample width {width}: {src}")
    if channels == 2:
        pcm = audioop.tomono(pcm, width, 0.5, 0.5)
        channels = 1
    if channels != TARGET_CHANNELS:
        raise SystemExit(f"expected mono/stereo WAV, got {channels} channels: {src}")
    if rate != TARGET_RATE:
        pcm, _ = audioop.ratecv(pcm, width, channels, rate, TARGET_RATE, None)

    with wave.open(str(dst), "wb") as writer:
        writer.setframerate(TARGET_RATE)
        writer.setnchannels(TARGET_CHANNELS)
        writer.setsampwidth(TARGET_WIDTH)
        writer.writeframes(pcm)

    if src.resolve() == dst.resolve():
        shutil.copystat(src, dst)
    print(f"prepared_wav={dst} rate={TARGET_RATE} channels={TARGET_CHANNELS} width={TARGET_WIDTH} bytes={len(pcm)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
