"""
图的共享状态。

设计约束：状态里只放「可序列化的事实」，不放连接、不放 LLM 客户端。
这样才能把中间状态落盘、回放、以及交给评测脚本逐字段比对。

累加型字段（evidences / guard_trips / calls）用 `operator.add` 做 reducer：
多个并行分支（三路 worker）同时往同一个字段写时，框架会合并而不是互相覆盖。
这是 LangGraph 里并行节点唯一的正确写法——覆盖型字段在并行分支下会丢数据。
"""

from __future__ import annotations

import operator
from typing import Annotated, TypedDict


class Todo(TypedDict, total=False):
    """Planner 产出的一个子任务。"""

    # 由代码按 r{轮次}t{序号} 生成，模型给的 id 一律不采用
    id: str
    question: str
    # 该子任务需要哪几路证据，取值 web / kb / sql
    routes: list[str]


class Citation(TypedDict, total=False):
    """压缩之后的引用条目，报告里的 [n] 直接指向它。"""

    index: int
    source: str
    claim: str
    snippet: str
    ref: str
    confidence: float
    # 这条引用支撑了哪些 todo，用于审查覆盖率
    todo_ids: list[str]


class AgentState(TypedDict, total=False):
    """主图状态。"""

    # ---------- 输入 ----------
    question: str

    # ---------- 规划 ----------
    todos: list[Todo]
    # 审查判定为证据不足的子任务描述，回灌给 planner 做二次检索
    missing: list[str]

    # ---------- 取证（累加）----------
    evidences: Annotated[list[dict], operator.add]
    # 本轮的 (route, 归一化 query)，用于重复调用检测（累加）
    calls: Annotated[list[str], operator.add]
    # 守卫命中记录（累加）
    guard_trips: Annotated[list[str], operator.add]

    # ---------- 压缩 ----------
    citations: list[Citation]

    # ---------- 计量 ----------
    steps: int
    tokens: int
    # 每路统计：{route: {"ok": n, "fail": n, "evidences": n, "elapsed": s}}
    route_stats: dict

    # ---------- 审查 ----------
    review_round: int
    review_notes: list[str]
    # 审查决定「还该不该再搜一轮」
    need_more: bool

    # ---------- 人工确认 ----------
    # 用户是否放行（HITL 关闭时恒为 True）
    approved: bool
    # 用户补充意见：同意时也可能带「顺便补一句」，所以两种决定都记
    feedback: str

    # ---------- 输出 ----------
    report: str
    # 终止原因：normal / guard / budget，报告里会注明
    stop_reason: str


def empty_state(question: str) -> AgentState:
    """构造一份初始状态，字段齐全便于落盘对比。"""
    return AgentState(
        question=question,
        todos=[],
        missing=[],
        evidences=[],
        calls=[],
        guard_trips=[],
        citations=[],
        steps=0,
        tokens=0,
        route_stats={},
        review_round=0,
        review_notes=[],
        need_more=False,
        approved=False,
        feedback="",
        report="",
        stop_reason="",
    )
