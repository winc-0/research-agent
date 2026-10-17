"""
配置加载。

统一从环境变量读取（.env 由 python-dotenv 载入），敏感信息不进代码、不进仓库。

本项目刻意不引入 OmegaConf：配置项都是扁平的标量，用 dataclass 足够，
少一层依赖也少一份运行期的不确定性。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# 项目根目录（app/conf/settings.py -> 上溯三级）
PROJECT_ROOT = Path(__file__).resolve().parents[2]

load_dotenv(PROJECT_ROOT / ".env")


def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


def _env_int(key: str, default: int) -> int:
    raw = _env(key, str(default))
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    raw = _env(key, str(default))
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass
class LLMConfig:
    """主链路模型；评测可单独指定更便宜的型号。"""
    model_name: str = field(default_factory=lambda: _env("LLM_MODEL_NAME", "qwen-plus"))
    base_url: str = field(default_factory=lambda: _env("LLM_BASE_URL"))
    api_key: str = field(default_factory=lambda: _env("LLM_API_KEY"))
    temperature: float = 0.0
    timeout: float = field(default_factory=lambda: _env_float("LLM_TIMEOUT", 120))
    max_retries: int = 2

    # 评测专用型号：默认 qwen-flash，全量跑一輪能省一个数量级的钱
    eval_model_name: str = field(default_factory=lambda: _env("LLM_EVAL_MODEL_NAME", "qwen-flash"))


@dataclass
class WebSearchConfig:
    """百炼 MCP WebSearch：原方案是 Tavily，这里换成已跑通的百炼 MCP。"""
    base_url: str = field(default_factory=lambda: _env("MCP_DASHSCOPE_BASE_URL"))
    api_key: str = field(default_factory=lambda: _env("MCP_API_KEY") or _env("LLM_API_KEY"))
    count: int = field(default_factory=lambda: _env_int("WEB_SEARCH_COUNT", 5))
    timeout: float = field(default_factory=lambda: _env_float("WEB_SEARCH_TIMEOUT", 15))
    # 百炼 WebSearch MCP 暴露的工具名
    tool_name: str = "bailian_web_search"
    # 与履约知识助手保持一致：不使用新版 mcp SDK 的握手方式（服务端会返回 500）
    protocol_version: str = "2024-11-05"


@dataclass
class KBConfig:
    """
    内部知识库：复用履约知识助手 Warehouse-KB 的检索服务。

    为什么不自己连 Milvus？履约知识助手 collection 里的向量是 BGE-M3（1024 维）生成的，
    物流研究助手要检索同一个 collection 就必须用同一个模型编码，等于要把 torch +
    FlagEmbedding 再装一遍（数 GB）。走 HTTP 调履约知识助手，既省掉这份依赖，
    也让「物流研究助手编排履约知识助手」这件事在服务边界上真实成立，而不是靠复制代码。
    """
    service_url: str = field(default_factory=lambda: _env("KB_SERVICE_URL", "http://localhost:8001"))
    timeout: float = field(default_factory=lambda: _env_float("KB_SERVICE_TIMEOUT", 60))
    top_k: int = field(default_factory=lambda: _env_int("KB_TOP_K", 8))


@dataclass
class SQLConfig:
    """
    结构化数据：复用仓配自助问数 LogiQL 的问数服务。

    为什么不自己连 MySQL 生成 SQL？NL2SQL 的难点不在执行，
    而在「从 32 张表里挑对字段」——那需要元数据召回（Qdrant 字段/指标向量 + ES 取值），
    等于把仓配自助问数的 12 节点链路重写一遍。调它的服务，物流研究助手只负责编排与证据组织。
    """
    service_url: str = field(default_factory=lambda: _env("ZHISHU_SERVICE_URL", "http://localhost:8010"))
    timeout: float = field(default_factory=lambda: _env_float("ZHISHU_SERVICE_TIMEOUT", 180))
    # 结果集行数上限：一条 SELECT 把整张表捞回来会瞬间撑爆上下文预算
    max_rows: int = field(default_factory=lambda: _env_int("SQL_MAX_ROWS", 200))


@dataclass
class GuardConfig:
    """守卫阈值。超限时不是报错崩掉，而是带着已收集的证据收尾。"""
    max_steps: int = field(default_factory=lambda: _env_int("GUARD_MAX_STEPS", 30))
    max_tokens: int = field(default_factory=lambda: _env_int("GUARD_MAX_TOKENS", 120000))
    review_max_rounds: int = field(default_factory=lambda: _env_int("REVIEW_MAX_ROUNDS", 2))


@dataclass
class HITLConfig:
    """
    报告生成前的人工确认。

    默认开启；CLI 会显式关掉（命令行没人盯着，挂起等于卡死）。
    超时兜底为「自动确认继续」——宁可少问一次，也不能把会话永远悬在那儿。
    """
    enabled: bool = field(default_factory=lambda: _env("HITL_ENABLED", "true").lower() in ("1", "true", "yes", "on"))
    timeout: float = field(default_factory=lambda: _env_float("HITL_TIMEOUT", 600))


@dataclass
class WorkspaceConfig:
    root: Path = field(default_factory=lambda: PROJECT_ROOT / _env("WORKSPACE_ROOT", "output"))


@dataclass
class Settings:
    llm: LLMConfig = field(default_factory=LLMConfig)
    web: WebSearchConfig = field(default_factory=WebSearchConfig)
    kb: KBConfig = field(default_factory=KBConfig)
    sql: SQLConfig = field(default_factory=SQLConfig)
    guard: GuardConfig = field(default_factory=GuardConfig)
    hitl: HITLConfig = field(default_factory=HITLConfig)
    workspace: WorkspaceConfig = field(default_factory=WorkspaceConfig)


settings = Settings()
