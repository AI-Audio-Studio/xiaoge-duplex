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
import random
import re
import time
import webbrowser
from dataclasses import replace
from typing import Literal

import aiohttp
from app import interaction_host
from app.agent_tools import query_knowledge_text
from app.backends import build_llm, build_semantic_llm, build_tts, make_tts_backend
from app.config_store import (
    DEFAULT_PERSONA_KEY,
    DEFAULT_PERSONAS,
    FIXED_AGENT_INSTRUCTIONS,
    PERSONA_KEYS,
    VOICE_KEYS,
    AgentConfig,
    effective_instructions,
)
from app.knowledge_index import KnowledgeIndex
from app.listening_host import (
    listen_cancel_ttl,
    listen_clear_guard,
    listen_consume_tail,
    listen_enter_aftermath,
    listen_interrupt_blocked,
    listen_tail_pending,
)
from app.online_interrupt_host import setup_online_interrupt
from app.output_links import say_linked
from app.output_sanitizer import sanitize_llm_stream
from app.project_config_store import load_project_config
from app.reply_policy import generation_instructions, generation_tools
from app.semantic_gate import MUSIC_TOOLS, VERSION, SemanticGate, bound_speech, diagnostic
from app.session_state import COMMAND_FAILURE, runtime, say_voice_welcome
from app.setup_taps import (
    SessionWiring,
    register_session_handlers,
    setup_kws,
    setup_live_transcript,
    setup_music,
    setup_mute_gate,
    setup_recording,
    setup_scenario_injection,
    setup_stt,
    setup_test_instrumentation,
    setup_web_audio,
    start_llm_warmup,
)
from app.standby_host import (
    EXIT_ENABLE,
    enter_standby,
    exit_arm_idle_timer,
    exit_arm_listen_timer,
    exit_reset_idle_timer,
    mark_agent_output_started,
    standby_cancel_all,
)
from common.config_utils import env_bool
from common.runtime import (
    append_turn_log as _append_turn_log,
    configure_utf8_stdio as _configure_utf8_stdio,
)
from common.text_rules import (
    LEADING_PUNCT_RE,
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
    RunContext,
    StopResponse,
    cli,
)
from livekit.agents.llm import ChatMessage, function_tool
from livekit.plugins import silero
from livekit.plugins.turn_detector.multilingual import MultilingualModel

logger = logging.getLogger("web-ui-agent")

_CONTINUATION_PROMPT_RE = re.compile(
    r"(好不好|要不要|想不想|有没有兴趣|继续听|继续讲|接着讲|往下讲)"
)
_CONTINUATION_TOPIC_RE = re.compile(r"(故事|讲|开头|段子|笑话|介绍|说下|说说)")
_CONFIRM_CONTINUE_RE = re.compile(
    r"^(好|好的|好啊|好呀|可以|行|行啊|嗯|嗯嗯|对|对的|讲|讲啊|讲吧|"
    r"继续|继续吧|继续讲|接着讲|接着吧|往下讲|说吧|开始吧|来吧|可以讲)$"
)
_USER_TEXT_STRIP_RE = re.compile(r"[\s，,。.、！!？?～~；;：:…—·、\-]+")
_MUSIC_TARGET_RE = re.compile(r"(歌|音乐|播放|放歌|放一下|声音|这首|曲子|歌曲|唱)")
_MUSIC_STOP_RE = re.compile(
    r"(停止|停掉|停下|暂停|别放|不要放|不放|关掉|关闭|结束|取消|打住|别播|不要播|不播|别唱|不要唱|不唱|切掉)"
)
_MUSIC_BARE_STOP_RE = re.compile(r"^(停|停吧|停下|停下吧)$")
# 裸停止口语(2026-09-14): "别放了"/"不要放了"/"暂停"/"停一下"这类话没有音乐词, 上面的
# 停止判定够不着 → 落 LLM 或被在播静默丢弃。只在外层确认过音乐上下文(云端在播;
# 2026-09-15 起本地通道下线, 只剩云端一路)时才按停止解析(music_ctx 形参);
# 无上下文保持聊天语义不动。
# 放/播/唱/听 形式必须带 别/不要/不想 前缀("唱吧"/"放吧"是播放不是停止); 暂停/停 可裸。
# 2026-09-17 自 llm-bak playgate 移植: 主语前缀(我/我们/咱们)——"我不想听了"这类
# 带主语的口语此前 ^ 锚定够不着, 落到在播门被静默吞掉。
_MUSIC_BARE_STOP_CTX_RE = re.compile(
    r"^(?:我|我们|咱们)?(?:先|快)?"
    r"(?:(?:别|不要|不想)(?:放|播|唱|听)|(?:暂停|停))"
    r"(?:一下|一会儿|会儿)?(?:了|吧|呗|呀|啊|嘛|咯)?$"
)
_MUSIC_NON_TARGET_RE = re.compile(r"(故事|录音|视频|电影|新闻|广播|播客|有声书|声音|语音|动画)")
# 在播静默门放行词(2026-09-16 方案B): 前置音乐正则层(play/resume/switch)已下线,
# 全走 LLM 工具; 但"云端在播时非音乐话静默丢弃"的在播门保留(歌声音量大, 这期间
# 用户的话多半是跟唱/闲聊, 放行进 LLM 会在放歌中插嘴)。只有命中这些词的轮次才
# 放行: 音乐控制动词 + 设备控制名词。歌/唱/声音/音乐/继续/接着 这些泛词刻意
# 不进表——歌词撞词误放行的代价(放歌中多嘴)高于误吞。
_MUSIC_PASS_IN_PLAY_RE = re.compile(
    r"下一[首曲]|上一[首曲]|换一?[首曲个]|再来一?[首曲]|切[一一下]?歌"
    r"|播放|放[一几]?[首个]歌|放歌|来[一几]?[首曲]|点[一]?[首曲]"
    r"|音量|静音|拍照|拍[一张个]*照|电量|电池|电压|充电"
    r"|小点声|小声点|大点声|大声点|声音(?:太|有点)(?:大|小)"
    r"|(?:不想|不要|别).{0,2}听了?|听[够腻烦]|算了|关掉|关了吧|安静"
    r"|^(?:请|麻烦|帮我|给我)?(?:把|将)?(?:调|弄|搞|整|开|放)(?:大|高|响|小|低|轻)"
    r"(?:一点|一点儿|点|点儿|些|一些)?(?:吧|呀|啊|嘛|呢|了)?$"
    r"|^(?:再)?(?:大|高|响|小|低|轻)(?:一点|一点儿|点|点儿|些|一些)(?:吧|呀|啊|嘛|呢|了)?$"
)
# 2026-09-17 在播门补洞(自 llm-bak playgate 四门移植, 方案B适配): 停止否定类
# ("我不想再听这歌了"/"听腻了"/"算了")此前不在放行表 → 被静默吞掉, 用户喊停无回应。
# 问句门 + 音乐词门照 llm-bak 移植; 设备词门沿用本表(llm-bak 的 g3 matches_command
# 探针在这棵树上没有对应物)。四门任一命中即放行落 LLM(正则 miss 的停止话术由
# LLM 调 stop_music 兜底), 全 miss 才静默吞轮。
_MUSIC_PASS_QUESTION_RE = re.compile(
    r"[?？]|吗|呢|什么|为什么|怎么|怎样|怎么样|多少|几|谁|哪"
    r"|好不好|行不行|是不是|能不能|会不会|要不要|有没有"
)
_MUSIC_PASS_MUSIC_RE = re.compile(
    r"换一?[首曲]|换个(?:别的|[歌曲])|换成|再来一?[首曲]|切[一一下]*歌|下一[首曲]|上一[首曲]"
    r"|换歌|不想听|不想唱|别放|别播|别唱|难听|不好听"
    r"|太吵|好吵|真吵|吵死|来[一]?[点首曲个]?别的"
)


def _music_playing_pass_gate(agent, compact: str) -> bool:
    """在播静默门放行判定(四门): 问句 → 点名(小歌/人设名) → 音乐词 → 设备词。
    模块级函数(非 self 方法): SimpleNamespace 测试桩无需补绑定。"""
    if _MUSIC_PASS_QUESTION_RE.search(compact):
        return True
    cfg = getattr(agent, "_agent_cfg", None)
    persona = (getattr(cfg, "persona_name", "") or "").strip() if cfg is not None else ""
    if "小歌" in compact or (persona and persona in compact):
        return True
    if _MUSIC_PASS_MUSIC_RE.search(compact):
        return True
    return bool(_MUSIC_PASS_IN_PLAY_RE.search(compact))


# 退出语规则表(2026-09-15,退出机制):整句命中即语义退出(静默进待命)。
# 与音乐/停止词过滤同一归一化(去空白/标点)后**整句**匹配——整句锚定防
# "播放拜拜这首歌"/"再见女朋友"这类歌名/正文误伤;问尾 吗/呢 不在语气词表,
# "退下是什么意思"/"小歌退下了吗"不中。未命中落 LLM 聊天,不退出。
_EXIT_WHOLE_WORDS = frozenset({"拜拜", "再见", "回见", "拜"})
_EXIT_PHRASE_RE = re.compile(
    r"^(?:你|你先|先|那|就|去)?"
    r"(?:退下|下去|回去|走|休息|歇着|待着|下班|退了)"
    r"(?:吧|啦|哟|哈|了|啊|哦)?$"
)
_EXIT_SENTENCE_RE = re.compile(
    r"^(?:没你的事了?|没你什么事了?|不用陪我了?|不用陪了|先这样吧|"
    r"就到这里吧|今天先到这|去忙你的吧|去忙吧)$"
)
# env 追加退出语(|分隔,如 "回去歇着|撤了"),同样归一化后整句比对。
_EXIT_PHRASES_ENV = frozenset(
    _USER_TEXT_STRIP_RE.sub("", p)
    for p in (os.getenv("XIAOGE_EXIT_PHRASES") or "").split("|")
    if p.strip()
)


def _is_exit_phrase(text: str) -> bool:
    compact = _USER_TEXT_STRIP_RE.sub("", text or "")
    if not compact:
        return False
    if compact in _EXIT_WHOLE_WORDS or compact in _EXIT_PHRASES_ENV:
        return True
    return bool(_EXIT_PHRASE_RE.fullmatch(compact) or _EXIT_SENTENCE_RE.fullmatch(compact))


