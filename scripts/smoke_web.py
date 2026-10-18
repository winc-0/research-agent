"""
Web 层冒烟：模拟浏览器走一遍 WS + REST，确认事件契约对得上。

    python scripts/smoke_web.py "各品类销售额排名如何"
    python scripts/smoke_web.py "问题" --revise "还想看华东区的拆分"

只验证「能不能跑通、事件是否齐全」，不评判报告质量。
HITL 环节由脚本自动应答：默认确认，传 --revise 则提意见要求补检索。
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
import uuid

import httpx
import websockets

BASE = "http://127.0.0.1:8000"
WS = "ws://127.0.0.1:8000"


async def main(question: str, revise: str | None = None) -> int:
    thread_id = str(uuid.uuid4())
    seen: dict[str, int] = {}
    report = ""
    started = time.perf_counter()

    async with websockets.connect(f"{WS}/ws/{thread_id}", max_size=8 * 1024 * 1024) as ws:
        first = json.loads(await ws.recv())
        print(f"[ws] {first['event']} → {first['data'].get('path')}")
        if first["event"] != "session_created":
            print("!! 首条事件不是 session_created")
            return 1

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(f"{BASE}/api/task", json={"query": question, "thread_id": thread_id})
            resp.raise_for_status()
            print(f"[http] POST /api/task → {resp.json()}")

        while True:
            # 单次运行最长 8 分钟，超时即判定卡住
            if time.perf_counter() - started > 480:
                print("!! 超时")
                return 1
            evt = json.loads(await asyncio.wait_for(ws.recv(), timeout=480))
            if evt.get("type") == "pong":
                continue
            name = evt.get("event", "?")
            seen[name] = seen.get(name, 0) + 1
            data = evt.get("data") or {}

            if name == "tool_start":
                print(f"[tool] {data.get('tool_name')} ← {data.get('args', {}).get('question', '')[:40]}")
            elif name == "assistant_call":
                print(f"[agent] {data.get('assistant_name')} {json.dumps(data.get('args'), ensure_ascii=False)[:100]}")
            elif name == "confirm_required":
                outline = data.get("outline") or []
                print(f"[confirm] 待确认大纲 {len(outline)} 项：")
                for i, item in enumerate(outline, 1):
                    print(f"          {i}. {item.get('问题')}  （{item.get('证据数')} 条 / {'/'.join(item.get('来源') or [])}）")
                decision = "revise" if revise else "approve"
                async with httpx.AsyncClient(timeout=15) as c:
                    await c.post(
                        f"{BASE}/api/confirm",
                        json={"thread_id": thread_id, "decision": decision, "feedback": revise or ""},
                    )
                print(f"[confirm] 已提交：{decision}" + (f"（{revise}）" if revise else ""))
            elif name == "task_result":
                report = data.get("result") or ""
                break
            elif name == "error":
                print(f"!! error: {evt.get('message')}")
                return 1

    print(f"\n事件统计：{seen}")
    print(f"报告 {len(report)} 字，耗时 {time.perf_counter() - started:.1f}s")
    if not report:
        return 1

    async with httpx.AsyncClient(timeout=15) as client:
        files = (await client.get(f"{BASE}/api/files", params={"path": ""})).json()
        print(f"工作区文件数：{len(files.get('files', []))}")
        health = (await client.get(f"{BASE}/api/health")).json()
        print(f"health：{health}")

    print("\n--- 报告开头 ---")
    print(report[:400])
    return 0


if __name__ == "__main__":
    argv = sys.argv[1:]
    revise = None
    if "--revise" in argv:
        i = argv.index("--revise")
        revise = argv[i + 1] if i + 1 < len(argv) else "请补充更多证据"
        argv = argv[:i] + argv[i + 2:]
    q = " ".join(argv).strip() or "各品类销售额排名如何"
    sys.exit(asyncio.run(main(q, revise)))
