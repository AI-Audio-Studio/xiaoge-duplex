"""R5.2.2 G3 cloud intent routing and X3 command validation.

命令清单已配置化: commands.yaml(与本文件同目录)是唯一事实来源, 启动时经
load_registry() 读入——参数/枚举/风险级别喂确定性注册表, examples/params 自动
生成 SemanticRouter 的分类提示词。改命令只改 YAML, 不动代码。

This module remains deterministic: every command-like turn is routed, slot-filled,
validated, and only then converted into protocol-shaped frames. Real robot
execution is still gated outside this module.
"""

from __future__ import annotations

import os
import random
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

Delivery = Literal[
    "data.cmd",
    "data.cmd after confirmation",
    "cloud_tool + data.reply",
    "cloud_knowledge + data.reply",
    "ask_split only",
]
IntentType = Literal[
    "control_cmd",
    "control_cmd_multi",
    "info_query",
    "knowledge_qa",
    "chat",
    "config",
]
Decision = Literal[
    "accept",
    "ask_missing",
    "ask_clarify",
    "ask_confirm",
    "reject_capability",
    "reject_policy",
    "cancel",
    "timeout",
]
NonExecutionAct = Literal[
    "capability_query",
    "prohibit",
    "hypothetical",
    "future_plan",
    "quotation",
]

EXECUTABLE_DELIVERIES = {"data.cmd", "data.cmd after confirmation"}
_MULTI_MARKERS = ("然后", "同时", "并且", "接着", "顺便")
_AFFIRMATIVE_CONFIRM = frozenset({"确认", "确认执行", "执行", "可以", "是的", "好的", "同意"})
_CANCEL_CONFIRM = frozenset({"取消", "取消执行", "算了", "不要了", "停止执行"})
_ASK_WORDS = ("什么", "怎么", "为什么", "多少", "哪里", "在哪", "哪些", "哪个", "介绍", "查询", "查一下", "干啥", "干嘛", "做啥")
_PROHIBIT_MARKERS = ("不要", "别", "先别", "不用", "禁止", "不准", "别让")
_HYPOTHETICAL_MARKERS = ("如果", "假如", "假设", "要是", "万一")
_FUTURE_MARKERS = ("等会", "待会", "稍后", "以后", "明天", "下次", "过一会", "将来")
_QUOTATION_MARKERS = ("他说", "她说", "他们说", "有人说", "刚才说", "原话", "转述")
_POLITE_EXECUTE_MARKERS = ("请", "帮我", "麻烦", "给我")
# 命令相关词表(与 commands.yaml 对齐): 只用于"未命中注册表时是否值得走
# semantic LLM 兜底"的闸门, 目录本身以 YAML 为准。
# 音乐词已随 2026-09-15 control_music 下线移除: 音乐话术不再触发 semantic 兜底,
# 由前置过滤层(云端直行)或聊天 LLM 自然处理。
_COMMAND_RELEVANT_MARKERS = (
    "音量",
    "声音",
    "静音",
    "拍照",
    "照片",
    "拍个",
    "拍一",
    "张照",
    "张相",
    "电量",
    "电池",
    "多少电",
    "电压",
    "电流",
    "充电",
    "续航",
)

# 能力问句自然回复模板(2026-09-02 用户反馈"可以，这项能力当前可用。"太生硬,
# 选 B 方案轮换, 不调用 LLM): available=注册表命中且设备有 cmd 能力。
_CAPABILITY_REPLIES = (
    "可以呀，随时都能做～",
    "没问题，这个我拿手～",
    "会呀，你一说我就动～",
    "可以的，想试就说一声～",
)
_CAPABILITY_MISSING_REPLIES = (
    "哎，这个我还不会，要不换个试试？",
    "这项技能还没解锁，试试别的？",
)


@dataclass(frozen=True)
class ParamSpec:
    name: str
    type: Literal["enum", "int", "bool", "str"]
    required: bool = True
    enum: tuple[Any, ...] = ()
    minimum: int | None = None
    maximum: int | None = None
    unit: str = ""


@dataclass(frozen=True)
class RegistryEntry:
    action: str
    capability_id: str
    intent_type: IntentType
    delivery: Delivery
    params: tuple[ParamSpec, ...]
    risk_level: Literal["low", "medium", "high"]
    owner: str
    unsupported_behavior: str
    source_seed: str
    positive_examples: tuple[str, ...] = ()
    # ask_missing 时的话术(如 adjust_volume 问方向); 空 = 通用"请补充参数"。
    missing_reply: str = ""
    match: Callable[[str], bool] | None = None
    extract: Callable[[str], dict[str, Any]] | None = None


_COMMAND_ACK_TIMEOUT_MS = 3000
_COMMAND_RESULT_TIMEOUT_MS = 3000


