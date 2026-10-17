"""
人工确认（HITL）：报告生成前让用户看大纲并给意见。

为什么放在这里而不是放在最前面：这一阶段「证据已经收齐、报告还没动笔」，
用户能判断的正是「这些材料够不够、方向对不对」——太早问，用户只能凭空猜；
太晚问，报告已经写完，改的代价等于重跑。

实现用 LangGraph 的 `interrupt`：
- 节点执行到 interrupt 时整个图挂起，状态由 checkpointer 落住
- 前端拿到大纲后回调 `/api/confirm`，外层用 `Command(resume=...)` 恢复
- 恢复时本节点**从头重跑一遍**，所以 interrupt 之前的逻辑必须无副作用

这条路径是个闭环：用户点「按意见补充检索」→ 回到 planner → 再走一轮取证 →
再确认一次。轮次受 review_max_rounds 约束，不会无限来回。
"""

from __future__ import annotations

from langgraph.types import interrupt

from app.agent.state import AgentState
from app.conf.settings import settings
from app.core.log import logger

ROUTE_LABEL = {"web": "联网", "kb": "知识库", "sql": "数仓"}


def build_outline(state: AgentState) -> list[dict]:
    """
    大纲从 todos 派生，而不是让模型现编一份。

    todos 是本轮真正执行的子任务，配上各自的证据条数，用户一眼就能看出
    「哪一块材料薄」——这正是他要做判断的地方。
    """
    todos = state.get("todos") or []
    citations = state.get("citations") or []

    counts: dict[str, int] = {}
    for c in citations:
        for tid in c.get("todo_ids") or []:
            counts[tid] = counts.get(tid, 0) + 1

    return [
        {
            "问题": t.get("question", ""),
            "证据数": counts.get(t.get("id", ""), 0),
            "来源": [ROUTE_LABEL.get(r, r) for r in t.get("routes") or []],
        }
        for t in todos
    ]


async def confirm(state: AgentState) -> dict:
    """挂起等用户确认；恢复值决定是继续写报告还是回到规划补检索。"""
    if not settings.hitl.enabled:
        return {"approved": True}

    outline = build_outline(state)
    round_no = int(state.get("review_round") or 0)

    logger.info(f"[confirm] 挂起等待用户确认大纲（{len(outline)} 项）")
    raw = interrupt(
        {
            "question": state["question"],
            "outline": outline,
            "round": round_no,
            "evidence_total": len(state.get("citations") or []),
        }
    )

    decision, feedback = _parse(raw)
    logger.info(f"[confirm] 用户决定：{decision}" + (f"，意见：{feedback[:60]}" if feedback else ""))

    if decision == "revise" and feedback:
        # 轮次额度用尽时不再回环，但把用户的意见带进报告，
        # 让它至少在「局限」里交代清楚哪里没答到
        if round_no >= settings.guard.review_max_rounds + 1:
            logger.warning("[confirm] 补检索轮次已用尽，直接出报告")
            return {
                "approved": True,
                "feedback": feedback,
                "review_notes": list(state.get("review_notes") or [])
                + [f"用户要求补充但轮次已用尽：{feedback}"],
            }

        return {
            "approved": False,
            "need_more": True,
            "feedback": feedback,
            "missing": [feedback],
            "review_round": round_no + 1,
            "review_notes": list(state.get("review_notes") or []) + [f"用户要求调整：{feedback}"],
        }

    return {"approved": True, "need_more": False, "feedback": feedback}


def _parse(raw) -> tuple[str, str]:
    """
    解析恢复值。

    前端、脚本、超时兜底三条路都可能调用，格式不统一时一律按「确认」处理——
    卡死在等人确认上，比少问一次更糟。
    """
    if isinstance(raw, dict):
        decision = str(raw.get("decision") or "approve").lower()
        feedback = str(raw.get("feedback") or "").strip()
        return ("revise" if decision == "revise" else "approve"), feedback
    if isinstance(raw, str) and raw.strip().lower() == "revise":
        return "revise", ""
    return "approve", ""
