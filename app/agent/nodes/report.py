"""
报告生成：Markdown + 引用脚注。

两处不让模型插手的地方：
1. 引用清单（参考来源）由代码拼——模型列来源时经常改写 URL 或漏项
2. 运行元信息（轮次、耗时、token、哪一路降级过）由代码拼——这是可信度的一部分，
   模型不知道真实数字，让它写只会编

引用编号的越界检查也在这里：模型偶尔会引用不存在的编号（幻觉引用），
统计出来既能在报告里提示，也是评测「引用准确率」的原始数据。
"""

from __future__ import annotations

import re

from app.agent import llm as llm_mod
from app.agent.prompts import render
from app.agent.state import AgentState
from app.core.log import logger

_CITE = re.compile(r"\[(\d{1,3})\]")

# 模型写引用的几种常见漂移：`<1>`、<1>、［1］、`[1]`。
# 不归一化的话，_check_citations 一个都认不出，引用准确率会变成假数据。
_CITE_ANGLE = re.compile(r"`?<\s*(\d{1,3})\s*>`?")
_CITE_FULLWIDTH = re.compile(r"[［\[]\s*(\d{1,3})\s*[］\]]")
_CITE_BACKTICK = re.compile(r"`\s*\[\s*(\d{1,3})\s*\]\s*`")

# 证据正文给模型的上限：再多就是浪费，compress 已经按重要性排过序
MAX_EVIDENCE_CHARS = 12000


async def report(state: AgentState) -> dict:
    """生成最终报告。"""
    question = state["question"]
    citations = state.get("citations") or []
    tokens = int(state.get("tokens") or 0)

    if not citations:
        # 零证据也要出东西：一份说明「为什么没查到」的短报告，
        # 比返回空字符串或抛错更能反映系统状态
        logger.warning("[report] 无证据，生成说明性报告")
        return {"report": _no_evidence(question, state), "tokens": tokens}

    prompt = render(
        "report",
        question=_with_feedback(question, state),
        evidences=_evidence_block(citations)[:MAX_EVIDENCE_CHARS],
    )

    try:
        reply = await llm_mod.ainvoke(prompt)
        tokens += reply.tokens
        body = _normalize_citations(reply.text.strip())
    except Exception as e:
        logger.error(f"[report] 生成失败：{type(e).__name__}: {e}")
        return {
            "report": _fallback(question, citations, f"报告生成调用失败：{type(e).__name__}"),
            "tokens": tokens,
            "stop_reason": state.get("stop_reason") or "report_error",
        }

    bad = _check_citations(body, len(citations))
    if bad:
        logger.warning(f"[report] 检测到越界引用编号 {sorted(bad)}（共 {len(citations)} 条证据）")

    parts = [f"# {question.strip()}", "", body, "", "---", "", _references(citations), "", _meta(state, citations, bad)]
    return {"report": "\n".join(parts), "tokens": tokens, "stop_reason": state.get("stop_reason") or "normal"}


def _with_feedback(question: str, state: AgentState) -> str:
    """
    把用户在确认环节提的意见拼进问题。

    不单独开一节模板变量：报告提示词只需要知道「还有哪些要求没被覆盖」，
    拼在问题后面语义最直接，也避免模板里出现一堆可选占位。
    """
    feedback = (state.get("feedback") or "").strip()
    if not feedback:
        return question
    return f"{question}\n\n用户的补充要求（能答就答，证据不足则写进局限）：{feedback}"


def _evidence_block(citations: list[dict]) -> str:
    """给模型的证据清单。编号、来源、结论、原文、出处缺一不可。"""
    rows = []
    for c in citations:
        rows.append(
            f"[{c['index']}] 来源={c['source']} 置信度={c['confidence']:.2f}\n"
            f"  结论：{c['claim']}\n"
            f"  原文：{c['snippet']}\n"
            f"  出处：{c['ref']}\n"
        )
    return "\n".join(rows)


