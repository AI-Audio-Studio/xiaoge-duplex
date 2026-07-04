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


@dataclass
class AppRuntime:
    # 循环/会话
    agent_loop: asyncio.AbstractEventLoop | None = None
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
    # 压话标志:用户当前这句话是否压着 AI 播报开口(附和拒识的上下文闸门)。
    # user_state_changed 在每次开口时直接覆盖(不粘滞、不靠提交复位)。
    overlap_turn_state: dict[str, bool] = field(
        default_factory=lambda: {"user_spoke_over_agent": False}
    )


runtime = AppRuntime()


def say_voice_welcome() -> None:
    """欢迎语(在 agent 循环线程执行;web 线程经 call_soon_threadsafe 调度)。"""
    session = runtime.session
    if session is None:
        logger.info("voice welcome skipped: session not ready")
        return
    try:
        logger.info("voice welcome say: %s", VOICE_WELCOME)
        session.say(VOICE_WELCOME, add_to_chat_ctx=False, allow_interruptions=False)
        append_turn_log("VOICE_WELCOME_SAY")
    except Exception:
        logger.exception("failed to say voice welcome")
