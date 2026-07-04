"""停止词 / 附和(背调) / 压话确认 / 数字归一化 —— 语音轮次的文本判定规则。

此前完整复制在 web_ui_agent.py 与 qwen_funasr_bailian_voice_agent.py 两处(~130 行),
收敛到这里作为唯一来源。纯文本逻辑,无 I/O、无状态,行为与原实现逐字节一致
(tests/test_ours_text_rules.py 锁定)。
"""

from __future__ import annotations

import re

PURE_DIGIT_RE = re.compile(r"^\d{2,16}$")

# 命中后:强制打断当前播放 + 跳过本次回复(见 VoiceAgent.on_user_turn_completed)。
STOP_WORDS = (
    "停",
    "停下",
    "停一下",
    "暂停",
    "好了",
    "行了",
    "别说",
    "别说了",
    "别讲",
    "别讲了",
    "别念了",
    "等一下",
    "等等",
    "等下",
    "稍等",
    "知道了",
    "我知道了",
    "闭嘴",
    "安静",
    "不听了",
    "不用了",
    "不要了",
    "休庭",  # FunASR 常把"停/暂停"误识成"休庭",兜底
)
# 可选引导词前缀:实测用户说"那别说了","那"不在停止词表也不在附和字集,
# ^ 锚定 fullmatch 失配 -> 没 skip_reply。"那/就/你"这类引导词不改变停止意图。
STOP_LEAD_IN = r"(?:那|你|就|请|先|那你|那就)?"
STOP_REPLY_PATTERNS = tuple(
    re.compile(rf"^\s*{STOP_LEAD_IN}{re.escape(w)}[一下吧呢啊呀了嘛]*\s*[。.！!，,、\s]*$")
    for w in STOP_WORDS
)

# 背调词(语气词):用户边听边"嗯/哦"的回应,不是新指令。命中后只跳过回复、
# 不强制打断——靠 resume_false_interruption 让 agent 把原话接着说完。
# 整句必须全部由语气字 + 标点组成才算("哦好吧"含实义不命中)。
BACKCHANNEL_CHARS = "嗯哦噢喔啊呃唉唔诶哼呢"
BACKCHANNEL_RE = re.compile(rf"^[{BACKCHANNEL_CHARS}][{BACKCHANNEL_CHARS}，,。.、！!？?～~\s]*$")

# 压话确认词:用户在 AI 播报期间说的"对/好/是的"等附和,不是新指令。
# "对/好"在 AI 提问后是真答案,不能无脑拒 -> 仅当本轮用户是"压着 AI 播报开口"
# (host 侧 overlap 状态)才按附和拒识。
OVERLAP_ACK_CHARS = BACKCHANNEL_CHARS + "对好是行的呀嘛"
ACK_STRIP_RE = re.compile(r"[\s，,。.、！!？?～~；;：:]+")

# 句首游离标点:FunASR 常把上句尾标点带到下句句首。仅用于显示净化,不动进上下文的原文。
LEADING_PUNCT_RE = re.compile(r"^[\s，,。.、！!？?～~；;：:…—·、\-]+")

# 轮级停止判定按标点分段,逐段判,而不是整句单 fullmatch。实测三类漏法都是
# "整句锚定"被多余成分顶掉:①早清残留/附和前缀"嗯。 停。"②引导词"那别说了"
# ③双停止词连说"行了,别说了。"。规则:每段要么命中停止词、要么是纯附和字,
# 且至少一段是停止词 -> 整轮拒识;含任何实义段则正常走轮次。
SEGMENT_SPLIT_RE = re.compile(r"[\s，,。.、！!？?～~；;：:]+")


def is_overlap_ack(text: str | None) -> bool:
    """整句剥标点后全部是压话确认字 -> 视为附和(是否生效由 host 的 overlap 状态决定)。"""
    if text is None:
        return False
    core = ACK_STRIP_RE.sub("", text.strip())
    return bool(core) and all(ch in OVERLAP_ACK_CHARS for ch in core)


def should_ignore_user_turn(text: str | None) -> bool:
    """停止词轮判定:各段均为停止词/纯附和,且至少一段是停止词。"""
    if text is None:
        return False
    segments = [seg for seg in SEGMENT_SPLIT_RE.split(text.strip()) if seg]
    if not segments:
        return False
    has_stop_word = False
    for seg in segments:
        if any(pattern.fullmatch(seg) for pattern in STOP_REPLY_PATTERNS):
            has_stop_word = True
            continue
        if all(ch in OVERLAP_ACK_CHARS for ch in seg):
            continue
        return False
    return has_stop_word


def is_backchannel(text: str | None) -> bool:
    """整句仅由语气字 + 标点组成 -> 背调,跳过回复、不打断。"""
    if text is None:
        return False
    normalized = text.strip()
    return bool(normalized) and bool(BACKCHANNEL_RE.fullmatch(normalized))


def normalize_spoken_digit_sequence(text: str | None) -> str | None:
    """ASR 会把"1、2、3、4、5"合并成"12345";还原成顿号分隔,让 LLM 按逐位数字理解。"""
    if text is None:
        return None
    stripped = text.strip()
    if not PURE_DIGIT_RE.fullmatch(stripped):
        return text
    return "、".join(stripped)
