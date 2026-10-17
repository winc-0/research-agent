"""
工具三：结构化数据（调仓配自助问数 LogiQL 的问数服务）。

仓配自助问数返回的是 SSE 流，事件体是 `{"stage": ...}` / `{"sql": ...}` / `{"result": [...]}`
三类混在一起推，这里一边收一边分捡，最后组装成一条证据卡片。

结果集会被截断（SQL_MAX_ROWS）并转成 Markdown 表格：
一行 JSON 直接塞进上下文既占 token 又读不出结构。
"""

from __future__ import annotations

import asyncio
import json

import httpx

from app.conf.settings import settings
from app.core.log import logger
from app.tools.base import Evidence, ToolResult, ToolTimer

SOURCE = "sql"

# 这些错误是确定性的：SQL 里引用了不存在的字段/表、被只读守卫拦下、
# 语法就是错的。重试一次只会拿到一模一样的报错，白等十几秒。
_NON_RETRYABLE = (
    "Unknown column",
    "Unknown table",
    "Unknown database",
    "doesn't exist",
    "You have an error in your SQL",
    "Syntax error",
    "只读",
    "禁止",
)


def _is_retryable(error: str) -> bool:
    """连接类、超时类错误值得重试；语义类错误不值得。"""
    return not any(k in (error or "") for k in _NON_RETRYABLE)


def _rows_to_table(rows: list[dict], max_rows: int) -> str:
    """把结果行转成 Markdown 表格，超出行数截断并注明。"""
    if not rows:
        return "（查询未返回结果）"

    shown = rows[:max_rows]
    headers = list(shown[0].keys())

    lines = ["| " + " | ".join(str(h) for h in headers) + " |",
             "|" + "|".join("---" for _ in headers) + "|"]
    for row in shown:
        lines.append("| " + " | ".join(_fmt(row.get(h)) for h in headers) + " |")

    if len(rows) > max_rows:
        lines.append(f"| … 共 {len(rows)} 行，此处展示前 {max_rows} 行 |" + "".join("|" for _ in headers[1:]))
    return "\n".join(lines)


def _fmt(value) -> str:
    """单元格格式化：None 与长浮点数都会污染表格。"""
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:,.2f}".rstrip("0").rstrip(".")
    text = str(value)
    return text if len(text) <= 40 else text[:37] + "..."


def _summarize(rows: list[dict]) -> str:
    """给证据一句可引用的结论文字（工具侧不调 LLM，只做结构化摘要）。"""
    if not rows:
        return "数仓查询没有返回任何行"
    if len(rows) == 1 and len(rows[0]) == 1:
        col, val = next(iter(rows[0].items()))
        return f"{col} = {_fmt(val)}"
    cols = list(rows[0].keys())
    return f"返回 {len(rows)} 行，字段：{'、'.join(str(c) for c in cols)}"


async def sql_query(question: str) -> ToolResult:
    """
    向仓配自助问数发起一次问数，返回结构化证据。

    :param question: 针对数仓的自然语言问题（由 planner 产出）
    """
    cfg = settings.sql
    result = ToolResult(source=SOURCE, ok=False)
    stages: list[str] = []
    sql = ""
    rows: list[dict] = []
    error = ""

    url = f"{cfg.service_url.rstrip('/')}/api/query"

    try:
        with ToolTimer() as timer:
            async with httpx.AsyncClient(timeout=cfg.timeout) as client:
                async with client.stream("POST", url, json={"query": question}) as resp:
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        line = line.strip()
                        if not line.startswith("data:"):
                            continue
                        raw = line[len("data:"):].strip()
                        if not raw:
                            continue
                        try:
                            payload = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(payload, dict):
                            continue

                        if payload.get("stage"):
                            stages.append(str(payload["stage"]))
                        if payload.get("sql"):
                            sql = str(payload["sql"])
                        if payload.get("result") is not None:
                            rows = payload["result"] if isinstance(payload["result"], list) else []
                        if payload.get("error"):
                            error = str(payload["error"])
    except Exception as e:
        result.elapsed = timer.elapsed
        result.error = f"问数服务调用失败：{type(e).__name__}: {e}"
        logger.warning(f"[sql_query] {result.error}")
        return result

    result.elapsed = timer.elapsed

    if error:
        # 仓配自助问数链路内部报错（SQL 语法错、只读拦截、连接失败等），
        # 对物流研究助手来说这条证据就是没拿到，记为失败让上层决定降级
        result.error = f"问数链路返回错误：{error}"
        result.retryable = _is_retryable(error)
        logger.warning(
            f"[sql_query] {result.error} | 可重试={result.retryable} | stages={stages}"
        )
        return result

    if not rows:
        result.ok = True
        result.evidences = []
        logger.info(f"[sql_query] 查询无结果：{question}")
        return result

    result.ok = True
    result.evidences = [Evidence(
        source=SOURCE,
        claim=_summarize(rows),
        snippet=_rows_to_table(rows, cfg.max_rows),
        ref=sql or question,
        # 结构化数据本身可信，但结论正确性依赖仓配自助问数生成的 SQL，
        # 所以给到 0.8 而不是满分
        confidence=0.8,
        payload={"question": question, "sql": sql, "rows": rows, "stages": stages},
    )]
    logger.info(f"[sql_query] {len(rows)} 行，耗时 {result.elapsed:.2f}s")
    return result


if __name__ == "__main__":
    async def _demo():
        r = await sql_query("2025 年各品类的出库货值排名")
        print(json.dumps(r.to_dict(), ensure_ascii=False, indent=2))

    asyncio.run(_demo())
