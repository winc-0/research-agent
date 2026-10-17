"""
汇聚压缩：去重 → 配额 → 编号 → 预算截断。

这一环最容易被漏掉，也是「上下文工程」在真实系统里最见功力的地方：
原始证据直接拼进提示词，会出现三种问题——同一段内容被两路各召回一次（浪费预算）、
某一路因为条数多把另一路挤掉（证据结构失衡）、总长超过模型窗口（静默截断，结论就开始瞎编）。

处理顺序有讲究：先按置信度全局排序去重，再做「按路轮转」的配额分配。
如果先切配额再去重，重复项会白占掉某一路的配额。
"""

from __future__ import annotations

from app.agent.state import AgentState
from app.core.log import logger
from app.core.text import jaccard

# 每路最多保留多少条：三路 × 8 = 24，足够覆盖，又不会让某一路独大
MAX_PER_ROUTE = 8
# 总条数上限
MAX_TOTAL = 24
# 总字符预算。中文 1 字约 0.7~1 token，12000 字留给报告生成约 8k~12k token，
# 加上提示词与输出，仍在 qwen-plus 的窗口内
MAX_CHARS = 12000
# 单条 snippet 截断长度：引用只需要关键片段，整段手册塞进去没有价值
SNIPPET_LIMIT = 600
# 判重阈值。1.0 是完全相同；同一手册的不同切片相似度通常在 0.5~0.7，不该被合并
DUP_THRESHOLD = 0.85

# 轮转取用顺序：内部事实优先，公开网页兜底
ROUTE_ORDER = ("kb", "sql", "web")


async def compress(state: AgentState) -> dict:
    """把散落的证据整理成带编号、可引用的引用表。"""
    return await _compress(state, dedup=True, quota=True)


def make_compress(*, dedup: bool = True, quota: bool = True):
    """
    构造带开关的压缩节点，供消融实验使用。

    :param dedup: 是否做跨源去重（关掉就相当于「直接把召回结果拼进提示词」）
    :param quota: 是否做按路配额与轮转（关掉就按置信度全局截断，某一路可以独占）

    线上路径用 `compress`，行为与这两个开关全开时完全一致。
    """
    async def _node(state: AgentState) -> dict:
        return await _compress(state, dedup=dedup, quota=quota)

    return _node


async def _compress(state: AgentState, *, dedup: bool, quota: bool) -> dict:
    raw = state.get("evidences") or []
    if not raw:
        logger.info("[compress] 无证据可压缩")
        return {"citations": []}

    # ---------- 1. 去重（按置信度降序扫描，留下更可信的那条） ----------
    ordered = sorted(raw, key=lambda e: -float(e.get("confidence") or 0.0))
    kept: list[dict] = []
    dropped = 0

    for ev in ordered:
        text = _dedup_text(ev)
        if not text.strip():
            # 只丢掉彻底为空的，这属于清理，不属于去重
            dropped += 1
            continue
        if dedup:
            if len(text) < 8:
                # claim 和 snippet 都几乎为空，这条没有引用价值
                dropped += 1
                continue
            if any(jaccard(text, _dedup_text(k)) >= DUP_THRESHOLD for k in kept):
                dropped += 1
                continue
        kept.append(ev)

    # ---------- 2. 配额 ----------
    if quota:
        kept, dropped = _apply_quota(kept), dropped
    else:
        # 不做配额：按置信度全局截断，条数上限仍然保留（否则预算会失控）
        dropped += max(0, len(kept) - MAX_TOTAL)
        kept = kept[:MAX_TOTAL]

    # ---------- 3. 预算截断 + 编号 ----------
    picked: list[dict] = []
    budget = MAX_CHARS
    for ev in kept:
        snippet = _clip(ev.get("snippet") or "", SNIPPET_LIMIT)
        cost = len(snippet) + len(ev.get("claim") or "") + len(ev.get("ref") or "")
        if cost > budget:
            break
        budget -= cost
        picked.append(
            {
                "source": ev.get("source") or "",
                "claim": ev.get("claim") or "",
                "snippet": snippet,
                "ref": ev.get("ref") or "",
                "confidence": round(float(ev.get("confidence") or 0.0), 4),
                "todo_ids": [ev["todo_id"]] if ev.get("todo_id") else [],
                "degraded_from": ev.get("degraded_from") or "",
            }
        )

    citations = []
    for i, item in enumerate(picked, start=1):
        item["index"] = i
        citations.append(item)

    logger.info(
        f"[compress] 原始 {len(raw)} 条 → 整理后 {len(kept)} 条 → 入选 {len(citations)} 条"
        f"（丢弃重复/无效 {dropped} 条，剩余预算 {budget} 字，"
        f"去重={'开' if dedup else '关'} 配额={'开' if quota else '关'}）"
    )
    return {"citations": citations}


def _apply_quota(kept: list[dict]) -> list[dict]:
    """按路分组，每路上限 + 轮转取用，避免某一路的条数优势挤掉其他路。"""
    groups: dict[str, list[dict]] = {}
    for ev in kept:
        groups.setdefault(ev.get("source") or "web", []).append(ev)

    # 组内仍保持置信度降序（kept 已按 conf 排过，这里是稳定切分）
    order = [r for r in ROUTE_ORDER if r in groups] + [r for r in groups if r not in ROUTE_ORDER]
    for route in order:
        groups[route] = groups[route][:MAX_PER_ROUTE]

    picked: list[dict] = []
    cursor = {route: 0 for route in order}
    while len(picked) < MAX_TOTAL:
        advanced = False
        for route in order:
            idx = cursor[route]
            bucket = groups[route]
            if idx >= len(bucket):
                continue
            cursor[route] = idx + 1
            picked.append(bucket[idx])
            advanced = True
            if len(picked) >= MAX_TOTAL:
                break
        if not advanced:
            break
    return picked


def _dedup_text(ev: dict) -> str:
    """
    判重只看 claim + snippet 前段。

    整段 snippet 参与比较会被长文本稀释——同一段话被不同来源截取、
    长短不一致时相似度会掉到阈值以下，反而漏判。
    """
    return f"{ev.get('claim') or ''} {(ev.get('snippet') or '')[:120]}"


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 3] + "..."
