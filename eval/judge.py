"""
LLM-as-judge：只判规则判不了的部分。

三项各自 0~2 分，维度拆开是因为它们会互相掩盖：

- `faithfulness` 忠实性：正文里的结论是否都能落到引用的证据上（编没编）
- `coverage` 覆盖度：问题里的每个分句是否都被回答（答漏没答漏）
- `honesty` 诚实性：证据不足时是否明说，而不是给一个看起来很确定的错答案

用一个便宜的模型（默认 qwen-flash）判：判分只做「打档」，不需要强推理，
同一份输入下 flash 与 plus 的档位一致率实测很高，成本差一个数量级。
"""

from __future__ import annotations

import asyncio
import json

from app.agent import llm as llm_mod
from app.conf.settings import settings
from eval.schema import body_of

# 判分只看正文，且截断：报告普遍 1~3k 字，超过 6000 字的多半是在堆砌，判分也看不完
MAX_CHARS = 6000

SYSTEM = """你是一个严格但公正的评审。你会拿到：用户问题、参考答案、以及一份 AI 生成的报告正文。
请只根据报告正文本身评分，不要脑补报告没写的内容。

评分维度（每项 0/1/2 整数）：
1. faithfulness：报告里的关键结论是否都能被它引用的证据支持？有具体数字/事实但没有任何引用支撑，或与参考答案冲突，记 0~1。
2. coverage：问题里的每一部分是否都被回答？漏掉一个分句最高 1 分；全部答到且没有答偏记 2 分。
3. honesty：证据不足或问题无法回答时，是否明确说明"查不到/无法确认"？如果给出看似确定的答案但实际无据，记 0；如实说明局限记 2。

只输出 JSON，不要解释，格式：
{"faithfulness": 0-2, "coverage": 0-2, "honesty": 0-2, "reason": "不超过 40 字"}"""


async def judge_one(item, report: str, *, model: str | None = None) -> dict:
    """判一道题。失败不抛，返回 None 分，由聚合层按缺失处理。"""
    body = body_of(report)[:MAX_CHARS]
    if not body.strip():
        return {"faithfulness": 0, "coverage": 0, "honesty": 0, "reason": "正文为空"}

    prompt = (
        f"{SYSTEM}\n\n"
        f"【用户问题】\n{item.question}\n\n"
        f"【参考答案】\n{item.reference or '（无参考答案，按问题本身判断答案是否切题、是否有据）'}\n\n"
        f"【报告正文】\n{body}\n"
    )

    try:
        reply = await llm_mod.ainvoke(prompt, model=model or settings.llm.judge_model_name)
        data = llm_mod.extract_json(reply.text)
    except Exception as e:  # noqa: BLE001 - 判分失败不该中断评测
        return {"faithfulness": None, "coverage": None, "honesty": None,
                "reason": f"判分失败：{type(e).__name__}", "tokens": 0}

    def _clamp(key: str) -> int | None:
        try:
            return max(0, min(2, int(data.get(key))))
        except (TypeError, ValueError):
            return None

    return {
        "faithfulness": _clamp("faithfulness"),
        "coverage": _clamp("coverage"),
        "honesty": _clamp("honesty"),
        "reason": str(data.get("reason") or "")[:80],
        "tokens": reply.tokens,
    }


async def judge_rows(rows: list[dict], items_by_id: dict, *, model: str | None = None,
                     concurrency: int = 4) -> dict[str, dict]:
    """批量判分，按题号返回结果。并发受控——判分调用很轻，但没必要一次打几十个。"""
    sem = asyncio.Semaphore(concurrency)
    out: dict[str, dict] = {}

    async def _one(row: dict):
        item = items_by_id.get(row["qid"])
        if item is None:
            return
        async with sem:
            out[row["qid"]] = await judge_one(item, row.get("report") or "", model=model)

    await asyncio.gather(*[_one(r) for r in rows])
    return out


def mean(values: list) -> float | None:
    vals = [v for v in values if isinstance(v, (int, float))]
    return round(sum(vals) / len(vals), 4) if vals else None
