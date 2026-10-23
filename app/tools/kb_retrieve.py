"""
工具二：内部知识库（调履约知识助手 的 /retrieve 接口）。

履约知识助手那条链路是「主体确认 → 向量召回 → RRF 融合 → 云端重排」，
重排阶段用的是 DashScope qwen3-rerank——本地 0.6B 模型实测低于不重排，
所以这里必须走服务，不能自己在物流研究助手里复刻。
"""

from __future__ import annotations

import json
import re

import httpx

from app.conf.settings import settings
from app.core.log import logger
from app.tools.base import Evidence, ToolResult, ToolTimer

SOURCE = "kb"

# 取正文首句作为 claim：切片往往是一整段说明书，
# 报告阶段需要一句能直接引用的话，而不是整段原文
_SENTENCE_SPLIT = re.compile(r"(?<=[。；;！!？?])")
# 图片与标题行先剥掉：手册切片常以 `![](...)` 或 `## 警告` 开头，
# 不处理的话 claim 会变成 "!" 或 "## 设备" 这类没有信息量的东西
_IMAGE_MD = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_CJK = re.compile(r"[\u4e00-\u9fff]")


def _first_sentence(text: str, limit: int = 120) -> str:
    """取切片里第一句有实质内容的中文句子。"""
    text = _IMAGE_MD.sub("", text or "")

    for raw_line in text.splitlines():
        line = raw_line.strip().lstrip("#").strip()
        # 太短或不含中文的行没有引用价值（目录项、页码、纯符号）
        if len(line) < 8 or not _CJK.search(line):
            continue
        head = _SENTENCE_SPLIT.split(line)[0].strip() or line
        return head if len(head) <= limit else head[: limit - 3] + "..."

    # 整段都没有合格的行时退化为截断，总比空字符串强
    flat = " ".join(text.split())
    return flat[:limit] + ("..." if len(flat) > limit else "")


def _norm_score(raw) -> float:
    """重排分数归一到 0~1：不同后端量纲不同，直接拿来当置信度会失准。"""
    try:
        score = float(raw)
    except (TypeError, ValueError):
        return 0.5
    if score > 1.0:
        # 部分后端给出的是 0~10 或对数概率，压回 0~1
        score = score / 10.0 if score <= 10 else 1.0
    if score < 0:
        score = 0.0
    return round(max(0.1, min(1.0, score)), 4)


async def kb_retrieve(query: str, top_k: int | None = None) -> ToolResult:
    """
    检索内部知识库，返回结构化证据。

    :param query: 检索词（由 planner 产出）
    :param top_k: 期望切片条数
    """
    cfg = settings.kb
    top_k = top_k or cfg.top_k
    result = ToolResult(source=SOURCE, ok=False)

    url = f"{cfg.service_url.rstrip('/')}/retrieve"

    try:
        with ToolTimer() as timer:
            async with httpx.AsyncClient(timeout=cfg.timeout) as client:
                resp = await client.post(url, json={"query": query, "top_k": top_k})
                resp.raise_for_status()
                payload = resp.json()
    except Exception as e:
        result.elapsed = timer.elapsed
        result.error = f"知识库服务调用失败：{type(e).__name__}: {e}"
        logger.warning(f"[kb_retrieve] {result.error}")
        return result

    result.elapsed = timer.elapsed

    chunks = payload.get("chunks") or []
    evidences = []
    for chunk in chunks:
        content = (chunk.get("content") or "").strip()
        if not content:
            continue
        item_name = chunk.get("item_name") or ""
        chunk_id = chunk.get("chunk_id")
        evidences.append(Evidence(
            source=SOURCE,
            claim=_first_sentence(content),
            snippet=content,
            ref=f"{item_name}#{chunk_id}" if item_name else f"chunk#{chunk_id}",
            confidence=_norm_score(chunk.get("score")),
            payload={"item_name": item_name, "chunk_id": chunk_id},
        ))

    result.ok = True
    result.evidences = evidences
    logger.info(f"[kb_retrieve] 命中 {len(evidences)} 条切片，耗时 {result.elapsed:.2f}s")
    return result


if __name__ == "__main__":
    import asyncio

    async def _demo():
        r = await kb_retrieve("CPD30 叉车的额定载荷是多少")
        print(json.dumps(r.to_dict(), ensure_ascii=False, indent=2))

    asyncio.run(_demo())
