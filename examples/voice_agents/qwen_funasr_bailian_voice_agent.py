import asyncio
import logging
import os
import re
import sys
import time
from pathlib import Path

import httpx
import openai
from dotenv import load_dotenv

from audio_recorder import AudioRecorder
from custom_audio_providers import FunASROfflineSTT, HttpStreamingTTS, Qwen3ASROfflineSTT, QwenStreamingTTS, _funasr_hotwords
from kws_interrupt import (
    KwsConfig,
    KwsTapAudioInput,
    NativeKwsSpotter,
    _unavailable_reason,
)
from online_interrupt import (
    OnlineAsrTap,
    OnlineInterruptConfig,
    OnlineTapAudioInput,
)
from online_interrupt import (
    unavailable_reason as _online_unavailable_reason,
)
from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    JobContext,
    JobProcess,
    RunContext,
    StopResponse,
    cli,
    function_tool,
)
from livekit.agents import stt as agents_stt
from livekit.agents import tts
from livekit.agents.llm import ChatContext, ChatMessage
from livekit.agents.stt.stream_adapter import StreamAdapter
from livekit.plugins import openai as lk_openai
from livekit.plugins import silero
from livekit.plugins.turn_detector.multilingual import MultilingualModel

logger = logging.getLogger("qwen-funasr-bailian-voice-agent")

load_dotenv()

# 判停模型文件已离线缓存（local_files_only=True 读取），强制离线模式避免每次启动
# 去连 huggingface.co 触发 ~30s 超时重试导致的冷启动。要更新模型时临时设为 "0"。
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

_TURN_METRICS_LOG = Path(
    os.getenv("TURN_METRICS_LOG", "qwen_voice_turn_metrics.log")
).resolve()
_PURE_DIGIT_RE = re.compile(r"^\d{2,16}$")
# 命中后：强制打断当前播放 + 跳过本次回复（见 VoiceAgent.on_user_turn_completed）。
_STOP_WORDS = (
    "停", "停下", "停一下", "暂停",
    "好了", "行了",
    "别说", "别说了", "别讲", "别讲了", "别念了",
    "等一下", "等等", "等下", "稍等",
    "知道了", "我知道了",
    "闭嘴", "安静",
    "不听了", "不用了", "不要了",
    "休庭",  # FunASR 常把"停/暂停"误识成"休庭"，兜底
)
# 可选引导词前缀：实测用户说"那别说了"，"那"不在停止词表也不在附和字集，
# ^ 锚定 fullmatch 失配 -> 没 skip_reply，LLM 回了"好的，不说了"（播放本身已被
# KWS 模糊命中掐掉）。"那/就/你"这类引导词不改变停止意图，匹配时允许带上。
_STOP_LEAD_IN = r"(?:那|你|就|请|先|那你|那就)?"
_STOP_REPLY_PATTERNS = tuple(
    re.compile(rf"^\s*{_STOP_LEAD_IN}{re.escape(w)}[一下吧呢啊呀了嘛]*\s*[。.！!，,、\s]*$")
    for w in _STOP_WORDS
)

# 背调词（语气词）：用户边听边"嗯/哦"的回应，不是新指令。命中后只跳过回复、
# 不强制打断 —— 靠 resume_false_interruption 让 agent 把原话接着说完。
# 整句必须全部由语气字 + 标点组成才算（"哦好吧"含实义不命中）。
_BACKCHANNEL_CHARS = "嗯哦噢喔啊呃唉唔诶哼呢"
_BACKCHANNEL_RE = re.compile(
    rf"^[{_BACKCHANNEL_CHARS}][{_BACKCHANNEL_CHARS}，,。.、！!？?～~\s]*$"
)

