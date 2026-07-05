"""聆听模式 host 助手 —— 全部在 agent 循环线程执行(见 LISTENING_MODE_DESIGN §5.7)。

纯状态机(listening_mode.ListeningController)之外的宿主接线:TTL 定时器、保护窗、
退出尾巴一次性标记、UI 横幅广播、"要整理吗"播报。状态挂在 runtime(单会话)。
"""

from __future__ import annotations

import time

from common.runtime import append_turn_log
from webpanel.bridge import broadcast

from app.session_state import runtime

# 退出"要整理吗"播报的打断保护时长(秒)
LISTEN_GUARD_S = 6.0


def listen_broadcast(on: bool) -> None:
    msg: dict = {"type": "listening", "on": bool(on)}
    if on:
        wake = runtime.listen_ctrl.wake_keyword if runtime.listen_ctrl else "小歌干活了"
        msg["hint"] = f"聆听模式 · 说『{wake}』或点通话键退出"
    broadcast(msg)


def listen_interrupt_blocked() -> bool:
    """聆听期 OR 退出提示保护窗内:用户语音不得打断小歌(进入提示 / 要整理吗)。

    聆听期间用户说的话本就不进显示/上下文,也不应能打断小歌的受控播报;退出后短暂保护
    "要整理吗",免被刚说的退出指令/前一句的残留 STT/在线2pass 切掉。关闭或正常态恒为 False。
    """
    c = runtime.listen_ctrl
    if c is None or not c.enabled:
        return False
    return c.active or time.monotonic() < runtime.listen_guard_until


def listen_arm_guard() -> None:
    runtime.listen_guard_until = time.monotonic() + LISTEN_GUARD_S


def listen_clear_guard() -> None:
    runtime.listen_guard_until = 0.0


def listen_tail_pending() -> bool:
    """退出后等"尾巴 final":KWS(声学,~即时)已翻 active=False、撤横幅,但该句 STT final
    滞后 ~1.5s 才到。一次性标记(+安全时限),覆盖这条尾巴 final——它含唤醒词及之前的监听内容,
    要丢;唤醒词之后接着说的真话要留(见 split_after_command)。"""
    return runtime.listen_exit_pending and time.monotonic() < runtime.listen_drain_until


def listen_arm_tail() -> None:
    if runtime.listen_ctrl is not None:
        runtime.listen_exit_pending = True
        runtime.listen_drain_until = time.monotonic() + runtime.listen_ctrl.drain_s


def listen_consume_tail() -> None:
    runtime.listen_exit_pending = False


def listen_cancel_ttl() -> None:
    if runtime.listen_ttl_handle is not None:
        runtime.listen_ttl_handle.cancel()
        runtime.listen_ttl_handle = None


def _listen_on_temp_ttl_expired() -> None:
    runtime.listen_ttl_handle = None
    if runtime.listen_ctrl is not None:
        runtime.listen_ctrl.drop_temp()
        runtime.listen_ctrl.clear_awaiting()
        append_turn_log("LISTEN_TEMP_DROPPED ttl")


def listen_arm_ttl() -> None:
    listen_cancel_ttl()
    if runtime.agent_loop is None or runtime.listen_ctrl is None:
        return
    runtime.listen_ttl_handle = runtime.agent_loop.call_later(
        runtime.listen_ctrl.temp_ttl_s, _listen_on_temp_ttl_expired
    )


def listen_ask_organize() -> None:
    if runtime.listen_ctrl is None or runtime.session is None:
        return
    runtime.listen_ctrl.awaiting_organize_answer = True
    listen_arm_guard()  # 保护这句不被刚说完的退出指令/前一句残留打断
    runtime.session.say(
        "刚才听的我先存着了,要整理一下吗?", add_to_chat_ctx=False, allow_interruptions=False
    )
    append_turn_log("LISTEN_ORGANIZE_ASK")


def listen_enter_aftermath(via: str, *, notice: bool) -> None:
    """进入收尾(控制器已置 active):取消旧 TTL、可选语音提示、UI 横幅。"""
    if runtime.listen_ctrl is None:
        return
    listen_cancel_ttl()  # 再入:旧待整理的定时器关掉(ctrl._enter 已 drop_temp)
    if notice and runtime.listen_ctrl.enter_notice and runtime.session is not None:
        # 进入提示不可打断;聆听期(active)用户语音已被 listen_interrupt_blocked 挡住打断路径
        runtime.session.say(
            runtime.listen_ctrl.enter_notice, add_to_chat_ctx=False, allow_interruptions=False
        )
    listen_broadcast(True)
    append_turn_log(f"LISTEN_ENTER via {via}")


def listen_exit_aftermath(via: str, *, ask: bool) -> None:
    """退出收尾:启动 TTL、撤 UI;ask 且有实质内容则主动问。"""
    if runtime.listen_ctrl is None:
        return
    listen_arm_tail()  # 退出尾巴标记:切分那条滞后 final(丢唤醒词及之前,留之后),免泄漏
    listen_arm_ttl()  # 定时删除(TTL)独立保留,不随整理开关
    listen_broadcast(False)
    append_turn_log(f"LISTEN_EXIT via {via}")
    if ask and runtime.listen_ctrl.organize_enabled and runtime.listen_ctrl.temp_has_substance():
        listen_ask_organize()


def listen_on_mic_toggle(now_muted: bool) -> None:
    """通话键(marshal 回 agent 循环):聆听期=退出(顺带已静音,不问);解除静音回正常+有待整理=补问。"""
    c = runtime.listen_ctrl
    if c is None or not c.enabled:
        return
    if c.active:
        c.force_exit()
        listen_exit_aftermath("mic", ask=False)  # 用户挂起中,不在此问
    elif (
        (not now_muted)
        and c.organize_enabled
        and c.temp_transcript
        and c.temp_has_substance()
        and not c.awaiting_organize_answer
    ):
        listen_ask_organize()
