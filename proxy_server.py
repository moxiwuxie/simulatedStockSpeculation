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
import gzip
import http.client
import os
import posixpath
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

    # ---------- 静态资源: 文本类 MIME 显式声明 utf-8 ----------
    # 需求101701-0: 初次访问页面乱码。SimpleHTTPRequestHandler.guess_type 返回的
    # text/html、text/css、application/javascript 均不带 charset, 部分手机浏览器
    # 会按本地默认编码(GBK)解码首屏 HTML → 中文乱码。此处统一补 "; charset=utf-8"。
    _TEXT_MIME = {"text/html", "text/css", "text/plain", "text/xml",
                  "application/javascript", "application/x-javascript",
                  "application/json", "image/svg+xml"}

    def guess_type(self, path):
        ctype = super().guess_type(path)
        base = ctype.split(";")[0].strip().lower()
        if base in self._TEXT_MIME and "charset" not in ctype:
            return base + "; charset=utf-8"
        return ctype

    # ---------- 静态资源: 按需 gzip ----------
    # 需求101702: 经 ngrok 访问时下行带宽是主要瓶颈 (实测 ~30-47 KB/s), 而 JS/CSS 未压缩。
    # 客户端支持 gzip 时压缩后发送, 内容每次取自磁盘 (不会串到旧版本)。
    _GZIP_EXT = {".html", ".css", ".js", ".mjs", ".json", ".svg", ".txt", ".xml", ".map"}
    _gzip_cache: dict = {}          # (路径, mtime_ns, size) → 压缩后字节
    _gzip_lock = threading.Lock()

    def _accepts_gzip(self) -> bool:
        return "gzip" in (self.headers.get("Accept-Encoding") or "").lower()

    def _serve_static_gzip(self) -> bool:
        """返回 True 表示已由本方法响应 (命中并成功压缩)。"""
        if not self._accepts_gzip():
            return False
        path_part = self.path.split("?")[0]
        if posixpath.splitext(path_part)[1].lower() not in self._GZIP_EXT:
            if path_part not in ("", "/"):     # 目录首页 → index.html
                return False
        local = Path(self.translate_path(self.path))
        if local.is_dir():
            local = local / "index.html"
        if not local.is_file():
            return False
        try:
            st = local.stat()
            key = (str(local), st.st_mtime_ns, st.st_size)
            with self._gzip_lock:
                body = self._gzip_cache.get(key)
            if body is None:
                with open(local, "rb") as fh:
                    raw = fh.read()
                if len(raw) < 1024:            # 太小无收益, 交回默认路径
                    return False
                body = gzip.compress(raw, 6)
                with self._gzip_lock:
                    if len(self._gzip_cache) > 32:
                        self._gzip_cache.clear()
                    self._gzip_cache[key] = body
        except OSError:
            return False

        self.send_response(200)
        self.send_header("Content-Type", self.guess_type(str(local)))
        self.send_header("Content-Encoding", "gzip")
        self.send_header("Vary", "Accept-Encoding")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        return True

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
            and k.lower() not in ("host", "content-length")
            and k.lower() != "x-forwarded-for"
        }
        # 需求101809: 透传真实客户端 IP, 否则后端只能看到 127.0.0.1 (本机反代对端),
        # 导致访问IP收集/封禁/限流全部失效。已有上游 XFF 则追加, 否则以对端地址为源。
        _peer = self.client_address[0] if self.client_address else "127.0.0.1"
        _xff = self.headers.get("X-Forwarded-For", "")
        headers["X-Forwarded-For"] = f"{_xff}, {_peer}" if _xff else _peer
        headers["X-Real-IP"] = _peer
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
            # Content-Length 按实际字节重算 (含 gzip 后的压缩长度);
            # content-encoding 必须原样透传, 否则客户端会拿到 gzip 字节却按明文解析。
            if lk in HOP_BY_HOP or lk == "content-length":
                continue
            # 需求101701-0: 后端文本响应同样补 charset, 避免手机浏览器按 GBK 解码 JSON
            if lk == "content-type":
                base = v.split(";")[0].strip().lower()
                if (base.startswith("text/") or base in self._TEXT_MIME) \
                        and "charset" not in v.lower():
                    v = base + "; charset=utf-8"
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    # ---------- 各方法 ----------
    def do_GET(self):
        if self._is_api():
            return self._proxy()
        if self._serve_static_gzip():
            return
        return super().do_GET()

    def do_HEAD(self):
        if self._is_api():
            return self._proxy()
        if self._serve_static_gzip():
            return
        return super().do_HEAD()

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
            elif "v=" in self.path:
                # 需求101702: 带版本号的静态资源 (?v=69) 内容随版本号变化, 可长期强缓存 ——
                # 经 ngrok 重复访问时直接命中本地缓存, 零下载。
                self.send_header("Cache-Control", "public, max-age=31536000, immutable")
            else:
                self.send_header("Cache-Control", "public, max-age=300")
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
