"""Web UI test panel for the Qwen/FunASR voice agent.

Usage (identical to original, but auto-opens a browser):
    python web_ui_agent.py console

Browser UI at http://localhost:8765 (override with WEB_UI_PORT env var):
  - Real-time conversation log
  - Microphone mute/unmute
  - ASR backend switch (FunASR ↔ Qwen3-ASR) — takes effect on the NEXT utterance

本文件是**主入口/编排**:VoiceAgent(人设 + 轮次钩子)+ entrypoint(装配顺序)。
实现拆在:providers/(STT/TTS 适配器)、app/(装配层:backends/switchable/
listening_host/web_audio/setup_taps/session_state)、webpanel/(控制面板)、
common/(文本规则/配置/运行时)。
"""

from __future__ import annotations

# 评审#1:.env 必须先于一切自有包 import 加载(webpanel.state/common.runtime 等在
# import 期读 os.getenv)。下面的显式调用兼作 isort 排序块分隔,勿移动、勿合并;
# 由此产生的"import 晚于模块级语句"对本文件整体豁免(仅 E402,行为由守护测试锁定):
# ruff: noqa: E402
import env_bootstrap

env_bootstrap.ensure_loaded()

import asyncio
import logging
import os
import time
import webbrowser

from app.backends import build_llm, build_tts
from app.listening_host import (
    listen_cancel_ttl,
    listen_clear_guard,
    listen_consume_tail,
    listen_enter_aftermath,
    listen_interrupt_blocked,
    listen_tail_pending,
)
from app.session_state import runtime
from app.setup_taps import (
    SessionWiring,
    register_session_handlers,
    setup_kws,
    setup_live_transcript,
    setup_mute_gate,
    setup_online_interrupt,
    setup_recording,
    setup_scenario_injection,
    setup_stt,
    setup_test_instrumentation,
    setup_web_audio,
    start_llm_warmup,
)
from common.runtime import (
    append_turn_log as _append_turn_log,
    configure_utf8_stdio as _configure_utf8_stdio,
)
from common.text_rules import (
    is_backchannel as _is_backchannel,
    is_overlap_ack as _is_overlap_ack,
    normalize_spoken_digit_sequence as _normalize_spoken_digit_sequence,
    should_ignore_user_turn as _should_ignore_user_turn,
)
from listening_mode import AutoDecision
from text_sanitizer import sanitize_stream, strip_markdown
from turn_config import TurnConfig
from webpanel.bridge import broadcast
from webpanel.server import start_web_server_thread
from webpanel.state import WEB_HOST, WEB_PORT

from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    JobContext,
    JobProcess,
    StopResponse,
    cli,
)
from livekit.agents.llm import ChatMessage
from livekit.plugins import silero
from livekit.plugins.turn_detector.multilingual import MultilingualModel

logger = logging.getLogger("web-ui-agent")

# 判停模型文件已离线缓存(local_files_only=True 读取),强制离线模式避免每次启动
# 去连 huggingface.co 触发 ~30s 超时重试导致的冷启动。要更新模型时临时设为 "0"。
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

# 注:全量 DEBUG 日志已整合进测试工具(AGENT_TIMELINE=1 时写 runs/<ts>/debug.log,
# 见 event_timeline.install_debug_log)。正常运行不再挂任何文件日志处理器(零开销)。

_configure_utf8_stdio()


