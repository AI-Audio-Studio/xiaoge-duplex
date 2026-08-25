"""Side-effect-free structured semantic fallback for unresolved robot-control turns."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from livekit.agents.llm import ChatContext
from livekit.agents.types import APIConnectOptions

from .config_utils import env_bool
from .g3_intent import RegistryEntry

logger = logging.getLogger("web-ui-agent")

SpeechAct = Literal[
    "execute",
    "capability_query",
    "state_query",
    "prohibit",
    "hypothetical",
    "future_plan",
    "quotation",
    "chat",
    "unknown",
]
Domain = Literal[
    "robot_control",
    "media_tool",
    "cloud_tool",
    "knowledge",
    "chat",
    "unknown",
]
AnswerMode = Literal["execute", "reply", "clarify", "delegate"]
RouterMode = Literal["off", "shadow", "enforce"]


class SemanticDecision(BaseModel):
    """Strict, data-only classification returned by the semantic model."""

    model_config = ConfigDict(extra="forbid", strict=True)

    speech_act: SpeechAct
    domain: Domain
    action: str | None = None
    slots: dict[str, str | int | bool] = Field(default_factory=dict)
    confidence: float = Field(ge=0.0, le=1.0)
    ambiguous: bool
    answer_mode: AnswerMode


@dataclass(frozen=True)
class SemanticRouterConfig:
    enabled: bool = True
    mode: RouterMode = "enforce"
    timeout_s: float = 1.2
    confidence_threshold: float = 0.86

    @classmethod
    def from_env(cls) -> SemanticRouterConfig:
        mode_value = os.getenv("XIAOGE_INTENT_MODE", "enforce").strip().lower()
        mode: RouterMode = mode_value if mode_value in {"off", "shadow", "enforce"} else "off"  # type: ignore[assignment]
        try:
            timeout_s = max(0.1, float(os.getenv("XIAOGE_INTENT_TIMEOUT_S", "1.2")))
            confidence = min(1.0, max(0.0, float(os.getenv("XIAOGE_INTENT_CONFIDENCE", "0.86"))))
        except ValueError:
            logger.warning("invalid semantic router numeric configuration; using defaults")
            timeout_s, confidence = 1.2, 0.86
        return cls(
            enabled=env_bool("XIAOGE_INTENT_ENABLE", True),
            mode=mode,
            timeout_s=timeout_s,
            confidence_threshold=confidence,
        )


@dataclass(frozen=True)
class SemanticRoute:
    decision: SemanticDecision | None
    status: Literal["accepted", "non_execute", "low_confidence", "invalid", "timeout", "error"]
    reason: str = ""

    @property
    def is_execution_candidate(self) -> bool:
        return self.status == "accepted" and self.decision is not None


class SemanticLLM(Protocol):
    def chat(self, **kwargs: Any) -> Any: ...


class SemanticRouter:
    """Classify text without tools; all proposed commands still require G3 validation."""

    def __init__(
        self,
        llm: SemanticLLM,
        registry: tuple[RegistryEntry, ...],
        config: SemanticRouterConfig | None = None,
    ) -> None:
        self._llm = llm
        self._registry = registry
        self.config = config or SemanticRouterConfig.from_env()
        self._actions = frozenset(entry.action for entry in registry)
        self._slot_names = {
            entry.action: frozenset(spec.name for spec in entry.params) for entry in registry
        }
        self._system_prompt = _build_system_prompt(registry)

    @property
    def enabled(self) -> bool:
        return self.config.enabled and self.config.mode != "off"

    async def route(self, text: str) -> SemanticRoute:
        if not self.enabled:
            return SemanticRoute(None, "invalid", "router disabled")
        try:
            raw = await asyncio.wait_for(self._collect(text), timeout=self.config.timeout_s)
        except TimeoutError:
            logger.warning("semantic router timeout")
            return SemanticRoute(None, "timeout", "classification timeout")
        except Exception as exc:
            logger.warning("semantic router request failed: %s", exc)
            return SemanticRoute(None, "error", type(exc).__name__)
        try:
            decision = SemanticDecision.model_validate_json(raw)
        except Exception as exc:
            logger.warning("semantic router invalid response: %s", exc)
            return SemanticRoute(None, "invalid", "schema validation failed")
        return self._classify(decision)

    async def _collect(self, text: str) -> str:
        chat_ctx = ChatContext.empty()
        chat_ctx.add_message(role="system", content=self._system_prompt)
        chat_ctx.add_message(
            role="user",
            content=json.dumps({"utterance": text}, ensure_ascii=False),
        )
        async with self._llm.chat(
            chat_ctx=chat_ctx,
            tools=[],
            response_format=SemanticDecision,
            conn_options=APIConnectOptions(
                max_retry=0,
                retry_interval=0.0,
                timeout=self.config.timeout_s,
            ),
        ) as stream:
            response = await stream.collect()
        return str(response.text)

    def _classify(self, decision: SemanticDecision) -> SemanticRoute:
        if decision.speech_act != "execute" or decision.domain != "robot_control":
            return SemanticRoute(decision, "non_execute", "not an immediate robot command")
        if decision.ambiguous or decision.confidence < self.config.confidence_threshold:
            return SemanticRoute(decision, "low_confidence", "ambiguous or below threshold")
        if not decision.action or decision.action not in self._actions:
            return SemanticRoute(decision, "invalid", "action is not registered")
        unexpected = set(decision.slots).difference(self._slot_names[decision.action])
        if unexpected:
            return SemanticRoute(decision, "invalid", "slots are not registered for action")
        if decision.answer_mode != "execute":
            return SemanticRoute(decision, "invalid", "answer mode conflicts with execution")
        return SemanticRoute(decision, "accepted", "candidate requires deterministic validation")


def _build_system_prompt(registry: tuple[RegistryEntry, ...]) -> str:
    catalog = [_catalog_item(entry) for entry in registry]
    return (
        "你是无副作用的语义分类器，只输出给定 JSON Schema。不得调用工具、执行动作或生成协议帧。"
        "区分‘现在执行’与能力询问、否定禁止、假设、未来计划、引用转述、状态查询和聊天。"
        "只有用户明确要求机器人现在执行一个动作时 speech_act=execute、domain=robot_control、"
        "answer_mode=execute。礼貌命令如‘请/帮我向前走’仍是 execute；‘你能向前走吗’是"
        "capability_query；‘不要向前走’是 prohibit。无法确定时 ambiguous=true 并选择 clarify。"
        "action 必须来自目录，slots 只填目录定义的参数，不得猜测缺失参数。动作目录："
        + json.dumps(catalog, ensure_ascii=False, separators=(",", ":"))
    )


def _catalog_item(entry: RegistryEntry) -> dict[str, Any]:
    return {
        "action": entry.action,
        "params": [
            {
                "name": spec.name,
                "type": spec.type,
                "required": spec.required,
                "enum": list(spec.enum),
                "minimum": spec.minimum,
                "maximum": spec.maximum,
                "unit": spec.unit,
            }
            for spec in entry.params
        ],
        "examples": list(entry.positive_examples[:3]),
    }
