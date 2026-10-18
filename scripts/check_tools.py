"""
三路工具的连通自检。

不依赖 LLM，只验证「工具能不能拿到证据」：
    python scripts/check_tools.py

排查顺序建议先看这一份，再跑主链路——工具层不通时，
上面链路的所有异常都会被包装成看不懂的报错。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.conf.settings import settings  # noqa: E402
from app.core.log import logger  # noqa: E402
from app.tools import kb_retrieve, sql_query, web_search  # noqa: E402

# 三路各自的样例问题：分别偏外部信息、内部文档、结构化数据
CASES = {
    "web": "2026年生成式人工智能服务备案的最新要求",
    "kb": "HAK180烫金机如何调节转印温度",
    "sql": "各品类的销售额排名",
}


async def main() -> int:
    logger.info("=== 工具连通自检 ===")
    logger.info(f"知识库服务: {settings.kb.service_url}")
    logger.info(f"问数服务:   {settings.sql.service_url}")

    results = {}

    web = web_search.web_search(CASES["web"])
    results["web"] = web

    kb = await kb_retrieve.kb_retrieve(CASES["kb"])
    results["kb"] = kb

    sql = await sql_query.sql_query(CASES["sql"])
    results["sql"] = sql

    failed = []
    print("\n" + "=" * 68)
    print(f"{'路':<6}{'状态':<8}{'证据数':<8}{'耗时s':<10}说明")
    print("=" * 68)
    for source, r in results.items():
        status = "OK" if r.ok else "FAIL"
        note = r.error[:40] if r.error else (r.evidences[0].claim[:40] if r.evidences else "无结果")
        print(f"{source:<6}{status:<8}{len(r.evidences):<8}{r.elapsed:<10.2f}{note}")
        if not r.ok:
            failed.append(source)
    print("=" * 68)

    if failed:
        print(f"\n未通：{', '.join(failed)}")
        print("提示：web 需要百炼 MCP 可用；kb 需要履约知识助手服务在跑；sql 需要仓配自助问数服务在跑。")
        return 1

    print("\n三路全部可用。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
