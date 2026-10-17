"""
Planner：把问题拆成结构化子任务。

把这一步交给框架黑盒、让模型自己看着办时，子任务既不可枚举也不可回灌。
这里改成显式输出 todo 列表，
理由是只有结构化才能：① 并行调度 ② 审查阶段逐项比对覆盖率 ③ 评测时算规划准确率。

规划失败不抛异常，退化为「单任务三路全上」。这既是兜底，也是消融实验里
"单智能体一把梭"那条基线的现成实现。
"""

from __future__ import annotations

from app.agent import llm as llm_mod
from app.agent.prompts import render
from app.agent.state import AgentState, Todo
from app.core.log import logger
from app.core.text import norm

# 三路证据源，顺序决定优先级（降级时按这个顺序找替代）
ROUTES = ("web", "kb", "sql")
MAX_TODOS = 5


async def plan(state: AgentState) -> dict:
    """拆分子任务。回环时只补缺失项，不重复规划已覆盖的。"""
    question = state["question"]
    missing = state.get("missing") or []
    round_no = state.get("review_round", 0)
    previous = [t.get("question", "") for t in (state.get("todos") or [])]

    feedback = _feedback(missing, previous)
    prompt = render("planner", question=question, feedback=feedback)

    try:
        reply = await llm_mod.ainvoke(prompt)
        data = llm_mod.extract_json(reply.text)
        todos = _normalize(data.get("todos"), start=round_no + 1)
        if not todos:
            raise ValueError("模型未给出任何有效子任务")
        todos = _ensure_progress(todos, previous, "；".join(missing))
        logger.info(f"[plan] 第 {round_no + 1} 轮，{len(todos)} 个子任务：{todos}")
        return {"todos": todos, "tokens": state.get("tokens", 0) + reply.tokens}

    except Exception as e:
        # 规划失败就退回"一把梭"：原始问题三路全上。
        # 计划质量下降，但链路仍然能出报告——这比整条链崩掉好。
        logger.warning(f"[plan] 规划失败，降级为单任务三路：{type(e).__name__}: {e}")
        fallback = [Todo(id=f"r{round_no + 1}t1", question=question, routes=list(ROUTES))]
        return {
            "todos": fallback,
            "tokens": state.get("tokens", 0),
            "guard_trips": ["plan:fallback 规划失败，降级为单任务三路"],
        }


async def plan_single(state: AgentState) -> dict:
    """
    单智能体基线：不拆子任务，原始问题三路全上，全程不调 LLM。

    这是消融实验的对照组——就是「一个 agent 拿到问题直接把三个工具都调一遍」
    的做法。不花 token，也不受模型随机性影响，所以每轮实验的起点完全一致。
    """
    round_no = state.get("review_round", 0)
    return {
        "todos": [Todo(id=f"r{round_no + 1}t1", question=state["question"], routes=list(ROUTES))],
    }


def _ensure_progress(todos: list[Todo], previous: list[str], feedback: str) -> list[Todo]:
    """
    补检索轮的兜底：模型原地打转时，强制把用户意见打进子任务。

    实测现象：用户要求「补充华南西南的区域差异」，模型还是原样产出
    「按品类汇总出库货值」——worker 的重复调用检测（按 route+问句归一化）会把它
    全部跳过，这一轮等于白走。这里做确定性兜底：只要新规划与上一轮完全重合，
    就把用户意见附加进问句，保证下游拿到的是不同的查询。
    """
    if not previous or not feedback.strip():
        return todos

    prev = {norm(q) for q in previous if q}
    if not prev:
        return todos

    if all(norm(t["question"]) in prev for t in todos):
        logger.warning("[plan] 补检索规划与上一轮完全重复，强制附加用户意见")
        for t in todos:
            t["question"] = f"{t['question']}；用户补充要求：{feedback}"
    return todos


def _feedback(missing: list[str], previous: list[str] = ()) -> str:
    """回环时的补检索指令；首轮为空。"""
    if not missing:
        return ""
    items = "\n".join(f"- {m}" for m in missing)
    prev_block = ""
    if previous:
        prev_block = (
            "上一轮已执行过的子任务（不要原样重复，也不要只换措辞）：\n"
            + "\n".join(f"- {q}" for q in previous)
            + "\n"
        )
    return (
        "这是补检索轮次。下面这些缺口上一轮没有拿到足够证据，"
        "请针对它们重新设计子任务（可以换一种问法或换一路证据源，但不要重复原来的措辞）：\n"
        f"{items}\n"
        f"{prev_block}"
        "如果上一轮某些查询返回了空结果，最常见的原因是问法里带了数据中根本不存在的"
        "限定条件（年份、地区、品类、型号等）。请去掉这类臆测的限定条件，用更宽的口径再问一次。\n"
        "只为这些缺口规划，不要重复上一轮已经覆盖的内容。"
    )


def _normalize(raw, start: int = 0) -> list[Todo]:
    """
    校验并规范化模型输出。

    模型常见问题：routes 写中文、id 缺失或重复、子任务过多。
    这里逐项修正而不是直接拒绝——能救的救，救不了的丢掉。

    id 一律由代码按「轮次 + 序号」重编，不采用模型给的值：
    回环时模型很容易沿用它上一轮的 t1，两轮 todo 串号后，
    证据归属就乱了，审查覆盖率会算错。
    """
    if not isinstance(raw, list):
        return []

    alias = {
        "网络": "web", "联网": "web", "搜索": "web", "websearch": "web",
        "知识库": "kb", "文档": "kb", "内部": "kb", "knowledge": "kb",
        "数据库": "sql", "数仓": "sql", "数据": "sql", "mysql": "sql",
    }

    todos: list[Todo] = []

    for item in raw:
        if not isinstance(item, dict):
            continue
        question = str(item.get("question") or "").strip()
        if len(question) < 4:
            continue

        routes = []
        for r in item.get("routes") or []:
            key = str(r).strip().lower()
            key = alias.get(key, key)
            if key in ROUTES and key not in routes:
                routes.append(key)
        if not routes:
            continue

        todos.append(
            Todo(
                id=f"r{start}t{len(todos) + 1}",
                question=question,
                routes=routes,
            )
        )
        if len(todos) >= MAX_TODOS:
            break

    return todos
