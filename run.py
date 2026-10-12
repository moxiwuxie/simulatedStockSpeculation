"""一键启动后端 + 前端 — 历史回放交易系统

用法 (三选一):
  python run.py              # 同时启动后端 + 前端 + 自动开浏览器
  python run.py backend      # 只启动后端
  python run.py frontend     # 只启动前端
  python run.py --no-browser # 不自动开浏览器

后端 API  : http://localhost:8010/docs
前端页面  : http://localhost:5501

开发时也可单独:
  cd backend  && source .venv/bin/activate && python3 -m uvicorn app.main:app --reload
  python3 proxy_server.py 5501        # 前端静态 + /api 反代到 8010
"""
from __future__ import annotations
import os
import sys
import time
import webbrowser
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BACKEND = ROOT / "backend"
FRONTEND = ROOT / "frontend"
VENV_PY = BACKEND / ".venv" / "bin" / "python3"


def check_venv():
    if not VENV_PY.exists():
        print(f"❌ 找不到虚拟环境: {VENV_PY}")
        print("   请先初始化后端环境:")
        print(f"   cd {BACKEND}")
        print(f"   python3 -m venv .venv")
        print(f"   source .venv/bin/activate")
        print(f"   pip install fastapi uvicorn pydantic-settings akshare requests")
        sys.exit(1)


def port_in_use(port: int) -> bool:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def start_backend():
    if port_in_use(8010):
        print("⚠️  端口 8010 已被占用, 跳过后端启动")
        return None

    print("🚀 启动后端 (FastAPI + uvicorn)...")
    # 关键: cwd 设为 backend/, 这样 uvicorn 才能找到 app 包
    env = os.environ.copy()
    proc = subprocess.Popen(
        [str(VENV_PY), "-m", "uvicorn", "app.main:app",
         "--host", "0.0.0.0", "--port", "8010",
         "--log-level", "warning"],
        cwd=str(BACKEND),
        env=env,
    )
    # 等就绪 (最多 30 秒, akshare 首次拉交易日历 + K 线会慢)
    for i in range(40):
        time.sleep(0.7)
        try:
            import urllib.request
            urllib.request.urlopen("http://localhost:8010/api/replay/status", timeout=0.8).read()
            print(f"  ✅ 后端就绪: http://localhost:8010  (约 {i*0.7:.1f}s)")
            return proc
        except Exception:
            pass
    print("  ⏳ 后端仍在初始化 (akshare 交易日历/K线缓存加载中), 启动较慢属正常")
    return proc


def start_frontend():
    if port_in_use(5501):
        print("⚠️  端口 5501 已被占用, 跳过前端启动")
        return None

    print("🎨 启动前端 (静态服务 + /api 反向代理)...")
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "proxy_server.py"), "5501"],
        cwd=str(ROOT),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(0.5)
    print("  ✅ 前端就绪: http://localhost:5501  (页面 + /api 反代 -> :8010)")
    return proc


def main():
    check_venv()

    args = set(sys.argv[1:])
    backend_on = "backend" in args or ("frontend" not in args and "all" not in args)
    frontend_on = "frontend" in args or ("backend" not in args and "all" not in args)
    # 不带参数 = 都起
    if backend_on and frontend_on and args & {"backend", "frontend"} == set():
        pass  # 默认都起
    # 仅显式指定 backend
    if "backend" in args and "frontend" not in args:
        frontend_on = False
    # 仅显式指定 frontend
    if "frontend" in args and "backend" not in args:
        backend_on = False

    no_browser = "--no-browser" in args

    procs = []
    if backend_on:
        p = start_backend()
        if p: procs.append(p)
    if frontend_on:
        p = start_frontend()
        if p: procs.append(p)

    if not no_browser and frontend_on:
        time.sleep(0.8)
        try: webbrowser.open("http://localhost:5501")
        except Exception: pass

    if not procs:
        print("\n⚠️  没有需要启动的服务 (端口可能已被占用)")
        return

    print(f"\n🟢 运行中 (Ctrl+C 退出)")
    print(f"   后端 Swagger : http://localhost:8010/docs")
    print(f"   前端页面     : http://localhost:5501")
    print()

    try:
        for p in procs: p.wait()
    except KeyboardInterrupt:
        print("\n🛑 停止...")
        for p in procs: p.terminate()
        for p in procs:
            try: p.wait(timeout=3)
            except: p.kill()
        print("👋 已退出")


if __name__ == "__main__":
    main()