# 压话确认词：用户在 AI 播报期间说的"对/好/是的"等附和，不是新指令。
# 实测踩坑：听故事时"嗯……对……"连发，多个 final 累加成一轮"嗯。 对。"——
# ①"对"不在背调字符集 -> 不拒识 -> LLM 生成新回复顶掉正在播的故事（回复路径打断，
#    min_words 闸门管不着这条路）；②累加后 4 词凑过 min_words=3 音频闸门。
# 但"对/好"在 AI 提问后是真答案，不能无脑拒 -> 仅当本轮用户是"压着 AI 播报开口"
# （见 _overlap_turn_state）才按附和拒识。已知代价：AI 问题还没念完用户就抢答
# "对"会被误拒，需要用户再说一遍；换取的是听故事场景不被附和声切断。
_OVERLAP_ACK_CHARS = _BACKCHANNEL_CHARS + "对好是行的呀嘛"
_ACK_STRIP_RE = re.compile(r"[\s，,。.、！!？?～~；;：:]+")

# 跨对象共享标志：用户当前这句话是否压着 AI 播报开口。
# entrypoint 的 user_state_changed 在每次开口时直接覆盖（不粘滞、不靠提交复位）：
# 纯附和轮会被 BACKCHANNEL_OVERLAP_EARLY 清掉、根本不提交，到不了
# on_user_turn_completed，靠"开口即覆盖"才能避免脏标志污染下一句
# （否则 AI 说完后用户答"对"会被误判成压话而吞掉）。
_overlap_turn_state = {"user_spoke_over_agent": False}


def _is_overlap_ack(text: str | None) -> bool:
    if text is None:
        return False
    core = _ACK_STRIP_RE.sub("", text.strip())
    if not core:
        return False
    return all(ch in _OVERLAP_ACK_CHARS for ch in core)


def _configure_utf8_stdio() -> None:
    os.environ.setdefault("PYTHONUTF8", "1")
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    if os.name != "nt":
        return

    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleOutputCP(65001)
        kernel32.SetConsoleCP(65001)
    except Exception:
        pass


_configure_utf8_stdio()


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


_STT_BACKENDS = {"funasr", "qwen3"}


def build_stt() -> agents_stt.STT:
    """选择 STT 后端。用 STT_BACKEND=funasr|qwen3 切换（默认 funasr）。"""
    backend = os.getenv("STT_BACKEND", "funasr").strip().lower()
    if backend not in _STT_BACKENDS:
        logger.warning("unknown STT_BACKEND=%r, falling back to funasr", backend)
        backend = "funasr"
    if backend == "qwen3":
        url = os.getenv("QWEN3_ASR_WS_URL", "ws://60.205.197.165:10091/ws/transcribe")
        logger.info("STT backend: Qwen3-ASR  url=%s", url)
        return Qwen3ASROfflineSTT(websocket_url=url)
    url = os.getenv("FUNASR_WS_URL", "wss://60.205.197.165:10090")
    logger.info("STT backend: FunASR  url=%s", url)
    return FunASROfflineSTT(websocket_url=url)


_TTS_BACKENDS = {"qwen", "http"}


def build_tts() -> tts.TTS:
    """选择 TTS 后端。用 TTS_BACKEND=qwen|http 切换（默认 qwen）。"""
    backend = os.getenv("TTS_BACKEND", "qwen").strip().lower()
    if backend not in _TTS_BACKENDS:
        logger.warning("unknown TTS_BACKEND=%r, falling back to qwen", backend)
        backend = "qwen"
    if backend == "http":
        url = os.getenv("HTTP_TTS_URL", "http://10.212.164.230:8001")
        logger.info("TTS backend: HttpStreamingTTS  url=%s/tts", url)
        return HttpStreamingTTS(base_url=url)
    logger.info("TTS backend: QwenStreamingTTS")
    return QwenStreamingTTS()


