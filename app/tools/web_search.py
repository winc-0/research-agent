"""
工具一：联网搜索（百炼 MCP WebSearch）。

原方案用 Tavily（境外付费 API），这里换成百炼 MCP——
履约知识助手项目里已经跑通过，key 是自己的，也省掉一层境外网络依赖。

调用方式刻意不使用新版 mcp SDK：`mcp` 2.x 握手会发 2026 版协议的
`server/discover` 方法，百炼服务端尚未支持，直接返回 500 导致握手就失败。
这里用标准 JSON-RPC 的 `initialize` + `tools/call` 两步，行为稳定。
"""

from __future__ import annotations

import json

import httpx

from app.conf.settings import settings
from app.core.log import logger
from app.tools.base import Evidence, ToolResult, ToolTimer

SOURCE = "web"


def _parse_rpc_message(raw_text: str) -> dict:
    """兼容纯 JSON 与 SSE（text/event-stream）两种响应体。"""
    if not raw_text:
        return {}

    for line in raw_text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[len("data:"):].strip()
        if not data or data == "[DONE]":
            continue
        try:
            return json.loads(data)
        except json.JSONDecodeError:
            continue

    try:
        return json.loads(raw_text)
    except json.JSONDecodeError:
        logger.warning(f"[web_search] 响应无法解析为 JSON，前 200 字符: {raw_text[:200]}")
        return {}


def _extract_docs(payload: dict) -> list[dict]:
    """从 JSON-RPC 响应里取出网页列表 [{"title","url","snippet"}]。"""
    if not isinstance(payload, dict):
        return []

    error = payload.get("error")
    if error:
        logger.warning(f"[web_search] MCP 返回错误: {error}")
        return []

    content = (payload.get("result") or {}).get("content") or []
    json_str = ""
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text":
            json_str = item.get("text") or ""
            break

    if not json_str:
        return []

    try:
        data = json.loads(json_str)
    except json.JSONDecodeError:
        logger.warning(f"[web_search] 工具返回不是合法 JSON，前 200 字符: {json_str[:200]}")
        return []

    pages = data.get("pages", []) if isinstance(data, dict) else []
    return [
        {
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "snippet": item.get("snippet", ""),
        }
        for item in pages
        if isinstance(item, dict)
    ]


def _search(query: str, count: int | None = None) -> list[dict]:
    """一次完整的 MCP 调用：握手 → initialized 通知 → tools/call。"""
    cfg = settings.web
    count = count or cfg.count
    if not query or not cfg.base_url or not cfg.api_key:
        return []

    headers = {
        "Authorization": f"Bearer {cfg.api_key}",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }

    with httpx.Client(timeout=cfg.timeout) as client:
        init_body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": cfg.protocol_version,
                "capabilities": {},
                "clientInfo": {"name": "zhixun-web-search", "version": "1.0.0"},
            },
        }
        resp = client.post(cfg.base_url, headers=headers, json=init_body)
        resp.raise_for_status()

        session_id = resp.headers.get("mcp-session-id")
        if session_id:
            headers["Mcp-Session-Id"] = session_id

        # 通知失败不影响正式调用，只记录
        try:
            client.post(
                cfg.base_url,
                headers=headers,
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            )
        except Exception as e:
            logger.warning(f"[web_search] initialized 通知失败，忽略: {e}")

        call_body = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": cfg.tool_name,
                "arguments": {"query": query, "count": count},
            },
        }
        resp = client.post(cfg.base_url, headers=headers, json=call_body)
        resp.raise_for_status()

        return _extract_docs(_parse_rpc_message(resp.text))


def web_search(query: str, count: int | None = None) -> ToolResult:
    """
    联网检索。失败时返回 ok=False 的空结果，不抛异常（交由上层降级处理）。

    :param query: 检索词（由 planner 产出的子任务改写而来）
    :param count: 期望条数
    """
    result = ToolResult(source=SOURCE, ok=False)

    try:
        with ToolTimer() as timer:
            docs = _search(query, count)
    except Exception as e:
        # 超时、鉴权失效、服务端 500 都走这里：联网是可选证据源，
        # 拿不到不应该让整条研究链路崩掉。
        # with 内抛异常时 __exit__ 仍会执行，所以这里能读到真实耗时
        result.elapsed = timer.elapsed
        result.error = f"联网搜索失败：{type(e).__name__}: {e}"
        logger.warning(f"[web_search] {result.error}")
        return result

    result.elapsed = timer.elapsed

    # 置信度按返回位次衰减：搜索引擎自己的排序是有信息量的
    evidences = []
    for idx, doc in enumerate(docs):
        snippet = (doc.get("snippet") or "").strip()
        if not snippet and not doc.get("title"):
            continue
        evidences.append(Evidence(
            source=SOURCE,
            claim=(doc.get("title") or "").strip() or snippet[:60],
            snippet=snippet,
            ref=doc.get("url", ""),
            confidence=max(0.3, 0.9 - idx * 0.1),
            payload=doc,
        ))

    result.ok = True
    result.evidences = evidences
    logger.info(f"[web_search] 命中 {len(evidences)} 条，耗时 {result.elapsed:.2f}s")
    return result


if __name__ == "__main__":
    r = web_search("2026年国内大模型备案政策最新要求")
    print(json.dumps(r.to_dict(), ensure_ascii=False, indent=2))