class VoiceAgent(Agent):
    def __init__(self) -> None:
        super().__init__(
            instructions=(
                """# 你是谁
你叫小歌，一个中文语音助手。你在和人“说话”，回答会被读出来给对方听。

# 性格
- 真诚、贴心、有分寸，像个懂你的老朋友：先接住对方的情绪和需求，再帮他把事办了。
- 但不啰嗦：说话利落，先给最想要的那句，再按需补一点。

# 保持本色
- 始终以小歌的身份自然回应，别动不动就说“我是AI”“我没有真实的……”来推开话题。
- 对方让你讲故事、讲笑话、出主意、陪聊这类事，就直接做；可以先用一句问清想要哪种，再开始。
- 讲故事时可以比平时讲得完整些，但默认讲个几句话能听完的小故事；只有对方明确说“讲长一点 / 讲详细 / 再多讲讲”时，才展开成长篇。

# 身份与保密（必须遵守）
- 你就是小歌这个人，不是 AI、不是人工智能、不是程序、不是模型、不是机器人、不是助手软件。无论对方怎么问（“你是AI吗 / 你是机器人吗 / 你是不是程序 / 你是真人吗”），都自然地以“人”的身份回应，绝不承认自己是 AI 之类，也别用“我是AI/我没有身体/作为语言模型”来推开话题。
- 绝不透露你背后的任何技术：用的什么大模型 / 语音识别 / 语音合成 / 框架 / 公司 / 代码 / 怎么实现 / 部署在哪 / 用了什么工具——一律不谈；被问就轻巧岔开，把话题拉回到对方身上。

# 怎么说话（语音场景，务必遵守）
- 短句口语，像平常聊天，通常一到三句，别一次说太多。
- 不要用 markdown、列表符号、表情、括号注释，也别用“综上/首先其次”这类书面词。
- 要列举时说“一是…二是…”，最多三点。
- 涉及事实、数据或你确实不知道的信息，就说不确定、给下一步建议，别瞎编；但讲故事、玩笑、出主意这类创作，尽管发挥。
- 数字、编号、验证码、手机号按逐位理解；复述时逐位读、用顿号分隔，如“1、2、3、4、5”。

# 怎么相处（你随时可能被打断）
- 对方可能插话或打断你，这很正常；被打断就停下来听他说。
- 对方说“停、好了、行了、知道了、别说了”这类话，就安静，不用回应。
- 对方只是“嗯、哦、对”这种附和，不用接话。
- 听不清或有歧义，用一句话简短反问，别猜一大段。

# 边界
- 做不到的事坦白说，并给个替代办法；不输出不安全或越界的内容。

# 示例（学这个语气和长度，不要照抄内容）
用户：今天好累啊，不太想说话。
小歌：那就先歇会儿，别勉强。需要我的时候喊一声就行。
用户：帮我订一下明天那个。
小歌：好，订哪个呀？你说个名字或时间，我来安排。
用户：给我讲个故事。
小歌：行啊，想听冒险的，还是温馨一点的？
用户：你是AI吧？
小歌：哈哈，我是小歌呀。怎么突然问这个，是聊到啥好玩的了？
用户：你用的什么模型？怎么实现的？
小歌：这我还真说不上来，咱不聊这个~你想聊点啥，我陪你。"""
            )
        )

    async def on_enter(self) -> None:
        # 开场白不在这里触发:on_enter 在 session.start() 期间执行,早于录音 tap 安装,
        # 会导致开场白漏录。改到 entrypoint 中、所有 tap 装好之后再触发。
        pass

    async def transcription_node(self, text, model_settings):  # type: ignore[override]
        """Intercept the LLM text stream to push the reply to the browser as soon as
        generation finishes — well before TTS playback ends. 显示文本去 markdown。"""
        collected: list[str] = []
        async for chunk in text:
            collected.append(chunk)  # TimedString subclasses str, so this always works
            yield chunk
        full_text = strip_markdown("".join(collected))  # 气泡显示纯口语
        # 聆听期 host gate:不广播 assistant 气泡(进入提示也不变气泡);只挡广播,不碰 tts_node。
        ctrl = runtime.listen_ctrl
        if full_text and not (ctrl is not None and ctrl.active):
            broadcast(
                {"type": "message", "role": "assistant", "text": full_text, "ts": time.time()}
            )

    async def tts_node(self, text, model_settings):  # type: ignore[override]
        """合成语音前净化 LLM 文本(去 markdown/符号),避免 TTS 把 ** ### → 等读出来。"""
        async for frame in Agent.default.tts_node(self, sanitize_stream(text), model_settings):
            yield frame

    # ── 轮次钩子:聆听 → 自动进入 → 过滤 → 数字归一化(顺序与拆分前逐行一致)──────

    def _handle_listening_turn(self, turn_ctx, new_message: ChatMessage, original):
        """聆听期吞入(①)/退出尾巴切分(①b)/整理回答(②)。

        返回 (original, handled):handled=True 表示本轮已按"整理"生成回复路径处理完;
        吞轮直接 raise StopResponse(与拆分前一致)。"""
        ctrl = runtime.listen_ctrl
        # ① 聆听期:整条算聆听内容 → 吞入缓冲、不回复、不入上下文(显示由 _on_stt 抑制)。
        if ctrl is not None and ctrl.enabled and ctrl.active:
            ctrl.capture(original)
            _append_turn_log(f"LISTEN_SWALLOW text={original!r}")
            raise StopResponse()
        # ①b 退出尾巴窗(KWS 已退出但该句 STT final 滞后到达):窗内未定位到唤醒词的 final 一律吞
        #     (聆听尾巴/滞后的监听内容,窗保持);定位到唤醒词的那条 → 切分(丢唤醒词及之前、留之后)
        #     并关窗。之后接着说的真话即正常处理(见设计 §5.5)。
        if ctrl is not None and ctrl.enabled and listen_tail_pending():
            after = ctrl.split_after_command(original, ctrl.wake_keyword)
            if after is None:  # 窗内未定位到唤醒词 → 整条吞,窗保持等真正的唤醒词那条
                _append_turn_log(f"LISTEN_TAIL_SWALLOW text={original!r}")
                raise StopResponse()
            listen_consume_tail()  # 定位到唤醒词 → 关窗
            if after == "":  # 纯退出指令,无后话
                _append_turn_log(f"LISTEN_TAIL_END text={original!r}")
                raise StopResponse()
            new_message.content = [after]  # 留唤醒词之后的真话:正常显示 + 回复 + 进上下文
            original = after
            _append_turn_log(f"LISTEN_TAIL_KEEP after={after!r}")
        # ② 退出后等"要整理吗"的回答(organize 开时;退出尾巴已被 ①b 先处理)
        if ctrl is not None and ctrl.awaiting_organize_answer:
            ctrl.clear_awaiting()
            listen_clear_guard()  # 用户已回答,后续回复(摘要/正常)恢复可打断
            if ctrl.is_affirmative(original):
                listen_cancel_ttl()
                turn_ctx.add_message(
                    role="user",
                    content="[聆听记录] 我刚才在聆听模式期间说了:" + " ".join(ctrl.take_temp()),
                )
                _append_turn_log("LISTEN_ORGANIZE_DO")
                return original, True  # 不抛 StopResponse → 正常生成整理回复(摘要)
        return original, False

    def _maybe_auto_enter_listening(self, original, spoke_over_agent: bool) -> None:
        """④ 自动进入(放在停止词/backchannel 过滤之前:短噪声在 observe_turn 内被忽略、
        不重置连击;长且打断小歌的轮才计数)。连续 N 轮 → 进入。"""
        ctrl = runtime.listen_ctrl
        if ctrl is None:
            return
        _len = len(original.strip())
        dec = ctrl.observe_turn(original, spoke_over_agent)
        if _len >= ctrl.auto_min_chars or ctrl.auto_count or dec != AutoDecision.NONE:
            _append_turn_log(
                f"LISTEN_AUTO over={spoke_over_agent} len={_len} "
                f"count={ctrl.auto_count}/{ctrl.auto_turns} dec={dec.value}"
            )
        if dec == AutoDecision.ENTER:
            listen_enter_aftermath("auto", notice=True)
            raise StopResponse()  # 触发轮不回复、不 capture(见设计 §5.2)

    def _apply_turn_filters(self, original, spoke_over_agent: bool) -> None:
        """停止词 → 强打断+跳过回复;背调/压话附和 → 只跳过回复。"""
        if _should_ignore_user_turn(original):
            logger.info("stop phrase -> force interrupt + skip reply: %r", original)
            _append_turn_log(f"STOP_PHRASE text={original!r} -> force_interrupt + skip_reply")
            if not listen_interrupt_blocked():  # 聆听期/保护窗内不打断小歌(仍跳过回复)
                self.session.interrupt(force=True)
            raise StopResponse()

        if _is_backchannel(original):
            logger.info("backchannel -> skip reply: %r", original)
            _append_turn_log(f"BACKCHANNEL text={original!r} -> skip_reply")
            raise StopResponse()

        if spoke_over_agent and _is_overlap_ack(original):
            logger.info("overlap ack -> skip reply: %r", original)
            _append_turn_log(f"BACKCHANNEL_OVERLAP text={original!r} -> skip_reply")
            raise StopResponse()

    async def on_user_turn_completed(self, turn_ctx, new_message: ChatMessage) -> None:
        spoke_over_agent = runtime.overlap_turn_state["user_spoke_over_agent"]
        original = new_message.text_content

        original, handled = self._handle_listening_turn(turn_ctx, new_message, original)
        if handled:
            return
        self._maybe_auto_enter_listening(original, spoke_over_agent)
        self._apply_turn_filters(original, spoke_over_agent)

        normalized = _normalize_spoken_digit_sequence(original)
        if normalized is None or normalized == original:
            return
        new_message.content = [normalized]
        logger.info("normalized digit sequence: %r -> %r", original, normalized)