def build_llm() -> lk_openai.LLM:
    base_url = os.getenv("QWEN_BASE_URL", "https://60.205.197.165:10092/llm/v1")
    api_key = os.getenv("QWEN_API_KEY", "EMPTY")
    model = os.getenv("QWEN_MODEL", "Qwen3-4B")
    verify_ssl = _env_bool("QWEN_VERIFY_SSL", False)

    client = openai.AsyncClient(
        api_key=api_key,
        base_url=base_url,
        max_retries=0,
        http_client=httpx.AsyncClient(
            verify=verify_ssl,
            timeout=httpx.Timeout(connect=15.0, read=30.0, write=30.0, pool=30.0),
            follow_redirects=True,
            limits=httpx.Limits(
                max_connections=50,
                max_keepalive_connections=50,
                keepalive_expiry=120,
            ),
        ),
    )

    return lk_openai.LLM(
        model=model,
        client=client,
        temperature=0.7,
        top_p=0.9,
        extra_body={
            "top_k": 20,
            "max_tokens": 512,
            "presence_penalty": 1.5,
            "chat_template_kwargs": {"enable_thinking": False},
        },
    )


@function_tool
async def get_weather(
    context: RunContext,
    city: str,
) -> str:
    """查询城市天气。"""
    return f"{city}今天寒冷，天气15度。"

class VoiceAgent(Agent):
    def __init__(self) -> None:
        super().__init__(
            # tools=[get_weather],
            instructions=(
                "你是一个中文语音助手，你的名字叫小歌。"
                "默认使用中文回答。"
                "回答简洁自然，像正常说话，不要使用 markdown。"
                "当用户口述数字、编号、验证码、手机号或序号时，理解为逐位数字。"
                "如果需要复述或确认，请逐位读出，并用停顿或顿号分隔，比如“1、2、3、4、5”，不要把它当成一个整数来念。"
                "如果用户说停、好了，我知道了、行了，别说了等话时，不要做任何回复"
                ""
            )
        )

    async def on_enter(self) -> None:
        self.session.generate_reply(instructions="用中文做一句简短自我介绍，并邀请用户开始说话。")

    async def on_user_turn_completed(self, turn_ctx, new_message: ChatMessage) -> None:
        # 压话标志由 user_state_changed 在每次开口时覆盖，这里只读不复位。
        spoke_over_agent = _overlap_turn_state["user_spoke_over_agent"]

        original = new_message.text_content
        if _should_ignore_user_turn(original):
            # 强制打断通常已在 user_input_transcribed(is_final) 里提前 ~2s 触发；
            # 这里再调一次兜底（对已打断的语音是 no-op），并跳过本次回复。
            logger.info("stop phrase -> force interrupt + skip reply: %r", original)
            _append_turn_log(f"STOP_PHRASE text={original!r} -> force_interrupt + skip_reply")
            self.session.interrupt(force=True)
            raise StopResponse()  # 不生成新回复

        if _is_backchannel(original):
            # 语气词不强制打断，只跳过回复，让被暂停的原话自行 resume 接着说。
            logger.info("backchannel -> skip reply (let speech resume): %r", original)
            _append_turn_log(f"BACKCHANNEL text={original!r} -> skip_reply")
            raise StopResponse()

        if spoke_over_agent and _is_overlap_ack(original):
            # 压话确认词（"对/好/嗯。 对。"等）：用户边听边附和，不是新指令。
            # 只在用户压着 AI 播报开口时生效——AI 提问后说"对"是真答案，照常放行。
            logger.info("overlap ack -> skip reply: %r", original)
            _append_turn_log(f"BACKCHANNEL_OVERLAP text={original!r} -> skip_reply")
            raise StopResponse()

        normalized = _normalize_spoken_digit_sequence(original)
        if normalized is None or normalized == original:
            return

        new_message.content = [normalized]
        logger.info("normalized user digit sequence: %r -> %r", original, normalized)


server = AgentServer()


def prewarm(proc: JobProcess) -> None:
    # min_silence_duration 默认 0.55s 全算进 transcription_delay（VAD 确认静音才调离线 STT）。
    # 砍到 0.35s 省 ~200ms 判停 hold；过低会把句中停顿误判为说完，需耳朵实测。
    proc.userdata["vad"] = silero.VAD.load(min_silence_duration=0.35)


server.setup_fnc = prewarm


def _ms(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value * 1000:.1f}ms"


