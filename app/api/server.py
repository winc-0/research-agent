"""
Web 服务层。

前端用的是本项目自带的那套 Vue 界面（Vite + WebSocket），它对后端的契约是固定的：

    POST /api/task            {query, thread_id}      → {thread_id}
    POST /api/upload          multipart(thread_id, files)
    GET  /api/files?path=...                          → {files: [...]}
    GET  /api/download?path=...                       → 文件流
    WS   /ws/{thread_id}                               → 事件流

事件流共六类：`session_created / tool_start / assistant_call /
confirm_required / task_result / error`。所以这一层的职责就一句话：**把图跑起来，
把节点动作翻译成这几类事件**。研究逻辑一律不下沉到这里，否则 CLI 与 Web 会各长一套逻辑。

会话隔离用「每会话一个工作目录 + 一个订阅者集合」实现：同一 thread_id 开多个
页面时事件会广播给全部连接，不是谁先连谁独占队列。
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from app.agent.graph import RECURSION_LIMIT, build_graph
from app.agent.state import empty_state
from app.conf.settings import settings
from app.core.bus import reset_sink, set_sink
from app.core.log import logger

app = FastAPI(title="物流研究助手 Research-Agent", description="多智能体深度研究服务")

# HITL 挂起需要把图中途的状态存住，恢复时再取出来。
# 进程内内存版够演示用；要跨重启续跑得换 SqliteSaver。
_CHECKPOINTER = MemorySaver()
_GRAPH = build_graph(checkpointer=_CHECKPOINTER)

# 前端跑在 Vite 的 5173，后端在 8000——必然跨源；本地演示场景下放开即可
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


# ---------------------------------------------------------------- 会话


@dataclass
class Session:
    """一个对话线程对应的工作目录与推送通道。"""

    thread_id: str
    path: Path
    created_at: float = field(default_factory=time.time)
    # 多个页面同时看同一会话时，事件要广播给所有连接
    subscribers: set[asyncio.Queue] = field(default_factory=set)
    running: bool = False
    # 同一页面可以连续问多个问题。图的断点用「会话id:序号」隔离，
    # 否则第二个问题会顺着上一个问题留下的 checkpoint 继续，累加型字段（证据）会串。
    run_seq: int = 0
    # HITL 等待中的确认队列；没有挂起任务时为 None
    confirm_queue: asyncio.Queue | None = None


_SESSIONS: dict[str, Session] = {}


def _new_session(thread_id: str) -> Session:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = settings.workspace.root / f"session_{stamp}_{thread_id[:8]}"
    path.mkdir(parents=True, exist_ok=True)
    session = Session(thread_id=thread_id, path=path)
    _SESSIONS[thread_id] = session
    logger.info(f"[session] 新建 {thread_id[:8]} → {path}")
    return session


def _get_session(thread_id: str) -> Session:
    if not _SAFE_ID.match(thread_id):
        raise HTTPException(status_code=400, detail="thread_id 非法")
    return _SESSIONS.get(thread_id) or _new_session(thread_id)


def _broadcast(session: Session, event: str, data: dict | None = None) -> None:
    """向该会话的所有订阅者推一条事件。队列满就丢——宁可少一条进度，也不阻塞研究链路。"""
    payload = {"type": "event", "event": event, "data": data or {}}
    for q in list(session.subscribers):
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            logger.warning(f"[session] 订阅队列已满，丢弃事件 {event}")


# ---------------------------------------------------------------- WebSocket


@app.websocket("/ws/{thread_id}")
async def websocket_endpoint(ws: WebSocket, thread_id: str):
    await ws.accept()
    session = _get_session(thread_id)

    queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
    session.subscribers.add(queue)

    # 前端靠这条事件拿到会话目录，之后才会去拉文件列表
    await ws.send_json(
        {"type": "event", "event": "session_created", "data": {"path": str(session.path)}}
    )

    async def pump() -> None:
        while True:
            await ws.send_json(await queue.get())

    pump_task = asyncio.create_task(pump())
    try:
        while True:
            raw = await ws.receive_text()
            if raw and '"ping"' in raw:
                await ws.send_json({"type": "pong"})
    except WebSocketDisconnect:
        pass
    except Exception as e:  # noqa: BLE001
        logger.info(f"[ws] 连接结束：{type(e).__name__}: {e}")
    finally:
        pump_task.cancel()
        session.subscribers.discard(queue)


# ---------------------------------------------------------------- 任务


@app.post("/api/task")
async def create_task(payload: dict):
    query = str(payload.get("query") or "").strip()
    thread_id = str(payload.get("thread_id") or "").strip() or str(uuid.uuid4())
    if not query:
        raise HTTPException(status_code=400, detail="query 不能为空")

    session = _get_session(thread_id)
    if session.running:
        # 图是有状态的单次运行，同一会话并发跑两遍会把证据写串
        raise HTTPException(status_code=409, detail="该会话已有任务在运行")

    session.running = True
    asyncio.create_task(_run_task(session, query))
    return {"thread_id": thread_id}


async def _run_task(session: Session, query: str) -> None:
    """跑一次完整研究，把过程翻译成前端事件。中途可能因人工确认而挂起。"""
    token = set_sink(lambda event, data: _broadcast(session, event, data))
    started = time.perf_counter()
    session.run_seq += 1
    config = {
        "recursion_limit": RECURSION_LIMIT,
        # 每问一次用新的断点 id，保证状态从空白开始
        "configurable": {"thread_id": f"{session.thread_id}:{session.run_seq}"},
    }

    try:
        payload: object = empty_state(query)
        final: dict = dict(payload)  # type: ignore[arg-type]

        # 可能挂起多次（用户连续要求补充检索），每次恢复后继续流式消费
        while True:
            async for mode, chunk in _GRAPH.astream(
                payload,
                config,
                stream_mode=["updates", "values"],
            ):
                if mode == "values":
                    final = dict(chunk)
                elif mode == "updates":
                    for node, update in chunk.items():
                        _report_node(session, node, update or {})

            snapshot = await _GRAPH.aget_state(config)
            interrupts = getattr(snapshot, "interrupts", ()) or ()
            if not interrupts:
                break

            payload = Command(resume=await _wait_confirm(session, interrupts[0].value))

        report_text = final.get("report") or ""
        if not report_text:
            raise RuntimeError("研究链路未产出报告")

        target = session.path / f"report_{datetime.now():%H%M%S}.md"
        target.write_text(report_text, encoding="utf-8")
        logger.info(f"[task] 报告已保存 {target}（{time.perf_counter() - started:.1f}s）")

        _broadcast(session, "task_result", {"result": report_text, "path": str(target)})
    except Exception as e:  # noqa: BLE001
        logger.exception("[task] 研究失败")
        _send_error(session, f"{type(e).__name__}: {e}")
    finally:
        session.confirm_queue = None
        reset_sink(token)
        session.running = False


async def _wait_confirm(session: Session, payload: dict) -> dict:
    """
    把中断载荷推给前端，等用户决定。

    超时兜底为「确认」：这个系统的定位是把事做完，等人比答错更伤。
    真需要严格把关时，把 HITL_TIMEOUT 调大或改成显式拒绝即可。
    """
    _broadcast(session, "confirm_required", payload or {})
    queue: asyncio.Queue = asyncio.Queue()
    session.confirm_queue = queue
    try:
        decision = await asyncio.wait_for(queue.get(), timeout=settings.hitl.timeout)
        return decision
    except asyncio.TimeoutError:
        logger.warning(f"[confirm] 等待确认超过 {settings.hitl.timeout:.0f}s，自动继续")
        _broadcast(
            session,
            "assistant_call",
            {"assistant_name": "确认环节", "args": {"结果": "等待超时，按确认继续"}},
        )
        return {"decision": "approve", "feedback": "", "timeout": True}
    finally:
        session.confirm_queue = None


@app.post("/api/confirm")
async def submit_confirm(payload: dict):
    """用户对大纲的决定：approve 直接写报告，revise 带着意见回规划。"""
    thread_id = str(payload.get("thread_id") or "")
    session = _SESSIONS.get(thread_id)
    if not session or session.confirm_queue is None:
        raise HTTPException(status_code=409, detail="当前没有等待确认的任务")

    decision = str(payload.get("decision") or "approve").lower()
    feedback = str(payload.get("feedback") or "").strip()
    if decision == "revise" and not feedback:
        raise HTTPException(status_code=400, detail="要求补充检索时必须填写意见")

    session.confirm_queue.put_nowait({"decision": decision, "feedback": feedback})
    logger.info(f"[confirm] 收到用户决定：{decision}")
    return {"ok": True}


def _send_error(session: Session, message: str) -> None:
    """error 事件前端读的是顶层 message 字段，不是 data。"""
    for q in list(session.subscribers):
        try:
            q.put_nowait({"type": "event", "event": "error", "message": message})
        except asyncio.QueueFull:
            pass


def _report_node(session: Session, node: str, update: dict) -> None:
    """把节点产出翻译成前端的 assistant_call，日志树才有内容可展开。"""
    if node == "plan":
        todos = update.get("todos") or []
        _broadcast(
            session,
            "assistant_call",
            {
                "assistant_name": "规划器",
                "args": {
                    "子任务数": len(todos),
                    "子任务": [{"问题": t["question"], "路径": t["routes"]} for t in todos],
                },
            },
        )
    elif node == "compress":
        citations = update.get("citations") or []
        _broadcast(
            session,
            "assistant_call",
            {"assistant_name": "证据压缩", "args": {"入选引用": len(citations)}},
        )
    elif node == "guard":
        if update.get("stop_reason"):
            _broadcast(
                session,
                "assistant_call",
                {"assistant_name": "预算守卫", "args": {"终止原因": update["stop_reason"]}},
            )
    elif node == "review":
        notes = update.get("review_notes") or []
        _broadcast(
            session,
            "assistant_call",
            {
                "assistant_name": "证据审查",
                "args": {
                    "轮次": update.get("review_round", 0),
                    "是否补检索": bool(update.get("need_more")),
                    "缺口": update.get("missing") or [],
                    "结论": notes[-1] if notes else "",
                },
            },
        )
    elif node == "confirm":
        _broadcast(
            session,
            "assistant_call",
            {
                "assistant_name": "人工确认",
                "args": {
                    "用户决定": "已确认" if update.get("approved", True) else "要求补充检索",
                    "意见": update.get("feedback") or "",
                },
            },
        )
    elif node == "report":
        _broadcast(
            session,
            "assistant_call",
            {"assistant_name": "报告撰写", "args": {"字数": len(update.get("report") or "")}},
        )


# ---------------------------------------------------------------- 文件


def _safe_path(raw: str) -> Path:
    """只允许访问工作区内的文件。前端传的是绝对路径，必须自己校验。"""
    target = Path(raw).expanduser().resolve()
    root = settings.workspace.root.resolve()
    if target != root and root not in target.parents:
        raise HTTPException(status_code=403, detail="路径超出工作区")
    return target


@app.get("/api/files")
async def list_files(path: str = ""):
    target = _safe_path(path) if path else settings.workspace.root
    if not target.exists() or not target.is_dir():
        return {"files": []}
    files = []
    for f in sorted(target.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not f.is_file():
            continue
        stat = f.stat()
        files.append(
            {
                "name": f.name,
                "path": str(f),
                "size": stat.st_size,
                "mtime": datetime.fromtimestamp(stat.st_mtime).strftime("%H:%M:%S"),
            }
        )
    return {"files": files}


@app.get("/api/download")
async def download(path: str):
    target = _safe_path(path)
    if not target.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(target, filename=target.name)


@app.post("/api/upload")
async def upload(thread_id: str = Form(""), files: list[UploadFile] = File(...)):
    session = _get_session(thread_id) if thread_id else _new_session(str(uuid.uuid4()))
    saved = []
    for item in files:
        name = Path(item.filename or "upload.bin").name
        target = session.path / name
        target.write_bytes(await item.read())
        saved.append({"name": name, "path": str(target), "size": target.stat().st_size})
    logger.info(f"[upload] {session.thread_id[:8]} 收 {len(saved)} 个文件")
    return {"files": saved}


@app.get("/api/health")
async def health():
    return {
        "ok": True,
        "sessions": len(_SESSIONS),
        "workspace": str(settings.workspace.root),
    }


# 报告里的相对路径引用、以及前端直接打开文件都走这里
settings.workspace.root.mkdir(parents=True, exist_ok=True)
app.mount("/outputs", StaticFiles(directory=str(settings.workspace.root)), name="outputs")


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")
