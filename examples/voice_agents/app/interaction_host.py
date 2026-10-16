"""V2 三态交互模式 host(2026-10-12,设备侧三态需求)—— XIAOGE_MODE_V2=1 才启用。

三态模型(runtime.interaction_mode,ctrl.state 下发同一枚举):
- sleeping 休眠:默认态。连接/attach 后云端即返回"休眠",不发欢迎语;唤醒 =
  真实用户输入到达——语音轮/KWS/手发文字(v2_wake_on_input),以及端侧
  authoritative 唤醒帧(按钮/本地KWS/GUI,webpanel.server 窄过滤后 marshal
  进来;2026-10-13 按键死锁修复:此前一日整帧退役误伤按钮,现只滤掉
  seq:1 镜像/周期镜像/VAD/DOA 噪声帧,authoritative 真实唤醒恢复受理)。
- dialogue 对话:正常语音/文字轮次,LLM 照常回复。
- listening 聆听:LLM 判定用户这句不是在对小歌说(not_addressed 工具)→ 仅
  ASR 转写不回复;每轮照常过 LLM(不打断过滤),LLM 按语义自行唤醒——
  产出文本 = exit_listening_v2("semantic_wake")回对话。

与 V1 的关系:runtime.standby 仍是 V1 权威;V2 下保持不变式
standby ⟺ mode == sleeping(enter_standby / v2_session_start /
v2_entrypoint_init 成对设置)。V2 不创建旧 ListeningController(setup_kws
置 runtime.listen_ctrl = None——其吞轮语义与"每轮都过 LLM"冲突),V1 聆听
钩子(_handle_listening_turn 等)因 listen_ctrl is None 天然失效。

线程语义:状态变更函数全部在 agent 循环线程执行(与 standby_host /
listening_host 一致);web 线程只读 mode_v2_enabled()。依赖方向:顶层 import
bridge/session_state;standby_host 顶层反向 import 本模块,故本模块对
standby_host 的依赖一律函数体内晚导入(与 listening_host 同款)。
"""

from __future__ import annotations

from common.config_utils import env_bool
from common.runtime import append_turn_log
from webpanel.bridge import broadcast

from app.session_state import runtime

# 三态枚举(ctrl.state.interaction_mode 取值)
MODE_SLEEPING = "sleeping"
MODE_DIALOGUE = "dialogue"
MODE_LISTENING = "listening"

# V2 总开关(默认关:现网行为不变)
_MODE_V2 = env_bool("XIAOGE_MODE_V2", False)

# 聆听态注入给 LLM 的轮次系统指令(on_user_turn_completed 里 add_message):
# 告知当前是聆听态,用户话可能是对别人说的环境音;明确在对小歌说话才正常
# 回答(即语义唤醒),否则调 not_addressed 保持安静。判定果断:没叫名字、
# 没向小歌提问/下指令、内容跟刚才聊的无关 → 就是在跟别人说;裸短指令(没叫
# 名字但要小歌执行动作,如"讲个故事")仍算对小歌说——2026-10-13 裸指令补丁
# 当日再收窄:只限动作指令,一般提问/陈述不算,否则旁聊泛回复回归;拿不准时
# 仍倾向正常回答(聆听态误沉默比误唤醒更伤,误唤醒下一轮还能回聆听)。
V2_LISTENING_INSTRUCTION = (
    "[聆听模式] 用户当前这句话可能不是在对小歌说的(可能在跟身边的人聊天、"
    "打电话、自言自语或跟其他设备说话)。判断标准:他叫你的名字、向你提问、"
    "让你做事、接着你们刚才的话题继续说——是在对小歌说,就正常回答(视为"
    "用户已唤醒你);没叫你的名字、但明确是要你执行动作的短指令(比如直接说"
    "“讲个故事”“放首歌”“拍张照”),也是在对你说,不因前面几轮他在跟别人"
    "聊天就判成跟别人说的;除此之外没叫你名字的话——跟身边的人商量事情、"
    "汇报工作、陈述、数数、只言片语——多数是在跟别人说,调用 not_addressed "
    "工具并保持安静,什么都不要说。拿不准时倾向正常回答,不要一直沉默。"
)


def mode_v2_enabled() -> bool:
    return _MODE_V2


def sleeping_v2_active() -> bool:
    return _MODE_V2 and runtime.interaction_mode == MODE_SLEEPING


def listening_v2_active() -> bool:
    return _MODE_V2 and runtime.interaction_mode == MODE_LISTENING


