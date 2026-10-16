"""待命/退出机制 host 助手 —— 全部在 agent 循环线程执行(与 listening_host 同一线程语义)。

退出机制(2026-09-15)三路触发,殊途同归进 standby 态:
- 语义退出:用户整句命中退出语(web_ui_agent._is_exit_phrase 规则表);
- 自动退出①:无输入持续 EXIT_IDLE_S(自最近一次用户输入/小歌播报结束起算;
  小歌在播报/放音乐、无音频客户端、duplex 关时顺延重臂不触发);
- 自动退出②:聆听模式持续 EXIT_LISTEN_S(先退聆听不问整理)。
进入待命 = broadcast standby on 帧告知端侧"无输入已满 30s"——纯通知态:
2026-09-16 起服务端不因此吞任何轮次(待命期音频/文字照常处理照常回复,
STT 气泡/历史/LLM/TTS 全照走,定时器照常臂);是否停发音频、是否唤醒,
都是客户端逻辑。ctrl.frontend_state 唤醒帧已退役(2026-10-13 服务端下线
处理):唤醒由真实用户输入驱动——V2 走 v2_wake_on_input,V1 回滚须还原
.bak_20261012_v2mode 文件集才恢复"发帧退待命+广播 off"的旧配对。
状态挂 runtime(单会话)。依赖方向:本模块顶层 import listening_host;
listening_host 在 enter/exit_aftermath 函数体内晚导入本模块(避免环)。
V2 三态(2026-10-12,XIAOGE_MODE_V2=1):standby ⟺ mode==sleeping 不变式,
enter/exit 成对同步 runtime.interaction_mode(状态机见 app/interaction_host.py);
本模块顶层 import interaction_host 无环(它对 standby_host 只在函数体内晚导入)。
"""

from __future__ import annotations

from common.config_utils import env_bool, env_float
from common.runtime import append_turn_log
from webpanel.bridge import audio_client_count, broadcast, session_client_count

from app import interaction_host, listening_host
from app.session_state import runtime

# 退出机制总开关(0 = 全关:语义退出不触发、自动定时器不臂,回到现网原行为)。
EXIT_ENABLE = env_bool("XIAOGE_EXIT_ENABLE", True)
# 自动退出①:无输入持续秒数(默认 30,对齐需求"大于30s")。
EXIT_IDLE_S = env_float("XIAOGE_EXIT_IDLE_S", 30.0)
# 自动退出②:聆听模式持续秒数(不看期间是否有说话)。
EXIT_LISTEN_S = env_float("XIAOGE_EXIT_LISTEN_S", 30.0)
# 守卫顺延重臂秒数:音频在途/无客户端/duplex 关 → 10s 后再看,不算"无输入"。
_DEFER_S = 10.0


def standby_active() -> bool:
    return runtime.standby


def standby_broadcast(on: bool, via: str) -> None:
    broadcast({"type": "standby", "on": bool(on), "reason": via})


def enter_standby(via: str) -> None:
    """进入待命(幂等):聆听在态连带退出(不问整理);撤两路定时器;广播 on 帧。零播报。"""
    if runtime.standby:
        return
    c = runtime.listen_ctrl
    if c is not None and c.active:
        c.force_exit()
        listening_host.listen_exit_aftermath("standby", ask=False)
    exit_cancel_idle_timer()
    exit_cancel_listen_timer()
    runtime.agent_output_in_flight = False
    # V2(2026-10-12):同步三态 mode → sleeping。先置 mode 再广播——桥接层按
    # mode 派生 ctrl.state,保证任何衍生帧都带目标态(不能先广播 listening off
    # 再改 mode,否则衍生帧短暂带旧态);从 listening 进来补一条 listening off
    # (面板聆听高亮收敛)。不变式 standby⟺sleeping 由此维持。
    if interaction_host.mode_v2_enabled():
        was_listening = runtime.interaction_mode == interaction_host.MODE_LISTENING
        runtime.interaction_mode = interaction_host.MODE_SLEEPING
        if was_listening:
            broadcast({"type": "listening", "on": False})
            append_turn_log("V2_LISTEN_EXIT via standby")
    runtime.standby = True
    standby_broadcast(True, via)
    append_turn_log(f"STANDBY_ENTER via {via}")


def exit_standby(via: str) -> None:
    """退出待命(幂等):广播 off 帧 + 重臂 idle(唤醒即正常,再 30s 无输入才会再待命)。"""
    if not runtime.standby:
        return
    # V2:mode → dialogue(先置再广播,衍生 ctrl.state 带目标态;唤醒必回对话)。
    if interaction_host.mode_v2_enabled():
        runtime.interaction_mode = interaction_host.MODE_DIALOGUE
    runtime.standby = False
    standby_broadcast(False, via)
    append_turn_log(f"STANDBY_EXIT via {via}")
    exit_arm_idle_timer()


def exit_cancel_idle_timer() -> None:
    if runtime.exit_idle_handle is not None:
        runtime.exit_idle_handle.cancel()
        runtime.exit_idle_handle = None


def exit_arm_idle_timer(delay: float | None = None) -> None:
    """臂/重臂无输入定时器(EXIT_ENABLE 关时不臂,调用方无需自判)。"""
    exit_cancel_idle_timer()
    if not EXIT_ENABLE or runtime.agent_loop is None:
        return
    runtime.exit_idle_handle = runtime.agent_loop.call_later(
        EXIT_IDLE_S if delay is None else delay, _on_idle_expired
    )