@dataclass(frozen=True)
class SessionState:
    trace_id: str = "trace-g3-local"
    session_id: str = "sess-g3-local"
    caps: frozenset[str] = field(
        default_factory=lambda: frozenset({"audio", "text", "cmd", "state"})
    )
    interaction_mode: Literal["active", "sleeping", "closed"] = "active"
    engine_gate: Literal["open", "closed"] = "open"
    command_dry_run: bool = False
    robot_action_enabled: bool = True
    pending_high_risk: dict[str, Any] | None = None


@dataclass(frozen=True)
class IntentResult:
    intent_type: IntentType
    confidence: float
    route_reason: str
    raw_text: str
    delivery: Delivery | None = None
    candidate_action: str | None = None
    slots: dict[str, Any] = field(default_factory=dict)
    detected_actions: tuple[str, ...] = ()
    needs_validator: bool = False
    source_seed: str = ""


@dataclass(frozen=True)
class ValidationResult:
    decision: Decision
    intent: IntentResult
    entry: RegistryEntry | None = None
    reason: str = ""


# ── 确定性中文解析(matcher/extractor), 按 commands.yaml 里的名字挂载 ──────────

# 音量抱怨句(2026-09-16 用户实测"当前声音有点小"被反向调小):
# "有点小/太小了/小了点/变小了/听不清"是描述现状而非祈使——极性描述的是
# 现状, 期望动作与之相反(嫌小→调大)。祈使句("调小一点/再小点")极性即方向,
# 不命中这些模式, 仍走下面的字面判定。
_VOL_COMPLAINT_UP_RE = re.compile(
    r"(?:有点儿?|过于|太|好|蛮|挺)(?:小|低|轻)"  # 有点小 / 太小了 / 好小声 / 挺低的
    r"|听不[清见到]"  # 声音听不清(嫌小)
    r"|变(?:小|低|轻)了"  # 变小了(带"了"=状态已变; "变小一点"是祈使, 不入)
    r"|(?:小|低|轻)了(?:一点|一点儿|点|些)"  # 小了点 / 小了一点 / 小了一些
)
_VOL_COMPLAINT_DOWN_RE = re.compile(
    r"(?:有点儿?|过于|太|好|蛮|挺)(?:大|高|响)"  # 有点大 / 太大了 / 好大声 / 挺响的
    r"|变(?:大|高|响)了"  # 变大了
    r"|(?:大|高|响)了(?:一点|一点儿|点|些)"  # 大了点 / 大了一点 / 大了一些
)


def _match_volume(text: str) -> bool:
    if _is_question(text):
        return False
    if "音量" in text or "声音" in text or "静音" in text:
        return any(
            t in text for t in (
                "调", "大", "小", "最大", "最小", "高", "低", "轻", "响",
                "静音", "关", "听不清", "听不见", "听不到", "%", "百分",
            )
        )
    # 裸"好大声/好小声"抱怨(2026-09-16): 句中无"声音/音量"也进音量路由。
    # 仅限强度词抱怨命中(太/好/有点+大/小…), "别大声说话/大声唱歌"无强度词不误收。
    if "大声" in text or "小声" in text:
        return bool(_VOL_COMPLAINT_UP_RE.search(text)
                    or _VOL_COMPLAINT_DOWN_RE.search(text))
    return False


def _extract_volume(text: str) -> dict[str, Any]:
    # 方向不明("音量调一下"/"音量调到60%")返回空 → ask_missing,
    # 用 YAML 里的 missing_reply 问"要调大还是调小"。
    if "静音" in text or "关掉" in text or "关闭" in text or "关了" in text:
        return {"command": "mute"}
    if "最大" in text:
        return {"command": "max"}
    if "最小" in text:
        return {"command": "min"}
    # 抱怨句先于字面极性(2026-09-16): "有点小"=嫌小要调大, 与"调小一点"相反。
    if _VOL_COMPLAINT_UP_RE.search(text):
        return {"command": "up"}
    if _VOL_COMPLAINT_DOWN_RE.search(text):
        return {"command": "down"}
    if "大" in text or "高" in text or "打开" in text or "响一点" in text:
        return {"command": "up"}
    if "小" in text or "低" in text or "轻" in text:
        return {"command": "down"}
    return {}


# 口语拆分式拍照说法(2026-09-08 用户实测"拍个照"两道闸门全漏):
# "拍个照/拍张照/拍个相/来张照片/照个相"——"拍"和"照/相"中间隔了量词。
_PHOTO_SPLIT_RE = re.compile(
    r"拍[个一几张]*[照相]"  # 拍个照 / 拍张照 / 拍一个照 / 拍几张
    r"|来[个一几张]*照片?"  # 来一张照片 / 来张照片 / 来个照
    r"|照[个一几张]*相"  # 照个相 / 照一张相
)


def _match_photo(text: str) -> bool:
    if _is_question(text):
        return False
    if "拍照" in text or "照张相" in text or "照相" in text:
        # "拍照的地方/拍照的时候"是描述不是祈使。
        return re.search(r"(?:拍照|照张相|照相)的", text) is None
    # 拍 + 无"的"gap + 照/照片/张照/张相: "拍的照片"是描述, gap 有"的"就放走聊天。
    if re.search(r"拍[^的]{0,3}(?:照片?|张照|张相)", text):
        return True
    if _PHOTO_SPLIT_RE.search(text) is not None:
        return True
    return re.search(r"拍[0-9一两二三四五六七八九十]+张", text) is not None


