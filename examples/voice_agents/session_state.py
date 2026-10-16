"""AppRuntime:agent 侧跨模块共享状态(单会话)。

原 web_ui_agent.py 的 20+ 模块级可变全局收敛为一个显式对象。线程语义与原全局
一致:web 线程只做"读简单属性 / 经 call_soon_threadsafe 派发",变更全部串行在
agent 循环线程(GIL 保证简单赋值原子)。将来要多会话,把本单例改为 per-session
实例即可(状态已聚拢)。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from common.runtime import append_turn_log

if TYPE_CHECKING:
    import asyncio

logger = logging.getLogger("web-ui-agent")

VOICE_WELCOME = "连接成功，欢迎使用小歌，请开始说话。"
COMMAND_EXECUTING = "好的，正在执行"
COMMAND_SUCCESS = "执行成功"
COMMAND_FAILURE = "执行失败，请稍后再试！"


@dataclass
class AppRuntime:
    # 循环/会话
    agent_loop: asyncio.AbstractEventLoop | None = None
    job_ctx: Any = None  # JobContext;供网关标记会话断开时 ctx.shutdown() 优雅退出
    session: Any = None  # AgentSession;供聆听助手/欢迎语在 agent 循环里 say
    # 面板可切换后端(upstream 装配才有;流式主STT 时为 None=面板 ASR 切换不可用)
    switchable_stt: Any = None
    switchable_tts: Any = None
    tts_backend_key: str = "cosyvoice"
    # 关麦门 / 测试录音(供 /api/mic)
    mute_gate: Any = None
    test_recorder: Any = None
    # 聆听模式(控制器 + host 侧一次性状态,见 listening_host)
    listen_ctrl: Any = None
    listen_ttl_handle: Any = None
    listen_guard_until: float = 0.0
    listen_drain_until: float = 0.0
    listen_exit_pending: bool = False
    # 浏览器音频 I/O 引用(WEB_AUDIO=1)
    ws_audio_input: Any = None
    ws_audio_output: Any = None
    # 本地音乐播放器(XIAOGE_MUSIC_ENABLED=1 时由 setup_music 实例化)。
    # 2026-09-15 本地(设备侧)音乐通道下线: 不再下发 control_music 帧, 音乐统一
    # 走云端曲库; 来源引导三字段(music_source/_pending/_playing_source)随之删除。
    music_player: Any = None
    # 知识库索引(KnowledgeIndex;入口处 best-effort _load,无索引文件时为 None/未就绪)
    knowledge_index: Any = None
    manual_text_handler: Any = None
    # 欢迎语异步入口(VoiceAgent.handle_voice_welcome): 说欢迎语前先热应用面板
    # 配置,修池化 worker 复连后欢迎语仍用旧音色。webpanel 经
    # run_coroutine_threadsafe 派发;None=未注册(旧代码/单测),回落同步直说。
    voice_welcome_handler: Any = None
    # 真实会话 attach 异步入口(VoiceAgent.handle_session_attach): merged WS
    # ctrl.hello 时经 run_coroutine_threadsafe 派发,per-session 重置(热应用
    # 配置 + 清 standby/遗留定时器 + 重臂 idle)。None=未注册(旧代码/单测),
    # 回落只撤定时器。2026-09-15 幽灵待命修复:entrypoint 只在进程启动跑一次。
    session_attach_handler: Any = None
    # API key bound by the gateway for the current merged session. It is never
    # taken from client frames and is cleared/replaced on every ctrl.hello.
    project_api_key: str | None = None
    # G3 协议会话状态:跨轮高风险确认 / 端侧能力。当前仍是单会话 runtime。
    g3_pending_high_risk: dict[str, Any] | None = None
    g3_caps: frozenset[str] = field(
        default_factory=lambda: frozenset({"audio", "text", "cmd", "state"})
    )
    # 压话标志:用户当前这句话是否压着 AI 播报开口(附和拒识的上下文闸门)。
    # user_state_changed 在每次开口时直接覆盖(不粘滞、不靠提交复位)。
    overlap_turn_state: dict[str, bool] = field(
        default_factory=lambda: {"user_spoke_over_agent": False}
    )
    # 待命/退出机制(2026-09-15,状态机见 app/standby_host.py):
    # standby = 待命态(语义退出/无输入30s/聆听超30s 进入)——2026-09-16 起纯通知态:
    # 只广播 standby on 帧给端侧,期间轮次照常处理照常回复,唤醒是客户端逻辑
    # (端侧发 ctrl.frontend_state awake,服务端广播 off 帧);
    # exit_idle_handle / exit_listen_handle = 无输入/聆听持续两路 call_later 定时器句柄;
    # duplex_enabled = 全双工开关镜像(_maybe_apply_agent_config 轮次起点同步;idle 守卫读)。
    # session_persona = 会话性格 key(2026-09-22,仅 -97):面板 ctrl.persona 帧设置,
    # 新会话 attach 落回默认档(config_store.DEFAULT_PERSONA_KEY,当前 witty);
    # None/空 = 隐藏回落路径(全局人设,面板三档化后不再主动发送)。
    # session_persona_explicit = 本会话是否显式选过档(2026-09-22 fullprompt 回归
    # 修复):attach 落默认档时 False——只带 traits/音色,不 mask 全局自定义完整
    # 提示词;仅显式 ctrl.persona 选择置 True,instructions_override 才让位。
    # _maybe_apply_agent_config 按它算生效配置(预设 traits/音色覆盖全局)。存 key
    # 而非快照:预设内容 POST 热更后下一轮即生效。
    standby: bool = False
    exit_idle_handle: Any = None
    exit_listen_handle: Any = None
    duplex_enabled: bool = True
    session_persona: str | None = None
    session_persona_explicit: bool = False


runtime = AppRuntime()


def say_voice_welcome(voice: str | None = None) -> None:
    """欢迎语(在 agent 循环线程执行;web 线程经 call_soon_threadsafe 调度)。

    voice: 当前生效音色(调用方在热应用配置后传入),仅写进埋点便于 e2e 断言,
    不影响播报内容;缺省时埋点保持旧格式(VOICE_WELCOME_SAY 不带后缀)。
    """
    session = runtime.session
    if session is None:
        logger.info("voice welcome skipped: session not ready")
        return
    try:
        logger.info("voice welcome say: %s (voice=%s)", VOICE_WELCOME, voice)
        session.say(VOICE_WELCOME, add_to_chat_ctx=False, allow_interruptions=False)
        append_turn_log(
            "VOICE_WELCOME_SAY" if voice is None else f"VOICE_WELCOME_SAY voice={voice}"
        )
    except Exception:
        logger.exception("failed to say voice welcome")


def say_command_status(text: str) -> None:
    """在 agent 循环线程播报命令状态，不写入聊天上下文。"""
    if text not in {COMMAND_EXECUTING, COMMAND_SUCCESS, COMMAND_FAILURE}:
        logger.error("command status skipped: invalid text=%r", text)
        return
    session = runtime.session
    if session is None:
        logger.info("command status skipped: session not ready text=%s", text)
        return
    try:
        session.say(text, add_to_chat_ctx=False, allow_interruptions=False)
        append_turn_log(f"COMMAND_STATUS_SAY text={text!r}")
    except Exception:
        logger.exception("failed to say command status: %s", text)


def say_command_result(text: str) -> None:
    """在 agent 循环线程播报查询类命令的数值结果(如电量)，不写入聊天上下文。

    与 say_command_status 的三句白名单不同: 这里是开放文本(由 webpanel_server
    从 data.cmd_result 载荷格式化而来), 只做非空/长度护栏。
    """
    text = str(text).strip()
    if not text or len(text) > 120:
        logger.error("command result skipped: invalid text=%r", text)
        return
    session = runtime.session
    if session is None:
        logger.info("command result skipped: session not ready text=%s", text)
        return
    try:
        session.say(text, add_to_chat_ctx=False, allow_interruptions=False)
        append_turn_log(f"COMMAND_RESULT_SAY text={text!r}")
    except Exception:
        logger.exception("failed to say command result: %s", text)