def _references(citations: list[dict]) -> str:
    """
    参考来源：纯代码生成，保证与正文编号严格对应。

    同一出处可能被多个切片引用（一份手册切成几段各命中一次），
    这里按出处合并编号，避免参考列表里出现一串相同 URL。
    """
    groups: dict[str, list[int]] = {}
    sources: dict[str, str] = {}
    order: list[str] = []
    for c in citations:
        key = c.get("ref") or "（无出处）"
        if key not in groups:
            groups[key] = []
            sources[key] = c.get("source") or ""
            order.append(key)
        groups[key].append(c["index"])

    lines = ["## 参考来源", ""]
    for key in order:
        label = "".join(f"[{i}]" for i in groups[key])
        src = {"web": "联网", "kb": "知识库", "sql": "数仓"}.get(sources[key], sources[key])
        lines.append(f"- {label} [{src}] {key}")
    return "\n".join(lines)


def _meta(state: AgentState, citations: list[dict], bad: set[int]) -> str:
    """运行元信息。面试时这一节能直接说明系统的可观测性。"""
    stats = state.get("route_stats") or {}
    rows = ["## 运行信息", ""]
    rows.append(f"- 检索轮次：{int(state.get('review_round') or 0)} 轮审查")
    rows.append(f"- 工具调用：{int(state.get('steps') or 0)} 次")
    rows.append(f"- token 消耗：{int(state.get('tokens') or 0)}")
    rows.append(f"- 入选证据：{len(citations)} 条（原始 {len(state.get('evidences') or [])} 条）")

    if stats:
        rows.append("- 分路统计：")
        for route, s in sorted(stats.items()):
            rows.append(
                f"  - {route}：成功 {s.get('ok', 0)} / 失败 {s.get('fail', 0)}，"
                f"证据 {s.get('evidences', 0)} 条，累计 {float(s.get('elapsed', 0)):.2f}s"
            )

    degraded = sorted({c.get("degraded_from") for c in citations if c.get("degraded_from")})
    if degraded:
        rows.append(f"- 降级记录：{'、'.join(degraded)} 失败后改走其他证据源")

    trips = state.get("guard_trips") or []
    if trips:
        rows.append("- 异常与守卫：")
        for t in trips[:10]:
            rows.append(f"  - {t}")
        if len(trips) > 10:
            rows.append(f"  - ……另有 {len(trips) - 10} 条")

    stop = state.get("stop_reason") or "normal"
    if stop != "normal":
        rows.append(f"- **提前收尾**：{stop}（证据可能不完整）")
    if bad:
        rows.append(f"- **引用异常**：正文出现越界编号 {sorted(bad)}，对应陈述缺少可靠出处")

    return "\n".join(rows)


def _normalize_citations(body: str) -> str:
    """
    把模型写歪的引用编号统一成 `[n]`。

    实测 qwen 会把 `[1]` 输出成 `<1>` 甚至带反引号的 `` `<1>` ``，
    这既影响读者阅读，也让引用准确率的统计失真。三种变形一次收掉。
    """
    body = _CITE_BACKTICK.sub(r"[\1]", body)
    body = _CITE_ANGLE.sub(r"[\1]", body)
    body = _CITE_FULLWIDTH.sub(r"[\1]", body)
    return body


def _check_citations(body: str, total: int) -> set[int]:
    """找出正文里超出证据范围的编号。"""
    return {int(m) for m in _CITE.findall(body) if int(m) < 1 or int(m) > total}


def _no_evidence(question: str, state: AgentState) -> str:
    trips = state.get("guard_trips") or []
    lines = [
        f"# {question.strip()}",
        "",
        "## 结论",
        "",
        "现有证据不足以回答该问题：三路证据源均未返回可用结果。",
        "",
        "## 局限",
        "",
        "工具层可能不可用（联网 / 知识库 / 数仓任一服务异常都会导致该路为空）。",
        "建议先运行 `python scripts/check_tools.py` 确认各路连通性后再重试。",
    ]
    if trips:
        lines += ["", "## 运行信息", ""] + [f"- {t}" for t in trips[:10]]
    return "\n".join(lines)


def _fallback(question: str, citations: list[dict], reason: str) -> str:
    """模型不可用时的兜底：把证据原样列出来，至少信息不丢。"""
    lines = [
        f"# {question.strip()}",
        "",
        f"> 报告生成失败（{reason}），以下为已检索到的证据原文。",
        "",
    ]
    lines += [f"- [{c['index']}] ({c['source']}) {c['claim']} —— {c['ref']}" for c in citations]
    lines += ["", "---", "", _references(citations)]
    return "\n".join(lines)