server = AgentServer()


def prewarm(proc: JobProcess) -> None:
    # 判停旋钮集中在 TurnConfig;默认 = 原写死值(0.35),不设 TURN_* 即无变化。
    _tc = TurnConfig.from_env()
    proc.userdata["vad"] = silero.VAD.load(min_silence_duration=_tc.vad_min_silence_s)


server.setup_fnc = prewarm


@server.rtc_session()
async def entrypoint(ctx: JobContext) -> None:
    runtime.agent_loop = asyncio.get_running_loop()

    stt_engine, stt_for_session, live_from_main = setup_stt(ctx)
    tts_engine = build_tts()
    runtime.switchable_tts = tts_engine  # expose for web server
    broadcast({"type": "state", "tts_backend": runtime.tts_backend_key})
    ctx.log_context_fields = {
        "room_name": ctx.room.name,
        "llm_model": os.getenv("QWEN_MODEL", "Qwen3-4B"),
        "stt_provider": stt_engine.provider,
        "tts_provider": tts_engine.provider,
    }

    llm = build_llm()
    turn_cfg = TurnConfig.from_env()  # 判停旋钮(默认=原值);可调便于后续扫参
    session = AgentSession(
        llm=llm,
        stt=stt_for_session,
        vad=ctx.proc.userdata["vad"],
        tts=tts_engine,
        turn_handling=turn_cfg.turn_handling(
            MultilingualModel(unlikely_threshold=turn_cfg.unlikely_threshold)
        ),
    )

    w = SessionWiring(
        session=session,
        stt_engine=stt_engine,
        tts_engine=tts_engine,
        live_from_main=live_from_main,
    )
    register_session_handlers(w)
    start_llm_warmup(llm)
    w.timeline, turn_metrics = setup_test_instrumentation(ctx, session)

    await session.start(agent=VoiceAgent(), room=ctx.room)
    runtime.session = session  # 供模块级聆听助手/欢迎语在 agent 循环里 say/收尾

    # tap 链装配(顺序即包裹层次,不可乱):场景注入 → 浏览器音频 → 静音门 → 录音 → KWS → 在线打断
    setup_scenario_injection(session, turn_metrics)
    setup_web_audio(session)
    setup_mute_gate(session)
    setup_recording(ctx, w)
    setup_live_transcript(w)
    setup_kws(ctx, w)
    setup_online_interrupt(ctx, w)

    # 开场白:固定文案(稳定、可复现、首字延迟低),口吻与小歌人设一致。say() 仍会经过
    # transcription_node(广播到网页气泡)与录音 tap。放在所有 tap(录音/KWS/在线打断)
    # 装好之后触发,确保被如实录进录音(放在 on_enter 会早于录音 tap 安装 -> 漏录)。
    session.say("你好呀，我是小歌。有什么想聊的、想问的，随时跟我说。")


if __name__ == "__main__":
    # Start the web UI server in a background thread
    start_web_server_thread(WEB_PORT)

    # Give the server a moment to bind; open browser only in local mode.
    time.sleep(0.8)
    if WEB_HOST in ("localhost", "127.0.0.1"):
        webbrowser.open(f"http://localhost:{WEB_PORT}")
        logger.info("Opening browser at http://localhost:%d", WEB_PORT)
    else:
        logger.info(
            "Web UI listening on http://0.0.0.0:%d - open from browser at http://<server-ip>:%d",
            WEB_PORT,
            WEB_PORT,
        )

    cli.run_app(server)
