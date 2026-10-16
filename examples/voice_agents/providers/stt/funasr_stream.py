"""FunASR 2pass 流式主 STT(判停/主STT 改造 · optimized 栈)。

设计见 TURN_STT_DESIGN.md。要点:
  - 流式:一条 WS 贯穿整轮会话,边说边出 interim/final,长句不丢字(替代离线+VAD 切段)。
  - **内置 VAD 输出门控防幽灵**:静音/底噪期 ASR 蹦出的文本一律丢弃(不累加/不发)。
  - **GAP 轮次聚合**:以"最后一帧有声"起算静默,静默 ≥ GAP 才发一条 FINAL(=一轮),
    中途短停顿只累加 → 一段连续话 = 一轮 = 一次回复。
  - **超集裁剪**:GAP 提前提交后 funasr 未内切时,2pass-offline 会把整窗(含已提交
    FINAL 的旧音频)重新识别再发一遍(F2 ⊇ F1);上游把同轮两条 FINAL 用空格拼接
    → 同句重复。裁掉已提交部分只留新尾巴。
  - 上游 endpointing/turn detector 不改:无 FINAL 时上游 EOU 空 transcript 早返回。

零改上游;opt-in(STT_BACKEND=funasr-stream / XIAOGE_STACK=optimized)。
内置 VAD 为**独立的 silero 实例**(不与 AgentSession 的 VAD 共享模型状态)。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import os
import time

import aiohttp
from common.config_utils import env_bool, env_float

from livekit.agents import APIConnectOptions, LanguageCode, stt
from livekit.agents._exceptions import APIConnectionError
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, NOT_GIVEN, NotGivenOr
from livekit.agents.vad import VADEventType
from providers.helpers import unverified_ssl_ctx

logger = logging.getLogger("funasr-stream")

_SAMPLE_RATE = 16000
_WATCHDOG_INTERVAL = 0.05
_DIAGNOSTICS_ENV = "XIAOGE_STT_DIAGNOSTICS"


def _diagnostics_enabled() -> bool:
    return os.getenv(_DIAGNOSTICS_ENV, "0").strip().lower() in {"1", "true", "yes", "on"}


def _frame_rms(data: bytes) -> float:
    """Return PCM16 RMS without affecting the audio path."""
    if len(data) < 2:
        return 0.0
    usable = len(data) - (len(data) % 2)
    samples = memoryview(data[:usable]).cast("h")
    if not samples:
        return 0.0
    return math.sqrt(sum(float(x) * x for x in samples) / len(samples))


def _diag_ms(at: float | None, origin: float) -> str:
    return "-" if at is None else f"{(at - origin) * 1000:.1f}"


# ── 语义判停兜底:一句话"像说完了吗"(用于自适应 GAP 提前提交) ──────────────────
_COMPLETE_END = "。！？!?…"  # 终止标点 → 多半说完
_MIDCLAUSE_PUNCT = "，,、；;：:"  # 句中标点 → 多半没说完
_FINAL_PARTICLES = "了吧吗呢啊嘛呀哈"  # 句末语气词 → 多半说完
# 结尾连接词/悬词 → 多半没说完(还要接着说):
_CONTINUE_TAILS = (
    "而且",
    "并且",
    "然后",
    "接着",
    "因为",
    "所以",
    "但是",
    "不过",
    "就是",
    "还有",
    "或者",
    "以及",
    "的",
    "和",
    "跟",
    "与",
    "把",
    "被",
    "在",
    "也",
    "还",
    "这个",
    "那个",
    "嗯",
    "呃",
    "那",
    "这",
)


def _looks_complete(text: str) -> bool:
    """保守判断一句话是否"像说完了"。仅明显说完才 True;不确定一律 False(等满 GAP_MAX)。"""
    t = text.rstrip()
    if not t:
        return False
    last = t[-1]
    if last in _COMPLETE_END:
        return True
    if last in _MIDCLAUSE_PUNCT:
        return False
    if any(t.endswith(w) for w in _CONTINUE_TAILS):
        return False
    if last in _FINAL_PARTICLES:
        return True
    return False  # 无明显信号 → 保守等满 GAP_MAX


def _commit_ready(silence: float, pending: str, gap_min: float, gap_max: float) -> bool:
    """轮次提交判定:静默 ≥GAP_MAX 必发;[GAP_MIN,GAP_MAX) 间仅"像说完"才发。"""
    if silence >= gap_max:
        return True
    if gap_min < gap_max and silence >= gap_min:
        return _looks_complete(pending)
    return False


# ── 超集裁剪:offline 整窗重识别去重 ──────────────────────────────────────────
_RECENT_FINAL_TTL_S = 2.5  # FINAL 提交后多久内的 offline 文本才参与裁剪
_TRIM_MIN_FINAL_LEN = 4  # 已提交 FINAL 至少这么长才裁(短词撞前缀概率高)
_LEAD_PUNCT = "，,。.、！!？?；;：:～~ \t\n"  # offline 结果常带的前导标点


def _align_to_prefix(a: str, b: str) -> tuple[int, int]:
    """a 整体对齐到 b 某前缀的最小编辑距离;返回 (距离, 所耗 b 前缀长度)。

    同距取最短前缀(=保最长尾巴):若 b 里还有 a 的重复,留给尾巴自己重放。
    """
    la, lb = len(a), len(b)
    if la == 0:
        return 0, 0
    prev = list(range(lb + 1))  # lev("", b[:j]) = j
    for i in range(1, la + 1):
        cur = [i] + [0] * lb
        ca = a[i - 1]
        for j in range(1, lb + 1):
            cost = 0 if ca == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
        prev = cur
    best_j, best_d = 0, prev[0]
    for j in range(1, lb + 1):
        if prev[j] < best_d:
            best_j, best_d = j, prev[j]
    return best_d, best_j


def _trim_recovered_prefix(
    text: str, recent: list[tuple[float, str]], now: float
) -> str:
    """offline 文本去重:以某条近期 FINAL 开头(容 ASR 噪声)则只留其后新内容。

    匹配容差 = max(1, len//6) 次编辑(同音误写级别);"改口重说整句"且 funasr
    恰好内切时会被误裁(残余风险,极罕见),XIAOGE_STT_SUPERSET_TRIM=0 可整体关闭。
    """
    body = text.lstrip(_LEAD_PUNCT)
    if not body:
        return ""
    for ts, done in reversed(recent):
        if now - ts > _RECENT_FINAL_TTL_S:
            continue
        done = done.strip(_LEAD_PUNCT)
        if len(done) < _TRIM_MIN_FINAL_LEN:
            continue
        dist, used = _align_to_prefix(done, body)
        if dist <= max(1, len(done) // 6):
            return body[used:].lstrip(_LEAD_PUNCT)
    return body


class FunASRStreamSTT(stt.STT):
    """FunASR 2pass 流式主 STT(内置 VAD 门控 + GAP 聚合)。"""

    def __init__(
        self,
        *,
        ws_url: str | None = None,
        verify_ssl: bool | None = None,
        gap_s: float | None = None,
        vad_activation: float | None = None,
        vad=None,  # 可注入独立 silero VAD;None 则自建
    ) -> None:
        super().__init__(
            capabilities=stt.STTCapabilities(
                streaming=True, interim_results=True, offline_recognize=False
            )
        )
        self._ws_url = ws_url or os.getenv("FUNASR_WS_URL", "wss://60.205.197.165:10090")
        self._verify_ssl = (
            verify_ssl
            if verify_ssl is not None
            else os.getenv("FUNASR_VERIFY_SSL", "0").strip().lower() in {"1", "true", "yes", "on"}
        )
        self._gap_s = gap_s if gap_s is not None else env_float("XIAOGE_AGG_GAP", 1.5)
        # 自适应 GAP 下限:句子"像说完"时静默达此值即提交(快);否则等满 _gap_s(稳)。
        # GAP_MIN >= GAP 时退化为恒定 GAP(=关闭自适应)。
        self._gap_min = env_float("XIAOGE_AGG_GAP_MIN", 0.8)
        # 超集裁剪开关(默认开);=0 回到"offline 重识别原样累加"的旧行为。
        self._superset_trim = env_bool("XIAOGE_STT_SUPERSET_TRIM", True)
        self._vad_activation = (
            vad_activation
            if vad_activation is not None
            else env_float("XIAOGE_STREAM_VAD_ACTIVATION", 0.5)
        )
        # 独立 silero VAD(只读逐帧概率做门控/最后有声时刻;不与 AgentSession VAD 共享状态)
        if vad is None:
            from livekit.plugins import silero

            vad = silero.VAD.load()
        self._vad = vad

    @property
    def model(self) -> str:
        return "funasr-2pass"

    @property
    def provider(self) -> str:
        return "FunASR-Stream"

    async def _recognize_impl(self, buffer, *, language=NOT_GIVEN, conn_options):  # type: ignore[override]
        raise NotImplementedError("FunASRStreamSTT is streaming-only (use stream())")

    def stream(
        self,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> stt.RecognizeStream:
        return _FunASRStreamStream(stt=self, conn_options=conn_options)

    async def aclose(self) -> None:
        return


class _FunASRStreamStream(stt.RecognizeStream):
    def __init__(self, *, stt: FunASRStreamSTT, conn_options: APIConnectOptions) -> None:
        super().__init__(stt=stt, conn_options=conn_options, sample_rate=_SAMPLE_RATE)
        self._impl = stt
        self._gap = stt._gap_s
        self._gap_min = stt._gap_min
        self._superset_trim = stt._superset_trim
        self._activation = stt._vad_activation
        # 聚合状态(单事件循环,无需锁)
        self._prefix = ""  # 已收尾段落
        self._seg = ""  # 当前段在线增量
        self._last_voiced: float | None = None
        self._voiced = False
        self._ghost_chars = 0  # 幽灵率 KPI:被门控丢弃的字数
        # 已提交 FINAL (monotonic 时刻, 文本) — 超集裁剪的比对参照
        self._recent_finals: list[tuple[float, str]] = []
        self._diag = _diagnostics_enabled()
        self._diag_started_at = time.monotonic()
        self._diag_frames = 0
        self._diag_samples = 0
        self._diag_first_audio_at: float | None = None
        self._diag_first_non_silent_at: float | None = None
        self._diag_first_text_at: float | None = None
        self._diag_raw_results = 0
        self._diag_vad_drops = 0
        self._diag_last_vad_prob: float | None = None
        self._diag_last_vad_state: bool | None = None

    async def _run(self) -> None:
        ssl_ctx = unverified_ssl_ctx(self._impl._ws_url, self._impl._verify_ssl)
        session = aiohttp.ClientSession()
        vad_stream = self._impl._vad.stream()
        ws: aiohttp.ClientWSResponse | None = None
        try:
            ws = await session.ws_connect(self._impl._ws_url, ssl=ssl_ctx, heartbeat=30)
            await ws.send_str(
                json.dumps(
                    {
                        "mode": "2pass",
                        "chunk_size": [5, 8, 4],
                        "chunk_interval": 10,
                        "wav_name": "funasr-stream",
                        "wav_format": "pcm",
                        "audio_fs": _SAMPLE_RATE,
                        "is_speaking": True,
                        "itn": False,
                    },
                    ensure_ascii=False,
                )
            )
            recv_task = asyncio.create_task(self._funasr_recv(ws), name="funasr-recv")
            vad_task = asyncio.create_task(self._vad_consume(vad_stream), name="funasr-vad")
            wd_task = asyncio.create_task(self._gap_watchdog(), name="funasr-gap")
            try:
                await self._forward(ws, vad_stream)  # 输入结束才返回
            finally:
                with contextlib.suppress(Exception):
                    await ws.send_str(json.dumps({"is_speaking": False}))
                for t in (recv_task, wd_task, vad_task):
                    t.cancel()
                await asyncio.gather(recv_task, wd_task, vad_task, return_exceptions=True)
        finally:
            if self._diag:
                logger.info(
                    "STT_DIAGNOSTIC_SUMMARY frames=%d samples=%d duration_ms=%.1f "
                    "first_audio_ms=%s first_non_silent_ms=%s first_text_ms=%s "
                    "raw_results=%d vad_drops=%d ghost_chars=%d",
                    self._diag_frames, self._diag_samples,
                    (time.monotonic() - self._diag_started_at) * 1000,
                    _diag_ms(self._diag_first_audio_at, self._diag_started_at),
                    _diag_ms(self._diag_first_non_silent_at, self._diag_started_at),
                    _diag_ms(self._diag_first_text_at, self._diag_started_at),
                    self._diag_raw_results, self._diag_vad_drops, self._ghost_chars,
                )
            with contextlib.suppress(Exception):
                await vad_stream.aclose()
            if ws is not None:
                with contextlib.suppress(Exception):
                    await ws.close()
            with contextlib.suppress(Exception):
                await session.close()

    async def _forward(self, ws, vad_stream) -> None:
        """读输入帧:**实时即送**(帧到即发,无节流),同帧喂内置 VAD。

        关键:输入帧本身已是实时节奏(`async for` 被帧到达驱动),所以直接发送即等于
        实时发送,不会堆积;任何人为节流(sleep)都会让发送慢于实时 → backlog 累积 →
        说得越久显示越落后。参照在线 tap(队列即取即发)。
        """
        async for frame in self._input_ch:
            if isinstance(frame, self._FlushSentinel):
                continue
            raw = bytes(frame.data)
            if self._diag:
                now = time.monotonic()
                rms = _frame_rms(raw)
                self._diag_frames += 1
                self._diag_samples += len(raw) // 2
                if self._diag_first_audio_at is None:
                    self._diag_first_audio_at = now
                    logger.info("STT_AUDIO_START bytes=%d samples=%d rms=%.1f", len(raw), len(raw) // 2, rms)
                if rms >= 100.0 and self._diag_first_non_silent_at is None:
                    self._diag_first_non_silent_at = now
                    logger.info("STT_AUDIO_FIRST_NON_SILENT elapsed_ms=%.1f rms=%.1f", (now - self._diag_started_at) * 1000, rms)
            with contextlib.suppress(Exception):
                vad_stream.push_frame(frame)  # 基类已重采样到 16k
            await ws.send_bytes(raw)

    async def _vad_consume(self, vad_stream) -> None:
        """逐帧读 VAD 概率,维护 voiced / last_voiced(门控 + GAP 起算)。"""
        async for ev in vad_stream:
            if ev.type == VADEventType.INFERENCE_DONE:
                probability = float(ev.probability)
                voiced = probability >= self._activation
                if self._diag:
                    self._diag_last_vad_prob = probability
                    if voiced != self._diag_last_vad_state:
                        logger.info("STT_VAD_STATE voiced=%s probability=%.3f threshold=%.3f elapsed_ms=%.1f", voiced, probability, self._activation, (time.monotonic() - self._diag_started_at) * 1000)
                        self._diag_last_vad_state = voiced
                self._voiced = voiced
                if self._voiced:
                    self._last_voiced = time.monotonic()

    def _accepting(self) -> bool:
        """门控:有声、或处于最后有声后的 GAP 窗内(容忍识别延迟的尾巴)。"""
        if self._voiced:
            return True
        return self._last_voiced is not None and (time.monotonic() - self._last_voiced) < self._gap

    async def _funasr_recv(self, ws) -> None:
        async for msg in ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                if msg.type in (
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSING,
                ):
                    break
                if msg.type == aiohttp.WSMsgType.ERROR:
                    raise APIConnectionError("FunASR 2pass websocket error")
                continue
            payload = json.loads(msg.data)
            mode = payload.get("mode", "")
            text = (payload.get("text") or "").strip()
            if not text:
                continue
            now = time.monotonic()
            accepting = self._accepting()
            last_age = "-" if self._last_voiced is None else f"{(now - self._last_voiced) * 1000:.1f}"
            if self._diag:
                self._diag_raw_results += 1
                if self._diag_first_text_at is None:
                    self._diag_first_text_at = now
                logger.info("STT_RAW_RESULT mode=%s text=%r voiced=%s vad_prob=%s last_voiced_age_ms=%s accept=%s action=%s", mode, text, self._voiced, "-" if self._diag_last_vad_prob is None else f"{self._diag_last_vad_prob:.3f}", last_age, accepting, "append" if accepting else "drop_vad")
            if not accepting:
                self._ghost_chars += len(text)  # 静音期文本 = 幽灵,丢弃
                if self._diag:
                    self._diag_vad_drops += 1
                    logger.info("STT_VAD_DROP text=%r chars=%d", text, len(text))
                continue
            if mode == "2pass-online":
                self._seg = self._seg + text
            elif mode == "2pass-offline":
                # 整窗重识别去重:GAP 提前提交后 funasr 未内切时,offline 会把已提交
                # FINAL 的音频整窗重新识别再发(前导常带标点);裁掉已提交部分只留
                # 新尾巴,防同轮两条 FINAL 被上游空格拼接成同句重复。
                raw_text = text
                if self._superset_trim:
                    text = _trim_recovered_prefix(
                        text, self._recent_finals, time.monotonic()
                    )
                if self._diag and text != raw_text:
                    logger.info("STT_OFFLINE_TRIM before=%r after=%r", raw_text, text)
                self._prefix = (self._prefix + text).strip()
                self._seg = ""
            else:
                continue
            self._emit_interim()

    async def _gap_watchdog(self) -> None:
        while True:
            await asyncio.sleep(_WATCHDOG_INTERVAL)
            pending = (self._prefix + self._seg).strip()
            if pending and self._last_voiced is not None:
                silence = time.monotonic() - self._last_voiced
                if _commit_ready(silence, pending, self._gap_min, self._gap):
                    self._emit_final(pending)
                    self._prefix = ""
                    self._seg = ""
                    self._last_voiced = None

    def _emit_interim(self) -> None:
        text = (self._prefix + self._seg).strip()
        if text:
            self._event_ch.send_nowait(
                stt.SpeechEvent(
                    type=stt.SpeechEventType.INTERIM_TRANSCRIPT,
                    alternatives=[stt.SpeechData(language=LanguageCode("zh"), text=text)],
                )
            )

    def _emit_final(self, text: str) -> None:
        if self._diag:
            logger.info("STT_FINAL_EMIT text=%r prefix=%r seg=%r", text, self._prefix, self._seg)
        self._event_ch.send_nowait(
            stt.SpeechEvent(
                type=stt.SpeechEventType.FINAL_TRANSCRIPT,
                alternatives=[stt.SpeechData(language=LanguageCode("zh"), text=text)],
            )
        )
        # 记录刚提交的 FINAL 供超集裁剪比对;顺手按 TTL 修剪旧条目。
        now = time.monotonic()
        self._recent_finals.append((now, text))
        self._recent_finals = [
            (ts, t) for ts, t in self._recent_finals if now - ts <= _RECENT_FINAL_TTL_S
        ]