def _extract_photo(text: str) -> dict[str, Any]:
    return {"mode": "on"}


def _match_battery(text: str) -> bool:
    # 查询天然是问句("还有多少电?"), 不能加 _is_question 闸门。
    # 2026-09-08 补口语变体: "还有电吗/没电了/续航多久"此前两道闸门全漏,
    # 掉进聊天兜底(用户观感"走了知识库")。裸"充电"只认状态问句语境,
    # 排除"怎么充电"类 how-to。
    if any(
        k in text
        for k in ("电量", "多少电", "电池", "电压", "电流", "充电状态", "还有电", "还剩电", "没电", "续航")
    ):
        return True
    return "充电" in text and not any(h in text for h in ("怎么", "如何", "为什么", "为啥"))


def _extract_battery(text: str) -> dict[str, Any]:
    if "电压" in text:
        kind = "vol"
    elif "电流" in text:
        kind = "cur"
    elif "充电" in text:
        kind = "chg"
    else:
        kind = "cap"
    # gauge_id 可选, 默认 1; 显式带上让帧自描述。
    gauge_id = 2 if ("2号" in text or "电池2" in text or "第二块" in text) else 1
    return {"type": kind, "gauge_id": gauge_id}


# matcher/extractor 注册名 → 函数。commands.yaml 的 matcher 字段引用这里的键;
# 引用了不存在的名字会在 load_registry() 里报错(fail loud, 启动期暴露配置错误)。
# music matcher 已随 2026-09-15 control_music 下线移除(本地音乐通道不再下发)。
_MATCHERS: dict[str, Callable[[str], bool]] = {
    "volume": _match_volume,
    "photo": _match_photo,
    "battery": _match_battery,
}
_EXTRACTORS: dict[str, Callable[[str], dict[str, Any]]] = {
    "volume": _extract_volume,
    "photo": _extract_photo,
    "battery": _extract_battery,
}

_DEFAULT_COMMANDS_YAML = Path(__file__).with_name("commands.yaml")


def load_registry(path: str | Path | None = None) -> tuple[RegistryEntry, ...]:
    """从 commands.yaml 构建命令注册表(唯一事实来源)。

    YAML 声明 action/参数/枚举/示例/风险级别; matcher/extractor 按名字挂载
    上面的确定性中文解析函数。文件缺失/为空/引用未知 matcher 时直接抛错,
    让配置错误在启动期暴露而不是运行期静默降级。
    """
    yaml_path = Path(path or os.getenv("XIAOGE_COMMANDS_YAML", "") or _DEFAULT_COMMANDS_YAML)
    try:
        raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(f"commands.yaml not found: {yaml_path}") from exc
    except yaml.YAMLError as exc:
        raise RuntimeError(f"commands.yaml parse error: {yaml_path}: {exc}") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("commands"), list):
        raise RuntimeError(f"commands.yaml must contain a 'commands' list: {yaml_path}")

    entries: list[RegistryEntry] = []
    for item in raw["commands"]:
        if not isinstance(item, dict) or "action" not in item:
            raise RuntimeError(f"commands.yaml: invalid command entry: {item!r}")
        action = str(item["action"])
        matcher_name = str(item.get("matcher") or "")
        extractor_name = str(item.get("extractor") or matcher_name)
        match = _MATCHERS.get(matcher_name) if matcher_name else None
        extract = _EXTRACTORS.get(extractor_name) if extractor_name else None
        if matcher_name and match is None:
            raise RuntimeError(f"commands.yaml: unknown matcher {matcher_name!r} for {action}")
        if extractor_name and extract is None:
            raise RuntimeError(f"commands.yaml: unknown extractor {extractor_name!r} for {action}")
        params = tuple(
            ParamSpec(
                name=str(p["name"]),
                type=str(p.get("type", "str")),
                required=bool(p.get("required", True)),
                enum=tuple(p.get("enum") or ()),
                minimum=p.get("minimum"),
                maximum=p.get("maximum"),
                unit=str(p.get("unit") or ""),
            )
            for p in item.get("params") or []
        )
        for spec in params:
            # YAML 1.1 会把裸 on/off/yes/no 解析成布尔——枚举值出现布尔
            # 几乎必然是这个坑(如 enum: [on]), 启动期直接报错。
            if any(isinstance(v, bool) for v in spec.enum):
                raise RuntimeError(
                    f"commands.yaml: enum of {action}.{spec.name} contains a boolean "
                    f"(YAML 1.1 parses bare on/off/yes/no as bool); "
                    f"quote the literal, e.g. enum: ['on']"
                )
        entries.append(
            RegistryEntry(
                action=action,
                capability_id=str(item.get("capability_id") or action),
                intent_type=str(item.get("intent_type") or "control_cmd"),  # type: ignore[arg-type]
                delivery=str(item.get("delivery") or "data.cmd"),  # type: ignore[arg-type]
                params=params,
                risk_level=str(item.get("risk_level") or "low"),  # type: ignore[arg-type]
                owner=str(item.get("owner") or "Cloud owner"),
                unsupported_behavior=str(item.get("unsupported_behavior") or "reply unsupported"),
                source_seed=str(item.get("source_seed") or f"commands.yaml:{action}"),
                positive_examples=tuple(item.get("examples") or ()),
                missing_reply=str(item.get("missing_reply") or ""),
                match=match,
                extract=extract,
            )
        )
    if not entries:
        raise RuntimeError(f"commands.yaml defines no commands: {yaml_path}")
    return tuple(entries)


