"""
六组消融的装配。

阶梯的排法不是「参数从少到多」，而是每一级只回答一个问题：

    G1 单智能体一把梭      —— 不拆任务、不筛选证据，这就是「一个 agent 调三个工具」
    G2 +多子任务规划        —— 规划这一层单独的贡献
    G3 +跨源去重            —— 去重单独的贡献（配额仍关）
    G4 +按路配额            —— 配额与轮转单独的贡献
    G5 +审查回环（完整）    —— 审查单独的贡献
    G6 完整但关掉去重        —— 留一法：把去重从完整链路里摘掉，看掉多少

G6 是故意的反向对照。只做累加式消融容易得出「每一层都有用」的漂亮结论，
留一法能看出某一层在其它层都在时是否还是必要的。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from app.agent.graph import RECURSION_LIMIT, build_graph
from app.agent.nodes import worker as worker_mod
from app.agent.nodes.compress import make_compress
from app.agent.nodes.planner import plan_single
from app.agent.nodes.review import review as real_review
from app.agent.state import AgentState, empty_state
from app.conf.settings import settings
from eval.replay import Pool
from eval.schema import QAItem

ITEM_TIMEOUT = 480


# ---------------------------------------------------------------- 节点实现

async def compress_none(state: AgentState) -> dict:
    """
    不压缩：全部证据按取证顺序直接编号。

    这就是「把召回结果原样拼进提示词」的做法。保留它当基线，是因为
    compress 模块注释里声称的三种问题（重复浪费预算、某一路挤掉另一路、
    超长静默截断）只有跟这个基线比才说得清。
    """
    citations = []
    for i, ev in enumerate(state.get("evidences") or [], start=1):
        citations.append(
            {
                "index": i,
                "source": ev.get("source") or "",
                "claim": ev.get("claim") or "",
                "snippet": ev.get("snippet") or "",
                "ref": ev.get("ref") or "",
                "confidence": round(float(ev.get("confidence") or 0.0), 4),
                "todo_ids": [ev["todo_id"]] if ev.get("todo_id") else [],
                "degraded_from": ev.get("degraded_from") or "",
            }
        )
    return {"citations": citations}


async def review_off(state: AgentState) -> dict:
    """关掉审查：不调模型，直接放行。省掉一次 LLM 调用也是成本对比的一部分。"""
    round_no = int(state.get("review_round") or 0)
    return {
        "need_more": False,
        "review_round": round_no + 1,
        "missing": [],
        "review_notes": list(state.get("review_notes") or []) + ["消融：审查已关闭"],
    }


def make_plan_replay(rounds: list[list[dict]]):
    """
    回放规划：按轮次吐出录制时的 todos，不调模型。

    轮次用 `review_round` 索引：首轮 plan 时它是 0，审查回环后变 1。
    录制时没有第 N 轮就直接返回空，让 worker 什么都不做——比随手编一份
    「差不多的规划」诚实，后者会把实验变成另一场实验。
    """

    async def _node(state: AgentState) -> dict:
        idx = int(state.get("review_round") or 0)
        if idx < len(rounds):
            return {"todos": rounds[idx]}
        return {
            "todos": [],
            "guard_trips": [f"replay: 录制中没有第 {idx + 1} 轮规划"],
        }

    return _node


def make_review_capped(rounds: list[list[dict]]):
    """
    审查照跑，但录制里没有下一轮证据时截断。

    不截断的话：审查判定「要补」→ 回 plan → 拿不到新 todos → worker 空转 →
    审查又判定「要补」，一直转到递归上限。这是回放模式的固有限制，
    如实记一条 note，比让它跑满 40 步强。
    """

    async def _node(state: AgentState) -> dict:
        update = await real_review(state)
        nxt = int(state.get("review_round") or 0) + 1
        if update.get("need_more") and nxt >= len(rounds):
            update["need_more"] = False
            update["review_notes"] = list(state.get("review_notes") or []) + [
                "回放：录制中没有下一轮取证，补检索被截断"
            ]
        return update

    return _node


# ---------------------------------------------------------------- 变体定义

@dataclass(frozen=True)
class Variant:
    id: str
    name: str
    plan: str       # single | replay
    compress: str   # none | dedup | quota | no_dedup
    review: bool
    desc: str

    @property
    def label(self) -> str:
        return f"{self.id} {self.name}"


VARIANTS: list[Variant] = [
    Variant("G1", "单智能体一把梭", "single", "none", False,
            "原始问句三路全上，证据不筛选、不审查"),
    Variant("G2", "+多子任务规划", "replay", "none", False,
            "拆成子任务并行取证，证据仍不筛选"),
    Variant("G3", "+跨源去重", "replay", "dedup", False,
            "加上跨源去重，仍不做按路配额"),
    Variant("G4", "+按路配额", "replay", "quota", False,
            "去重 + 每路上限 + 按路轮转"),
    Variant("G5", "+审查回环（完整）", "replay", "quota", True,
            "完整链路，与线上一致"),
    Variant("G6", "完整但关掉去重", "replay", "no_dedup", True,
            "留一法对照：其它都在，只去掉跨源去重"),
]

BY_ID = {v.id: v for v in VARIANTS}


def _overrides(variant: Variant, rounds: list[list[dict]]) -> dict:
    plan_node = plan_single if variant.plan == "single" else make_plan_replay(rounds)

    compress_node = {
        "none": compress_none,
        "dedup": make_compress(dedup=True, quota=False),
        "quota": make_compress(dedup=True, quota=True),
        "no_dedup": make_compress(dedup=False, quota=True),
    }[variant.compress]

    review_node = make_review_capped(rounds) if variant.review else review_off

    return {"plan": plan_node, "compress": compress_node, "review": review_node}


# ---------------------------------------------------------------- 执行

async def run_item(item: QAItem, variant: Variant, pool: Pool) -> dict:
    """在回放模式下跑一题，返回最终状态与运行元信息。"""
    settings.hitl.enabled = False
    pool.misses = []

    graph = build_graph(overrides=_overrides(variant, pool.rounds))
    final: dict = dict(empty_state(item.question))
    started = time.perf_counter()
    error = ""

    worker_mod.set_caller(pool.caller)
    try:
        async for mode, chunk in graph.astream(
            empty_state(item.question),
            stream_mode=["values"],
            config={"recursion_limit": RECURSION_LIMIT},
        ):
            if mode == "values":
                final = dict(chunk)
    except Exception as e:  # noqa: BLE001 - 单题失败不能拖垮整批
        error = f"{type(e).__name__}: {e}"
    finally:
        worker_mod.set_caller(None)

    return {
        "qid": item.id,
        "layer": item.layer,
        "variant": variant.id,
        "question": item.question,
        "report": final.get("report") or "",
        "citations": final.get("citations") or [],
        "evidences": final.get("evidences") or [],
        "todos": final.get("todos") or [],
        "route_stats": final.get("route_stats") or {},
        "tokens": int(final.get("tokens") or 0),
        "steps": int(final.get("steps") or 0),
        "review_round": int(final.get("review_round") or 0),
        "stop_reason": final.get("stop_reason") or "",
        "guard_trips": final.get("guard_trips") or [],
        "pool_misses": pool.misses,
        "elapsed": round(time.perf_counter() - started, 2),
        "error": error,
    }


async def run_all(variant: Variant, items: list[QAItem], pool_dir=None) -> list[dict]:
    """一个变体跑完全部题。串行——回放只省取证，不省模型调用。"""
    from eval.replay import POOL_DIR, Pool

    out: list[dict] = []
    for i, item in enumerate(items, start=1):
        pool = Pool.load(item.id, pool_dir or POOL_DIR)
        try:
            row = await asyncio.wait_for(run_item(item, variant, pool), timeout=ITEM_TIMEOUT)
        except asyncio.TimeoutError:
            row = {
                "qid": item.id, "layer": item.layer, "variant": variant.id,
                "question": item.question, "report": "", "error": "timeout",
                "elapsed": ITEM_TIMEOUT, "tokens": 0, "steps": 0,
                "citations": [], "evidences": [], "route_stats": {},
                "review_round": 0, "stop_reason": "timeout", "guard_trips": [],
                "pool_misses": [], "todos": [],
            }
        except Exception as e:  # noqa: BLE001 - 单题炸掉不该拖垮整批
            row = {
                "qid": item.id, "layer": item.layer, "variant": variant.id,
                "question": item.question, "report": "", "error": f"{type(e).__name__}: {e}",
                "elapsed": 0.0, "tokens": 0, "steps": 0,
                "citations": [], "evidences": [], "route_stats": {},
                "review_round": 0, "stop_reason": "error", "guard_trips": [],
                "pool_misses": [], "todos": [],
            }
        out.append(row)
        print(f"  [{variant.id}] ({i}/{len(items)}) {item.id} 完成 {row['elapsed']}s", flush=True)
    return out