def _music_control_intent(original: str, *, music_ctx: bool = False) -> str | None:
    """音乐停止意图(2026-09-16 方案B): 前置音乐正则层只保留"停止"一路——
    在播时说停止词要秒级生效且零 LLM 延迟, 不适合等 LLM 工具往返; play/resume/
    switch 全部下放给 LLM 工具(语义判别交给模型)。返回 "stop" 或 None。"""
    compact = _USER_TEXT_STRIP_RE.sub("", original or "")
    if not compact:
        return None
    mentions_music = bool(_MUSIC_TARGET_RE.search(compact))
    # 裸"停/停下"只在音乐实际在播时按音乐停止; 无音乐时让 stop phrase 分支静默打断
    # TTS, 避免用户打断故事却听到"音乐现在没在播"。
    if music_ctx and _MUSIC_BARE_STOP_RE.fullmatch(compact):
        return "stop"
    if music_ctx and _MUSIC_BARE_STOP_CTX_RE.fullmatch(compact):
        return "stop"
    # 停止门(2026-09-14): "播"字门此前会被非对象词里的"播"点亮——"别放广播"/"暂停
    # 播客"里的 广播/播客 不是音乐, 却因含"播"误过门 → 本地在播时错发 pause 帧。
    # 剔掉非对象词再判"播"; mentions_music/"播放" 不动, "别播了"这类裸播动词仍过门
    # (动词自身的"播"剔不掉)。
    if _MUSIC_STOP_RE.search(compact) and (
        mentions_music or "播放" in compact or "播" in _MUSIC_NON_TARGET_RE.sub("", compact)
    ):
        return "stop"
    return None


# 判停模型文件已离线缓存(local_files_only=True 读取),强制离线模式避免每次启动
# 去连 huggingface.co 触发 ~30s 超时重试导致的冷启动。要更新模型时临时设为 "0"。
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

# 注:全量 DEBUG 日志已整合进测试工具(AGENT_TIMELINE=1 时写 runs/<ts>/debug.log,
# 见 event_timeline.install_debug_log)。正常运行不再挂任何文件日志处理器(零开销)。

_configure_utf8_stdio()


def _defer_session_say(session, text: str, **kwargs) -> None:
    loop = asyncio.get_running_loop()
    loop.call_soon(lambda: session.say(text, **kwargs))


# ---- 命令记忆标记(2026-09-03): StopResponse 路径命令不进聊天历史, 静默补一行 ----
_CMD_MARKER_PREFIX = "[系统] 已执行: "

_MOVE_DIR_CN = {
    "forward": "前进",
    "backward": "后退",
    "left": "向左移动",
    "right": "向右移动",
}
_TURN_DIR_CN = {"back": "向后转", "right": "右转", "left": "左转"}

# 3 条配置化命令(adjust_volume/take_photo/check_battery)的
# 参数值 → 中文动词短语; 供命令记忆与口语化确认共用。
# control_music 已于 2026-09-15 下线(本地音乐通道不再下发)。
# 2026-09-20 volset(自 llm-bak 移植): max/min 两档并入 set(100/0),
# set 的档位值在分支里插值(不进静态表)。
_DESCRIBE_VOLUME_CN = {
    "up": "调大音量",
    "down": "调小音量",
    "mute": "静音",
}
_BATTERY_NOUN_CN = {
    "cap": "电量",
    "vol": "电池电压",
    "cur": "电池电流",
}


def _describe_executed_frames(frames: list[dict]) -> str:
    """把已下发的 data.cmd 帧压成一句中文描述, 作为命令记忆标记的正文。"""
    parts: list[str] = []
    for frame in frames:
        if frame.get("type") != "data.cmd":
            continue
        action = str(frame.get("action") or "")
        params = frame.get("params") or {}
        if action == "navigation.move":
            move = _MOVE_DIR_CN.get(str(params.get("direction") or ""), "移动")
            distance = params.get("distance_cm")
            parts.append(f"{move}({distance}厘米)" if distance is not None else move)
        elif action == "motion.turn":
            turn = _TURN_DIR_CN.get(str(params.get("direction") or ""), "转身")
            angle = params.get("angle_deg")
            parts.append(f"{turn}({angle}度)" if angle is not None else turn)
        elif action == "adjust_volume":
            command = str(params.get("command") or "")
            if command == "set":
                parts.append(f"音量调到{params.get('value', 50)}")
            else:
                parts.append(_DESCRIBE_VOLUME_CN.get(command, "调节音量"))
        elif action == "take_photo":
            parts.append("拍了张照")
        elif action == "check_battery":
            noun = _BATTERY_NOUN_CN.get(str(params.get("type") or ""), "电量")
            gauge = params.get("gauge_id")
            parts.append(f"查{noun}({gauge}号)" if gauge not in (None, 1) else f"查{noun}")
        else:
            detail = ", ".join(f"{k}={v}" for k, v in params.items())
            parts.append(f"{action}({detail})" if detail else action)
    return "+".join(parts) if parts else "控制指令"


# ---- 命令口语化确认(2026-09-04): 替代生硬的"好的，正在执行" ----
_CMD_ACK_MOVE_CN = {
    "forward": "往前走",
    "backward": "往后退",
    "left": "往左挪",
    "right": "往右挪",
}
_CMD_ACK_TURN_CN = {
    "back": "向后转",
    "right": "向右转",
    "left": "向左转",
}
_CMD_ACK_GESTURE_CN = {
    "laugh": "笑一个",
    "cry": "哭一个",
}
_CMD_ACK_VOLUME_CN = {
    "up": "把音量调大",
    "down": "把音量调小",
    "mute": "把声音关掉",
}


def _natural_command_ack(frames: list[dict]) -> str:  # noqa: C901  # 2026-09-08 服务器同步存量挂账,见 CODE_GUIDELINES §5
    """把将要下发的 data.cmd 帧说成一句口语化确认, 动作直接进话里。

    单动作点名("好嘞，我这就往前走"), 多动作串先后("好嘞，我先往前走，然后向左转"),
    认不出的动作退回通用确认。前缀三选一随机, 连续命令不重样。
    """
    verbs: list[str] = []
    for frame in frames:
        if frame.get("type") != "data.cmd":
            continue
        params = frame.get("params") or {}
        action = str(frame.get("action") or "")
        if action == "navigation.move":
            verbs.append(_CMD_ACK_MOVE_CN.get(str(params.get("direction") or ""), ""))
        elif action == "motion.turn":
            verbs.append(_CMD_ACK_TURN_CN.get(str(params.get("direction") or ""), ""))
        elif action == "gesture.perform":
            verbs.append(_CMD_ACK_GESTURE_CN.get(str(params.get("gesture") or ""), ""))
        elif action == "adjust_volume":
            command = str(params.get("command") or "")
            if command == "set":
                verbs.append(f"把音量调到{params.get('value', 50)}")
            else:
                verbs.append(_CMD_ACK_VOLUME_CN.get(command, ""))
        elif action == "take_photo":
            verbs.append("拍张照")
        elif action == "check_battery":
            # 这句要进 TTS, 不用括号: "看看电量" / "看看2号电池电压"。
            noun = _BATTERY_NOUN_CN.get(str(params.get("type") or ""), "电量")
            gauge = params.get("gauge_id")
            verbs.append(f"看看{gauge}号{noun}" if gauge not in (None, 1) else f"看看{noun}")
    verbs = [v for v in verbs if v]
    if not verbs:
        return random.choice(("好嘞，这就来", "收到，马上执行", "好的，这就办"))
    prefix = random.choice(("好嘞，", "收到，", "好，"))
    if len(verbs) == 1:
        return random.choice(
            (
                f"{prefix}我这就{verbs[0]}",
                f"{prefix}我{verbs[0]}咯",
            )
        )
    return f"{prefix}我先" + "，然后".join(verbs)


# ── 设备命令帧(2026-09-16 方案B): 原 G3 确定性命令层下线, 3 条配置化命令 ──
# (adjust_volume/take_photo/check_battery)改为 LLM function tool。帧结构与原
# g3_intent.build_outputs 逐字段一致(commands.yaml 是唯一事实来源), 设备侧
# 零改动; 超时口径沿用 3s。
_CMD_ACK_TIMEOUT_MS = 3000
_CMD_RESULT_TIMEOUT_MS = 3000
# 音量档位(2026-09-20 volset, 自 llm-bak 移植): max/min 并入 set(100/0),
# set 帧恒带 value(0-100)。
_VOLUME_COMMANDS = ("up", "down", "mute", "set")
_BATTERY_TYPES = ("cap", "vol", "cur")


def _build_cmd_frame(action: str, capability_id: str, params: dict) -> dict:
    """组一条 data.cmd 帧(与原 g3_intent.build_outputs 同构, risk 全 low)。"""
    now_ms = int(time.time() * 1000)
    trace_id = f"trace-g3-{now_ms}"
    session_id = os.getenv("XIAOGE_SESSION_ID", "sess-g3-local")
    utterance_id = f"utt-g3-{now_ms}"
    return {
        "type": "data.cmd",
        "trace_id": trace_id,
        "session_id": session_id,
        "utterance_id": utterance_id,
        "cmd_id": f"cmd-g3-{abs(hash((trace_id, utterance_id, now_ms))) % 1_000_000:06d}",
        "capability_id": capability_id,
        "action": action,
        "params": params,
        "risk_level": "low",
        "ack_timeout_ms": _CMD_ACK_TIMEOUT_MS,
        "result_timeout_ms": _CMD_RESULT_TIMEOUT_MS,
        "issued_at_ms": now_ms,
    }


# ── 天气查询(云侧工具): Open-Meteo 免key; 默认城市 .env 可配(机器人所在地) ──
_WMO_CODE_TEXT = {
    0: "晴",
    1: "晴间少云",
    2: "多云",
    3: "阴",
    45: "雾",
    48: "冻雾",
    51: "毛毛雨",
    53: "毛毛雨",
    55: "毛毛雨",
    56: "冻毛毛雨",
    57: "冻毛毛雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    66: "冻雨",
    67: "冻雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    77: "雪粒",
    80: "阵雨",
    81: "强阵雨",
    82: "暴雨",
    85: "阵雪",
    86: "阵雪",
    95: "雷雨",
    96: "雷雨伴冰雹",
    99: "雷雨伴冰雹",
}
_WEATHER_HTTP_TIMEOUT_S = 5.0
_WEATHER_CACHE_TTL_S = 600.0  # 同一座城 10 分钟内直接用缓存, 不刷接口
_WEATHER_CACHE: dict[str, tuple[float, str]] = {}
_WEATHER_GEO_CACHE: dict[str, tuple[float, float, str]] = {}
_weather_http: aiohttp.ClientSession | None = None
_WEATHER_LLM_GUIDE = (
    " 用一两句口语告诉用户重点(现在几度什么天、明后天变化、要不要带伞添衣),"
    "不要把数字全念一遍,不要提数据来源或接口。"
)
# 天气引导词(2026-09-10): 链路=两次LLM+一次拉数, 答句前静默好几秒 — 工具一
# 被调先说这句垫住感知延迟。随机三选一, 与命令口语化确认同风格。
_WEATHER_ACK_CN = ("好的，我来查一下", "好嘞，我查一下哈", "稍等，我查一下")