def default_registry() -> tuple[RegistryEntry, ...]:
    """命令注册表视图(读取 commands.yaml; 路径可用 XIAOGE_COMMANDS_YAML 覆盖)。"""
    return load_registry()


class G3IntentEngine:
    def __init__(self, registry: Iterable[RegistryEntry] | None = None) -> None:
        self.registry = tuple(registry or default_registry())

    def handle_text(
        self,
        text: str,
        state: SessionState | None = None,
        *,
        utterance_id: str = "utt-g3-local",
        now_ms: int | None = None,
    ) -> list[dict[str, Any]]:
        state = state or SessionState()
        now_ms = int(time.time() * 1000) if now_ms is None else now_ms
        intent = self.route(text, state)
        validation = self.validate(intent, state)
        return self.build_outputs(validation, state, utterance_id=utterance_id, now_ms=now_ms)

    def route(self, text: str, state: SessionState | None = None) -> IntentResult:
        state = state or SessionState()
        normalized = _normalize_text(text)
        if state.interaction_mode in {"sleeping", "closed"} or state.engine_gate != "open":
            return IntentResult(
                intent_type="chat",
                confidence=1.0,
                route_reason="engine_gate_closed",
                raw_text=text,
                delivery="cloud_tool + data.reply",
            )

        if state.pending_high_risk is not None and _is_confirmation_cancel(normalized):
            return IntentResult(
                intent_type="control_cmd",
                confidence=1.0,
                route_reason="high_risk_cancel",
                raw_text=text,
                delivery="data.cmd after confirmation",
            )
        if state.pending_high_risk is not None and _is_confirmation_accept(normalized):
            return IntentResult(
                intent_type="control_cmd",
                confidence=1.0,
                route_reason="high_risk_confirm",
                raw_text=text,
                delivery="data.cmd after confirmation",
            )

        matched = self._matched_entries(normalized)
        control = [entry for entry in matched if entry.intent_type == "control_cmd"]
        referenced_control = [
            entry
            for entry in self._candidate_entries(normalized)
            if entry.intent_type == "control_cmd"
        ]
        non_execution = _non_execution_act(normalized)
        if (
            non_execution == "capability_query"
            and not _is_capability_query(normalized)
            and _match_battery(normalized)
        ):
            # 吗式电量状态问句("还有电吗/在充电吗")是查询本体, 不是能力问句:
            # 此前被 _is_plain_command_question 拦成 info_query 罐头回复,
            # 不产 data.cmd。放行落到 check_battery 匹配出真实查询帧。
            non_execution = None
        if non_execution is not None and (
            referenced_control
            or _has_command_relevant_marker(normalized)
            or _capability_action(normalized) != ""
        ):
            entry = (
                referenced_control[0]
                if referenced_control
                else self._entry(_capability_action(normalized))
            )
            return IntentResult(
                intent_type="info_query" if non_execution == "capability_query" else "chat",
                confidence=0.99,
                route_reason=non_execution,
                raw_text=text,
                delivery="cloud_tool + data.reply",
                candidate_action=entry.action if entry is not None else None,
                source_seed=entry.source_seed if entry is not None else "semantic_safety_gate",
            )
        if len(control) >= 2 or (len(control) == 1 and _has_multi_marker(normalized)):
            return IntentResult(
                intent_type="control_cmd_multi",
                confidence=0.98,
                route_reason="multi_command_gate",
                raw_text=text,
                delivery="ask_split only",
                detected_actions=tuple(entry.action for entry in control),
                source_seed="X3-SHEET-MULTI",
            )
        if len(control) == 1:
            entry = control[0]
            if _embedded_control(normalized):
                # 命令词嵌在谈论/罗列语境里（前后都有实质内容），不是祈使句。
                # 不做确定性执行，交给 LLM 聊天兜底。
                return IntentResult(
                    intent_type="chat",
                    confidence=0.6,
                    route_reason="chat_fallback",
                    raw_text=text,
                    delivery="cloud_tool + data.reply",
                    candidate_action=entry.action,
                    source_seed="embedded_control_divert",
                )
            slots = entry.extract(normalized) if entry.extract else {}
            return IntentResult(
                intent_type="control_cmd",
                confidence=0.92,
                route_reason="registry_exact",
                raw_text=text,
                delivery=entry.delivery,
                candidate_action=entry.action,
                slots=slots,
                needs_validator=True,
                source_seed=entry.source_seed,
            )

        reply_only = [
            entry for entry in matched if entry.intent_type in {"info_query", "knowledge_qa"}
        ]
        if reply_only:
            entry = reply_only[0]
            return IntentResult(
                intent_type=entry.intent_type,
                confidence=0.86,
                route_reason="x3_reply_only",
                raw_text=text,
                delivery=entry.delivery,
                candidate_action=entry.action,
                slots=entry.extract(normalized) if entry.extract else {},
                source_seed=entry.source_seed,
            )

        if _looks_like_knowledge(normalized) and not _embedded_control(normalized):
            return IntentResult(
                intent_type="knowledge_qa",
                confidence=0.86,
                route_reason="knowledge_route",
                raw_text=text,
                delivery="cloud_knowledge + data.reply",
            )
        return IntentResult(
            intent_type="chat",
            confidence=0.5,
            route_reason="chat_fallback",
            raw_text=text,
            delivery="cloud_tool + data.reply",
        )

    def validate(self, intent: IntentResult, state: SessionState | None = None) -> ValidationResult:
        state = state or SessionState()
        if intent.route_reason == "engine_gate_closed":
            return ValidationResult("reject_policy", intent, reason="engine gate closed")
        if intent.intent_type == "control_cmd_multi":
            return ValidationResult("ask_clarify", intent, reason="multi command blocked")
        if intent.intent_type in {"chat", "info_query", "knowledge_qa"}:
            return ValidationResult(
                "accept", intent, self._entry(intent.candidate_action or ""), "reply only"
            )
        if intent.route_reason == "high_risk_cancel":
            return ValidationResult("cancel", intent, reason="high risk canceled")
        if intent.route_reason == "high_risk_confirm":
            if state.pending_high_risk is None:
                return ValidationResult(
                    "ask_clarify", intent, reason="no pending high risk command"
                )
            pending_action = str(state.pending_high_risk.get("action", ""))
            pending_entry = self._entry(pending_action)
            pending_slots = state.pending_high_risk.get("slots", {})
            if not isinstance(pending_slots, dict):
                pending_slots = {}
            pending_intent = IntentResult(
                intent_type="control_cmd",
                confidence=float(state.pending_high_risk.get("confidence", 1.0)),
                route_reason="high_risk_confirm",
                raw_text=str(state.pending_high_risk.get("raw_text", intent.raw_text)),
                delivery=pending_entry.delivery if pending_entry is not None else intent.delivery,
                candidate_action=pending_action,
                slots=dict(pending_slots),
                needs_validator=True,
                source_seed=str(state.pending_high_risk.get("source_seed", "pending_high_risk")),
            )
            if pending_entry is None:
                return ValidationResult("ask_clarify", pending_intent, reason="unknown pending action")
            invalid = _invalid_param_reason(pending_intent.slots, pending_entry)
            if invalid:
                return ValidationResult("reject_policy", pending_intent, pending_entry, invalid)
            return ValidationResult("accept", pending_intent, pending_entry, "confirmed")

        entry = self._entry(intent.candidate_action or "")
        if entry is None:
            return ValidationResult("ask_clarify", intent, reason="unknown action")
        if entry.delivery not in EXECUTABLE_DELIVERIES:
            return ValidationResult("accept", intent, entry, "reply-only delivery")
        if "cmd" not in state.caps:
            return ValidationResult("reject_capability", intent, entry, "cmd cap not granted")

        missing = [
            spec.name for spec in entry.params if spec.required and spec.name not in intent.slots
        ]
        if missing:
            return ValidationResult("ask_missing", intent, entry, f"missing: {','.join(missing)}")
        invalid = _invalid_param_reason(intent.slots, entry)
        if invalid:
            return ValidationResult("reject_policy", intent, entry, invalid)
        if entry.risk_level == "high" and state.pending_high_risk is None:
            return ValidationResult("ask_confirm", intent, entry, "high risk requires confirmation")
        return ValidationResult("accept", intent, entry, "validated")

    def build_outputs(
        self,
        result: ValidationResult,
        state: SessionState | None = None,
        *,
        utterance_id: str,
        now_ms: int,
    ) -> list[dict[str, Any]]:
        state = state or SessionState()
        base = {
            "trace_id": state.trace_id,
            "session_id": state.session_id,
            "utterance_id": utterance_id,
            "ts_ms": now_ms,
        }
        intent = result.intent

        if (
            result.decision == "accept"
            and intent.intent_type == "control_cmd"
            and result.entry
            and result.entry.delivery in EXECUTABLE_DELIVERIES
        ):
            return [
                {
                    "type": "data.cmd",
                    "trace_id": state.trace_id,
                    "session_id": state.session_id,
                    "utterance_id": utterance_id,
                    "cmd_id": _cmd_id(state.trace_id, utterance_id, now_ms),
                    "capability_id": result.entry.capability_id,
                    "action": result.entry.action,
                    "params": dict(intent.slots),
                    "risk_level": result.entry.risk_level,
                    "ack_timeout_ms": _COMMAND_ACK_TIMEOUT_MS,
                    "result_timeout_ms": _COMMAND_RESULT_TIMEOUT_MS,
                    "issued_at_ms": now_ms,
                }
            ]
        if result.decision == "ask_confirm":
            return [_reply(base, "control_cmd", "这个操作风险较高，请先确认是否执行。", "ack")]
        if result.decision == "ask_missing":
            # 命令专属追问(如 adjust_volume 问方向)优先, 否则通用话术。
            text = (
                result.entry.missing_reply
                if result.entry is not None and result.entry.missing_reply
                else "我还缺少必要参数，请补充后再说一遍。"
            )
            return [_reply(base, "control_cmd", text, "ack")]
        if result.decision == "reject_capability":
            return [_reply(base, "control_cmd", "当前设备没有授权这个控制能力。", "ack")]
        if result.decision == "reject_policy":
            return [_reply(base, "system", "当前状态不允许执行这个请求。", "ack")]
        if result.decision == "cancel":
            return [_reply(base, "control_cmd", "已取消，不会下发控制指令。", "ack")]
        if intent.route_reason == "capability_query":
            available = result.entry is not None and "cmd" in state.caps
            # 2026-09-02 用户反馈"可以，这项能力当前可用。"太生硬: 自然模板轮换(B 方案, 不走 LLM)。
            text = random.choice(
                _CAPABILITY_REPLIES if available else _CAPABILITY_MISSING_REPLIES
            )
            return [_reply(base, "info_query", text, "final_only")]
        if intent.route_reason in {
            "prohibit",
            "hypothetical",
            "future_plan",
            "quotation",
        }:
            return [
                _reply(base, "chat", "明白，我不会把这句话当成现在要执行的指令。", "final_only")
            ]
        if intent.intent_type == "control_cmd_multi":
            return [
                _reply(
                    base,
                    "control_cmd",
                    "我听到了多个操作，请拆成两句，或告诉我先执行哪一个。",
                    "ack",
                )
            ]
        if intent.intent_type == "knowledge_qa":
            # 知识问句不再走确定性 RAG 直答(标题+首句拼接结构死板, 回复也不进
            # 对话历史); 不产帧, 落到 LLM 聊天, 由其按人设自然调 query_knowledge。
            return []
        if intent.intent_type == "info_query":
            # 电量查询已改为 check_battery 真实 data.cmd 查询(见 commands.yaml);
            # 其余信息类问题没有确定性设备状态源, 不产帧, 落到 LLM 聊天兜底。
            return []
        return [
            _reply(base, "chat", "我可以聊天，也可以在授权范围内处理单条控制指令。", "final_only")
        ]

    def function_call_output(
        self, name: str, payload: dict[str, Any], state: SessionState | None = None
    ) -> ValidationResult:
        """Validate an internal function-call result before any WSS frame is built."""

        state = state or SessionState()
        if name in {"execute_command", "send_data_cmd", "set_robot_state"}:
            intent = IntentResult(
                intent_type="control_cmd",
                confidence=0.0,
                route_reason="forbidden_function_call",
                raw_text=str(payload),
            )
            return ValidationResult("reject_policy", intent, reason="function cannot send command")
        if name == "parse_command_slots":
            intent = IntentResult(
                intent_type="control_cmd",
                confidence=float(payload.get("confidence", 0.0)),
                route_reason="function_parse_command_slots",
                raw_text=str(payload.get("raw_text", "")),
                delivery=str(payload.get("delivery", "data.cmd")),  # type: ignore[arg-type]
                candidate_action=str(payload.get("action", "")),
                slots=dict(payload.get("slots", {})),
                needs_validator=True,
                source_seed=str(payload.get("source_seed", "")),
            )
            return self.validate(intent, state)
        intent = IntentResult(
            intent_type="chat",
            confidence=0.0,
            route_reason="reply_only_function_call",
            raw_text=str(payload),
            delivery="cloud_tool + data.reply",
        )
        return ValidationResult("accept", intent, reason="reply-only function")

    def semantic_candidate(
        self,
        *,
        raw_text: str,
        action: str,
        slots: dict[str, Any],
        confidence: float,
        state: SessionState | None = None,
    ) -> ValidationResult:
        """Validate an untrusted semantic candidate through the command authority."""
        entry = self._entry(action)
        payload = {
            "raw_text": raw_text,
            "action": action,
            "delivery": entry.delivery if entry is not None else "data.cmd",
            "slots": slots,
            "confidence": confidence,
            "source_seed": "semantic_router",
        }
        result = self.function_call_output("parse_command_slots", payload, state)
        if entry is None:
            return result
        unexpected = set(slots).difference(spec.name for spec in entry.params)
        if unexpected:
            names = ",".join(sorted(unexpected))
            return ValidationResult(
                "reject_policy", result.intent, entry, f"unexpected slots: {names}"
            )
        return result

    def command_relevant(self, text: str) -> bool:
        """Return whether unresolved text is worth the semantic-control fallback."""
        normalized = _normalize_text(text)
        return bool(self._candidate_entries(normalized)) or _has_command_relevant_marker(normalized)

    def action_catalog(self) -> tuple[RegistryEntry, ...]:
        """Expose the current allow-list to side-effect-free semantic classification."""
        return tuple(
            entry
            for entry in self.registry
            if entry.intent_type == "control_cmd" and entry.delivery in EXECUTABLE_DELIVERIES
        )

    def _entry(self, action: str) -> RegistryEntry | None:
        return next((entry for entry in self.registry if entry.action == action), None)

    def _matched_entries(self, normalized: str) -> list[RegistryEntry]:
        return [
            entry for entry in self.registry if entry.match is not None and entry.match(normalized)
        ]

    def _candidate_entries(self, normalized: str) -> list[RegistryEntry]:
        reference = _reference_text(normalized)
        return [
            entry
            for entry in self.registry
            if (entry.match is not None and entry.match(reference))
            or any(_normalize_text(example) in normalized for example in entry.positive_examples)
        ]