def enter_listening_v2(via: str) -> None:
    """dialogue → listening(幂等):LLM 判定这句不是在对小歌说(not_addressed)。

    先置 mode 再广播(桥接层按 mode 派生 ctrl.state);撤 idle、臂聆听持续
    定时器(每轮聆听重臂,EXIT_LISTEN_S 无语音 → 休眠)。
    """
    if not _MODE_V2 or runtime.interaction_mode == MODE_LISTENING:
        return
    runtime.interaction_mode = MODE_LISTENING
    broadcast({"type": "listening", "on": True})
    append_turn_log(f"V2_LISTEN_ENTER via {via}")
    from app import standby_host  # 晚导入:standby_host 顶层反向依赖本模块

    standby_host.exit_cancel_idle_timer()
    standby_host.exit_arm_listen_timer()


def exit_listening_v2(via: str) -> None:
    """listening → dialogue(幂等):LLM 产出文本(语义唤醒)或用户主动输入。

    撤聆听持续定时器、臂 idle(回对话,EXIT_IDLE_S 无输入 → 休眠)。
    """
    if not _MODE_V2 or runtime.interaction_mode != MODE_LISTENING:
        return
    runtime.interaction_mode = MODE_DIALOGUE
    broadcast({"type": "listening", "on": False})
    append_turn_log(f"V2_LISTEN_EXIT via {via}")
    from app import standby_host

    standby_host.exit_cancel_listen_timer()
    standby_host.exit_arm_idle_timer()


def v2_wake_on_input(where: str) -> None:
    """休眠唤醒:任意真实用户输入(语音轮/KWS/手发文字)到达而仍是休眠态 → 对话。

    唤醒入口唯一:端侧 authoritative 唤醒帧(按钮/本地KWS/GUI)也经
    webpanel.server._schedule_frontend_wake marshal 到这里(where=
    frontend_<event>);镜像/噪声帧在入口前已被滤掉,不是休眠态则幂等返回
    (兼兜底首轮语音在中途到达,不让轮次卡在休眠态)。
    """
    if not _MODE_V2 or runtime.interaction_mode != MODE_SLEEPING:
        return
    append_turn_log(f"V2_WAKE_ON_INPUT via {where}")
    from app import standby_host

    standby_host.exit_standby(f"input_{where}")


def v2_user_speech_activity() -> None:
    """用户语音活动锚点:VAD speaking 起始与 STT interim/final 到达都调,
    按当前模式重臂"无语音输入"定时器——listening 臂聆听持续表
    (EXIT_LISTEN_S),dialogue 重臂 idle 表(EXIT_IDLE_S)。

    2026-10-12 现网问题#2:旧锚点只在开口瞬间臂一次 idle,超长单轮连续说话
    (实测 30.3s)期间无任何重臂,30s 表中途到期把还在说话的用户误判待命;
    现在 interim 持续到达即语音活动,超时从最后一次活动重新起算。
    sleeping 无定时器、V1 无此语义,均 no-op(V1 开口重臂 idle 仍在
    setup_taps 原路径)。
    """
    if not _MODE_V2:
        return
    from app import standby_host  # 晚导入:standby_host 顶层反向依赖本模块

    if runtime.interaction_mode == MODE_LISTENING:
        standby_host.exit_arm_listen_timer()
    elif runtime.interaction_mode == MODE_DIALOGUE:
        standby_host.exit_reset_idle_timer()


def v2_entrypoint_init() -> None:
    """进程启动(entrypoint,含池化预热期):直接休眠态,不臂任何定时器。

    池化无会话期间 idle 守卫(session_client_count==0)本来会停表;真会话
    attach 由 v2_session_start 接管。V1 路径的 standby_cancel_all+臂 idle
    在 V2 下整体让位。
    """
    if not _MODE_V2:
        return
    runtime.standby = True
    runtime.interaction_mode = MODE_SLEEPING
    runtime.agent_output_in_flight = False
    append_turn_log("V2_ENTRYPOINT_INIT mode=sleeping")


def v2_session_start() -> None:
    """真会话 attach(ctrl.hello → handle_session_attach):重置为休眠态。

    不广播——hello 的 ctrl.state 应答帧已带 sleeping(webpanel.server 硬编码,
    避免"应答帧先于本 marshal 到达 agent 循环"读到上一会话遗留 mode 的竞窗)。
    """
    if not _MODE_V2:
        return
    runtime.standby = True
    runtime.interaction_mode = MODE_SLEEPING
    runtime.agent_output_in_flight = False
    from app import standby_host

    standby_host.exit_cancel_idle_timer()
    standby_host.exit_cancel_listen_timer()
    append_turn_log("V2_SESSION_START mode=sleeping")
