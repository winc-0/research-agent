"""
主图装配。

    plan → workers → compress → guard ─┬→ review → confirm ─┬→ plan（补检索，有界）
                                       │                     └→ report
                                       └→ report（守卫触发，提前收尾）

为什么不用 `create_deep_agent` 一行搞定：那样每一块的职责、阈值、降级路径
都在框架黑盒里，既没法调也不能评测。自己搭图之后，六块各自可单测、
可开关、可做消融——自己搭图的理由就在这里。

`confirm` 是 HITL 挂起点，需要 checkpointer 才能工作：
挂起时把状态存下来，用户确认后用 `Command(resume=...)` 从断点继续。
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from app.agent.nodes.compress import compress
from app.agent.nodes.confirm import confirm
from app.agent.nodes.guard import guard
from app.agent.nodes.planner import plan
from app.agent.nodes.report import report
from app.agent.nodes.review import review
from app.agent.nodes.worker import workers
from app.agent.state import AgentState

# 回环最多 2 轮，加上六节点本身，正常一次运行不超过 20 步；
# 留足余量，避免合法路径被框架的递归上限误杀
RECURSION_LIMIT = 40


def _after_guard(state: AgentState) -> str:
    """守卫命中就直接出报告，不再进审查。"""
    return "report" if state.get("stop_reason") else "review"


def _after_review(state: AgentState) -> str:
    """证据不足且还有轮次额度就回规划，否则交由用户确认。"""
    return "plan" if state.get("need_more") else "confirm"


def _after_confirm(state: AgentState) -> str:
    """用户要求补检索才回规划，否则写报告。"""
    return "plan" if state.get("need_more") else "report"


def build_graph(*, checkpointer=None, overrides: dict | None = None):
    """
    编译主图。

    checkpointer 传了才能用 interrupt；CLI 场景不需要 HITL，可以传 None
    （此时 settings.hitl.enabled 会被关掉，confirm 节点直接放行）。

    :param overrides: 按节点名替换实现。消融实验靠它换掉 plan / compress / review，
                      连线与其余节点完全不动——保证「只改一层，其它都一样」。
    """
    nodes = {
        "plan": plan,
        "workers": workers,
        "compress": compress,
        "guard": guard,
        "review": review,
        "confirm": confirm,
        "report": report,
    }
    for name, fn in (overrides or {}).items():
        if name not in nodes:
            raise KeyError(f"未知节点：{name}（可选：{'/'.join(nodes)}）")
        nodes[name] = fn

    g = StateGraph(AgentState)

    for name, fn in nodes.items():
        g.add_node(name, fn)

    g.add_edge(START, "plan")
    g.add_edge("plan", "workers")
    g.add_edge("workers", "compress")
    g.add_edge("compress", "guard")
    g.add_conditional_edges("guard", _after_guard, {"review": "review", "report": "report"})
    g.add_conditional_edges("review", _after_review, {"plan": "plan", "confirm": "confirm"})
    g.add_conditional_edges("confirm", _after_confirm, {"plan": "plan", "report": "report"})
    g.add_edge("report", END)

    return g.compile(checkpointer=checkpointer)