def _reply(base: dict[str, Any], intent_type: str, text: str, speak_policy: str) -> dict[str, Any]:
    return {
        "type": "data.reply",
        **base,
        "intent_type": intent_type,
        "text": text,
        "speak_policy": speak_policy,
    }


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", "", text.strip().lower())


def _reference_text(text: str) -> str:
    return re.sub(r"[吗呢？?]+$", "", text)


def _has_command_relevant_marker(text: str) -> bool:
    return any(marker in text for marker in _COMMAND_RELEVANT_MARKERS)


def _capability_action(text: str) -> str:
    # 与 commands.yaml 三条命令对齐: 能力问句("你能调大音量吗")指向注册的动作,
    # 供非执行闸门给出自然的能力答复。
    # 音乐能力问句不再指向 control_music(2026-09-15 下线): 云端放歌仍可用,
    # 留在注册表会让"你能放音乐吗"落到 _CAPABILITY_MISSING_REPLIES"我还不会"
    # —— 删除该分支让音乐能力问句落到聊天 LLM 自然回答。
    if "音量" in text or "声音" in text or "静音" in text:
        return "adjust_volume"
    if any(k in text for k in ("电量", "电池", "多少电", "电压", "电流", "还有电", "没电", "续航")):
        return "check_battery"
    if "拍照" in text or "照片" in text or "照相" in text:
        return "take_photo"
    return ""