def _wmo_text(code) -> str:
    """WMO weather code → 中文现象(表外代码兜底不炸)。"""
    try:
        code = int(code)
    except (TypeError, ValueError):
        return "未知天气"
    return _WMO_CODE_TEXT.get(code, f"天气代码{code}")


def _format_weather(data: dict, city_label: str) -> str:
    """Open-Meteo payload → 给 LLM 的中文事实卡(不是给用户念的原文)。"""
    cur = data.get("current") or {}
    daily = data.get("daily") or {}
    days = daily.get("time") or []
    parts = [f"{city_label}天气:"]
    bits = []
    if cur.get("temperature_2m") is not None:
        bits.append(f"现在{cur['temperature_2m']}°C")
    if cur.get("weather_code") is not None:
        bits.append(_wmo_text(cur["weather_code"]))
    if cur.get("relative_humidity_2m") is not None:
        bits.append(f"湿度{cur['relative_humidity_2m']}%")
    if cur.get("wind_speed_10m") is not None:
        bits.append(f"风速{cur['wind_speed_10m']}km/h")
    if bits:
        parts.append("，".join(bits))
    for i, day in enumerate(days):
        tag = ("今天", "明天", "后天")[i] if i < 3 else day
        lo = (daily.get("temperature_2m_min") or [None] * len(days))[i]
        hi = (daily.get("temperature_2m_max") or [None] * len(days))[i]
        code = (daily.get("weather_code") or [None] * len(days))[i]
        rain = (daily.get("precipitation_probability_max") or [None] * len(days))[i]
        seg = [tag]
        if code is not None:
            seg.append(_wmo_text(code))
        if lo is not None or hi is not None:
            seg.append(f"{lo}~{hi}°C")
        if rain:
            seg.append(f"降雨概率{rain}%")
        parts.append("，".join(seg))
    if len(parts) > 1:
        return parts[0] + " " + "；".join(parts[1:]) + "。" + _WEATHER_LLM_GUIDE
    return parts[0] + "。"


async def _weather_fetch_json(url: str, params: dict) -> dict | None:
    """共享一个 aiohttp session; 超时/网络错按"查不到"处理, 不抛出去炸会话。"""
    global _weather_http
    if _weather_http is None or _weather_http.closed:
        _weather_http = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=_WEATHER_HTTP_TIMEOUT_S)
        )
    try:
        async with _weather_http.get(url, params=params) as resp:
            if resp.status != 200:
                return None
            return await resp.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return None


async def _weather_resolve_city(city: str) -> tuple[float, float, str] | None:
    """城市名 → (lat, lon, 展示名)。空串/等于默认城市名 → .env 固定坐标
    (默认城市不经地理编码: 同名地名多, 固定坐标才准); 其他城市 geocode 一次并缓存。"""
    default_city = os.getenv("XIAOGE_WEATHER_CITY", "").strip()
    if not city or city == default_city:
        try:
            lat = float(os.getenv("XIAOGE_WEATHER_LAT", ""))
            lon = float(os.getenv("XIAOGE_WEATHER_LON", ""))
            return lat, lon, default_city or "本地"
        except ValueError:
            if not default_city:
                return None
            city = default_city  # 只配了名字没配坐标: 退回按名字查
    geo = _WEATHER_GEO_CACHE.get(city)
    if geo is not None:
        return geo
    data = await _weather_fetch_json(
        "https://geocoding-api.open-meteo.com/v1/search",
        {"name": city, "count": 8, "language": "zh", "format": "json"},
    )
    results = (data or {}).get("results") or []
    cn = [r for r in results if r.get("country_code") == "CN"] or results
    # 有人口的按人口挑(同名小地名为大城市让位); 都没人口取第一个。
    best = max(cn, key=lambda r: r.get("population") or 0) if cn else None
    if best is None:
        return None
    label = best.get("name") or city
    admin1 = best.get("admin1")
    if admin1 and admin1 != label:
        label = f"{label}({admin1})"
    geo = (float(best["latitude"]), float(best["longitude"]), label)
    _WEATHER_GEO_CACHE[city] = geo
    return geo


# 能力边界兜底(2026-09-20 自 llm-bak capguard 移植): prompt 护栏之外的
# 工具层硬门——明显不属于产品知识库的检索词直接挡回, 不消耗 RAG 往返。
_UNSUPPORTED_KNOWLEDGE_QUERY_RE = re.compile(
    r"(怎么去|路线|导航|地址|附近|景点|好玩|旅游|长隆|肯德基|航班|机票|酒店|门票|快递|打车|外卖)"
)
_UNSUPPORTED_CAPABILITY_REQUEST_RE = re.compile(
    r"(导航|路线|怎么去|出行计划|行程规划|规划路线|附近(?:有什么|的)?|开门|开锁|门锁|打开门|门能开)"
)


def _is_unsupported_capability_request(text: str) -> bool:
    compact = re.sub(r"\s+", "", text or "")
    if any(word in compact for word in ("讲个笑话", "讲笑话", "讲个故事")):
        return False
    return bool(_UNSUPPORTED_CAPABILITY_REQUEST_RE.search(compact))


def _unsupported_capability_reply(text: str) -> str:
    compact = re.sub(r"\s+", "", text or "")
    if any(word in compact for word in ("开门", "开锁", "门锁", "打开门", "门能开")):
        return "这个我现在还不能直接控制，当前没有开门或门锁工具。"
    return "这个我现在还不能直接完成，当前没有导航、路线或附近地点查询工具；需要导航的话请使用地图 App。"


_DUPLICATE_USER_TURN_WINDOW_S = 3.0

_PRODUCT_KNOWLEDGE_QUERY_RE = re.compile(
    r"(小歌|歌尔|机器人|设备|产品|功能|本领|能力|规格|硬件|软件|网络|升级|隐私|使用|设置|故障)"
)


def _is_unsupported_knowledge_query(query: str) -> bool:
    compact = re.sub(r"\s+", "", query or "")
    return bool(_UNSUPPORTED_KNOWLEDGE_QUERY_RE.search(compact)) and not bool(
        _PRODUCT_KNOWLEDGE_QUERY_RE.search(compact)
    )


# 人设固定规则段已下沉 config_store.FIXED_AGENT_INSTRUCTIONS(2026-09-20 自
# llm-bak fullprompt 移植): 网关 GET 要按当前人设拼默认全文, 而网关进程不能
# import web_ui_agent。此处留别名供既有引用/测试。规则正文维护在 config_store。
_FIXED_AGENT_INSTRUCTIONS = FIXED_AGENT_INSTRUCTIONS


def _build_agent_instructions(cfg: AgentConfig) -> str:
    """完整 instructions:面板自定义完整提示词优先,否则人设可编辑块+固定规则段。

    拼装逻辑在 config_store(网关 GET 默认全文也用),此处保留函数名供既有
    引用/测试。instructions_override 有值时整段替换(人设块不参与)。
    """
    return effective_instructions(cfg)


def _entrypoint_api_key() -> str | None:
    """Read only the gateway-injected session key, never an end-user frame."""
    value = os.getenv("XIAOGE_SESSION_API_KEY", "").strip()
    return value or None


# 欢迎语任务轮询它——按需 spawn 时 ctrl.hello 可先于 entrypoint 收尾到达。
_ENTRYPOINT_DONE = False