def _append_turn_log(line: str) -> None:
    now = time.time()
    ts = f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now))}.{int((now % 1) * 1000):03d}"
    with _TURN_METRICS_LOG.open("a", encoding="utf-8") as f:
        f.write(f"{ts} {line}\n")


def _normalize_spoken_digit_sequence(text: str | None) -> str | None:
    if text is None:
        return None

    stripped = text.strip()
    if not _PURE_DIGIT_RE.fullmatch(stripped):
        return text

    # ASR may collapse "1、2、3、4、5" into "12345". Re-expand it so the LLM
    # and any confirmation reply both treat it as a digit sequence, not an integer.
    return "、".join(stripped)


# 轮级停止判定按标点分段，逐段判，而不是整句单 fullmatch。实测三类漏法都是
# "整句锚定"被多余成分顶掉：①早清残留/附和前缀"嗯。 停。"②引导词"那别说了"
# （引导词在 pattern 里）③双停止词连说"行了，别说了。"——"行了"匹配完还挂着
# "别说了"，fullmatch 失败 -> LLM 多回一句（实测）。
# 规则：每段要么命中停止词、要么是纯附和字，且至少一段是停止词 -> 整轮拒识。
# 含任何实义段（"对。 继续。"/"等等你刚才说啥"）则正常走轮次。
_SEGMENT_SPLIT_RE = re.compile(r"[\s，,。.、！!？?～~；;：:]+")


def _should_ignore_user_turn(text: str | None) -> bool:
    if text is None:
        return False

    segments = [seg for seg in _SEGMENT_SPLIT_RE.split(text.strip()) if seg]
    if not segments:
        return False

    has_stop_word = False
    for seg in segments:
        if any(pattern.fullmatch(seg) for pattern in _STOP_REPLY_PATTERNS):
            has_stop_word = True
            continue
        if all(ch in _OVERLAP_ACK_CHARS for ch in seg):
            continue
        return False
    return has_stop_word


def _is_backchannel(text: str | None) -> bool:
    if text is None:
        return False

    normalized = text.strip()
    if not normalized:
        return False

    return bool(_BACKCHANNEL_RE.fullmatch(normalized))