def _non_execution_act(text: str) -> NonExecutionAct | None:
    if _is_quotation(text):
        return "quotation"
    if any(marker in text for marker in _PROHIBIT_MARKERS):
        return "prohibit"
    if any(marker in text for marker in _HYPOTHETICAL_MARKERS):
        return "hypothetical"
    if any(marker in text for marker in _FUTURE_MARKERS):
        return "future_plan"
    if _is_capability_query(text) or _is_plain_command_question(text):
        return "capability_query"
    return None


def _is_plain_command_question(text: str) -> bool:
    """Treat bare command-shaped yes/no questions as non-executable."""
    return text.endswith(("吗", "呢", "？", "?"))


def _is_quotation(text: str) -> bool:
    if any(marker in text for marker in _QUOTATION_MARKERS):
        return True
    return bool(re.search(r"[‘'\"](?:[^’'\"]+)[’'\"]", text))


def _is_capability_query(text: str) -> bool:
    if not _is_question(text):
        return False
    if any(marker in text for marker in _POLITE_EXECUTE_MARKERS):
        return False
    return bool(
        re.search(r"(?:你|小歌)?(?:能|会|可以|支持|能否|可不可以|会不会).*(?:吗|呢|？|\?)$", text)
    )


def _is_question(text: str) -> bool:
    return any(word in text for word in _ASK_WORDS) or text.endswith(("吗", "呢", "？", "?"))


