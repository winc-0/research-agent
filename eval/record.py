"""
录制：把真实链路的取证过程完整跑一遍并落盘。

录制阶段**跑的是线上那张图**，不换任何节点——只做两件事：

1. 在 plan 外面包一层，把每一轮产出的 todos 记下来。回放时按轮次原样吐回去，
   这样六组消融拿到的是同一份规划，差异只来自下游。
2. 在 worker 的取证函数外面包一层，把每次 (路, 问题) → 结果 记下来。
   包装器记完立刻转调真实实现，所以录制的输出与线上完全一致。

另外每个问题都会额外用原始问句把三路各打一次——那是 G1「单智能体一把梭」
基线需要的证据，回放时不能临时去调真接口。
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime

from app.agent.graph import RECURSION_LIMIT, build_graph
from app.agent.nodes import worker as worker_mod
from app.agent.nodes.planner import plan as real_plan
from app.agent.state import empty_state
from app.conf.settings import settings
from app.core.log import logger
from eval.replay import POOL_DIR, Pool, pool_exists
from eval.schema import QAItem

# 单题录制上限。跨源题最慢实测 97 秒，留足余量；超时说明某个下游在拖，
# 与其等到天荒地老，不如记一条失败继续跑下一题
ITEM_TIMEOUT = 480

BASELINE_ROUTES = ("web", "kb", "sql")


async def record_item(item: QAItem, *, force: bool = False) -> dict:
    """录制一道题。已有池子且不强制时直接复用。"""
    if pool_exists(item.id) and not force:
        return {"qid": item.id, "skipped": True}

    pool = Pool(qid=item.id, question=item.question, rounds=[], evidence={}, meta={}, misses=[])
    settings.hitl.enabled = False

    # ---------- 1. 基线需要的证据：原始问句 × 三路 ----------
    sem = asyncio.Semaphore(3)
    single = await asyncio.gather(
        *[_timed_real_call(pool, route, item.question, sem) for route in BASELINE_ROUTES]
    )

    # ---------- 2. 真实图跑一遍 ----------
    async def recording_plan(state):
        update = await real_plan(state)
        pool.rounds.append(update.get("todos") or [])
        return update

    async def recording_call(route: str, question: str):
        result = await worker_mod._real_call(route, question)
        pool.pack(route, question, result)
        return result

    graph = build_graph(overrides={"plan": recording_plan})
    started = time.perf_counter()
    final: dict = {}
    error = ""

    worker_mod.set_caller(recording_call)
    try:
        async for mode, chunk in graph.astream(
            empty_state(item.question),
            stream_mode=["values"],
            config={"recursion_limit": RECURSION_LIMIT},
        ):
            if mode == "values":
                final = dict(chunk)
    except Exception as e:  # noqa: BLE001 - 录制失败也要落盘，方便单独重跑
        error = f"{type(e).__name__}: {e}"
        logger.error(f"[record] {item.id} 链路异常：{error}")
    finally:
        worker_mod.set_caller(None)

    elapsed = round(time.perf_counter() - started, 2)

    pool.meta = {
        "layer": item.layer,
        "recorded_at": datetime.now().isoformat(timespec="seconds"),
        "baseline_ok": sum(1 for r in single if r.ok),
        "rounds": len(pool.rounds),
        "elapsed": elapsed,
        "tokens": int(final.get("tokens") or 0),
        "steps": int(final.get("steps") or 0),
        "citations": len(final.get("citations") or []),
        "evidences": len(final.get("evidences") or []),
        "stop_reason": final.get("stop_reason") or "",
        "error": error,
    }
    path = pool.dump(POOL_DIR)
    logger.info(
        f"[record] {item.id} 完成：{len(pool.evidence)} 次调用，"
        f"{pool.meta['evidences']} 条证据，{len(pool.rounds)} 轮规划，{elapsed}s → {path.name}"
    )
    return {"qid": item.id, "skipped": False, "calls": len(pool.evidence), **pool.meta}


async def _timed_real_call(pool: Pool, route: str, question: str, sem: asyncio.Semaphore):
    """基线取证：真实调用并记账。失败只记不抛，基线本来就允许某一路为空。"""
    async with sem:
        result = await worker_mod._real_call(route, question)
        pool.pack(route, question, result)
        return result


async def record_all(items: list[QAItem], *, force: bool = False) -> list[dict]:
    """逐题录制。串行执行——三路工具已经把单题内的并发吃满了，
    再叠一层题目级并发只会让仓配自助问数那边的 NL2SQL 链路排队超时。"""
    out: list[dict] = []
    total = len(items)
    for i, item in enumerate(items, start=1):
        logger.info(f"[record] ({i}/{total}) {item.id} {item.question[:40]}")
        try:
            row = await asyncio.wait_for(record_item(item, force=force), timeout=ITEM_TIMEOUT)
        except asyncio.TimeoutError:
            logger.error(f"[record] {item.id} 超时（>{ITEM_TIMEOUT}s），跳过")
            row = {"qid": item.id, "error": "timeout"}
        except Exception as e:  # noqa: BLE001 - 单题出问题不该让 40 题从头再来
            logger.error(f"[record] {item.id} 失败：{type(e).__name__}: {e}")
            row = {"qid": item.id, "error": f"{type(e).__name__}: {e}"}
        out.append(row)
    return out
