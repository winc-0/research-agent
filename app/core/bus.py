"""
运行期事件总线。

节点自己不知道事件往哪送——它只调用 `emit()`，由外层（Web 服务）在
当前 asyncio 上下文里注入一个 sink。这样同一套图既能跑 CLI（sink 为空，
零开销），也能跑 Web（sink 往队列里写），不需要为 Web 复刻一份带回调的图。

用 ContextVar 而不是全局变量：并发跑多个会话时，各自的 sink 互不串台。
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from typing import Callable

from app.core.log import logger

EventSink = Callable[[str, dict], None]

_sink: ContextVar[EventSink | None] = ContextVar("zhixun_event_sink", default=None)


def set_sink(sink: EventSink) -> Token:
    """在进入图之前注入 sink，返回的 token 用于恢复。"""
    return _sink.set(sink)


def reset_sink(token: Token) -> None:
    _sink.reset(token)


def emit(event: str, data: dict | None = None) -> None:
    """
    发一条事件。

    事件是**观测手段**，不是业务逻辑：sink 抛错必须吞掉，
    否则前端一个 WebSocket 断连能把整条研究链路带崩。
    """
    sink = _sink.get()
    if sink is None:
        return
    try:
        sink(event, data or {})
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[bus] 事件发送失败（已忽略）：{type(e).__name__}: {e}")
