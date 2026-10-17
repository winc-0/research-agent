"""
命令行入口。

    python -m app.agent.run "问题"
    python -m app.agent.run "问题" --quiet          # 只输出报告正文
    python -m app.agent.run "问题" --save out.md    # 报告落盘到指定路径

这一层刻意不做重试和兜底——所有容错都在节点内部完成。
如果这里还要 try/except，说明节点层的错误处理有漏。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path

from app.agent.graph import RECURSION_LIMIT, build_graph
from app.agent.state import empty_state
from app.conf.settings import settings
from app.core.log import logger


async def run(question: str, *, verbose: bool = True) -> dict:
    """
    执行一次研究任务，返回最终状态。

    CLI 强制关掉 HITL：命令行没有人盯着，挂起等确认等于卡死。
    确认环节是 Web 才有的能力（见 app/api/server.py 的恢复循环）。
    """
    settings.hitl.enabled = False

    graph = build_graph()
    state = empty_state(question)
    final: dict = dict(state)
    started = time.perf_counter()

    async for mode, chunk in graph.astream(
        state,
        stream_mode=["updates", "values"],
        config={"recursion_limit": RECURSION_LIMIT},
    ):
        if mode == "values":
            # values 每次给的是完整状态快照，最后一条就是最终态
            final = dict(chunk)
        elif mode == "updates" and verbose:
            for node, update in chunk.items():
                _echo(node, update or {})

    if verbose:
        print(f"\n总耗时 {time.perf_counter() - started:.2f}s")

    return final


def _echo(node: str, update: dict) -> None:
    """节点的进度摘要。只打关键数字，不打中间态全文。"""
    if node == "plan":
        todos = update.get("todos") or []
        print(f"[规划] {len(todos)} 个子任务")
        for t in todos:
            print(f"       {t['id']}  {t['question'][:60]}  ← {'/'.join(t['routes'])}")

    elif node == "workers":
        ev = update.get("evidences") or []
        steps = update.get("steps") or 0
        stats = update.get("route_stats") or {}
        detail = "，".join(f"{k}:{v.get('evidences', 0)}条" for k, v in sorted(stats.items()))
        print(f"[取证] 累计 {steps} 次调用，新增 {len(ev)} 条证据（{detail}）")
        for trip in update.get("guard_trips") or []:
            print(f"       ⚠ {trip}")

    elif node == "compress":
        print(f"[压缩] 入选 {len(update.get('citations') or [])} 条引用")

    elif node == "guard":
        if update.get("stop_reason"):
            print(f"[守卫] 触发：{update['stop_reason']}")

    elif node == "review":
        notes = update.get("review_notes") or []
        print(f"[审查] {'；'.join(notes[-1:])}")

    elif node == "report":
        print(f"[报告] 生成完成，{len(update.get('report') or '')} 字")


def _save(report: str, path: Path | None) -> Path:
    """报告落盘。不指定路径时写到 output/ 下按时间戳命名。"""
    target = path or (settings.workspace.root / f"{datetime.now():%Y%m%d_%H%M%S}.md")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(report, encoding="utf-8")
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description="物流研究助手 Research-Agent：多智能体深度研究")
    parser.add_argument("question", nargs="*", help="研究问题")
    parser.add_argument("--save", type=Path, default=None, help="报告保存路径")
    parser.add_argument("--quiet", action="store_true", help="只输出报告正文")
    parser.add_argument("--json", action="store_true", help="额外输出完整状态（评测用）")
    args = parser.parse_args()

    question = " ".join(args.question).strip()
    if not question:
        question = input("请输入研究问题：").strip()
    if not question:
        print("问题为空")
        return 2

    final = asyncio.run(run(question, verbose=not args.quiet))
    report = final.get("report") or ""

    if args.quiet:
        print(report)
    else:
        print("\n" + "=" * 70 + "\n")
        print(report)

    if report and not args.quiet:
        path = _save(report, args.save)
        logger.info(f"报告已保存：{path}")

    if args.json:
        payload = {k: v for k, v in final.items() if k != "report"}
        print("\n---STATE---")
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))

    return 0 if report else 1


if __name__ == "__main__":
    sys.exit(main())
