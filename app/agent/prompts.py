"""提示词加载。模板放 prompts/*.md，用 ${var} 占位，避免和 JSON 示例里的花括号冲突。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from string import Template

from app.conf.settings import PROJECT_ROOT

PROMPT_DIR = PROJECT_ROOT / "prompts"


@lru_cache(maxsize=32)
def load(name: str) -> Template:
    path = PROMPT_DIR / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"提示词模板不存在：{path}")
    return Template(path.read_text(encoding="utf-8"))


def render(name: str, **kwargs) -> str:
    """safe_substitute：模板里漏传的变量保持原样，不会抛错中断整条链路。"""
    return load(name).safe_substitute(**kwargs).strip()