class VoiceAgent(Agent):
    def __init__(self, api_key: str | None = None) -> None:
        # Project config is selected by the gateway-authenticated API key. The key is
        # retained only in memory and is never included in logs.
        self._project_api_key = api_key
        cfg = load_project_config(api_key)
        super().__init__(instructions=_build_agent_instructions(cfg))
        self._agent_cfg = cfg
        self._last_assistant_text = ""
        self._awaiting_continue_confirmation = False
        self._last_submitted_user_turn: tuple[str, float] | None = None
        self._wiring: SessionWiring | None = None
        # 连接欢迎语去重(2026-09-16):attach(handle_session_attach)说了置 True,
        # 旧入口(网页音频 WS 连接/按钮唤醒 → handle_voice_welcome)即 no-op。
        self._voice_welcome_said = False
        self._semantic_gate = None

    def _gate_enabled(self):
        return interaction_host.mode_v2_enabled() and env_bool("XIAOGE_SEMANTIC_GATE", False)

    def _gate(self):
        if self._semantic_gate is None:
            self._semantic_gate = SemanticGate.create(build_semantic_llm())
        return self._semantic_gate

    def _authorize_music(self, context, action, argument=""):
        if getattr(self, "_gate_enabled", lambda: False)():
            return self._gate().authorize(context, action, argument)
        return None

    def _require_gate_turn(self, context):
        if getattr(self, "_gate_enabled", lambda: False)():
            return self._gate().require(getattr(context, "speech_handle", None))
        return None

    async def _say_linked(self, context, text, *, purpose, history=False):
        gate = self._gate() if self._gate_enabled() else None
        return await say_linked(self.session, context, gate, text, purpose=purpose, history=history)

    async def _command_ack(self, context, frames, reason):
        self._require_gate_turn(context)
        dispatched = self._dispatch_g3_cmd_frames(frames, reason=reason, say_ack=False)
        text = _natural_command_ack(frames) if dispatched else COMMAND_FAILURE
        await self._say_linked(context, text, purpose="command", history=True)

    def _music_guard_kwargs(self, context):
        if getattr(self, "_gate_enabled", lambda: False)():
            return {"guard": lambda: self._require_gate_turn(context)}
        return {}

    async def llm_node(self, chat_ctx, tools, model_settings):
        if not self._gate_enabled():
            return sanitize_llm_stream(
                Agent.default.llm_node(self, chat_ctx, tools, model_settings)
            )
        if self._duplex_off():
            raise StopResponse()
        speech = bound_speech()
        if speech is None:
            logger.warning("SEMANTIC_GATE_NO_SPEECH version=%s", VERSION)
            raise StopResponse()
        message = next(
            (
                item
                for item in reversed(chat_ctx.items)
                if isinstance(item, ChatMessage) and item.role == "user"
            ),
            None,
        )
        if message is None:
            raise StopResponse()
        gate = self._gate()
        grant = gate.grants.get(speech.id)
        if grant is not None:
            gate.bind(message, grant.decision, speech)
        if grant is None:
            decision = await gate.admit(message, manual=speech.input_details.modality == "text")
            if not decision.addressed:
                interaction_host.enter_listening_v2("semantic_gate")
                raise StopResponse()
            grant = gate.bind(message, decision, speech)
            interaction_host.exit_listening_v2("semantic_gate_pass")
            exit_reset_idle_timer()
        gate.require(speech)
        if grant.confirmation is not None:
            return grant.confirmation
        # The SDK updates this turn's execution ToolContext from the same list.
        tools[:] = generation_tools(tools, grant.decision)
        if grant.used:
            tools[:] = [tool for tool in tools if tool.id not in MUSIC_TOOLS]
        clean = gate.clean_context(
            chat_ctx,
            generation_instructions(self.instructions, decision=grant.decision, tools=tools),
            summarize=grant.decision.summarize_background,
        )
        diagnostic(
            "SEMANTIC_GENERATION_TOOLS",
            epoch=gate.epoch,
            message=grant.message_id,
            speech=speech.id,
            action=grant.decision.music_action,
            music_tools=[
                tool.id
                for tool in tools
                if tool.id in {"play_music", "stop_music", "switch_music", "resume_music"}
            ],
            random_wrapper=not grant.used
            and grant.decision.music_action == "play"
            and not grant.decision.music_target,
        )
        return sanitize_llm_stream(Agent.default.llm_node(self, clean, tools, model_settings))

    def attach_wiring(self, wiring: SessionWiring) -> None:
        self._wiring = wiring

    def _is_duplicate_user_turn(self, text: str) -> bool:
        compact = re.sub(r"\s+", "", text or "")
        now = time.monotonic()
        previous = self._last_submitted_user_turn
        self._last_submitted_user_turn = (compact, now)
        if not compact or previous is None:
            return False
        old_text, old_at = previous
        return old_text == compact and now - old_at < _DUPLICATE_USER_TURN_WINDOW_S

    # ── 音乐控制工具(function call 单路):委托给 runtime.music_player ──
    # 2026-09-15 本地音乐通道下线: 音乐播放只有云端曲库一路, 不再有来源选择。
    @function_tool
    async def play_music(self, context: RunContext, music_id: str = "") -> str:
        """播放云端音乐(服务器曲库)。仅当用户当前这句话明确要求现在播放歌曲/音乐
        时调用,例如"放一首歌"/"播放音乐"/"来首歌"/"听首歌"/"放某某歌";
        当前明确要求"唱首歌"/"给我唱一首"在本产品表示播放云端音乐,不是现场合成人声。
        用户按风格/类型点歌但没给具体歌名(如"轻松一点的歌"/"有氛围感的音乐")时,
        传空字符串或 random,随机播放一首,不要只口头答应。普通聊天、只是提到音乐/唱歌/声音/音量、问你会不会唱歌/刚才唱了什么、
        讨论调音量、或想营造气氛时绝对不要调用。用户想换歌(下一首/上一首/换一首/切歌)
        时改用 switch_music;用户问歌手/歌曲常识(如"刘德华有哪些歌")时不要调用,直接用知识回答。

        Args:
            music_id: 歌曲名(可模糊匹配,如"声动未来")。空字符串或 "random" 表示随机播放一首。
        """
        grant = self._authorize_music(context, "play", music_id)
        player = runtime.music_player
        if player is None:
            return "音乐功能没启用。"
        try:
            self._require_gate_turn(context)
            name = await player.play_for_tool(music_id or None, **self._music_guard_kwargs(context))
        except StopResponse:
            raise
        except Exception as exc:
            logger.exception("play_music failed")
            return f"播放出错了:{exc}"
        if name is None:
            return (
                "云端曲库里没有匹配的歌。若用户只是问常识(如某歌手有哪些歌),"
                "用你自己的知识直接回答,不用再提曲库;"
                "若用户是想播放,告诉用户云端曲库里没有、要不要换一首,不要假装已播放。"
            )
        if grant is not None:
            grant.confirmation = f"准备播放《{name}》。"
        # 工具结果给 LLM 看,不是给用户念的原文。
        return (
            f"已准备播放《{name}》。先用一句简短自然的话告诉用户要放这首歌,"
            f"不要说“换一首/来首别的”,也不要重复模板原文;"
            f"这句提示说完后音乐会开始出声。"
        )

    @function_tool
    async def stop_music(self, context: RunContext) -> str:
        """停止播放云端音乐。用户想现在就停音乐时调用: "停音乐/别放了/关掉音乐/停止
        音乐", 以及口语变体 "我不想听了/不想再听了/这歌听够了/听腻了/太吵了/算了
        不放了"; 没在播时也如实告诉用户, 不要假装已停止。"""
        grant = self._authorize_music(context, "stop")
        player = runtime.music_player
        if player is None:
            return "音乐没在播。"
        try:
            self._require_gate_turn(context)
            stopped = await player.stop_for_tool(**self._music_guard_kwargs(context))
        except StopResponse:
            raise
        except Exception as exc:
            logger.exception("stop_music failed")
            return f"停止出错了:{exc}"
        if not stopped:
            return "音乐本来就没在播,告诉用户就行,不用做任何动作。"
        if grant is not None:
            grant.confirmation = "音乐已停止。"
        return "音乐已停止。用一句简短自然的话告诉用户,不要加多余动作。"

    @function_tool
    async def switch_music(self, context: RunContext, direction: str = "next") -> str:
        """切换云端音乐到上一首或下一首(服务器曲库内循环切换,最后一首的下一首
        回到第一首)。用户说"下一首/上一首/换一首/切歌"想换歌时调用,正在播放
        也会立即切过去;没在播时切换最近播过那首的上一首/下一首。用户只是聊
        歌曲话题(如"这首歌谁唱的")不要调用。

        Args:
            direction: "next"=下一首/换一首, "prev"=上一首。默认 "next"。
        """
        grant = self._authorize_music(context, "switch", direction)
        player = runtime.music_player
        if player is None:
            return "音乐功能没启用。"
        try:
            self._require_gate_turn(context)
            name = await player.switch_for_tool(direction, **self._music_guard_kwargs(context))
        except StopResponse:
            raise
        except Exception as exc:
            logger.exception("switch_music failed")
            return f"切换出错了:{exc}"
        if name is None:
            return "云端曲库里没有歌,告诉用户就行,不要假装已切换。"
        if grant is not None:
            grant.confirmation = f"已切换到《{name}》。"
        self._remember_executed(f"切换云端音乐(《{name}》)")
        # 工具结果给 LLM 看,不是给用户念的原文。
        return (
            f"已切换到《{name}》。先用一句简短自然的话告诉用户切到了哪首,"
            f"这句提示说完后音乐会开始出声。"
        )

    # ── 设备控制工具(2026-09-16 方案B): 原 G3 命令层工具化, 单 LLM 直调 ──
    # 工具内组帧走 _dispatch_g3_cmd_frames(与原 g3 链同一出口: 广播+口语化确认+
    # 命令记忆), 然后 raise StopResponse 压掉第二轮 LLM——与原确定性路径同为
    # "一句口语确认"的零额外延迟口径。参数枚举与 commands.yaml 对齐。
    @function_tool
    async def adjust_volume(
        self,
        context: RunContext,
        command: Literal["up", "down", "mute", "set"],
        value: int = 50,
    ) -> None:
        """调节机器人音量(自己的说话音量)。用户想现在就调时调用:
        "音量大一点/调大声点/调大一点"→up, "音量小一点/调小声/小一点"→down, "静音"→mute,
        "音量调到60/调到30"→set(数值填 value), "音量调到最大/开到最大"→set value=100,
        "音量调到最小"→set value=0, "声音回正常/调回正常"→set value=50。
        用户只说"大一点/小一点"而没给数字时必须用 up/down, 不要自己编一个数值用 set。

        Args:
            command: 音量方向, 只能取 "up"(调大)/"down"(调小)/"mute"(静音)/"set"(调到具体档位)。
            value: set 档的目标音量(0-100): 最大=100, 最小=0, 正常=50, "调到60"就传 60。非 set 时忽略。
        """
        self._require_gate_turn(context)
        if command not in _VOLUME_COMMANDS:
            return f"参数不合法:{command!r}。只能是 up/down/mute/set,请重新调用。"
        params = {"command": command}
        if command == "set":
            # set 帧恒带 value(0-100); 缺失/越界回落协议默认 50(设备侧同款兜底)。
            params["value"] = value if isinstance(value, int) and 0 <= value <= 100 else 50
        await self._command_ack(
            context,
            [_build_cmd_frame("adjust_volume", "audio.volume", params)],
            reason="llm_tool_adjust_volume",
        )
        raise StopResponse()

    @function_tool
    async def take_photo(self, context: RunContext) -> None:
        """让机器人拍一张照片。只有用户明确要求现在/马上执行拍照时调用，例如“拍照/拍张照/帮我拍一张”;
        “还有照片”“有照片吗”“照片呢”、询问是否会拍照、回顾过去照片或只是聊到照片时不要调用。
        """
        self._require_gate_turn(context)
        await self._command_ack(
            context,
            [_build_cmd_frame("take_photo", "camera.photo", {"mode": "on"})],
            reason="llm_tool_take_photo",
        )
        raise StopResponse()

    @function_tool
    async def check_battery(
        self,
        context: RunContext,
        battery_type: Literal["cap", "vol", "cur"],
        gauge_id: int | None = None,
    ) -> None:
        """查询机器人电池状态。用户想现在就查时调用: 问电量/还有多少电/能用多久→"cap",
        电池电压→"vol", 电池电流→"cur"。查询结果由设备
        稍后自动播报, 不要自己编数值。

        Args:
            battery_type: 查询类型: "cap"=电量, "vol"=电池电压, "cur"=电池电流。
            gauge_id: 电池/电表编号, 只有用户明确说"2号电池"时才传 2, 默认不传。
        """
        self._require_gate_turn(context)
        if battery_type not in _BATTERY_TYPES:
            return f"参数不合法:{battery_type!r}。只能是 cap/vol/cur,请重新调用。"
        params = {"type": battery_type}
        if gauge_id in (1, 2):
            params["gauge_id"] = gauge_id
        await self._command_ack(
            context,
            [_build_cmd_frame("check_battery", "power.battery", params)],
            reason="llm_tool_check_battery",
        )
        raise StopResponse()

    # ── 会话判定工具(2026-10-10):LLM 判定"这轮不是在跟小歌说话" ──
    # V1:与退出语同口径——静默吞轮(零播报零历史)+ enter_standby 广播 on 帧
    # (reason=not_addressed),端侧收帧停发音频、按钮唤醒广播 off。EXIT_ENABLE=0
    # 时只吞轮不进待命(退出机制总关,同语义退出门)。
    # V2(2026-10-12,XIAOGE_MODE_V2=1):→ 聆听态而非休眠——用户还在屋里说话,
    # 后续轮次照过 LLM、转写照发设备,仅不回复;LLM 语义判定在对小歌说即自行
    # 唤醒(transcription_node 首个文本 chunk → exit_listening_v2)。
    # 防线(2026-10-13):V2 下再补一发 session.interrupt()——模型若在调用
    # 本工具的同轮带出了文字(已入 TTS 播报队列),打断残留播报;先
    # enter_listening_v2 再打断,保证打断派生的 ctrl.state 帧读到 listening。
    # 不能 await interrupt(它等自己的 speech 完成事件会死锁),同步触发即可;
    # generation 侧对工具协程有 shield,打断不会砍掉本工具的执行。
    @function_tool
    async def not_addressed(self, context: RunContext) -> None:
        """用户当前这句话不是在跟你说的时候调用, 调用后保持安静、什么都不要说;
        同一次回复里除调用本工具外不要输出任何文字, 调用后也不要再补任何话。
        典型情况: 他在和身边的人聊天或商量事情、在跟别人打电话或开视频、
        在自言自语、在跟家里其他设备或另一个语音助手说话、在给旁边的人念内容
        或教人操作。
        判断要果断: 同时满足"没叫你的名字或称呼你"和"没在向你提问、给你下
        指令、找你聊天倾诉"和"内容跟你们正在聊的话题无关", 就是在跟别人说,
        立刻调用; 只有他叫你的名字、向你提问、让你做事、顺着你们刚才的话题
        继续说, 才是对你说话, 这时绝对不要调用。"""
        if getattr(self, "_gate_enabled", lambda: False)():
            # Even an anomalous direct invocation cannot overrule valid gate admission.
            self._gate().require(getattr(context, "speech_handle", None))
            return None
        _append_turn_log("NOT_ADDRESSED_HIT text_rule=llm_tool")
        if interaction_host.mode_v2_enabled():
            interaction_host.enter_listening_v2("not_addressed")
            session = getattr(context, "session", None)
            if session is not None:
                try:
                    session.interrupt()
                    _append_turn_log("NOT_ADDRESSED_INTERRUPT fired")
                except Exception:
                    logger.exception("not_addressed interrupt failed")
        elif EXIT_ENABLE:
            enter_standby("not_addressed")
        raise StopResponse()

    @function_tool
    async def resume_music(self, context: RunContext) -> str:
        """继续播放刚才暂停的云端音乐。用户明确说"继续放/继续播/接着放/恢复播放"
        且指的是音乐时调用。用户说"继续讲/接着说/继续吧"指的是接着说刚才的内容时
        不要调用;没暂停过音乐时也不要调用。
        """
        grant = self._authorize_music(context, "resume")
        player = runtime.music_player
        if player is None:
            return "音乐功能没启用。"
        try:
            self._require_gate_turn(context)
            name = await player.resume_last_for_tool(**self._music_guard_kwargs(context))
        except StopResponse:
            raise
        except Exception as exc:
            logger.exception("resume_music failed")
            return f"恢复出错了:{exc}"
        if name is None:
            return "刚才没有播放过音乐,告诉用户就行,不要假装已恢复。"
        if grant is not None:
            grant.confirmation = f"已恢复播放《{name}》。"
        self._remember_executed(f"恢复播放云端音乐(《{name}》)")
        return f"已恢复播放《{name}》。先用一句简短自然的话告诉用户,这句提示说完后音乐会继续出声。"

    # ── 知识库检索工具:委托给 runtime.knowledge_index ──
    @function_tool
    async def query_knowledge(self, context: RunContext, query: str) -> str:
        """查询小歌产品知识库(产品手册、规格、FAQ、使用场景、用户补充知识等)。

        只在用户问到小歌自身的硬件规格、软件功能、网络要求、使用场景、升级方式、
        隐私政策、以及"小歌都会啥/会什么/能干什么/有什么本事"等能力类问题时调用。
        这不是开放知识库/地图/旅游/路线工具;用户问景点、地址、附近、怎么去、航班、酒店、
        餐饮、通用知识或普通聊天时不要调用。

        Args:
            query: 用户问题的精炼关键词(如"麦克风阵列规格"/"如何升级模型"/"网络要求"/"会什么")。
        """
        if _is_unsupported_knowledge_query(query):
            logger.info("query_knowledge blocked unsupported query=%r", query)
            return (
                "这个问题不在小歌产品知识库范围内。不要使用本工具结果回答;"
                "请告诉用户当前没有地图路线/旅游/航班/酒店等实时查询能力,"
                "建议使用地图导航或对应 App。"
            )
        idx = runtime.knowledge_index
        logger.info(
            "query_knowledge called: query=%r idx_ready=%s",
            query,
            idx is not None and idx.is_ready(),
        )
        result = await query_knowledge_text(idx, query)
        logger.info("query_knowledge result: query=%r code=%s ok=%s", query, result.code, result.ok)
        return result.text

    # ── 天气查询工具: Open-Meteo(免key), 默认城市 .env 可配 ──
    @function_tool
    async def query_weather(self, context: RunContext, city: str = "") -> str:
        """查询天气。用户问天气/气温/冷不冷/下不下雨(今天/明天/后天/某城市)时调用,
        如"今天天气怎么样""北京明天冷不冷"。用户没说城市就传空字符串(默认查机器人
        所在地);说了城市就把城市名传进去。

        Args:
            city: 城市名,如"北京"。用户没提城市时传空字符串。
        """
        self._require_gate_turn(context)
        await self._say_linked(context, "我帮你查一下天气", purpose="weather")
        self._require_gate_turn(context)
        city = (city or "").strip()
        geo = await _weather_resolve_city(city)
        self._require_gate_turn(context)
        if geo is None:
            logger.warning("query_weather: no geo for city=%r", city)
            return "没查到这个城市。如实告诉用户,让他换个说法(比如城市的全名),不要编天气。"
        lat, lon, label = geo
        cache_key = f"{lat:.2f},{lon:.2f}"
        hit = _WEATHER_CACHE.get(cache_key)
        if hit is not None and time.time() - hit[0] < _WEATHER_CACHE_TTL_S:
            _append_turn_log(f"WEATHER_BY_TOOL city={city!r} label={label!r} ok=True cached=True")
            return hit[1]
        data = await _weather_fetch_json(
            "https://api.open-meteo.com/v1/forecast",
            {
                "latitude": lat,
                "longitude": lon,
                "timezone": "auto",
                "forecast_days": 3,
                "current": "temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m",
                "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
                "precipitation_probability_max",
            },
        )
        self._require_gate_turn(context)
        if not data or "error" in data:
            _append_turn_log(f"WEATHER_BY_TOOL city={city!r} label={label!r} ok=False")
            return "天气查询暂时失败。如实告诉用户现在查不到,不要编数字。"
        text = _format_weather(data, label)
        _WEATHER_CACHE[cache_key] = (time.time(), text)
        _append_turn_log(f"WEATHER_BY_TOOL city={city!r} label={label!r} ok=True cached=False")
        # 工具结果给 LLM 看, 不是给用户念的原文。
        return text

    def _reset_live_transcript(self, reason: str) -> None:
        live = getattr(self._wiring, "live", None)
        reset_turn = getattr(live, "reset_turn", None)
        if callable(reset_turn):
            reset_turn(reason)

    def _finalize_g3_user_message(
        self,
        original: str,
        enabled: bool,
        utterance_id: str | None = None,
        reason: str = "consumed_voice_turn",
    ) -> None:
        if not enabled:
            return
        live = getattr(self._wiring, "live", None)
        finish_turn = getattr(live, "finish_turn", None)
        if callable(finish_turn):
            utterance_id = finish_turn(reason, utterance_id)
        display_text = LEADING_PUNCT_RE.sub("", original)
        if display_text:
            broadcast(
                {
                    "type": "message",
                    "role": "user",
                    "utterance_id": utterance_id,
                    "text": display_text,
                    "ts": time.time(),
                }
            )

    def _finish_consumed_voice_turn(self, original: str, enabled: bool, reason: str) -> None:
        if not enabled:
            return
        self._finalize_g3_user_message(original, True, reason=reason)
        self._reset_live_transcript(reason)

    def _remember_executed(self, desc: str) -> None:
        """命令执行成功后向持久 chat_ctx 注入一行轻量标记(user 侧, [系统] 前缀)。

        StopResponse 路径的命令连原话都不进聊天历史, 聊天 LLM 对"刚才做了什么/
        再来一次"完全失忆。这里静默补一条标记(不 say、不广播、不发事件), 下一轮
        LLM 请求就能看到。连续相同命令原地合并 ×N, 防命令风暴刷爆历史。
        写入失败只记日志, 绝不影响命令执行本身。
        """
        text = f"{_CMD_MARKER_PREFIX}{desc}"
        try:
            ctx = getattr(self, "_chat_ctx", None)  # 框架 say(add_to_chat_ctx) 落历史的同一对象
            if ctx is None or not hasattr(ctx, "add_message"):
                return  # 测试桩等无 chat_ctx 场景, 静默跳过
            items = getattr(ctx, "items", None) or []
            last = items[-1] if items else None
            if (
                isinstance(last, ChatMessage)
                and last.role == "user"
                and isinstance(last.content, list)
                and len(last.content) == 1
                and isinstance(last.content[0], str)
                and str(last.content[0]).startswith(_CMD_MARKER_PREFIX)
            ):
                prev = str(last.content[0])
                merged = re.match(r"^(.*) ×(\d+)$", prev)
                base = merged.group(1) if merged else prev
                if base == text:
                    count = int(merged.group(2)) + 1 if merged else 2
                    last.content = [f"{text} ×{count}"]
                    _append_turn_log(f"CMD_MARKER_MERGE count={count} desc={desc!r}")
                    return
            ctx.add_message(role="user", content=text)
            _append_turn_log(f"CMD_MARKER desc={desc!r}")
        except Exception:
            logger.exception("failed to inject command marker into chat_ctx")

    async def on_enter(self) -> None:
        # 开场白不在这里触发:on_enter 在 session.start() 期间执行,早于录音 tap 安装,
        # 会导致开场白漏录。改到 entrypoint 中、所有 tap 装好之后再触发。
        pass

    async def transcription_node(self, text, model_settings):  # type: ignore[override]
        """Intercept the LLM text stream to push the reply to the browser as soon as
        generation finishes — well before TTS playback ends. 显示文本去 markdown。"""
        collected: list[str] = []
        reply_id = f"reply-{time.time_ns()}"
        gate = self._gate() if getattr(self, "_gate_enabled", lambda: False)() else None
        speech = bound_speech() if gate is not None else None
        epoch = gate.epoch if gate is not None else None
        # V2(2026-10-12):聆听态下 LLM 产出文本 = 语义唤醒(每轮照过 LLM,LLM 自己
        # 判定这句在对小歌说)。首个非空 chunk 即切回对话(撤聆听表/臂 idle)——
        # TTS 已随 yield 流出,整句生成完再切会让 listening off/ctrl.state 状态帧
        # 滞后音频数秒,设备端 UI 迟一步收敛。V1/非聆听态下 exit 幂等早退,零影响。
        v2_wake_checked = False
        async for chunk in text:
            if gate is not None:
                if epoch != gate.epoch:
                    raise StopResponse()
                gate.require_output(speech, str(chunk), sum(map(len, collected)))
            if (
                not getattr(self, "_gate_enabled", lambda: False)()
                and not v2_wake_checked
                and str(chunk).strip()
            ):
                v2_wake_checked = True
                interaction_host.exit_listening_v2("semantic_wake")
            collected.append(chunk)  # TimedString subclasses str, so this always works
            yield chunk
        if gate is not None:
            gate.require_output(speech)
            if speech is not None and speech.id in gate.outputs.links:
                if len("".join(collected)) != len(gate.outputs.require(speech).text):
                    speech.interrupt(force=True)
                    raise StopResponse()
        full_text = strip_markdown("".join(collected))  # 气泡显示纯口语
        # 聆听期 host gate:不广播 assistant 气泡(进入提示也不变气泡);只挡广播,不碰 tts_node。
        ctrl = runtime.listen_ctrl
        if full_text and not (ctrl is not None and ctrl.active):
            broadcast(
                {
                    "type": "message",
                    "role": "assistant",
                    "text": full_text,
                    "ts": time.time(),
                    "utterance_id": reply_id,
                }
            )
        if not getattr(self, "_gate_enabled", lambda: False)():
            self._remember_assistant_turn(full_text)

    async def tts_node(self, text, model_settings):  # type: ignore[override]
        """合成语音前净化 LLM 文本(去 markdown/符号),避免 TTS 把 ** ### → 等读出来。"""
        gate = self._gate() if self._gate_enabled() else None
        speech = bound_speech() if gate is not None else None
        linked = gate is not None and speech is not None and speech.id in gate.outputs.links
        if gate is not None:
            gate.require_output(speech)
        if linked:
            text = gate.outputs.text_stream(speech, text)
        stream = Agent.default.tts_node(self, sanitize_stream(text), model_settings)
        if linked:
            stream = gate.outputs.audio_stream(speech, stream)
        async for frame in stream:
            if gate is not None:
                gate.require_output(speech)
            yield frame

    # ── 轮次钩子:聆听 → 自动进入 → 过滤 → 数字归一化(顺序与拆分前逐行一致)──────

    def _remember_assistant_turn(self, text: str) -> None:
        if not text:
            return
        if getattr(self, "_gate_enabled", lambda: False)():
            self._gate().remember_assistant(text)
        compact = _USER_TEXT_STRIP_RE.sub("", text)
        self._awaiting_continue_confirmation = bool(
            _CONTINUATION_PROMPT_RE.search(compact) and _CONTINUATION_TOPIC_RE.search(compact)
        )
        self._last_assistant_text = text

    def _maybe_add_continuation_instruction(self, turn_ctx, original: str) -> str:
        """Add a current-turn-only instruction while preserving the user's visible text."""
        if not self._awaiting_continue_confirmation:
            return original
        if _should_ignore_user_turn(original):
            self._awaiting_continue_confirmation = False
            return original

        compact = _USER_TEXT_STRIP_RE.sub("", original or "")
        if not compact or not _CONFIRM_CONTINUE_RE.fullmatch(compact):
            self._awaiting_continue_confirmation = False
            return original

        instruction = "我确认继续。请直接接着刚才的内容往下讲正文，不要重复刚才的询问或铺垫。"
        turn_ctx.add_message(role="system", content=instruction)
        self._awaiting_continue_confirmation = False
        _append_turn_log(
            "CONTINUE_CONFIRM_INJECT "
            f"original={original!r} previous_assistant={self._last_assistant_text!r}"
        )
        return "继续讲"

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
        不重置连击;打断小歌的轮只要满 auto_interrupt_min_chars 就计数,连续 N 次 → 进入)。"""
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

    def _music_control_intent(self, original: str, *, music_ctx: bool = False) -> str | None:
        return _music_control_intent(original, music_ctx=music_ctx)

    # ── 音乐播放(云端单路, 2026-09-15): 本地(设备侧)通道下线, 不再下发 control_music ──

    def _dispatch_g3_cmd_frames(
        self, frames: list[dict], *, reason: str = "g3_protocol_cmd", say_ack: bool = True
    ) -> bool:
        """广播 data.cmd 帧并口语化确认(g3 协议命令下发; say_ack=False 静默下发,
        供改口让位等"紧接着还有别的播报"的场景)。"""
        dispatched = broadcast({"type": "g3_protocol", "dry_run": False, "frames": frames})
        _append_turn_log(
            f"G3_PROTOCOL_CMD dispatched={dispatched} frames={frames!r} reason={reason}"
        )
        if dispatched:
            self._remember_executed(_describe_executed_frames(frames))
        if say_ack:
            self.session.say(
                _natural_command_ack(frames) if dispatched else COMMAND_FAILURE,
                add_to_chat_ctx=False,
                allow_interruptions=False,
            )
        self._reset_live_transcript(reason)
        return dispatched

    async def _apply_turn_filters(
        self,
        original,
        spoke_over_agent: bool,
        *,
        finalize_user_message: bool = False,
    ) -> None:
        """停止词 → 强打断+跳过回复;背调/压话附和 → 只跳过回复;
        音乐停止前置直行; 在播静默门(方案B: 泛音乐词不劫持, 全走 LLM 工具)。"""
        player = runtime.music_player
        compact = _USER_TEXT_STRIP_RE.sub("", original or "")
        if _is_unsupported_capability_request(original):
            logger.info("unsupported capability blocked: %r", original)
            _append_turn_log(f"UNSUPPORTED_CAPABILITY_BLOCK text={original!r}")
            self.session.say(
                _unsupported_capability_reply(original),
                add_to_chat_ctx=False,
                allow_interruptions=False,
            )
            if finalize_user_message:
                self._finish_consumed_voice_turn(original, True, "unsupported_capability")
            raise StopResponse()
        # 音乐上下文(2026-09-14): 云端在播。裸停止口语("别放了"/"暂停"/"停一下")
        # 只在有上下文时按停止解析, 无上下文保持聊天语义。
        music_ctx = player is not None and bool(getattr(player, "is_playing", False))
        if (
            player is not None
            and self._music_control_intent(original, music_ctx=music_ctx) == "stop"
        ):
            stopped = await player.stop_for_tool()
            _append_turn_log(f"MUSIC_STOP_BY_VOICE stopped={stopped} text={original!r}")
            if stopped:
                self._remember_executed("停止云端音乐")
            _defer_session_say(
                self.session,
                "好的，音乐停了。" if stopped else "音乐现在没在播。",
                add_to_chat_ctx=False,
                allow_interruptions=False,
            )
            if finalize_user_message:
                self._finish_consumed_voice_turn(original, True, "music_stop")
            raise StopResponse()
        if _should_ignore_user_turn(original):
            logger.info("stop phrase -> force interrupt + skip reply: %r", original)
            _append_turn_log(f"STOP_PHRASE text={original!r} -> force_interrupt + skip_reply")
            if player is not None and getattr(player, "is_playing", False):
                stopped = await player.stop_for_tool()
                _append_turn_log(f"MUSIC_STOP_BY_STOP_PHRASE stopped={stopped} text={original!r}")
                if stopped:
                    self._remember_executed("停止云端音乐")
            if not listen_interrupt_blocked():  # 聆听期/保护窗内不打断小歌(仍跳过回复)
                self.session.interrupt(force=True)
            raise StopResponse()

        if (
            player is not None
            and getattr(player, "is_playing", False)
            and not _music_playing_pass_gate(self, compact)
        ):
            logger.info("music playing -> skip non-music user reply: %r", original)
            _append_turn_log(f"MUSIC_PLAYING_SKIP_REPLY text={original!r}")
            raise StopResponse()

        if _is_backchannel(original):
            logger.info("backchannel -> skip reply: %r", original)
            _append_turn_log(f"BACKCHANNEL text={original!r} -> skip_reply")
            raise StopResponse()

        if spoke_over_agent and _is_overlap_ack(original):
            logger.info("overlap ack -> skip reply: %r", original)
            _append_turn_log(f"BACKCHANNEL_OVERLAP text={original!r} -> skip_reply")
            raise StopResponse()

    def _duplex_off(self) -> bool:
        # 全双工关 = 仅流式 ASR 出字(零 LLM/TTS/历史)。2026-09-21 自 llm-bak
        # 移植:该开关只在当前会话生效,由 runtime.duplex_enabled 承载;
        # agent_config 里的旧字段不再控制。
        return not getattr(runtime, "duplex_enabled", True)

    @staticmethod
    def _effective_agent_config(
        cfg: AgentConfig, persona_key: str | None, explicit: bool = False
    ) -> AgentConfig:
        """会话性格生效配置(2026-09-22,仅 -97)。

        预设 traits 覆盖全局人设性格块。音色完全独立：只使用项目默认音色，或
        显式的 session voice override；persona preset 自身不再携带音色语义。
        提示词);attach 落的默认档 explicit=False 不 mask——否则三档化后每个
        新会话都让全局自定义提示词失效(2026-09-22 fullprompt 回归修复)。
        存 key 引用而非内容快照:预设经 POST 热更后下一轮即取新文;键固定三档
        不会消失,真取不到(老配置缺键)→ 回落全局。persona_name 保持全局——
        换的是性格不是名字。
        """
        if not persona_key or persona_key not in PERSONA_KEYS:
            return cfg
        persona = (cfg.personas or DEFAULT_PERSONAS).get(persona_key)
        if not persona:
            return cfg
        updates: dict[str, object] = {"persona_traits": persona["traits"]}
        if explicit:
            updates["instructions_override"] = None
        return replace(cfg, **updates)

    @staticmethod
    def _apply_session_overrides(cfg: AgentConfig) -> AgentConfig:
        """Apply only the in-memory overrides for the authenticated session."""
        updates: dict[str, object] = {}
        persona_key = getattr(runtime, "session_persona", None)
        if persona_key:
            cfg = VoiceAgent._effective_agent_config(
                cfg, persona_key, getattr(runtime, "session_persona_explicit", True)
            )
        voice = getattr(runtime, "session_voice", None)
        if voice in VOICE_KEYS:
            updates["tts_voice"] = voice
        return replace(cfg, **updates) if updates else cfg

    async def _maybe_apply_agent_config(self) -> None:
        """轮次起点热应用面板配置(data/agent_config.json 按 mtime 检查)。

        生效配置 = 全局配置 + 会话性格覆盖(见 _effective_agent_config):对比
        与应用都以生效配置为准——性格切换帧本身不改 mtime,靠 runtime.
        session_persona 变化改变生效配置触发应用;预设内容 POST 改的是文件,
        走 mtime 路径,同一条路殊途同归。
        任意字段变了才动:人设 → update_instructions(下一次 LLM 调用即用新
        instructions);音色变了才换 TTS 实例(纯人设变更不重建,旧 WS 池照用),
        且仅当前后端是 cosyvoice——连接池进程级共享、aclose 为空操作,在途
        旧流各持旧实例引用自续,零代价。
        load_agent_config 自吞异常(文件损坏 → 默认),这里只兜 apply 的异常,
        绝不让配置热更打断正常对话。
        """
        cfg = load_project_config(
            getattr(runtime, "project_api_key", None) or getattr(self, "_project_api_key", None)
        )
        persona_key = getattr(runtime, "session_persona", None)
        eff = self._effective_agent_config(
            cfg, persona_key, getattr(runtime, "session_persona_explicit", False)
        )
        eff = self._apply_session_overrides(eff)
        if eff == self._agent_cfg:
            return
        try:
            await self.update_instructions(_build_agent_instructions(eff))
            if (
                eff.tts_voice != self._agent_cfg.tts_voice
                and runtime.switchable_tts is not None
                and runtime.tts_backend_key == "cosyvoice"
            ):
                old_tts = runtime.switchable_tts.switch_backend(
                    make_tts_backend("cosyvoice", voice=eff.tts_voice)
                )
                await old_tts.aclose()
            self._agent_cfg = eff
            _append_turn_log(
                f"AGENT_CONFIG_APPLIED name={eff.persona_name!r} voice={eff.tts_voice}"
                f" duplex={'on' if getattr(runtime, 'duplex_enabled', True) else 'off'}"
                f" instructions={'custom' if eff.instructions_override else 'default'}"
                f" persona={persona_key or 'none'}"
            )
            logger.info(
                "agent config hot-applied: name=%r voice=%s persona=%s",
                eff.persona_name,
                eff.tts_voice,
                persona_key or "none",
            )
        except Exception:
            logger.exception("agent config hot-apply failed, keep old config")

    async def handle_voice_welcome(self) -> None:
        """连接欢迎语(旧入口):先热应用面板配置再开口(修欢迎语音色滞后)。

        池化 worker 常驻,TTS 只在轮次起点换实例,而欢迎语在轮次之前播——
        用户改音色后复连,第一句仍是旧音色。这里在 say 前先跑一次
        _maybe_apply_agent_config(mtime 检查,无变更零开销);应用失败已被
        其内部吞掉(保持旧配置),欢迎语照说不挡连接。音色随埋点带出,
        e2e 可直断言"这一句用的是哪个音色"。

        2026-09-16 起连接欢迎语统一到 handle_session_attach(每个 ctrl.hello
        恰一句,机器人自动重连也有);本方法只剩网页音频 WS 连接/按钮唤醒两
        个旧入口,经 _voice_welcome_said 去重——attach 已说过即 no-op。

        V2(2026-10-12,XIAOGE_MODE_V2=1):连接即休眠、无欢迎语(设备侧三态
        需求)——本方法连同两个旧入口整体 no-op。
        """
        if interaction_host.mode_v2_enabled():
            return
        if self._voice_welcome_said:
            return
        self._voice_welcome_said = True
        await self._maybe_apply_agent_config()
        if self._duplex_off():
            # 全双工关:连接欢迎语跳过(ASR 测试模式要安静)。
            _append_turn_log("VOICE_WELCOME_SKIP duplex_off")
            return
        say_voice_welcome(self._agent_cfg.tts_voice)

    async def _voice_welcome_when_ready(self) -> None:
        """attach 触发的连接欢迎语:延迟到 entrypoint 收尾再开口。

        按需 spawn 时 ctrl.hello 可先于 entrypoint 收尾到达(healthz ready 早于
        引擎/索引装载),过早 say 会丢帧/丢气泡——轮询 _ENTRYPOINT_DONE(音频
        tap 装完/runtime.session 就绪时置位)。池化复用 worker 上该标志早已置
        位,这里第一个 tick 即开口。
        """
        while not _ENTRYPOINT_DONE:
            await asyncio.sleep(0.25)
        if self._duplex_off():
            # 全双工关:连接欢迎语跳过(ASR 测试模式要安静)。
            _append_turn_log("VOICE_WELCOME_SKIP duplex_off")
            return
        say_voice_welcome(self._agent_cfg.tts_voice)

    async def handle_session_attach(self) -> None:
        """真实会话 attach 重置(2026-09-15 幽灵待命修复)。

        entrypoint 只在 worker 进程启动跑一次:预热池化时欢迎语对着空气
        说完,其播报结束锚点臂上的 idle 定时器在池化期间被守卫 10s 一次
        顺延成链,首个会话连上后下一次到期就 STANDBY_ENTER 打进新会话
        吞掉全部轮次。merged WS 的 ctrl.hello 才是池化 worker 复用的真实
        会话起点——这里做 per-session 重置:热应用面板配置(音色/人设跟手,
        先于任何轮次)+ 清脏 standby + 撤遗留定时器 + 重臂满额 idle。会话性格
        同批落回默认档(2026-09-22 改版,用户指定):三档即全部选项,冷面幽默
        (witty)是默认档,新会话 attach 恒落 witty——复用 worker 带着上一会话
        的 persona key,不重置新会话第一句(含欢迎语)就还是上一档音色。默认档
        标记非显式(session_persona_explicit=False):只带 traits/音色,不让全局
        自定义完整提示词让位——mask 是会话内显式选档的语义。
        埋点 SESSION_ATTACH_RESET 带 duplex 态,e2e 直断言。
        """
        if getattr(self, "_semantic_gate", None) is not None:
            self._semantic_gate.reset()
        runtime.session_persona = DEFAULT_PERSONA_KEY
        runtime.session_voice = None
        runtime.session_voice_explicit = False
        runtime.session_persona_explicit = False  # 默认档非显式:不 mask 全局完整提示词
        await self._maybe_apply_agent_config()
        # V2(2026-10-12):会话起点 = 休眠态(v2_session_start 置 standby+mode、
        # 撤两路定时器、零广播——hello 的 ctrl.state 应答帧已带 sleeping);
        # V1 保持原 per-session 重置(清脏 standby + 撤遗留定时器)。
        if interaction_host.mode_v2_enabled():
            interaction_host.v2_session_start()
        else:
            standby_cancel_all()
        runtime.duplex_enabled = self._agent_cfg.duplex_enabled
        _append_turn_log(
            f"SESSION_ATTACH_RESET duplex={'on' if runtime.duplex_enabled else 'off'} persona={DEFAULT_PERSONA_KEY}"
        )
        # 面板开关对齐(2026-10-12 恢复 Web 端全双工开关):attach 落项目默认后
        # 推 ctrl.duplex_state,面板下拉按真实态显示(项目默认 off 时不再错显
        # "开启")。面板收到帧即刷新,无需应答。
        broadcast(
            {
                "type": "ctrl.duplex_state",
                "trace_id": f"trace-g3-{int(time.time() * 1000)}",
                "session_id": os.getenv("XIAOGE_SESSION_ID", "sess-g3-local"),
                "enabled": runtime.duplex_enabled,
                "ts_ms": int(time.time() * 1000),
            }
        )
        # 连接欢迎语 + idle 臂(2026-09-16 统一到 attach;V2 让位):
        # V2 连接即休眠、无欢迎语——只压 _voice_welcome_said 旗标(旧入口若在
        # 其后触发也 no-op,双保险,handle_voice_welcome 自身也有 V2 早退),
        # 不臂 idle(休眠态无定时器,唤醒/输入时再臂)。
        if interaction_host.mode_v2_enabled():
            self._voice_welcome_said = True
            return
        exit_arm_idle_timer()
        # 每个 ctrl.hello 都是真实会话起点,恰说一句固定短欢迎语("连接成功,
        # 欢迎使用小歌,请开始说话。")。原先的三个分散入口都有缺口——机器人
        # 自动重连既无独立音频 WS 也无 wake_event=button,一句欢迎都听不到;
        # 按需 spawn 的 entrypoint 开场白又会迟到数秒落进会话(用户听成"多一句
        # 自我介绍")。开口延到 entrypoint 收尾(见 _voice_welcome_when_ready),
        # 池化复用 worker 上零延迟。旗标不做"已说就跳过"检查——它是实例级,
        # 池化 worker 跨会话复用,上一会话留下的 True 会吃掉新会话的欢迎语;
        # attach 本身就是会话起点,恒说恰一句并重置旗标(旧入口若在其后触发
        # 即 no-op)。
        self._voice_welcome_said = True
        asyncio.get_running_loop().create_task(self._voice_welcome_when_ready())

    async def handle_manual_text(self, text: str) -> None:
        original = text.strip()
        if not original:
            return
        _append_turn_log(f"MANUAL_TEXT text={original!r}")
        # 手动文本与同一请求的语音 final 可能在浏览器双通道同时到达；
        # 但独立的手动问题应始终允许处理，不在这里按文本盲目丢弃。
        await self._maybe_apply_agent_config()
        # V2(2026-10-12):手发文字 = 用户主动找小歌——休眠态自愈唤醒(兜底唤醒
        # 帧丢失),聆听态即刻回对话(面板文字必然是对小歌说的,不等 LLM 判定)。
        interaction_host.v2_wake_on_input("manual_text")
        if not getattr(self, "_gate_enabled", lambda: False)():
            interaction_host.exit_listening_v2("manual_text")
        # 待命不吞文字(2026-09-16):待命=服务端纯通知态——on 帧告知端侧"无输入
        # 30s",端侧自行决定停不停发音频;音频/文字照来的轮次服务端照常处理照常
        # 回。ctrl.frontend_state 唤醒帧已退役(2026-10-13):唤醒=真实用户输入
        # (文字/语音轮本身,v2_wake_on_input)。
        if EXIT_ENABLE and _is_exit_phrase(original):
            _append_turn_log(f"EXIT_PHRASE_HIT text={original!r}")
            broadcast({"type": "message", "role": "user", "text": original, "ts": time.time()})
            enter_standby("semantic")
            return
        exit_reset_idle_timer()  # 文字也是输入:idle 重臂
        # 全双工关:同语音轮口径,只显示不回复。
        if self._duplex_off():
            _append_turn_log(f"MANUAL_TEXT_SUPPRESSED duplex_off text={original!r}")
            broadcast({"type": "message", "role": "user", "text": original, "ts": time.time()})
            return
        if getattr(self, "_gate_enabled", lambda: False)():
            message = ChatMessage(role="user", content=[original])
            decision = await self._gate().admit(message, manual=True)
            if not decision.addressed:
                broadcast({"type": "message", "role": "user", "text": original, "ts": time.time()})
                return
            self.session.generate_reply(user_input=message, input_modality="text")
            return
        try:
            await self._apply_turn_filters(original, False)
        except StopResponse:
            broadcast({"type": "message", "role": "user", "text": original, "ts": time.time()})
            return
        mark_agent_output_started()
        self.session.generate_reply(user_input=original, input_modality="text")

    async def on_user_turn_completed(self, turn_ctx, new_message: ChatMessage) -> None:
        spoke_over_agent = runtime.overlap_turn_state["user_spoke_over_agent"]
        original = new_message.text_content

        if getattr(self, "_is_duplicate_user_turn", lambda _text: False)(original):
            logger.info("duplicate user turn suppressed: %r", original)
            _append_turn_log(f"DUPLICATE_USER_TURN_SUPPRESSED text={original!r} source=voice")
            self._finish_consumed_voice_turn(original, True, "duplicate_user_turn")
            raise StopResponse()

        await self._maybe_apply_agent_config()
        # V2(2026-10-12):休眠态照来的语音轮自愈唤醒(云端三态权威,语音轮
        # 即真实用户输入——ctrl.frontend_state 帧已退役,这已是唯一唤醒路径)。
        interaction_host.v2_wake_on_input("voice_turn")
        # 待命不吞轮(2026-09-16):STANDBY_SWALLOW 门已拆——待命只是服务端通知态
        # (idle/聆听超时/语义退出 → on 帧给端侧),唤醒与否、停不停发音频都是客户端
        # 逻辑;待命期照来的语音轮走全管线(显示/历史/LLM/回复)。
        # 语义退出:整句命中退出语 → 显示该句气泡,静默进待命(零播报),吞轮零历史。
        if EXIT_ENABLE and _is_exit_phrase(original):
            self._finalize_g3_user_message(original, True, reason="exit")
            _append_turn_log(f"EXIT_PHRASE_HIT text={original!r}")
            enter_standby("semantic")
            raise StopResponse()
        # 用户输入锚点:无输入自动退出定时器重臂回满额(EXIT_IDLE_S)。V2 聆听态
        # 换锚聆听持续表(每轮聆听重臂 EXIT_LISTEN_S)——idle 若保持臂着,会在
        # 聆听中途先到期,以 "idle" 而非 "listen_timeout" 误入休眠。
        if getattr(self, "_gate_enabled", lambda: False)():
            if self._duplex_off():
                self._finalize_g3_user_message(original, True, reason="duplex_off")
                raise StopResponse()
            decision = await self._gate().admit(new_message)
            if not decision.addressed:
                self._finalize_g3_user_message(original, True, reason="semantic_gate")
                interaction_host.enter_listening_v2("semantic_gate")
                raise StopResponse()
            # Dialogue restoration occurs only after llm_node binds a live speech.
            return
        if interaction_host.listening_v2_active():
            exit_arm_listen_timer()
            # V2 聆听轮:不过过滤链(每轮都过 LLM——停止词/附和/音乐门都是语义
            # 吞轮,与"LLM 语义判定唤醒"冲突),只注入聆听指令;不 raise,正常
            # 返回让 _generate_reply 照跑——用户消息照进历史、照经 tap 广播
            # (conversation_item_added,满足"聆听仅做 ASR 转写"),LLM 按指令
            # 判定:在对小歌说 → 出文本(transcription_node 切回对话);否则
            # not_addressed(保持聆听)。数字归一化跳过:只判定在不在对小歌
            # 说,数字无关。
            turn_ctx.add_message(role="system", content=interaction_host.V2_LISTENING_INSTRUCTION)
            return
        exit_reset_idle_timer()
        # 全双工关(2026-09-15):仅流式 ASR 出字——final 广播为 user 气泡,吞轮
        # (零 LLM/TTS/历史)。门序:语义退出 > duplex(都在过滤链之前;待命不设门)。
        if self._duplex_off():
            self._finalize_g3_user_message(original, True, reason="duplex_off")
            _append_turn_log(f"DUPLEX_OFF_TURN text={original!r}")
            raise StopResponse()
        original, handled = self._handle_listening_turn(turn_ctx, new_message, original)
        if handled:
            return
        original = self._maybe_add_continuation_instruction(turn_ctx, original)
        self._maybe_auto_enter_listening(original, spoke_over_agent)
        await self._apply_turn_filters(
            original,
            spoke_over_agent,
            finalize_user_message=True,
        )
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
    global _ENTRYPOINT_DONE
    runtime.agent_loop = asyncio.get_running_loop()
    runtime.job_ctx = ctx  # 供网关标记会话断开时优雅退出进程(webpanel/server.py #3)

    stt_engine, stt_for_session, live_from_main = setup_stt(ctx)
    tts_engine = build_tts()
    runtime.switchable_tts = tts_engine  # expose for web server
    broadcast({"type": "state", "tts_backend": runtime.tts_backend_key})

    # 不注入 log_context_fields:console/pool 形态下这些静态字段会被 CLI 渲染成每行末尾的
    # extra JSON(cli/log.py),4 进程合流一份日志时全是重复噪声、还撑长行触发折行。需要结构化
    # 上下文时改走 start(JSON)形态再按需加。
    ctx.log_context_fields = {}

    llm = build_llm()
    # Semantic Router 已移除(2026-09-16 方案B): 命令理解并入聊天 LLM(prompt 规则 + function 工具)。
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
    turn_metrics = setup_test_instrumentation(ctx, w)  # 设 w.timeline/record_settings/record_dir

    agent = VoiceAgent(api_key=_entrypoint_api_key())
    await session.start(agent=agent, room=ctx.room)
    runtime.session = session  # 供模块级聆听助手/欢迎语在 agent 循环里 say/收尾
    runtime.manual_text_handler = agent.handle_manual_text
    # 欢迎语也走 agent: 复连即先热应用面板配置(音色跟手),见 handle_voice_welcome。
    runtime.voice_welcome_handler = agent.handle_voice_welcome
    # 真实会话 attach(merged WS ctrl.hello)也走 agent: per-session 重置
    # (热应用配置 + 清 standby/遗留定时器 + 重臂 idle)。entrypoint 只在
    # 进程启动跑一次,池化复用的会话起点在 webpanel.server 的 ctrl.hello
    # 分支 marshal 进来——2026-09-15 幽灵待命修复,见 handle_session_attach。
    runtime.session_attach_handler = agent.handle_session_attach
    # 退出机制(2026-09-15):进程起点重置待命态/两路定时器 + 臂 idle(按需
    # spawn 即刻有真实会话的路径;预热 worker 的这路定时器在池化无会话的
    # 首次到期时被 session_client_count()==0 守卫彻底停表,attach 再重臂)。
    # V2(2026-10-12)让位:进程起点(含池化预热期)直接休眠态、不臂任何定时器
    # ——真会话起点是 ctrl.hello → v2_session_start,唤醒/输入时再臂对应表。
    runtime.duplex_enabled = True
    if interaction_host.mode_v2_enabled():
        interaction_host.v2_entrypoint_init()
    else:
        standby_cancel_all()
        exit_arm_idle_timer()

    # tap 链装配(顺序即包裹层次,不可乱):场景注入 → 浏览器音频 → 静音门 → 录音 → KWS → 在线打断
    setup_scenario_injection(session, turn_metrics)
    setup_web_audio(session)
    setup_mute_gate(session)
    setup_recording(ctx, w)
    setup_live_transcript(w)
    agent.attach_wiring(w)
    setup_kws(ctx, w)
    setup_online_interrupt(ctx, w)
    setup_music(ctx, w)
    # 连接欢迎语放行点(2026-09-16):音频 tap 全装完(session.say 可闻/可录、
    # runtime.session 已就绪)。attach 的欢迎语任务轮询此标志开口——知识索引
    # 装载在其后,不挡出声。
    _ENTRYPOINT_DONE = True

    # 知识库索引:best-effort 加载已持久化的索引;无索引文件时 is_ready()=False,
    # query_knowledge 工具会返回"未启用"。需要重建索引时跑 `python -m app.knowledge_index rebuild`。
    ki = KnowledgeIndex()
    if ki._load():
        runtime.knowledge_index = ki
        logger.info("knowledge index loaded: %d chunks", ki.doc_count)
    else:
        logger.warning(
            "knowledge index not loaded (no persisted index at %s). "
            "Run `python -m app.knowledge_index rebuild` to build.",
            ki.index_dir,
        )
        runtime.knowledge_index = ki  # 仍挂上,工具会优雅返回"未启用"

    # 开场白已下线(2026-09-16):entrypoint 的固定自我介绍只在进程启动说一次——
    # 按需 spawn(池忙时新会话触发 spawn)会话已先行 attach,这句迟到数秒落进
    # 真实会话,用户听成"连上后多一句自我介绍";池化预热时则对空气说完,纯
    # 浪费一次 TTS。连接欢迎语统一到 handle_session_attach:每个 ctrl.hello
    # 恰说一句固定短欢迎语("连接成功,欢迎使用小歌,请开始说话。"),
    # 音色跟手/ duplex 门也随之由 attach 路径承担。
    _append_turn_log("WELCOME_INTRO_REMOVED entrypoint")


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
