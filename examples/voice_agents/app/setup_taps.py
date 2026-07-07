"""entrypoint 的装配函数:后端选择、会话事件处理器、测试仪表、tap 链(录音/KWS/在线打断)。

拆自 web_ui_agent.entrypoint(原 ~400 行单函数)。装配顺序由 entrypoint 编排,
各函数职责单一;跨步骤共享的可变状态收在 SessionWiring(替代原闭包隐式捕获)。
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from audio_recorder import AudioRecorder
from common.runtime import append_turn_log as _log, ms as _ms
from common.text_rules import (
    LEADING_PUNCT_RE,
    is_overlap_ack,
    should_ignore_user_turn,
)
from kws_interrupt import KwsConfig, KwsTapAudioInput, NativeKwsSpotter, _unavailable_reason
from listening_mode import ListeningController, ListeningEvent
from live_transcript import LiveTranscript, LiveTranscriptConfig
from mute_gate import MuteGate
from webpanel.bridge import broadcast
from webpanel.state import WEB_AUDIO

from app.backends import build_stt
from app.listening_host import (
    listen_enter_aftermath,
    listen_exit_aftermath,
    listen_interrupt_blocked,
    listen_tail_pending,
)
from app.session_state import runtime
from app.web_audio import WebSocketAudioInput, WebSocketAudioOutput
from livekit.agents import JobContext
from livekit.agents.llm import ChatContext, ChatMessage
from livekit.agents.stt.stream_adapter import StreamAdapter

logger = logging.getLogger("web-ui-agent")


@dataclass
class SessionWiring:
    """一次会话装配的共享可变状态(原 entrypoint 闭包捕获的局部变量,显式化)。"""

    session: Any
    stt_engine: Any
    tts_engine: Any
    live_from_main: bool
    live: Any = None  # LiveTranscript(装配后期才创建;事件处理器按属性晚绑定)
    timeline: Any = None
    record_settings: Any = None  # RecordSettings(RECORD_MODE/TIMELINE_LEVEL 解析,PR-A2)
    record_dir: Any = None  # 录音/审计产物落盘目录(instrumentation 算一次,recording 复用)
    turn_trace: dict[str, float] = field(default_factory=lambda: {"started_at": time.time()})
    # vad_speaking/vad_off_ts:用于在线软打断的 VAD 佐证(防短幽灵词/接话误打断)。
    online_state: dict[str, object] = field(
        default_factory=lambda: {
            "accum": "",
            "fired_at": 0.0,
            "vad_speaking": False,
            "vad_off_ts": 0.0,
        }
    )


def setup_stt(ctx: JobContext) -> tuple[Any, Any, bool]:
    """主STT 选择。返回 (stt_engine, stt_for_session, live_from_main)。

    XIAOGE_STACK=optimized 默认走 funasr-stream;STT_BACKEND 显式覆盖:
      funasr(默认/upstream) = 离线 FunASR + StreamAdapter(VAD 硬切)
      funasr-stream         = FunASR 2pass 流式(GAP 聚合 + VAD 门控,不过 StreamAdapter)
      iflytek               = 讯飞 RTASR(可选第三方)
    流式后端均"不过 StreamAdapter"且 switchable_stt=None(面板 ASR 热切换不适用,重启切换)。
    """
    stack = (os.getenv("XIAOGE_STACK") or "upstream").strip().lower()
    default_stt = "funasr-stream" if stack == "optimized" else "funasr"
    stt_mode = (os.getenv("STT_BACKEND") or default_stt).strip().lower()
    if stt_mode == "iflytek":
        from providers.stt.iflytek import IFlyTekRTASR

        stt_engine = IFlyTekRTASR()
        runtime.switchable_stt = None
        stt_for_session = stt_engine
    elif stt_mode == "funasr-stream":
        from providers.stt.funasr_stream import FunASRStreamSTT

        stt_engine = FunASRStreamSTT()  # 内置独立 silero VAD;GAP/门控见模块
        runtime.switchable_stt = None
        stt_for_session = stt_engine  # 流式,不过 StreamAdapter
    else:
        stt_engine = build_stt()
        runtime.switchable_stt = stt_engine  # expose for web server(可热切换)
        stt_for_session = StreamAdapter(stt=stt_engine, vad=ctx.proc.userdata["vad"])
    _log(f"STT_START provider={stt_engine.provider} mode={stt_mode} stack={stack}")
    # 显示同源:流式主STT(有原生 interim)用主STT 文本驱动 live 气泡(与内容/上下文同源);
    # 离线后端无 interim,仍由在线2pass 驱动气泡。在线2pass tap 始终保留作打断用。
    live_from_main = stt_mode in {"funasr-stream", "iflytek"}
    return stt_engine, stt_for_session, live_from_main


def _log_user_item(w: SessionWiring, item: ChatMessage) -> None:
    """TURN_USER 指标日志 + 用户气泡广播。"""
    w.turn_trace["started_at"] = item.metrics.get("started_speaking_at", time.time())
    line = (
        "TURN_USER "
        f"text={item.text_content!r} "
        f"speech={_ms(item.metrics.get('started_speaking_at'))}"
        f"->{_ms(item.metrics.get('stopped_speaking_at'))} "
        f"transcription_delay={_ms(item.metrics.get('transcription_delay'))} "
        f"end_of_turn_delay={_ms(item.metrics.get('end_of_turn_delay'))} "
        f"on_user_turn_completed_delay={_ms(item.metrics.get('on_user_turn_completed_delay'))}"
    )
    logger.info(line)
    _log(line)
    # push to browser
    broadcast(
        {
            "type": "message",
            "role": "user",
            # 仅显示净化:去掉句首游离标点(FunASR 把上句尾标点带到句首);上下文用原文不变
            "text": LEADING_PUNCT_RE.sub("", item.text_content or ""),
            "ts": time.time(),
        }
    )


def _log_assistant_item(w: SessionWiring, item: ChatMessage) -> None:
    """TURN_ASSISTANT 指标日志(文本已由 transcription_node 广播,此处不再广播)。"""
    wall_clock_e2e = None
    started_at = w.turn_trace.get("started_at")
    assistant_started = item.metrics.get("started_speaking_at")
    if started_at is not None and assistant_started is not None:
        wall_clock_e2e = assistant_started - started_at
    line = (
        "TURN_ASSISTANT "
        f"text={item.text_content!r} "
        f"llm_ttft={_ms(item.metrics.get('llm_node_ttft'))} "
        f"tts_ttfb={_ms(item.metrics.get('tts_node_ttfb'))} "
        f"playback_latency={_ms(item.metrics.get('playback_latency'))} "
        f"e2e_latency={_ms(item.metrics.get('e2e_latency'))} "
        f"wall_clock_e2e={_ms(wall_clock_e2e)} "
        f"speaking={_ms(item.metrics.get('started_speaking_at'))}"
        f"->{_ms(item.metrics.get('stopped_speaking_at'))}"
    )
    logger.info(line)
    _log(line)


def _handle_agent_state(w: SessionWiring, event) -> None:
    broadcast({"type": "state", "agent_state": event.new_state})
    if event.new_state != "speaking":
        return
    w.online_state["accum"] = ""
    user_stopped = w.turn_trace.get("user_stopped_at")
    felt = event.created_at - user_stopped if user_stopped is not None else None
    _log(f"FELT_LATENCY felt={_ms(felt)} (user_stop->agent_speak)")


def register_session_handlers(w: SessionWiring) -> None:
    """注册 5 个会话事件处理器(指标日志/浏览器广播/压话标志/早打断)。"""
    session = w.session

    @session.on("conversation_item_added")
    def _on_item(event) -> None:
        item = event.item
        if not isinstance(item, ChatMessage):
            return
        if item.role == "user":
            _log_user_item(w, item)
        elif item.role == "assistant":
            _log_assistant_item(w, item)

    @session.on("agent_state_changed")
    def _on_agent_state(event) -> None:
        _handle_agent_state(w, event)

    @session.on("user_state_changed")
    def _track_user(event) -> None:
        _handle_user_state(w, event)

    @session.on("user_input_transcribed")
    def _on_stt(event) -> None:
        _handle_stt_event(w, event)

    @session.on("agent_false_interruption")
    def _on_false_interrupt(event) -> None:
        _log(f"FALSE_INTERRUPTION resumed={event.resumed}")


def _handle_user_state(w: SessionWiring, event) -> None:
    """开口/停说:压话标志覆盖、VAD 佐证时间戳、TTS/STT 连接预热。"""
    if event.old_state == "speaking" and event.new_state != "speaking":
        w.turn_trace["user_stopped_at"] = event.created_at
        w.online_state["accum"] = ""
        w.online_state["vad_speaking"] = False
        w.online_state["vad_off_ts"] = time.monotonic()
    if event.new_state == "speaking":
        runtime.overlap_turn_state["user_spoke_over_agent"] = w.session.agent_state == "speaking"
        w.online_state["vad_speaking"] = True
        asyncio.create_task(asyncio.to_thread(w.tts_engine.prewarm_connection))
        if hasattr(w.stt_engine, "prewarm_connection"):
            asyncio.create_task(w.stt_engine.prewarm_connection())


def _handle_stt_event(w: SessionWiring, event) -> None:
    """interim/final 转写:live 气泡驱动 + 停止词早打断 + 压话附和早清。"""
    session = w.session
    # 聆听期 + 退出尾巴待处理期都不弹气泡(挡滞后到达、含唤醒词及之前内容的尾巴)
    ctrl = runtime.listen_ctrl
    listening = ctrl is not None and (ctrl.active or listen_tail_pending())
    if not event.is_final:
        # 显示同源:流式主STT 的 interim 驱动 live 气泡(全量置换)。
        if w.live_from_main and w.live is not None and not listening:
            w.live.feed_full(event.transcript)
        return
    # 中途 FINAL:并入 live 气泡累计,防超长轮内 interim 清零导致气泡缩水(消失再重来)。
    if w.live_from_main and w.live is not None and not listening:
        w.live.feed_commit(event.transcript)
    _log(f"STT_FINAL text={event.transcript!r}")
    if should_ignore_user_turn(event.transcript):
        if not listen_interrupt_blocked():  # 聆听期/保护窗内的"停"等不打断小歌
            session.interrupt(force=True)
            _log(f"STOP_PHRASE_EARLY text={event.transcript!r} -> force_interrupt")
        return
    # 聆听期不做 overlap-ack 早清(让该轮流到 on_user_turn_completed 由 ① capture)
    if (
        not listening
        and runtime.overlap_turn_state["user_spoke_over_agent"]
        and is_overlap_ack(event.transcript)
    ):
        session.clear_user_turn()
        _log(f"BACKCHANNEL_OVERLAP_EARLY text={event.transcript!r} -> clear_user_turn")


def start_llm_warmup(llm: Any) -> None:
    """LLM 冷启动预热:fire-and-forget 一发极小请求,与 session.start 并发跑。失败无害。"""

    async def _warmup_llm() -> None:
        try:
            warm_ctx = ChatContext.empty()
            warm_ctx.add_message(role="user", content="hi")
            async with llm.chat(chat_ctx=warm_ctx) as stream:
                async for _ in stream:
                    break
        except Exception as exc:
            logger.debug("llm warmup skipped: %s", exc)

    asyncio.create_task(_warmup_llm())


def setup_test_instrumentation(ctx: JobContext, w: SessionWiring) -> Any:
    """结构化事件时间线 + 判停 KPI。开关解析见 RecordSettings(PR-A2),**默认=现状**
    (未设 → AGENT_TIMELINE 主导),PC/测试形态逐字节不变。设 w.timeline/record_settings/
    record_dir,返回 turn_metrics(仅 debug 档非 None):
      - `debug`(≡ AGENT_TIMELINE=1):全事件 + debug.log + KPI 进 runs/(现状);
      - `audit`:轮次级白名单 timeline 进 recordings/,不落 debug.log/KPI(K3);
      - `off`:不装 timeline(full/single 无 timeline 时仍算好 record_dir 供录音复用)。"""
    from app.record_settings import RecordSettings

    settings = RecordSettings.from_env()
    w.record_settings = settings
    session = w.session
    repo_root = Path(__file__).resolve().parents[3]
    level = settings.timeline_level

    if level == "off":
        if settings.record_mode in {"full", "single"}:
            w.record_dir = settings.target_dir(repo_root)
        return None
    try:
        from event_timeline import EventTimeline

        run_dir = settings.target_dir(repo_root)
        w.record_dir = run_dir
        timeline = EventTimeline(run_dir, level=level)
        timeline.attach(session)
        ctx.add_shutdown_callback(timeline.aclose)
        w.timeline = timeline
        if level == "audit":  # 审计档:仅轮次级 timeline,不落 debug.log/KPI
            _log(f"AUDIT_TIMELINE dir={run_dir}")
            logger.info("audit timeline -> %s", run_dir)
            return None
        # debug 档:另加判停 KPI 仪表盘 + 全量 DEBUG 日志(现状,收尾写 turn_kpis.json)。
        from turn_metrics import TurnMetrics

        turn_metrics = TurnMetrics(timeline.directory, timeline=timeline)
        turn_metrics.attach(session)
        ctx.add_shutdown_callback(turn_metrics.aclose)
        _log("TURN_METRICS attached")
        from event_timeline import install_debug_log, remove_debug_log

        dbg_state = install_debug_log(run_dir)
        ctx.add_shutdown_callback(lambda: remove_debug_log(dbg_state))
        _log(f"TIMELINE dir={timeline.directory}")
        logger.info("event timeline + debug log -> %s", timeline.directory)
        return turn_metrics
    except Exception as exc:  # 时间线初始化失败绝不阻塞启动
        logger.warning("event timeline disabled: %s", exc)
        return None


def setup_scenario_injection(session: Any, turn_metrics: Any) -> None:
    """录音回放注入(自动化测试):仅设了 AGENT_SCENARIO 才启用,默认正常麦克风。
    必须在 recorder/KWS/online tap 包裹之前替换,使注入音频被如实录音并经各 tap。"""
    scenario = os.getenv("AGENT_SCENARIO", "").strip()
    if not scenario:
        return
    try:
        from scripted_audio import ScriptedAudioInput

        si = ScriptedAudioInput.from_scenario(scenario)
        session.input.audio = si
        if turn_metrics is not None and si.expect:
            turn_metrics.set_expected(si.expect)
        _log(f"SCENARIO_INJECT path={scenario} expect={'Y' if si.expect else 'N'}")
        logger.info("scenario injection active: %s", scenario)
    except Exception as exc:  # 注入失败绝不阻塞:退回正常麦克风
        logger.warning("scenario injection disabled: %s", exc)


def setup_web_audio(session: Any) -> None:
    """WEB_AUDIO=1:浏览器 PCM 入向源替换 input;出向包裹 output(headless 时无本地链)。"""
    import sys

    if not WEB_AUDIO:
        return
    runtime.ws_audio_input = WebSocketAudioInput()
    session.input.audio = runtime.ws_audio_input
    if not sys.stdin.isatty():
        runtime.ws_audio_output = WebSocketAudioOutput(None)
    elif session.output.audio is not None:
        runtime.ws_audio_output = WebSocketAudioOutput(session.output.audio)
    if runtime.ws_audio_output is not None:
        session.output.audio = runtime.ws_audio_output
    _log("WS_AUDIO_ACTIVE sample_rate=16000")
    logger.info("WebSocket audio mode active - clients connect to /ws/audio")


def setup_mute_gate(session: Any) -> None:
    """静音门(关麦=真关麦):最内层包裹(在 recorder/KWS/在线2pass 之前),关麦时下游
    所有消费者收静音 → 不转写/不打断/真人声不出本机。默认直通,零影响。"""
    if session.input.audio is not None:
        runtime.mute_gate = MuteGate(session.input.audio)
        session.input.audio = runtime.mute_gate


def _install_test_recorder(
    ctx: JobContext,
    w: SessionWiring,
    rec_dir: Any,
    *,
    mono: bool,
    segment_seconds: float | None = None,
) -> None:
    """装 TestRecorder 到指定目录;失败绝不阻塞启动。"""
    from test_recorder import TestRecorder

    recorder = TestRecorder(rec_dir, write_mono_tracks=mono, segment_seconds=segment_seconds)
    recorder.install(w.session)
    runtime.test_recorder = recorder  # 暴露给 /api/mic 做暂停/继续
    recorder.set_paused(bool(getattr(w.stt_engine, "muted", False)))  # 与当前静音状态对齐
    ctx.add_shutdown_callback(recorder.aclose)


def setup_recording(ctx: JobContext, w: SessionWiring) -> None:
    """录音(PR-A2):RECORD_MODE `full`/`single` → TestRecorder 进 recordings/<id>/(single 仅
    duplex);`off` → 不录;`legacy`(未设,现状)→ timeline 开=多轨进 run 目录、否则单文件混音。"""
    session = w.session
    settings = w.record_settings
    mode = settings.record_mode if settings else "legacy"

    if mode == "off":
        _log("RECORDING off (XIAOGE_RECORD_MODE=off)")
        return
    if mode in {"full", "single"}:
        try:
            _install_test_recorder(
                ctx,
                w,
                w.record_dir,
                mono=settings.writes_mono_tracks,
                segment_seconds=settings.segment_seconds,
            )
            _log(f"RECORDER mode={mode} dir={w.record_dir} seg={settings.segment_seconds}")
        except Exception as exc:  # 录音初始化失败绝不阻塞启动
            logger.warning("recorder disabled: %s", exc)
        return

    # legacy(现状):测试模式(timeline 开)= 多轨 TestRecorder 进 run 目录;正常模式 = 单文件混音。
    if w.timeline is not None:
        try:
            _install_test_recorder(ctx, w, w.timeline.directory, mono=True)
            _log(f"TEST_RECORDER dir={w.timeline.directory}")
        except Exception as exc:
            logger.warning("test recorder disabled: %s", exc)
    else:
        recorder = AudioRecorder(session_dir="recordings")
        recorder.install(session)
        ctx.add_shutdown_callback(recorder.aclose)
        _log(
            f"AUDIO_RECORDER dir={recorder.directory} "
            f"input={session.input.audio!r} output={session.output.audio!r}"
        )


def setup_live_transcript(w: SessionWiring) -> None:
    """实时转写显示(Web 面板 live 气泡):独立模块,默认可 LIVE_TRANSCRIPT=0 关。"""
    lt_cfg = LiveTranscriptConfig.from_env()
    w.live = LiveTranscript(broadcast, lt_cfg, timeline=w.timeline) if lt_cfg.enabled else None
    if w.live is not None:
        w.live.attach(w.session)
        _log(f"LIVE_TRANSCRIPT new_turn_gap={lt_cfg.new_turn_gap_s}")


def setup_kws(ctx: JobContext, w: SessionWiring) -> None:
    """本地 KWS 强打断 + 聆听模式控制器(命令词并入 KWS 词表)。缺模型/依赖自动降级。"""
    from dataclasses import replace

    session = w.session

    def _on_kws_hit(keyword: str) -> None:
        # 进入聆听(尚未 active)需立即停小歌→强打断;聆听期/退出保护窗内不打断
        if not listen_interrupt_blocked():
            session.interrupt(force=True)
        # 聆听命令词优先(本回调已在 agent 循环):进入/退出后 return,不走停止词逻辑
        ctrl = runtime.listen_ctrl
        if ctrl is not None and ctrl.enabled:
            evt = ctrl.observe_keyword(keyword)
            if evt == ListeningEvent.ENTERED:
                listen_enter_aftermath("kws", notice=True)
                return
            if evt == ListeningEvent.EXITED:
                listen_exit_aftermath("kws", ask=True)
                return
        logger.info("KWS strong interrupt: %r", keyword)
        _log(f"STOP_KWS_EARLY keyword={keyword!r} -> force_interrupt")
        if w.timeline is not None:
            w.timeline.emit("interrupt.kws", {"keyword": keyword}, source="kws")

    # 聆听模式控制器(纯状态机,默认关);命令词追加到 KWS 词表(from_env 会整体覆盖,故用 replace)
    runtime.listen_ctrl = ListeningController.from_environment()
    kws_config = KwsConfig.from_env()
    if runtime.listen_ctrl.enabled and runtime.listen_ctrl.keywords:
        kws_config = replace(
            kws_config, keywords=tuple(kws_config.keywords) + runtime.listen_ctrl.keywords
        )
        _log(f"LISTEN_ENABLED keywords={runtime.listen_ctrl.keywords}")
    kws_spotter = NativeKwsSpotter.try_create(
        kws_config,
        loop=asyncio.get_running_loop(),
        on_hit=_on_kws_hit,
    )
    if kws_spotter is not None and session.input.audio is not None:
        session.input.audio = KwsTapAudioInput(session.input.audio, kws_spotter)
        ctx.add_shutdown_callback(lambda: asyncio.to_thread(kws_spotter.aclose))
        _log(f"KWS_ACTIVE keywords={kws_config.keywords}")
    else:
        _log(f"KWS_DISABLED reason={_unavailable_reason(kws_config)!r}")