def _invalid_param_reason(slots: dict[str, Any], entry: RegistryEntry) -> str:
    for spec in entry.params:
        if spec.name not in slots:
            continue
        value = slots[spec.name]
        if spec.type == "enum" and value not in spec.enum:
            return f"{spec.name} enum invalid"
        if spec.type == "bool" and not isinstance(value, bool):
            return f"{spec.name} type invalid"
        if spec.type == "str" and not isinstance(value, str):
            return f"{spec.name} type invalid"
        if spec.type == "int":
            if isinstance(value, bool) or not isinstance(value, int):
                return f"{spec.name} type invalid"
            if spec.minimum is not None and value < spec.minimum:
                return f"{spec.name} lower than minimum"
            if spec.maximum is not None and value > spec.maximum:
                return f"{spec.name} greater than maximum"
    return ""


def _has_multi_marker(text: str) -> bool:
    return any(marker in text for marker in _MULTI_MARKERS)


# 控制命令的"锚点词"：用于定位命令词在句中的位置，判断是否被谈论/罗列。
# 覆盖各 control matcher 的关键词（与 commands.yaml 对齐）。
# 音乐锚点已随 2026-09-15 control_music 下线移除: 音乐话术终点是无帧→LLM 聊天,
# 少了锚点只影响路由标签(knowledge_qa/chat_fallback), 两者用户不可见。
_CONTROL_ANCHORS: tuple[str, ...] = (
    # adjust_volume
    "音量", "声音", "静音",
    # take_photo (含"拍个照"拆分式, 与 _PHOTO_SPLIT_RE 对齐)
    "拍照", "照片", "照相", "照张相", "个照", "个相", "张照", "张相",
    # check_battery (含"还有电/没电"口语变体)
    "电量", "电池", "电压", "电流", "多少电", "充电状态", "还有电", "还剩电", "没电", "续航",
)