def exit_reset_idle_timer() -> None:
    """锚点(用户输入/小歌播报结束)重臂:回到满额 EXIT_IDLE_S。"""
    exit_arm_idle_timer()


def mark_agent_output_started() -> None:
    """Pause idle accounting until the current assistant reply has finished."""
    runtime.agent_output_in_flight = True
    # The 30s window is user-idle time, not LLM/TTS wall-clock time.  Cancel
    # the pending deadline now; mark_agent_output_finished() starts a fresh
    # full window after playout/commit completes.
    exit_cancel_idle_timer()


def mark_agent_output_finished() -> None:
    """Resume a fresh idle window after the assistant item is fully committed."""
    runtime.agent_output_in_flight = False
    exit_reset_idle_timer()


def _audio_in_flight() -> bool:
    out = runtime.ws_audio_output
    return bool(out is not None and getattr(out, "_pushed_duration", 0) > 0)


def _agent_output_in_flight() -> bool:
    return bool(runtime.agent_output_in_flight or _audio_in_flight())


def _on_idle_expired() -> None:
    runtime.exit_idle_handle = None
    if not EXIT_ENABLE:
        return
    # 不拿 runtime.standby 当早退门(2026-09-16):待命=纯通知态,轮次照常处理、
    # 锚点照常重臂本定时器——若在此早退,定时器就死在待命里;待命中到期落到
    # enter_standby 幂等早退即可(不重播 on 帧)。
    # 顺延守卫:这些状态都不算"无输入",10s 后再看(期间锚点会重臂回满额)。
    if not runtime.duplex_enabled:  # ASR 测试模式:轮次被吞,无从拿用户输入锚点
        exit_arm_idle_timer(_DEFER_S)
        return
    if _audio_in_flight():  # 小歌播报/云端音乐在播
        exit_arm_idle_timer(_DEFER_S)
        return
    if _agent_output_in_flight():  # LLM/TTS/playout 尚未完成
        exit_arm_idle_timer(_DEFER_S)
        return
    if session_client_count() == 0 and not (
        interaction_host.mode_v2_enabled() and audio_client_count() > 0
    ):
        # 会话 WS 一个都没有 = 池化待命/会话已走,不是"纯文字面板":彻底停表,
        # 真会话 attach(ctrl.hello → handle_session_attach)再重臂。2026-09-15
        # 修复:此前这里与纯文字面板同走 10s 顺延,预热欢迎语锚定的定时器在
        # 池化期间每 10s 重臂成链,首个会话连上后守卫全过,下一次到期就幽灵
        # STANDBY_ENTER 打进新会话吞掉全部轮次(e2e 33/40 的 7 失败同根因)。
        # V2(2026-10-12)除外:设备侧只连 /ws/audio 不连 merged WS——session=0
        # 但 audio>0 是真设备会话,不停表,照常到期进休眠。
        return
    if audio_client_count() == 0:  # 纯文字面板:无音频客户端,不自动退
        exit_arm_idle_timer(_DEFER_S)
        return
    enter_standby("idle")


def exit_cancel_listen_timer() -> None:
    if runtime.exit_listen_handle is not None:
        runtime.exit_listen_handle.cancel()
        runtime.exit_listen_handle = None


def exit_arm_listen_timer() -> None:
    """聆听进入时臂:聆听着实"无输入"(临时转写不进轮次),持续 EXIT_LISTEN_S 即退。"""
    exit_cancel_listen_timer()
    if not EXIT_ENABLE or runtime.agent_loop is None:
        return
    runtime.exit_listen_handle = runtime.agent_loop.call_later(EXIT_LISTEN_S, _on_listen_expired)


def _on_listen_expired() -> None:
    runtime.exit_listen_handle = None
    if not EXIT_ENABLE:
        return
    # V2(2026-10-12):listening 持续 EXIT_LISTEN_S 无语音 → 休眠。V2 无
    # ListeningController(listen_ctrl=None),走不了下面 c.active 早退,先分流。
    if interaction_host.mode_v2_enabled():
        if runtime.interaction_mode == interaction_host.MODE_LISTENING:
            enter_standby("listen_timeout")
        return
    # 待命不早退(2026-09-16):聆听在态照常 force_exit + aftermath
    # (否则待命期起的聆听会卡死);enter_standby 对已待命幂等早退。
    c = runtime.listen_ctrl
    if c is None or not c.active:
        return
    c.force_exit()
    listening_host.listen_exit_aftermath("standby", ask=False)
    enter_standby("listen_timeout")


def standby_cancel_all() -> None:
    """会话起点重置:清 standby 态 + 撤两路定时器(池化 worker 复用不带脏状态)。

    脏 standby 清除时补播 off 帧:真会话 attach(merged WS ctrl.hello 的
    reset)与幽灵定时器到期之间存在竞窗,客户端可能已收到 on 帧却等不到
    唤醒帧——这里广播 off 让端侧状态必然收敛(收到没发过的 off 幂等无害)。
    """
    if runtime.standby:
        runtime.standby = False
        standby_broadcast(False, "session_start")
        append_turn_log("STANDBY_RESET session_start")
    exit_cancel_idle_timer()
    exit_cancel_listen_timer()
    runtime.agent_output_in_flight = False
