"""
审查回环：检查证据是否覆盖了每个子任务，不足就回 Planner 补检索。

有界是关键——`REVIEW_MAX_ROUNDS` 决定最多补几轮。无界回环是这类系统
最常见的翻车点，而常见实现里往往没有这一层。

判定分两段：先用确定性规则挡掉「明显没救」的情况（不花 LLM），
再让模型判断「证据是否真的支撑了该子任务的核心事实」。
"""

from __future__ import annotations

import re

from app.agent import llm as llm_mod
from app.agent.prompts import render
from app.agent.state import AgentState
from app.conf.settings import settings
from app.core.log import logger


async def review(state: AgentState) -> dict:
    """判断是否需要再补一轮检索。"""
    todos = state.get("todos") or []
    citations = state.get("citations") or []
    round_before = int(state.get("review_round") or 0)
    tokens = int(state.get("tokens") or 0)

    # ---------- 确定性前置判断 ----------
    if not todos:
        return {"need_more": False, "review_round": round_before + 1, "missing": []}

    if not citations:
        # 零证据有两种原因，处置完全不同：
        # ① 工具层整体不可用 → 再规划也拿不到东西，直接出报告
        # ② 工具可用但这轮没问到点上（典型：问法里带了数据里不存在的年份/口径，
        #    查询返回 0 行）→ 值得换问法再试一轮，这正是审查回环的意义
        stats = state.get("route_stats") or {}
        reachable = [r for r, s in stats.items() if s.get("ok", 0) > 0]
        allow_more = round_before + 1 < settings.guard.review_max_rounds

        if reachable and allow_more:
            logger.warning(f"[review] 工具可用（{'/'.join(reachable)}）但本轮无证据，补检索换问法")
            return {
                "need_more": True,
                "review_round": round_before + 1,
                "missing": [t["question"] for t in todos],
                "review_notes": list(state.get("review_notes") or [])
                + [f"工具可用（{'/'.join(reachable)}）但未取到证据，追加一轮换问法"],
            }

        logger.warning("[review] 无任何证据且无可用工具路，跳过补检索")
        return {
            "need_more": False,
            "review_round": round_before + 1,
            "missing": [t["question"] for t in todos],
            "review_notes": list(state.get("review_notes") or []) + ["未获得任何证据，工具层可能不可用"],
        }

    # ---------- 模型判断 ----------
    coverage = _coverage(todos, citations)
    digest = _digest(citations)
    prompt = render("review", question=state["question"], coverage=coverage, evidences=digest)

    try:
        reply = await llm_mod.ainvoke(prompt)
        tokens += reply.tokens
        data = llm_mod.extract_json(reply.text)
    except Exception as e:
        # 审查本身失败时选择「不再补检索」。理由：解析失败不代表证据不足，
        # 继续补一轮反而可能把已有结论搅乱，且多花一次规划钱。
        logger.warning(f"[review] 审查失败，按证据充分处理：{type(e).__name__}: {e}")
        return {
            "need_more": False,
            "review_round": round_before + 1,
            "missing": [],
            "tokens": tokens,
            "review_notes": list(state.get("review_notes") or []) + [f"审查调用失败：{type(e).__name__}"],
        }

    missing_ids = [str(x) for x in (data.get("missing") or [])]
    by_id = {t["id"]: t["question"] for t in todos}
    missing_questions = [by_id[i] for i in _resolve(missing_ids, by_id)]
    # 完全对不上的 id 是模型的幻觉，记下来
    ghost = [i for i in missing_ids if not _resolve([i], by_id)]

    allow_more = round_before + 1 < settings.guard.review_max_rounds
    need_more = bool(missing_questions) and allow_more

    reason = str(data.get("reason") or "").strip()
    note = f"第 {round_before + 1} 轮审查：{reason or '无说明'}"
    if ghost:
        note += f"（模型返回了不存在的子任务 id：{ghost}，已忽略）"
    if missing_questions and not allow_more:
        note += f"；补检索轮次已达上限 {settings.guard.review_max_rounds}，不再追加"

    logger.info(f"[review] {note}；需补检索={need_more}")

    return {
        "need_more": need_more,
        "review_round": round_before + 1,
        "missing": missing_questions,
        "review_notes": list(state.get("review_notes") or []) + [note],
        "tokens": tokens,
    }


def _resolve(ids: list[str], by_id: dict[str, str]) -> list[str]:
    """
    把模型返回的 id 对齐到真实 id。

    实测模型会把 `r1t2` 简写成 `t2`——直接按字符串比对会全判为幻觉，
    缺口一个都识别不出来，补检索永远不触发。这里按「末尾序号」做二级匹配。
    """
    by_num: dict[str, str] = {}
    for real in by_id:
        m = re.search(r"t(\d+)$", real)
        if m:
            by_num[m.group(1)] = real

    out: list[str] = []
    for raw in ids:
        s = str(raw).strip()
        if s in by_id:
            out.append(s)
            continue
        m = re.search(r"(\d+)$", s)
        if m and m.group(1) in by_num:
            out.append(by_num[m.group(1)])

    # 多个写法可能指向同一个 id，去重且保序
    return list(dict.fromkeys(out))


def _coverage(todos: list[dict], citations: list[dict]) -> str:
    """每个子任务拿到几条证据、最高置信度多少。"""
    lines = []
    for todo in todos:
        hit = [c for c in citations if todo["id"] in (c.get("todo_ids") or [])]
        if hit:
            best = max(float(c.get("confidence") or 0) for c in hit)
            lines.append(f"- {todo['id']}「{todo['question']}」（需 {'/'.join(todo.get('routes') or [])}）：{len(hit)} 条证据，最高置信度 {best:.2f}")
        else:
            lines.append(f"- {todo['id']}「{todo['question']}」（需 {'/'.join(todo.get('routes') or [])}）：0 条证据")
    return "\n".join(lines)


def _digest(citations: list[dict], limit: int = 20) -> str:
    """证据摘要只给 claim，不带 snippet——审查看的是覆盖，不是细节。"""
    rows = [f"[{c['index']}] ({c['source']}) {c['claim']}" for c in citations[:limit]]
    if len(citations) > limit:
        rows.append(f"……另有 {len(citations) - limit} 条未列出")
    return "\n".join(rows)
