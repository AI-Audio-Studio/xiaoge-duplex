"""录音转码旁路任务(PR-B,D-13/D-21/D-22)。

会话结束后把 `recordings/<id>/*.wav` 转成 Opus/FLAC 省磁盘。**池管理器侧独立组件,与 agent
进程生死解耦**:agent 只写 WAV(D-10),本组件扫描 → 转码 → 校验 → 删源。绝不丢审计数据。

- **CODEC(D-10)**:`XIAOGE_RECORD_CODEC` 由本组件消费——`opus`(默认部署)/`flac`(无损)/
  `wav`(不转码)。agent 不读该开关。
- **校验分档(D-21)**:FLAC 无损 → 解码采样数**逐一相等**;Opus 有损(libopus 内部 48kHz)→
  **时长差 ≤ 容差**(1 opus 帧 + pre-skip 余量),采样数直比不成立。
- **限流(D-22)**:worker 并发 ≤1~2、低优先级(POSIX `os.nice`);校验/编码失败**保底留 WAV**。
- **崩溃恢复(P-5)**:启动扫描 `recordings/*/` 遗留 `.wav` 入队。
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import av

logger = logging.getLogger("poolmgr-transcoder")

_AV_CODEC = {"opus": "libopus", "flac": "flac"}
_EXT = {"opus": ".opus", "flac": ".flac"}
# Opus 时长校验容差(秒):1 个 opus 帧最长 60ms + pre-skip 余量,取 70ms。
_OPUS_DURATION_TOL_S = 0.07


@dataclass
class TranscodeResult:
    src: Path
    dst: Path | None  # 成功=产物路径;不转码/失败=None
    ok: bool
    reason: str = ""


def _decode_info(path: Path) -> tuple[int, int]:
    """解码任意 av 可读音频,返回 (rate, total_samples)。"""
    with av.open(str(path)) as container:
        stream = container.streams.audio[0]
        rate = int(stream.rate)
        total = sum(int(fr.samples) for fr in container.decode(stream))
    return rate, total


def _encode(src: Path, dst: Path, av_codec: str) -> None:
    """WAV → dst(容器由后缀推断:.opus=ogg/opus、.flac=flac)。"""
    inc = av.open(str(src))
    outc = av.open(str(dst), "w")
    try:
        ins = inc.streams.audio[0]
        outs = outc.add_stream(av_codec, rate=int(ins.rate))
        outs.layout = "stereo" if int(ins.channels) == 2 else "mono"
        for frame in inc.decode(ins):
            frame.pts = None
            for pkt in outs.encode(frame):
                outc.mux(pkt)
        for pkt in outs.encode(None):  # flush
            outc.mux(pkt)
    finally:
        outc.close()
        inc.close()


def _validate(src: Path, dst: Path, codec: str) -> tuple[bool, str]:
    """D-21 分档校验。"""
    src_rate, src_samples = _decode_info(src)
    dst_rate, dst_samples = _decode_info(dst)
    if codec == "flac":  # 无损:采样数逐一相等
        ok = src_samples == dst_samples
        return ok, f"flac samples dst={dst_samples} src={src_samples}"
    # opus:时长比较(dst 可能 48kHz 重采样,采样数直比不成立)
    src_dur = src_samples / src_rate if src_rate else 0.0
    dst_dur = dst_samples / dst_rate if dst_rate else 0.0
    ok = abs(src_dur - dst_dur) <= _OPUS_DURATION_TOL_S
    return ok, f"opus dur dst={dst_dur:.3f}s src={src_dur:.3f}s tol={_OPUS_DURATION_TOL_S}s"


def transcode_file(wav_path: str | Path, codec: str) -> TranscodeResult:
    """转一个 WAV。`wav`/未知 codec 不转码;成功删源、失败保留源(D-22:绝不丢审计数据)。"""
    wav_path = Path(wav_path)
    if codec not in _AV_CODEC:
        return TranscodeResult(wav_path, None, True, f"no transcode (codec={codec})")
    dst = wav_path.with_suffix(_EXT[codec])
    try:
        _encode(wav_path, dst, _AV_CODEC[codec])
        ok, reason = _validate(wav_path, dst, codec)
    except Exception as exc:  # 编码/校验异常:删半成品产物,留源
        with contextlib.suppress(Exception):
            if dst.exists():
                dst.unlink()
        logger.warning("transcode error %s: %s", wav_path.name, exc)
        return TranscodeResult(wav_path, None, False, f"error: {exc}")
    if ok:
        with contextlib.suppress(Exception):
            wav_path.unlink()  # 校验通过才删源
        return TranscodeResult(wav_path, dst, True, reason)
    with contextlib.suppress(Exception):
        dst.unlink()  # 校验失败:删产物,留源
    logger.warning("transcode validation failed %s: %s", wav_path.name, reason)
    return TranscodeResult(wav_path, None, False, f"validation failed: {reason}")


def iter_session_wavs(session_dir: str | Path) -> list[Path]:
    """列出一个会话目录下的 .wav(不含已转码产物)。"""
    d = Path(session_dir)
    return sorted(d.glob("*.wav")) if d.is_dir() else []


def _write_json_atomic(path: Path, obj: object) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _remap_files(node: object, renames: dict[str, str]) -> None:
    """递归把 manifest 里 `file=<旧名>` 换成 `<新名>`;兼容不分段(tracks/duplex)与分段
    (segments[].tracks/duplex)两种结构。"""
    if isinstance(node, dict):
        f = node.get("file")
        if isinstance(f, str) and f in renames:
            node["file"] = renames[f]
        for v in node.values():
            _remap_files(v, renames)
    elif isinstance(node, list):
        for item in node:
            _remap_files(item, renames)


def rewrite_manifest(manifest_path: str | Path, renames: dict[str, str]) -> None:
    """转码成功后回写 `audio_manifest.json` 的 file 引用后缀(B-1:审计索引不悬空,W2)。"""
    p = Path(manifest_path)
    if not renames or not p.is_file():
        return
    with contextlib.suppress(Exception):
        manifest = json.loads(p.read_text(encoding="utf-8"))
        _remap_files(manifest, renames)
        _write_json_atomic(p, manifest)


def transcode_dir(session_dir: str | Path, codec: str) -> list[TranscodeResult]:
    """转一个会话目录的所有 wav,**整目录为一个工作单元**(单 worker 处理→无并发改 manifest
    竞态);全部转完后一次性回写 manifest 的 file 后缀(B-1)。"""
    d = Path(session_dir)
    results: list[TranscodeResult] = []
    renames: dict[str, str] = {}
    for wav in iter_session_wavs(d):
        r = transcode_file(wav, codec)
        results.append(r)
        if r.ok and r.dst is not None:
            renames[wav.name] = r.dst.name
    rewrite_manifest(d / "audio_manifest.json", renames)
    return results


class Transcoder:
    """转码任务队列 + 低优先级 worker(D-22)。release 入队会话目录;启动扫描遗留(P-5)。"""

    def __init__(
        self, recordings_root: str | Path, *, codec: str = "opus", workers: int = 1
    ) -> None:
        self._root = Path(recordings_root)
        self._codec = codec
        self._workers = max(1, min(2, int(workers)))  # D-22:并发 ≤2
        self._q: queue.Queue[Path | None] = queue.Queue()
        self._enqueued_at: dict[Path, float] = {}
        self._lock = threading.Lock()
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()

    @property
    def _enabled(self) -> bool:
        """codec 落在可编码集才启用;`off`/`wav` 等 = 停用(保持 WAV、无 worker)。"""
        return self._codec in _AV_CODEC

    def start(self) -> None:
        if not self._enabled:
            logger.info("transcoder disabled (codec=%s, 保持 WAV)", self._codec)
            return
        for i in range(self._workers):
            t = threading.Thread(target=self._run, name=f"transcoder-{i}", daemon=True)
            t.start()
            self._threads.append(t)

    def enqueue_dir(self, session_dir: str | Path) -> bool:
        """把一个会话目录**整体**入队(一目录=一工作单元:转完原子回写 manifest,免竞态)。
        停用档(codec=off/wav)不入队——无 worker 排空,入了只会让 queue_depth 悬非零(保持 WAV)。"""
        if not self._enabled:
            return False
        d = Path(session_dir)
        if not d.is_dir() or not iter_session_wavs(d):
            return False
        with self._lock:
            self._enqueued_at[d] = time.monotonic()
        self._q.put(d)
        return True

    def scan_leftovers(self) -> int:
        """启动时扫描 recordings/*/ 遗留录音目录入队(崩溃恢复,P-5)。返回目录数。"""
        total = 0
        if self._root.is_dir():
            for session in sorted(self._root.iterdir()):
                if self.enqueue_dir(session):
                    total += 1
        logger.info("transcoder scanned %d leftover session dir(s)", total)
        return total

    def metrics(self) -> dict:
        """队列深度(待转目录数)、最老任务时长(→ §11 监控 R7)。"""
        with self._lock:
            oldest = min(self._enqueued_at.values(), default=None)
        age = (time.monotonic() - oldest) if oldest is not None else 0.0
        return {"queue_depth": self._q.qsize(), "oldest_task_age_s": round(age, 1)}

    def _run(self) -> None:
        with contextlib.suppress(Exception, AttributeError):
            if hasattr(os, "nice"):
                os.nice(10)  # D-22:低优先级(POSIX;Windows 无 nice,忽略)
        while not self._stop.is_set():
            item = self._q.get()
            if item is None:
                return
            try:
                transcode_dir(item, self._codec)  # 整目录:转码 + 回写 manifest(B-1)
            except Exception:
                logger.warning("transcode task crashed: %s", item, exc_info=True)
            finally:
                with self._lock:
                    self._enqueued_at.pop(item, None)

    def stop(self) -> None:
        self._stop.set()
        for _ in self._threads:
            self._q.put(None)
