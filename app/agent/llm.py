"""
LLM 接入与结构化输出解析。

统一收口两件事，避免每个节点各写一套：
1. 调用 + token 计量：返回值里带本次消耗，节点把它累加进 state
2. JSON 提取：模型偶尔会加 ```json 围栏或前置解释，这里做容错
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from langchain_openai import ChatOpenAI

from app.conf.settings import settings

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


@dataclass
class LLMReply:
    """一次 LLM 调用的结果。"""

    text: str
    tokens: int = 0


def get_llm(*, model: str | None = None, temperature: float | None = None) -> ChatOpenAI:
    """
    构造 chat 模型。

    qwen 走 OpenAI 兼容端点，所以直接复用 ChatOpenAI，不需要额外适配层。
    评测脚本可以传 model 覆盖成更便宜的型号。
    """
    cfg = settings.llm
    return ChatOpenAI(
        model=model or cfg.model_name,
        base_url=cfg.base_url,
        api_key=cfg.api_key,
        temperature=cfg.temperature if temperature is None else temperature,
        timeout=cfg.timeout,
        max_retries=cfg.max_retries,
    )


async def ainvoke(prompt: str, *, model: str | None = None, temperature: float | None = None) -> LLMReply:
    """异步调用一次，返回文本与 token 消耗。"""
    resp = await get_llm(model=model, temperature=temperature).ainvoke(prompt)
    usage = getattr(resp, "usage_metadata", None) or {}
    tokens = int(usage.get("total_tokens") or 0)
    text = resp.content if isinstance(resp.content, str) else str(resp.content)
    return LLMReply(text=text, tokens=tokens)


def extract_json(text: str) -> dict:
    """
    从模型输出里取出第一个 JSON 对象。

    依次尝试：原文整体 → 剥 ```json 围栏 → 花括号配对扫描。
    配对比 `find("{")`+`rfind("}")` 稳：模型常在 JSON 后面补一句解释，
    解释里若再出现 `{}`（比如「routes 只能是 {web, kb, sql}」），
    首尾截取就会把两个片段一起吞进去，变成非法 JSON。
    """
    raw = (text or "").strip()
    if not raw:
        raise ValueError("模型返回为空")

    for candidate in _candidates(raw):
        try:
            data = json.loads(candidate, strict=False)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data

    raise ValueError(f"无法从模型输出中解析 JSON：{raw[:200]}")


def _candidates(raw: str) -> list[str]:
    """按成功率从高到低生成待解析片段。"""
    out = [raw]

    fenced = _FENCE.search(raw)
    if fenced:
        out.append(fenced.group(1).strip())

    out.extend(_balanced_objects(raw))
    return out


def _balanced_objects(raw: str) -> list[str]:
    """扫描出所有「花括号配平」的片段，跳过字符串内的括号。"""
    found: list[str] = []
    depth = 0
    start = -1
    in_str = False
    escaped = False

    for i, ch in enumerate(raw):
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue

        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0 and start != -1:
                found.append(raw[start : i + 1])
                start = -1

    return found
