"""
日志：控制台 + 按天切分的文件。

研究型链路的中间状态（规划了哪几步、哪路证据没拿到、审查回环补了什么）
全靠日志回溯，所以文件日志默认开启。
"""

from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger

from app.conf.settings import PROJECT_ROOT

LOG_DIR = PROJECT_ROOT / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

# 去掉 loguru 默认的 sink，避免重复输出
logger.remove()

logger.add(
    sys.stderr,
    level="INFO",
    format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan> - <level>{message}</level>",
)

logger.add(
    LOG_DIR / "zhixun_{time:YYYY-MM-DD}.log",
    level="DEBUG",
    rotation="10 MB",
    retention="7 days",
    encoding="utf-8",
    # 研究链路的日志量不小，按天切分 + 保留一周足够回溯
)

__all__ = ["logger"]