# 祈使句允许的礼貌/称呼前缀：剥掉这些之后剩下的才算"实质内容"
_POLITE_LEADS: tuple[str, ...] = (
    "请", "请你", "请帮我", "帮我", "帮忙", "麻烦你", "麻烦", "给我",
    "你能", "你能帮", "你可以", "可以", "能不能", "小歌", "你好", "哎", "喂",
)

# 命令词之后允许出现的参数/语气尾巴（从句尾往前剥）
_PARAM_SUFFIX = re.compile(
    r"[0-9一二两三四五六七八九十百千万半点米厘公分步度%‰大小高低快慢"
    r"左右前后上下吧了呀啊哦嘛呢吗呗，。！？、,!?~～\s]+$"
)


def _strip_polite_lead(text: str) -> str:
    changed = True
    while changed and text:
        changed = False
        for lead in _POLITE_LEADS:
            if text.startswith(lead):
                text = text[len(lead) :]
                changed = True
        if text and text[0] in "，。！？、,!?~～ \t":
            text = text[1:]
            changed = True
    return text


def _strip_param_tail(text: str) -> str:
    while True:
        stripped = _PARAM_SUFFIX.sub("", text)
        if stripped == text:
            return stripped
        text = stripped


def _embedded_control(text: str) -> bool:
    """命令词被"谈论/罗列"而非"下达"：前后都剩实质内容时返回 True。

    只拦双侧嵌入（如"我们在讨论……有向前走向后走，你觉得还差哪些"）：
    命令词前面有话题、后面还有描述/追问，语言学上是对动作的引用而不是
    祈使句，应交给 LLM 聊天兜底。单侧内容（"请你往前走"、"我是说往前走"、
    "往前走两米然后停下"）不算嵌入，保持原有确定性路由不变。
    """
    positions = [(text.find(k), len(k)) for k in _CONTROL_ANCHORS if k in text]
    if not positions:
        return False
    first = min(pos for pos, _ in positions)
    last = max(pos + size for pos, size in positions)
    lead = _strip_polite_lead(text[:first])
    tail = _strip_param_tail(text[last:])
    # "除了"虽只有两个字，但引出的是对比/排除话题（"除了向前走你还会干啥"），
    # 后面必然还有内容，视为话题前置。
    lead_substantive = len(lead) >= 3 or lead.endswith("除了")
    return lead_substantive and len(tail) >= 3


def _looks_like_knowledge(text: str) -> bool:
    """Mark short product/小歌 knowledge questions as knowledge_qa.

    The knowledge_qa intent emits no frames (see build_outputs) and falls
    through to the LLM chat fallback, which answers via the query_knowledge
    tool per persona. Broad open-domain phrases like “什么是/为什么/介绍一下杭州”
    should stay in normal chat so G3 does not answer with a fixed fake-RAG
    template.
    """

    if len(text) > 30:
        # 长句基本是闲聊, 只是不巧同时带出"机器人/设备"和"功能/价格"等词
        # (2026-09-08 会话实测三例 52~400 字闲聊全被误判进知识路径);
        # 短句才可能是正经知识问句, 超长一律落回聊天。
        return False
    product_subject = any(
        k in text for k in ("小歌", "产品", "设备", "机器人", "说明书", "手册")
    )
    org_subject = any(k in text for k in ("公司", "企业", "智元", "厂商", "厂家", "品牌"))
    knowledge_topic = any(
        k in text
        for k in (
            "知识",
            "功能",
            "能力",
            "本事",
            "硬件",
            "规格",
            "参数",
            "价格",
            "地址",
            "网络要求",
            "升级",
            "隐私",
            "使用场景",
            "全双工",
        )
    )
    if (product_subject or org_subject) and knowledge_topic:
        return True
    return "全双工" in text and any(k in text for k in ("小歌", "语音", "通话", "对话"))


def _is_confirmation_accept(text: str) -> bool:
    return text in _AFFIRMATIVE_CONFIRM


def _is_confirmation_cancel(text: str) -> bool:
    return text in _CANCEL_CONFIRM


def _cmd_id(trace_id: str, utterance_id: str, now_ms: int) -> str:
    suffix = abs(hash((trace_id, utterance_id, now_ms))) % 1_000_000
    return f"cmd-g3-{suffix:06d}"
