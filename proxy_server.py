"""前端服务 + 反向代理 — 单端口同时提供静态页面与后端 API

作用:
  浏览器只访问本服务 (默认 5501)。本服务:
    - /            → 直接返回 frontend/ 下的静态文件 (index.html / *.js / *.css)
    - /api/...     → 反向代理到后端 uvicorn (默认 127.0.0.1:8010)

好处: 前后端同源, 无跨端口、无 CORS; 手机只要访问同一端口即可。

用法:
  python proxy_server.py                # 监听 :5501 (IPv4 + IPv6 双栈), 后端 127.0.0.1:8010
  python proxy_server.py 5501           # 指定端口
  BACKEND_PORT=9000 python proxy_server.py
"""
from __future__ import annotations
import http.client
import os
import socket
import sys
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FRONTEND = ROOT / "frontend"

BACKEND_HOST = os.environ.get("BACKEND_HOST", "127.0.0.1")
BACKEND_PORT = int(os.environ.get("BACKEND_PORT", "8010"))

# 逐跳首部 (hop-by-hop): 不参与转发
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
}


class ProxyHandler(SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # ---------- 反代 ----------
    def _is_api(self) -> bool:
        return self.path == "/api" or self.path.startswith("/api/") \
            or self.path.startswith("/api?")

    def _proxy(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None

        headers = {
            k: v for k, v in self.headers.items()
            if k.lower() not in HOP_BY_HOP
            and k.lower() not in ("host", "content-length", "accept-encoding")
        }
        if body is not None:
            headers["Content-Length"] = str(len(body))

        conn = http.client.HTTPConnection(BACKEND_HOST, BACKEND_PORT, timeout=120)
        try:
            conn.request(self.command, self.path, body=body, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
            status, resp_headers = resp.status, resp.getheaders()
        except Exception as e:
            self.send_response(502)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            msg = f"反向代理失败: 无法连接后端 {BACKEND_HOST}:{BACKEND_PORT} ({e})".encode()
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(msg)
            return
        finally:
            conn.close()

        self.send_response(status)
        for k, v in resp_headers:
            lk = k.lower()
            # Content-Length 由本服务按实际字节重算; 编码类首部不转发(已禁用压缩)
            if lk in HOP_BY_HOP or lk in ("content-length", "content-encoding"):
                continue
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    # ---------- 各方法 ----------
    def do_GET(self):
        return self._proxy() if self._is_api() else super().do_GET()

    def do_HEAD(self):
        return self._proxy() if self._is_api() else super().do_HEAD()

    def do_POST(self):
        return self._proxy() if self._is_api() else self._static_no_body()

    def do_PUT(self):
        return self._proxy() if self._is_api() else self._static_no_body()

    def do_PATCH(self):
        return self._proxy() if self._is_api() else self._static_no_body()

    def do_DELETE(self):
        return self._proxy() if self._is_api() else self._static_no_body()

    def do_OPTIONS(self):
        return self._proxy() if self._is_api() else self._static_no_body()

    def _static_no_body(self):
        """静态资源不支持这些方法: 反代之外统一 405"""
        self.send_response(405)
        self.send_header("Allow", "GET, HEAD")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def end_headers(self):
        # HTML 不做启发式缓存: 否则浏览器会沿用旧 index.html → 旧 app.js(指向 :8000) →
        # 在 https 域名(如 ngrok)下被混合内容拦截, 表现为页面数据不显示。
        if not self._is_api():
            p = self.path.split("?")[0]
            if p.endswith("/") or p.endswith(".html"):
                self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        super().end_headers()

    def log_message(self, fmt, *args):
        # 只打印反代请求, 静态资源刷屏信息静默
        if self._is_api():
            sys.stderr.write("[proxy] %s %s\n" % (self.command, self.path))
        # 其余静默
        return


class _V6Server(ThreadingHTTPServer):
    """IPv6 监听: 让 http://localhost:5501 (可能解析为 ::1) 也能访问"""
    address_family = socket.AF_INET6


def _bind_servers(port: int, handler) -> list:
    """同时监听 IPv4 与 IPv6。

    macOS 上浏览器会把 localhost 优先解析成 ::1, 只绑 0.0.0.0 会导致
    localhost:5501 连接被拒 (ERR_CONNECTION_REFUSED)。与 python -m http.server 行为对齐。
    """
    servers = []
    try:
        servers.append(ThreadingHTTPServer(("0.0.0.0", port), handler))       # IPv4
    except OSError as e:
        print(f"⚠️  IPv4 0.0.0.0:{port} 绑定失败: {e}")
    try:
        servers.append(_V6Server(("::", port), handler))                      # IPv6
    except OSError as e:
        print(f"⚠️  IPv6 [::]:{port} 绑定失败: {e}")
    return servers


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5501
    handler = partial(ProxyHandler, directory=str(FRONTEND))
    servers = _bind_servers(port, handler)
    if not servers:
        print(f"❌ 无法绑定端口 {port} (可能已被占用)")
        sys.exit(1)

    print(f"🎨 前端+反代已启动: http://localhost:{port}")
    print(f"   /       → {FRONTEND}")
    print(f"   /api/*  → http://{BACKEND_HOST}:{BACKEND_PORT}/api/*")
    print(f"   监听: {' + '.join(s.server_address[0] for s in servers)}")

    for s in servers:
        threading.Thread(target=s.serve_forever, daemon=True).start()
    try:
        threading.Event().wait()   # 主线程挂起, 由 Ctrl+C 结束
    except KeyboardInterrupt:
        print("\n🛑 停止代理服务")
    finally:
        for s in servers:
            s.server_close()


if __name__ == "__main__":
    main()
