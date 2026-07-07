"""结构化事件时间线(自动化测试 P0 的数据基座)。

订阅 AgentSession 的框架事件 + 提供 emit() 供自定义埋点(KWS/在线打断/停止词等)调用,
统一写成 JSONL(每行一个事件),带双时钟(monotonic 排序 + wall 关联),落到 run 目录。

硬约束(必须遵守):**测试功能不得影响/阻塞正常流程**。本模块据此设计:
  1. **默认不启用**——只有显式开启(AGENT_TIMELINE=1)才会创建与 attach,正常运行零开销;
  2. **绝不阻塞事件循环**——emit() 只做非阻塞入队,磁盘 I/O 全部在后台线程;队列满则丢弃
     并计数(宁丢日志不卡主流程);
  3. **绝不把异常抛回框架**——每个事件处理器与写盘都 try/except 兜底。
纯旁路只读,不修改任何会话状态。
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import queue
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("event-timeline")


def install_debug_log(run_dir: Path) -> tuple[Any, Any, Any]:
    """把全量 DEBUG 日志整合进测试 run 目录(取代旧的 always-on .run/agent.log)。

    用 QueueHandler + QueueListener:日志线程只入队,真正写盘在 listener 线程,
    **非阻塞**。返回 (listener, queue_handler, file_handler) 供 remove_debug_log 收尾。
    只在测试模式下调用,正常运行不挂任何文件日志处理器。
    """
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(run_dir / "debug.log", mode="w", encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    log_queue: queue.SimpleQueue[Any] = queue.SimpleQueue()
    queue_handler = logging.handlers.QueueHandler(log_queue)
    queue_handler.setLevel(logging.DEBUG)
    listener = logging.handlers.QueueListener(log_queue, file_handler, respect_handler_level=True)
    listener.start()
    root = logging.getLogger()
    root.addHandler(queue_handler)
    if root.level == logging.NOTSET or root.level > logging.DEBUG:
        root.setLevel(logging.DEBUG)
    return listener, queue_handler, file_handler


async def remove_debug_log(state: tuple[Any, Any, Any]) -> None:
    """关闭测试 DEBUG 日志:摘掉 handler、停 listener(刷盘)、关文件。不阻塞事件循环。"""
    listener, queue_handler, file_handler = state
    try:
        logging.getLogger().removeHandler(queue_handler)
    except Exception:
        pass
    try:
        import asyncio

        await asyncio.to_thread(listener.stop)
    except Exception:
        pass
    try:
        file_handler.close()
    except Exception:
        pass


def _now_us() -> int:
    return time.monotonic_ns() // 1_000


def _wall_us() -> int:
    return time.time_ns() // 1_000


def _as_dict(metrics: Any) -> dict[str, Any]:
    """把 item.metrics(dict 或 dict-like)安全转成 JSON 可序列化的浅字典。"""
    if not metrics:
        return {}
    try:
        out: dict[str, Any] = {}
        for k, v in dict(metrics).items():
            out[str(k)] = v if isinstance(v, (int, float, str, bool, type(None))) else str(v)
        return out
    except Exception:
        return {}


class EventTimeline:
    """订阅 session 事件 + emit() 自定义事件,后台线程写 <run_dir>/timeline.jsonl。"""

    def __init__(self, run_dir: Path, *, level: str = "debug", queue_size: int = 10_000) -> None:
        # level: "debug"=全事件(现状);"audit"=轮次级白名单(含对话文本,不落高频调试事件)。
        self._level = level
        self._dir = Path(run_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path = self._dir / "timeline.jsonl"
        self._fp = self._path.open("a", encoding="utf-8")
        self._seq = 0
        self._dropped = 0
        # 有界队列:满了丢弃而不是阻塞(硬约束:绝不卡主流程)。
        self._q: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=queue_size)
        self._thread = threading.Thread(target=self._writer, name="event-timeline", daemon=True)
        self._thread.start()

    @property
    def directory(self) -> Path:
        return self._dir

    def _writer(self) -> None:
        """后台线程:把所有磁盘 I/O 挪出事件循环。"""
        while True:
            item = self._q.get()
            if item is None:
                break
            try:
                self._fp.write(json.dumps(item, ensure_ascii=False) + "\n")
                self._fp.flush()
            except Exception:
                pass
        try:
            self._fp.close()
        except Exception:
            pass

    def emit(
        self,
        type: str,
        payload: dict[str, Any] | None = None,
        *,
        turn_id: str | None = None,
        source: str = "app",
    ) -> None:
        """在事件循环线程调用:只构造字典 + 非阻塞入队,绝不阻塞、绝不抛出。"""
        try:
            if self._level == "audit":
                from app.record_settings import audit_allows

                if not audit_allows(type):
                    return  # audit 档只落白名单事件(轮次/打断/错误/生命周期)
            self._seq += 1  # emit 只在单线程事件循环上调用,无需加锁
            event = {
                "eventId": f"evt_{self._seq:06d}",
                "type": type,
                "atUs": _now_us(),
                "wallUs": _wall_us(),
                "turnId": turn_id,
                "source": source,
                "payload": payload or {},
            }
            self._q.put_nowait(event)
        except queue.Full:
            self._dropped += 1
        except Exception:
            pass

    def _attach_high_freq(self, session: Any) -> None:
        """debug 档才订阅的高频事件(状态翻转 + asr.interim/final);audit 档跳过。"""

        @session.on("agent_state_changed")
        def _on_agent_state(ev: Any) -> None:
            try:
                self.emit(
                    "agent_state.changed",
                    {"old": getattr(ev, "old_state", None), "new": getattr(ev, "new_state", None)},
                    source="session",
                )
            except Exception:
                pass

        @session.on("user_state_changed")
        def _on_user_state(ev: Any) -> None:
            try:
                self.emit(
                    "user_state.changed",
                    {"old": getattr(ev, "old_state", None), "new": getattr(ev, "new_state", None)},
                    source="session",
                )
            except Exception:
                pass

        @session.on("user_input_transcribed")
        def _on_transcribed(ev: Any) -> None:
            try:
                is_final = getattr(ev, "is_final", True)
                self.emit(
                    "asr.final" if is_final else "asr.interim",
                    {"text": getattr(ev, "transcript", "")},
                    source="stt",
                )
            except Exception:
                pass

    def attach(self, session: Any) -> None:
        """挂到 AgentSession 的框架事件上(与现有日志处理器并存,纯增量、各自 try/except)。

        audit 档**按档跳过高频订阅**(agent/user 状态翻转、asr.interim/final)=零成本,
        只订阅轮次级 conversation_item_added(turn.*,白名单内)。"""
        if self._level != "audit":  # debug 档:全量订阅(现状)
            self._attach_high_freq(session)

        @session.on("conversation_item_added")
        def _on_item(ev: Any) -> None:
            try:
                item = getattr(ev, "item", None)
                role = getattr(item, "role", None)
                if role not in ("user", "assistant"):
                    return
                self.emit(
                    f"turn.{role}",
                    {
                        "text": getattr(item, "text_content", None),
                        "metrics": _as_dict(getattr(item, "metrics", None)),
                    },
                    source="session",
                )
            except Exception:
                pass

    async def aclose(self) -> None:
        """优雅收尾:停后台线程并把剩余事件刷盘;join 放到线程池,不阻塞事件循环。"""
        try:
            if self._dropped:
                logger.warning("event timeline dropped %d events (queue full)", self._dropped)
            self.emit("timeline.closed", {"dropped": self._dropped}, source="app")
            self._q.put_nowait(None)
        except Exception:
            pass
        try:
            import asyncio

            await asyncio.to_thread(self._thread.join, 2.0)
        except Exception:
            pass
