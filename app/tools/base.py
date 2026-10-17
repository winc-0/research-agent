"""
工具的统一契约。

让子 agent 直接返回拼好的字符串，拿到手里没法去重、
没法编号引用、也没法评测「这句话到底有没有证据支持」。
这里规定：每路工具必须返回结构化的 `Evidence` 列表，
后续汇聚、压缩、审查、引用全部建立在这个结构上。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Evidence:
    """
    一条证据卡片。

    :param source: 来源路由，`web` / `kb` / `sql`
    :param claim: 这条证据支持的一句话结论（由工具侧提炼，不是原文照抄）
    :param snippet: 原文片段 / 结果片段，用于生成报告时引用和人工核对
    :param ref: 引用来源：网页 URL / 文档名 + 切片号 / 实际执行的 SQL
    :param confidence: 0~1 的置信度，来自检索打分或工具自评
    :param payload: 原始负载（网页对象、切片元数据、结果行），报告阶段一般不用，
                    留着是为了评测时回溯
    """
    source: str
    claim: str
    snippet: str
    ref: str
    confidence: float = 0.5
    payload: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "claim": self.claim,
            "snippet": self.snippet,
            "ref": self.ref,
            "confidence": round(self.confidence, 4),
        }


@dataclass
class ToolResult:
    """
    一次工具调用的完整结果。

    失败不抛异常：`ok=False` + `error` 说明原因，`evidence` 为空列表。
    这样上层的降级逻辑只需要看 `ok`，不必 try/except 每一种异常。
    """
    source: str
    ok: bool
    evidences: list[Evidence] = field(default_factory=list)
    error: str = ""
    elapsed: float = 0.0
    # 该路实际消耗的 token（有 LLM 参与的路才统计）
    tokens: int = 0
    # 这次失败值不值得重试。超时、连接中断、5xx 值得；
    # 「字段不存在」「语法错误」这类确定性失败重试一次结果完全一样，纯属浪费
    retryable: bool = True

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "ok": self.ok,
            "error": self.error,
            "elapsed": round(self.elapsed, 3),
            "count": len(self.evidences),
            "evidences": [e.to_dict() for e in self.evidences],
        }


class ToolTimer:
    """统计单次工具调用耗时，配合 with 使用。"""

    def __init__(self) -> None:
        self.elapsed = 0.0
        self._start = 0.0

    def __enter__(self) -> "ToolTimer":
        self._start = time.perf_counter()
        return self

    def __exit__(self, *exc) -> bool:
        self.elapsed = time.perf_counter() - self._start
        # 异常不外吞，交给调用方统一处理
        return False