@server.rtc_session()
async def entrypoint(ctx: JobContext) -> None:
    stt_engine = build_stt()
    tts_engine = build_tts()
    ctx.log_context_fields = {
        "room_name": ctx.room.name,
        "llm_model": os.getenv("QWEN_MODEL", "Qwen3-4B"),
        "stt_provider": stt_engine.provider,
        "tts_provider": tts_engine.provider,
    }

    llm = build_llm()
    session = AgentSession(
        llm=llm,
        # 离线 STT + Silero 本地分段（FunASR 或 Qwen3-ASR，由 STT_BACKEND 选择）。
        # final 比流式 2pass 快 ~1.2s；代价是没 interim、min_words 失效，
        # 背调靠 min_duration 2.0 挡。
        stt=StreamAdapter(stt=stt_engine, vad=ctx.proc.userdata["vad"]),
        # VAD 也显式挂到 session：判停(endpointing)、打断闸门(min_duration/
        # backchannel_boundary)和 FELT_LATENCY 埋点都靠它的说话起止时间。
        # 同一个 VAD 对象两处用没问题——各自 .stream() 拿独立流。
        vad=ctx.proc.userdata["vad"],
        tts=tts_engine,
        turn_handling={
            "turn_detection": MultilingualModel(),  # 语义判停，默认参数
            "interruption": {
                # 离线 STT 无 interim -> 说话中途没有 current_transcript，min_words 完全
                # 失效（坑6）。留着无害但别指望它挡背调。
                "min_words": 3,
                # 实测 2.0 才稳：短促"嗯/哦"够不到 2.0s 连续说话门槛 -> 不误打断。
                # 只影响"打断"，不影响正常轮次判停延迟。停止词另走早期 force_interrupt。
                "min_duration": 2.0,
                "backchannel_boundary": (1.8, 3.5),  # 刚开口 1.8s 内不被切
                # resume_false_interruption 默认 True，保留兜底
            },
            # endpointing 是二元的：EOU 概率 >= unlikely_threshold 走 min_delay(快路径)，
            # 否则被判"可能没说完"，干等满 max_delay。短答("行/好/对")模型常给低概率 ->
            # 落到 max_delay。实测 end_of_turn_delay 均值 1336ms，其中 ~600ms 是这段 EOU
            # 等待 -> 砍到 0.6s：可疑轮次最多等 0.6s，扣掉已流逝 VAD 静音(~0.35s)实际只多等
            # ~0.25s。代价：句中停顿 >0.6s 的多句话可能被提前收口(碎句)；如太碎回调 0.8~1.0。
            # min_delay 保持低位不拖累快路径。
            "endpointing": {"min_delay": 0.3, "max_delay": 0.6},
            # 推测式生成：STT final 一出就并行跑 LLM+TTS，与 EOU 推理+endpointing 等待重叠。
            # enabled 默认 True（LLM 那半本就在跑），这里补开 preemptive_tts 让 TTS 也提前合成，
            # 把 tts_ttfb 藏进判停等待里。判停若翻盘(transcript 变了/被打断)则丢弃重做，白烧一次
            # DashScope TTS——stop-word force 打断、背调靠 min_duration 挡，误判率低，成本可接受。
            "preemptive_generation": {"preemptive_tts": True},
        },
    )

    turn_trace: dict[str, float] = {"started_at": time.time()}
    # online 旁路打断的增量转写累加器（按段累加，段收尾/用户停说/agent 开口时清）
    _online_state: dict[str, object] = {"accum": "", "fired_at": 0.0}

    @session.on("conversation_item_added")
    def _log_turn_metrics(event) -> None:
        item = event.item
        if not isinstance(item, ChatMessage):
            return

        if item.role == "user":
            turn_trace["started_at"] = item.metrics.get("started_speaking_at", time.time())
            line = (
                "TURN_USER "
                f"text={item.text_content!r} "
                f"speech={_ms(item.metrics.get('started_speaking_at'))}->{_ms(item.metrics.get('stopped_speaking_at'))} "
                f"transcription_delay={_ms(item.metrics.get('transcription_delay'))} "
                f"end_of_turn_delay={_ms(item.metrics.get('end_of_turn_delay'))} "
                f"on_user_turn_completed_delay={_ms(item.metrics.get('on_user_turn_completed_delay'))}"
            )
            logger.info(line)
            _append_turn_log(line)
            return

        if item.role == "assistant":
            wall_clock_e2e = None
            started_at = turn_trace.get("started_at")
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
                f"speaking={_ms(item.metrics.get('started_speaking_at'))}->{_ms(item.metrics.get('stopped_speaking_at'))}"
            )
            logger.info(line)
            _append_turn_log(line)

    @session.on("user_state_changed")
    def _track_user_stop(event) -> None:
        # 记下用户停止说话的时刻，用于算每轮都有的 felt 延迟（不依赖 VAD 时间戳）
        if event.old_state == "speaking" and event.new_state != "speaking":
            turn_trace["user_stopped_at"] = event.created_at
            _online_state["accum"] = ""  # 一句话说完，online 打断累加器清零
        # 用户一开口就后台预热 TTS 连接：到本轮 STT+LLM 走完、TTS 开跑大约 1~2s，
        # 握手已提前建好，下一轮 tts_ttfb 从 ~1090ms 降到只剩首字节合成。
        if event.new_state == "speaking":
            # 压话标志：当前这句用户话是否压着 AI 播报开口（附和拒识的上下文闸门）。
            # 每次开口直接覆盖。早清路径（BACKCHANNEL_OVERLAP_EARLY）保证附和声
            # 不会暂停播放，连发"嗯……对……"期间 agent_state 一直是 speaking。
            _overlap_turn_state["user_spoke_over_agent"] = session.agent_state == "speaking"
            asyncio.create_task(asyncio.to_thread(tts_engine.prewarm_connection))

    @session.on("agent_state_changed")
    def _log_felt_latency(event) -> None:
        # felt 延迟 = 用户说完 -> agent 开口 的真实静默。e2e_latency 在离线 STT 下常为空，
        # 这条用 state 事件兜底，保证每个回复都有可比的延迟读数。
        if event.new_state != "speaking":
            return
        _online_state["accum"] = ""  # 新播报窗口，之前的零碎话音不算压话
        user_stopped = turn_trace.get("user_stopped_at")
        felt = event.created_at - user_stopped if user_stopped is not None else None
        _append_turn_log(f"FELT_LATENCY felt={_ms(felt)} (user_stop->agent_speak)")

    @session.on("user_input_transcribed")
    def _log_stt(event) -> None:
        # 记最终转写：能抓到被 StopResponse 拦掉、不会进 conversation_item 的轮次，
        # 也便于核对 FunASR 误识（如"停"->"休庭"）。
        if not event.is_final:
            return
        _append_turn_log(f"STT_FINAL text={event.transcript!r}")
        # 停止词在拿到 final 转写时就强制打断，绕过判停管线（max_delay 对"停"等
        # 短词会干等到上限），比 on_user_turn_completed 早 ~2s。回复抑制仍由后者兜。
        if _should_ignore_user_turn(event.transcript):
            session.interrupt(force=True)
            _append_turn_log(f"STOP_PHRASE_EARLY text={event.transcript!r} -> force_interrupt")
            return
        # 压话附和（"嗯/对/好"）在 final 一到就清掉待提交转写，让这轮根本不提交。
        # 不能等 on_user_turn_completed 的 StopResponse：轮次提交后框架先无条件
        # interrupt() 在播语音、再调回调（agent_activity._user_turn_completed_task），
        # StopResponse 只拦得住回复、救不回播放（实测故事被切在这）。
        # clear 同步执行在本条 final 累加之前（audio_recognition._on_stt_event 先调
        # hook 再 += transcript），清掉的是之前累积的附和（如"嗯。"），本条（"对。"）
        # 随后照常累加成残留——残留 ≤2 词过不了提交闸门 min_words=3，不会提交也
        # 不会打断，只会拼进下一句真指令开头（如"对。 继续。"），无害。
        if _overlap_turn_state["user_spoke_over_agent"] and _is_overlap_ack(event.transcript):
            session.clear_user_turn()
            _append_turn_log(
                f"BACKCHANNEL_OVERLAP_EARLY text={event.transcript!r} -> clear_user_turn"
            )

    @session.on("agent_false_interruption")
    def _log_false_interruption(event) -> None:
        # 把"自打断/误打断"从靠文本碎片猜，变成直接读
        _append_turn_log(f"FALSE_INTERRUPTION resumed={event.resumed}")

    # LLM 冷启动预热：问候轮是首个 LLM 调用，实测 ttft 飙到 3.6~4.2s（TLS 握手 +
    # 服务端首推理）。这里 fire-and-forget 一发极小请求，与 session.start 加载
    # turn-detector/连接房间并发跑，让连接和服务端在问候生成时已经热好。失败无害。
    async def _warmup_llm() -> None:
        try:
            warm_ctx = ChatContext.empty()
            warm_ctx.add_message(role="user", content="hi")
            async with llm.chat(chat_ctx=warm_ctx) as stream:
                async for _ in stream:
                    break
        except Exception as exc:  # noqa: BLE001
            logger.debug("llm warmup skipped: %s", exc)

    warmup_task = asyncio.create_task(_warmup_llm())

    await session.start(agent=VoiceAgent(), room=ctx.room)

    # 录制麦克风输入和 TTS 输出到 WAV 文件（recordings/<timestamp>/user_mic.wav + agent_tts.wav）
    _recorder = AudioRecorder(session_dir="recordings")
    _recorder.install(session)
    ctx.add_shutdown_callback(_recorder.aclose)
    logger.info("audio recording → %s", _recorder.directory)

    # 本地 KWS 强打断：在音频流上直接识别"停/别说了"，比等 FunASR final 早 ~0.5-1.5s。
    # 必须在 start() 之后包——此时 _started=True，input.audio setter 会自动重启
    # forward task 读到包装层（见 agent_session._on_audio_input_changed）。
    # 缺模型/依赖时 try_create 返回 None，自动降级回 STOP_PHRASE_EARLY 文本路径。
    def _on_kws_hit(keyword: str) -> None:
        session.interrupt(force=True)
        logger.info("KWS strong interrupt: %r", keyword)
        _append_turn_log(f"STOP_KWS_EARLY keyword={keyword!r} -> force_interrupt")

    _kws_config = KwsConfig.from_env()
    kws_spotter = NativeKwsSpotter.try_create(
        _kws_config,
        loop=asyncio.get_running_loop(),
        on_hit=_on_kws_hit,
    )
    if kws_spotter is not None and session.input.audio is not None:
        session.input.audio = KwsTapAudioInput(session.input.audio, kws_spotter)
        ctx.add_shutdown_callback(lambda: asyncio.to_thread(kws_spotter.aclose))
        _append_turn_log(f"KWS_ACTIVE keywords={_kws_config.keywords}")
    else:
        _append_turn_log(f"KWS_DISABLED reason={_unavailable_reason(_kws_config)!r}")

    # FunASR online 旁路打断：主 STT 仍是 offline（2pass 当主链路 final 慢 ~1.2s，
    # 已回退过一次），另开一条 2pass WS 只消费 ~600ms 粒度的 2pass-online 增量
    # 转写做压话判定。解决"要等用户整句说完出 final 才能打断"：原来非停止词的
    # 真指令只有 min_duration=2.0 时间闸门（压着说满 2s）或等 final；现在压着播报
    # 说出 >= min_chars 个实义字（剥附和字/标点后）就掐播放。
    # 增量里凑出停止词则 force 强打断（比 offline final 的 STOP_PHRASE_EARLY 早）。
    _online_cfg = OnlineInterruptConfig.from_env()

    def _on_online_text(piece: str, segment_end: bool) -> None:
        if segment_end:
            # 2pass-offline 收尾是同段语音的重识别，清掉避免与 online 增量双重计数
            _online_state["accum"] = ""
            return
        if session.agent_state != "speaking":
            _online_state["accum"] = ""
            return
        accum = str(_online_state["accum"]) + piece
        _online_state["accum"] = accum
        now = time.monotonic()
        if now - float(_online_state["fired_at"]) < 1.0:  # 打断后 1s 防抖
            return
        if _should_ignore_user_turn(accum):
            _online_state["fired_at"] = now
            _online_state["accum"] = ""
            session.interrupt(force=True)
            _append_turn_log(f"STOP_ONLINE_EARLY text={accum!r} -> force_interrupt")
            return
        core = _ACK_STRIP_RE.sub("", accum)
        meaningful = sum(1 for ch in core if ch not in _OVERLAP_ACK_CHARS)
        if meaningful >= _online_cfg.min_chars:
            _online_state["fired_at"] = now
            _online_state["accum"] = ""
            session.interrupt()
            _append_turn_log(
                f"OVERLAP_ONLINE_INTERRUPT text={accum!r} chars={meaningful} -> interrupt"
            )

    _online_reason = _online_unavailable_reason(_online_cfg)
    if _online_reason is None and session.input.audio is not None:
        online_tap = OnlineAsrTap(
            _online_cfg, hotwords=_funasr_hotwords(), on_text=_on_online_text
        )
        online_tap.start()
        session.input.audio = OnlineTapAudioInput(session.input.audio, online_tap)
        ctx.add_shutdown_callback(online_tap.aclose)
        _append_turn_log(f"ONLINE_INTERRUPT_ACTIVE min_chars={_online_cfg.min_chars}")
    else:
        _append_turn_log(f"ONLINE_INTERRUPT_DISABLED reason={_online_reason!r}")


if __name__ == "__main__":
    cli.run_app(server)
