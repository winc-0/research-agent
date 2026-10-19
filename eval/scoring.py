"""
自动评分：能算的都用规则算。

为什么不全交给 LLM 判：判分模型本身有随机性，同一份报告两次打分能差一档，
拿这种数字去写「提升 12 个百分点」是不负责任的。所以事实命中、引用有效性、
引用覆盖率全部用确定性规则，只有「表达是否忠实、有没有答偏」才值得问一次模型。

数值比对做了「万/亿」换算和 2% 容差：模型写「约 263.6 万」和写 2,636,218.99
是同一件事，严格字符串比对会把正确答案判错。
"""

from __future__ import annotations

import re

from app.core.text import norm

# 引用编号：[1] / [12]
_CITE = re.compile(r"\[(\d{1,3})\]")
# 数字：允许千分位与小数，后面可跟万/亿
_NUM = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(万|亿)?")
_MULT = {"万": 1e4, "亿": 1e8}

# 「明确说查不到」的表述。第五层题只有命中这里才算过关
_REFUSAL = (
    "无法", "未检索到", "未找到", "没有找到", "没有检索到", "查不到", "无法确认",
    "不包含", "未提供", "不存在", "不足以", "没有相关", "无相关", "未覆盖",
    "无法回答", "不能确定", "没有数据", "缺少",
)


def numbers(text: str) -> list[float]:
    """抽出文本里的全部数值，带万/亿换算。"""
    text = _CITE.sub(" ", text or "")
    out: list[float] = []
    for raw, unit in _NUM.findall(text):
        try:
            val = float(raw.replace(",", ""))
        except ValueError:
            continue
        if unit:
            val *= _MULT[unit]
        out.append(val)
    return out


def fact_hits(body: str, facts: list[dict]) -> list[bool]:
    """逐条判断事实是否出现在正文里。"""
    flat = norm(body)
    nums = numbers(body)
    hits: list[bool] = []

    for fact in facts:
        if fact.get("type") == "num":
            target = float(fact["value"])
            tol = float(fact.get("tol") or 0.02)
            low, high = target * (1 - tol), target * (1 + tol)
            hits.append(any(low <= n <= high for n in nums))
        else:
            alts = [norm(a) for a in fact.get("any") or []]
            hits.append(any(a and a in flat for a in alts))

    return hits


def cite_stats(body: str, total: int) -> dict:
    """
    引用有效性。

    `invalid` 是模型引用了不存在的编号（幻觉引用），这是硬伤：
    它会让读者以为有出处，其实没有。
    """
    used = [int(m) for m in _CITE.findall(body or "")]
    invalid = [n for n in used if n < 1 or n > total]
    return {
        "cited": len(used),
        "invalid": len(invalid),
        # 用不重复的引用数衡量「引了多少条证据」，避免同一编号反复出现把数字撑大
        "distinct": len(set(used)),
        "valid_rate": round(1 - len(invalid) / len(used), 4) if used else 0.0,
    }


def paragraph_citation_ratio(body: str) -> float:
    """
    正文段落里带引用的比例。

    只统计有实质内容的段落：标题、表格行、分隔线不算——
    标题天然不带引用，把它们算进分母会让所有变体一起偏低，失去区分度。
    """
    paras: list[str] = []
    for block in re.split(r"\n\s*\n", body or ""):
        text = block.strip()
        if not text or text.startswith("#") or text.startswith("|") or text.startswith("---"):
            continue
        if len(text) < 30:
            continue
        paras.append(text)

    if not paras:
        return 0.0
    with_cite = sum(1 for p in paras if _CITE.search(p))
    return round(with_cite / len(paras), 4)


def declared_limitation(body: str) -> bool:
    """是否明确承认「查不到」。不可答层靠这个判，而不是靠关键词猜答案对不对。"""
    text = body or ""
    return any(k in text for k in _REFUSAL)


def route_hit(route_stats: dict, expect_routes: list[str]) -> float | None:
    """
    期望路由是否真的取到了证据。

    返回 None 表示这道题本来就不该有路由命中（不可答层的题）。
    只看「证据条数 > 0」，不看是否成功调用——调用成功但返回空，
    对回答没有帮助，不该算命中。
    """
    if not expect_routes:
        return None
    hit = sum(1 for r in expect_routes if (route_stats.get(r) or {}).get("evidences", 0) > 0)
    return round(hit / len(expect_routes), 4)


def score_row(item, row: dict) -> dict:
    """把一道题的运行结果算成一行指标。"""
    from eval.schema import body_of

    body = body_of(row.get("report") or "")
    hits = fact_hits(body, item.facts)
    cites = cite_stats(body, len(row.get("citations") or []))

    return {
        "qid": item.id,
        "layer": item.layer,
        "fact_hit": round(sum(hits) / len(hits), 4) if hits else None,
        "fact_n": len(hits),
        "cite_valid": cites["valid_rate"],
        "cite_invalid": cites["invalid"],
        "cite_distinct": cites["distinct"],
        "cite_coverage": paragraph_citation_ratio(body),
        "declared": declared_limitation(body),
        "route_hit": route_hit(row.get("route_stats") or {}, item.expect_routes),
        "citations": len(row.get("citations") or []),
        "evidences": len(row.get("evidences") or []),
        "todos": len(row.get("todos") or []),
        "tokens": row.get("tokens") or 0,
        "steps": row.get("steps") or 0,
        "elapsed": row.get("elapsed") or 0.0,
        "pool_miss": len(row.get("pool_misses") or []),
        "stop_reason": row.get("stop_reason") or "",
        "error": row.get("error") or "",
        "report_chars": len(body),
        # 报告正文一并存下来：指标掉分时得能直接看是哪句话没写对，
        # 否则只能重跑一遍（重跑又要花一次模型钱）
        "report": row.get("report") or "",
    }
