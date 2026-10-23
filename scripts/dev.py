"""
一键启动：体检 → 起后端 → 起前端。

    python scripts/dev.py                  # 体检 + 后端 + 前端
    python scripts/dev.py --no-frontend    # 只起后端
    python scripts/dev.py --skip-check     # 跳过体检

为什么是 Python 脚本而不是 .bat / .sh：本项目的依赖服务跨仓库、跨平台，
用脚本统一做「前置条件说得清、缺什么提示得准」，比写两份 shell 更省事。

体检只做提示，不做拦截：履约知识助手 / 仓配自助问数不可达时物流研究助手照样能跑（对应那一路会走降级路径），
这是设计如此，所以脚本不会因为这个退出。
"""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND_PORT = 8000
FRONTEND_PORT = 5173

# 依赖服务的默认地址（与 .env.example 保持一致）
KB_URL = os.getenv("KB_SERVICE_URL", "http://127.0.0.1:8001")
SQL_URL = os.getenv("NL2SQL_SERVICE_URL", "http://127.0.0.1:8010")


def _say(msg: str = "") -> None:
    print(msg, flush=True)


def _ok(msg: str) -> None:
    _say(f"  [ok]   {msg}")


def _warn(msg: str) -> None:
    _say(f"  [warn] {msg}")


def _fail(msg: str) -> None:
    _say(f"  [fail] {msg}")


def _port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.8) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


def _service_alive(url: str, timeout: float = 3.0) -> bool:
    """
    探活优先打 /docs：三个服务都是 FastAPI，根路径没有路由会返回 404，
    只看根路径会把「活着」误判成「挂了」。探不通再退化为端口探测。

    必须显式绕开系统代理：本机常挂着全局代理（HTTP_PROXY），
    urllib 默认会照着走，于是访问 127.0.0.1 也被劫持成 502，探测结果全错。
    """
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"{url.rstrip('/')}/docs", timeout=timeout) as resp:
            return 200 <= resp.status < 500
    except urllib.error.HTTPError:
        # 有响应就是活着
        return True
    except Exception:
        return False


def _env_value(key: str) -> str:
    env_file = ROOT / ".env"
    if not env_file.is_file():
        return ""
    for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        if name.strip() == key:
            return value.strip().strip('"').strip("'")
    return ""


def check() -> bool:
    """返回体检是否「基本可用」（只影响提示语气，不阻断启动）。"""
    _say("\n体检：")
    healthy = True

    # 1. .env
    if not (ROOT / ".env").is_file():
        _fail(".env 不存在 → 先执行 copy .env.example .env")
        healthy = False
    else:
        _ok(".env 已存在")

    # 2. API Key 是否还是占位符
    key = _env_value("LLM_API_KEY")
    if not key or "填入" in key or key.startswith("sk-xxx"):
        _fail("LLM_API_KEY 还没填（规划 / 审查 / 报告全靠它）")
        healthy = False
    else:
        _ok(f"LLM_API_KEY 已配置（{key[:6]}…）")

    # 3. 依赖服务
    for label, url in (("履约知识助手检索 :8001", KB_URL), ("仓配自助问数问数 :8010", SQL_URL)):
        if _service_alive(url):
            _ok(f"{label} 可达")
        else:
            _warn(f"{label} 不可达 → 对应那一路会走降级路径，报告里会标注缺失")
            healthy = False

    # 4. 前端依赖
    if not (ROOT / "frontend" / "node_modules").is_dir():
        _warn("frontend/node_modules 不存在 → 前端会跳过，先执行 cd frontend && npm install")

    return healthy


def _spawn(cmd: list[str], cwd: Path, label: str) -> subprocess.Popen:
    _say(f"  起 {label}: {' '.join(cmd)}")
    return subprocess.Popen(cmd, cwd=str(cwd))


def main() -> int:
    parser = argparse.ArgumentParser(description="物流研究助手 开发态一键启动")
    parser.add_argument("--no-frontend", action="store_true", help="只起后端")
    parser.add_argument("--skip-check", action="store_true", help="跳过体检")
    parser.add_argument("--backend-port", type=int, default=BACKEND_PORT, help=f"后端端口（默认 {BACKEND_PORT}）")
    parser.add_argument("--frontend-port", type=int, default=FRONTEND_PORT, help=f"前端端口（默认 {FRONTEND_PORT}）")
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    _say("物流研究助手 · 开发态启动")

    if not args.skip_check:
        check()
    else:
        _say("\n（已跳过体检）")

    if _port_open(args.backend_port):
        _fail(f"端口 {args.backend_port} 已被占用 → 后端可能已经在跑，先停掉再来")
        return 2

    children: list[subprocess.Popen] = []

    try:
        _say("\n启动：")
        children.append(
            _spawn(
                [sys.executable, "-m", "uvicorn", "app.api.server:app",
                 "--host", "127.0.0.1", "--port", str(args.backend_port)],
                ROOT,
                f"后端 :{args.backend_port}",
            )
        )

        # 等后端起来再起前端，避免页面先打开、WebSocket 先失败一次
        deadline = time.time() + 20
        backend_url = f"http://127.0.0.1:{args.backend_port}"
        while time.time() < deadline and not _service_alive(backend_url, timeout=1):
            if children[0].poll() is not None:
                _fail("后端启动失败，看上面的报错")
                return 1
            time.sleep(0.4)

        if not args.no_frontend and (ROOT / "frontend" / "node_modules").is_dir():
            npm = shutil.which("npm") or "npm"
            if _port_open(args.frontend_port):
                _warn(f"端口 {args.frontend_port} 已被占用 → 跳过前端")
            else:
                # --host 127.0.0.1：Vite 默认绑 localhost，机器上 localhost 可能只解析到 ::1，
                # 结果 127.0.0.1 打不开（用代理访问还会变成 502）。显式绑死 IPv4 更省事。
                cmd = [npm, "run", "dev", "--", "--host", "127.0.0.1",
                       "--port", str(args.frontend_port), "--strictPort"]
                # Windows 上 npm 是 .cmd，CreateProcess 不能直接执行，需要垫一层 cmd /c
                if npm.lower().endswith((".cmd", ".bat")):
                    cmd = ["cmd", "/c", *cmd]
                children.append(_spawn(cmd, ROOT / "frontend", f"前端 :{args.frontend_port}"))
        elif not args.no_frontend:
            _warn("前端未启动（缺 node_modules）")

        _say("\n就绪：")
        _say(f"  后端    {backend_url}   文档 /docs")
        if len(children) > 1:
            _say(f"  界面    http://127.0.0.1:{args.frontend_port}")
        _say("\nCtrl+C 一起退出\n")

        while True:
            for child in children:
                if child.poll() is not None:
                    _fail(f"有进程退出（返回码 {child.returncode}），其余一并停止")
                    return 1
            time.sleep(0.5)

    except KeyboardInterrupt:
        _say("\n正在停止…")
        return 0
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
        for child in children:
            try:
                child.wait(timeout=8)
            except subprocess.TimeoutExpired:
                child.kill()
        _say("已退出")


if __name__ == "__main__":
    raise SystemExit(main())
