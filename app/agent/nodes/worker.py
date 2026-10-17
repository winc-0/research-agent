"""
Worker：三路并行取证。

框架自带的 subagent 机制一次只跑一路、返回拼接字符串，可观测性和可控性都不够。
这里改成显式调度，同时具备四件事：

1. 并行：一个 todo 的多路、多个 todo 之间全部并发
2. 重复调用检测：同一 (路, 归一化问题) 只打一次，命中即跳过并记账
3. 三级降级：重试 → 换路 → 标记缺失继续（绝不因为一路失败让整条链崩掉）
4. 计量：每次调用的耗时、条数、尝试次数都进 state，报告和评测都要用
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

from app.agent.state import AgentState
from app.core.bus import emit
from app.core.log import logger
from app.core.text import norm
from app.tools import kb_retrieve, sql_query, web_search
from app.tools.base import ToolResult

# 某一路彻底失败时，按这个链找替代。web 是最后一站，没有替代。
FALLBACK_ROUTE = {"sql": "kb", "kb": "web", "web": None}

# 全局并发闸。sql 那一路背后是 12 节点的 NL2SQL 链路，
# 不设上限会把仓配自助问数服务和数仓一起压垮，进而让整批调用一起超时。
MAX_CONCURRENCY = 5

# 展示用名称：前端日志树里直接显示这个，不透出内部 route 代号
TOOL_LABEL = {"web": "联网搜索", "kb": "内部知识库检索", "sql": "数据仓库查询"}

# 底层取证实现的替换点，评测回放用。
#
# 消融实验要「同一份证据、只换编排层」，就必须把取证这一步从链路里摘出来：
# 录制阶段注入一个「记录后转调真实工具」的包装，回放阶段注入一个「只查池子」的实现。
# 之所以放在模块级而不是改函数签名，是因为取证发生在 gather 的协程里，
# 逐层传参会把节点、降级、重试三条路径全部改一遍，代价远大于收益。
_REPLAY_CALLER: Callable[[str, str], Awaitable[ToolResult]] | None = None


def set_caller(caller: Callable[[str, str], Awaitable[ToolResult]] | None) -> None:
    """替换底层取证实现。传 None 恢复真实调用（默认）。"""
    global _REPLAY_CALLER
    _REPLAY_CALLER = caller


def call_key(route: str, question: str) -> str:
    """重复调用的判定键：同一路 + 归一化后的问题。"""
    return f"{route}::{norm(question)}"


async def workers(state: AgentState) -> dict:
    """并行执行本轮所有 (子任务 × 证据路)。"""
    todos = state.get("todos") or []
    if not todos:
        return {}

    already = set(state.get("calls") or [])
    jobs: list[tuple[dict, str, str]] = []
    trips: list[str] = []
    planned_calls: list[str] = []

    for todo in todos:
        for route in todo.get("routes") or []:
            key = call_key(route, todo["question"])
            if key in already:
                # 同一轮规划重复、或跨轮重复——不再打一次，省时间也省钱
                trips.append(f"repeat_skip: {key}")
                continue
            jobs.append((todo, route, key))
            planned_calls.append(key)

    if not jobs:
        logger.info("[workers] 本轮无新增调用（全部命中重复检测）")
        # 这不是正常情况：说明规划原地打转，本轮不会有任何新证据，
        # 记进 guard_trips 让报告的运行信息里能看见，而不是静默空转
        trips.append("revision_no_new_jobs: 本轮子任务与上一轮重复，未产生新调用")
        return {"guard_trips": trips}

    sem = asyncio.Semaphore(MAX_CONCURRENCY)
    results = await asyncio.gather(
        *[_run_one(todo, route, key, sem) for todo, route, key in jobs],
        return_exceptions=True,
    )

    rows: list[dict] = []
    for item in results:
        if isinstance(item, Exception):
            # gather 里漏出来的异常不该发生（_run_one 自己兜住了），
            # 真出现就记下来，不让它污染其他路的结果
            logger.error(f"[workers] 任务异常逃逸：{type(item).__name__}: {item}")
            trips.append(f"task_error: {item}")
            continue
        rows.append(item)

    # ---------- 三级降级第 2 级：整个子任务一路都没成功时换一路再试 ----------
    degraded = await _degrade(todos, rows, sem)
    rows.extend(degraded)

    # ---------- 汇总 ----------
    evidences: list[dict] = []
    for row in rows:
        for ev in row["evidences"]:
            ev = dict(ev)
            # todo_id 决定审查阶段能算出「哪一项没有证据」
            ev["todo_id"] = row["todo_id"]
            # 降级来的证据要标明真实来源路与原始意图路，评测时区分得开
            ev["degraded_from"] = row.get("degraded_from", "")
            evidences.append(ev)

    for row in rows:
        if row["attempts"] > 1 and row["ok"]:
            trips.append(f"retry_recovered: {row['route']}::{row['todo_id']}（第 {row['attempts']} 次成功）")
        if not row["ok"]:
            trips.append(f"route_failed: {row['route']}::{row['todo_id']} - {row['error'][:60]}")

    steps = sum(r["attempts"] for r in rows)
    logger.info(
        f"[workers] {len(rows)} 次调用，{len(evidences)} 条证据，"
        f"失败 {sum(1 for r in rows if not r['ok'])} 次"
    )

    return {
        "evidences": evidences,
        "calls": planned_calls + [r["key"] for r in degraded],
        "guard_trips": trips,
        "steps": state.get("steps", 0) + steps,
        "route_stats": _merge_stats(state.get("route_stats") or {}, rows),
    }


async def _run_one(todo: dict, route: str, key: str, sem: asyncio.Semaphore) -> dict:
    """跑一路，失败重试一次。返回统一结构的行。"""
    async with sem:
        # 上报「开始调用」而不是「调用结束」：前端要的是进度感，
        # 一次 sql 调用平均 8 秒，等结果再推送等于全程无反馈
        emit(
            "tool_start",
            {
                "tool_name": TOOL_LABEL.get(route, route),
                "args": {"route": route, "todo": todo.get("id", ""), "question": todo["question"]},
            },
        )
        result = await _call(route, todo["question"])
        attempts = 1

        if not result.ok and result.retryable:
            logger.warning(f"[workers] {route} 首次失败，重试：{result.error[:80]}")
            retry = await _call(route, todo["question"])
            attempts = 2
            # 两次都失败时保留信息量更大的那条错误（通常是第一次的根因更清楚）
            result = retry if retry.ok else result
        elif not result.ok:
            # 确定性失败（SQL 引用了不存在的字段之类），重试毫无意义，直接降级
            logger.info(f"[workers] {route} 确定性失败，不重试：{result.error[:80]}")

        return {
            "todo_id": todo.get("id", ""),
            "route": route,
            "key": key,
            "ok": result.ok,
            "evidences": [e.to_dict() for e in result.evidences],
            "error": result.error,
            "elapsed": result.elapsed,
            "attempts": attempts,
            "degraded_from": "",
        }


async def _degrade(todos: list[dict], rows: list[dict], sem: asyncio.Semaphore) -> list[dict]:
    """
    第 2 级降级：子任务的所有路都失败，换一条没试过的路再打一次。

    只在「全失败」时才触发，避免每路失败都去换路导致调用量翻倍。
    """
    ok_todos = {r["todo_id"] for r in rows if r["ok"]}
    failed = [t for t in todos if t.get("id") not in ok_todos]
    if not failed:
        return []

    out: list[dict] = []
    for todo in failed:
        tried = set(todo.get("routes") or [])
        # 沿 FALLBACK 链找一个没试过的路
        alt = None
        for route in todo.get("routes") or []:
            candidate = FALLBACK_ROUTE.get(route)
            if candidate and candidate not in tried:
                alt = candidate
                break
        if not alt:
            continue

        key = call_key(alt, todo["question"])
        logger.info(f"[workers] 降级：{todo.get('id')} 改走 {alt}")
        row = await _run_one(todo, alt, key, sem)
        row["degraded_from"] = ",".join(todo.get("routes") or [])
        out.append(row)

    return out


async def _call(route: str, question: str) -> ToolResult:
    """把三路工具统一成异步接口。评测回放时先查注入的实现。"""
    if _REPLAY_CALLER is not None:
        return await _REPLAY_CALLER(route, question)
    return await _real_call(route, question)


async def _real_call(route: str, question: str) -> ToolResult:
    """
    真实的取证实现（不受回放钩子影响）。

    录制阶段要用它：钩子里如果回调 `_call` 会自己调自己，所以必须分开。
    """
    if route == "web":
        # web_search 内部是同步 httpx（百炼 MCP 用的是同步协议），
        # 必须丢到线程池，否则会阻塞整个事件循环，并行就退化成串行
        return await asyncio.to_thread(web_search.web_search, question)
    if route == "kb":
        return await kb_retrieve.kb_retrieve(question)
    if route == "sql":
        return await sql_query.sql_query(question)
    raise ValueError(f"未知证据路：{route}")


def _merge_stats(prev: dict, rows: list[dict]) -> dict:
    """跨轮累加统计。每路分开记，报告里要展示哪一路在拖后腿。"""
    stats = {k: dict(v) for k, v in prev.items()}
    for row in rows:
        s = stats.setdefault(
            row["route"], {"ok": 0, "fail": 0, "evidences": 0, "elapsed": 0.0, "attempts": 0}
        )
        s["ok" if row["ok"] else "fail"] += 1
        s["evidences"] += len(row["evidences"])
        s["elapsed"] = round(float(s["elapsed"]) + row["elapsed"], 3)
        s["attempts"] += row["attempts"]
    return stats
