"""
守卫：给自主循环装刹车。

这类系统最常见的翻车不是"答错"，而是"停不下来"——规划→检索→审查不足→再规划，
每轮都在花钱，最后一轮比第一轮好不了多少。三处刹车：

1. 步数上限：工具调用总次数
2. token 上限：LLM 累计消耗
3. 循环检测：补检索时反复用同样的问法打同样的路（重复率高说明在原地打转）

触发后的行为统一是「带着已有证据收尾」，而不是抛异常。
一份标注了「证据不完整」的报告，比一个 500 错误有用。
"""

from __future__ import annotations

from app.agent.state import AgentState
from app.conf.settings import settings
from app.core.log import logger


async def guard(state: AgentState) -> dict:
    """检查是否该停止扩大检索范围。空返回表示放行。"""
    cfg = settings.guard
    steps = int(state.get("steps") or 0)
    tokens = int(state.get("tokens") or 0)
    calls = state.get("calls") or []
    round_no = int(state.get("review_round") or 0)

    stop = ""
    reason = ""

    if steps >= cfg.max_steps:
        stop, reason = "step_budget", f"工具调用已达上限 {cfg.max_steps} 次"
    elif tokens >= cfg.max_tokens:
        stop, reason = "token_budget", f"token 消耗已达上限 {cfg.max_tokens}"
    else:
        dup = len(calls) - len(set(calls))
        # 首轮里重复是规划问题（worker 已跳过），但只要还没进回环就不算卡死；
        # 进过回环后重复率仍过半，基本可以判定在打转。
        # 同时要求至少 2 条重复，避免一次正常的问题重合就误判。
        if round_no >= 1 and dup >= 2 and calls and dup / len(calls) >= 0.5:
            stop, reason = "loop_detected", f"重复调用占比 {dup}/{len(calls)}，判定为原地打转"

    if not stop:
        return {}

    logger.warning(f"[guard] 触发守卫：{reason}，带着现有证据收尾")
    return {
        "stop_reason": stop,
        "guard_trips": [f"guard:{stop} - {reason}"],
    }
