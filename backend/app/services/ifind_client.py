"""同花顺 iFinD MCP 客户端 —— 真实看盘数据接入层

复刻 iFinD 官方 call.py 的 MCP 协议流程:
    initialize -> 读取响应头 Mcp-Session-Id -> notifications/initialized -> tools/call

密钥来自 settings.IFIND_AUTH_TOKEN (backend/.env 或进程环境变量)。未配置密钥、
网关认证失败 (401)、网络异常等一切失败情况都**不抛异常**, 统一返回
``(False, 错误说明)``, 由调用方 (watch.py) 回落占位数据, 保证接口永不 5xx。
"""
from __future__ import annotations
import json
import logging
from typing import Any, Dict, Optional, Tuple

import requests

from ..core.config import settings

try:                       # 网关证书链不完整, 需放宽校验并静音告警
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except Exception:          # pragma: no cover - 环境缺 urllib3 时忽略
    pass

logger = logging.getLogger(__name__)

BASE = "https://api-mcp.51ifind.com:8643/ds-mcp-servers"
SERVERS: Dict[str, str] = {
    "stock": f"{BASE}/hexin-ifind-ds-stock-mcp",
    "fund": f"{BASE}/hexin-ifind-ds-fund-mcp",
    "edb": f"{BASE}/hexin-ifind-ds-edb-mcp",
    "news": f"{BASE}/hexin-ifind-ds-news-mcp",
    "bond": f"{BASE}/hexin-ifind-ds-bond-mcp",
    "global_stock": f"{BASE}/hexin-ifind-ds-global-stock-mcp",
    "index": f"{BASE}/hexin-ifind-ds-index-mcp",
    "future": f"{BASE}/hexin-ifind-ds-futures-mcp",
}

INIT_TIMEOUT = 15          # initialize 握手超时
CALL_TIMEOUT = 30          # 单次取数超时

# 进程级会话复用: {server_type: Mcp-Session-Id} / {server_type: 递增请求id}
_sessions: Dict[str, str] = {}
_req_ids: Dict[str, int] = {}


def token() -> Optional[str]:
    """返回可用的 iFinD 密钥

    过滤两类无效值: 插件的占位表达式 ``${connector.*}``, 以及向导文案类占位
    (含空白/非 ASCII 字符) —— 后者可能是插件环境变量注入到进程时的说明文本。
    """
    t = (settings.IFIND_AUTH_TOKEN or "").strip()
    if not t or t.startswith("${connector."):
        return None
    if not t.isascii() or any(c.isspace() for c in t):
        return None
    return t


def is_configured() -> bool:
    """是否已配置 iFinD 密钥"""
    return token() is not None


def _next_id(server_type: str) -> int:
    _req_ids[server_type] = _req_ids.get(server_type, 0) + 1
    return _req_ids[server_type]


def _headers(server_type: str) -> Dict[str, str]:
    h = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": token() or "",
    }
    sid = _sessions.get(server_type)
    if sid:
        h["Mcp-Session-Id"] = sid
    return h


def _post(server_type: str, payload: Dict, timeout: int) -> requests.Response:
    return requests.post(
        SERVERS[server_type],
        json=payload,
        headers=_headers(server_type),
        verify=False,
        timeout=timeout,
    )


def _ensure_session(server_type: str) -> None:
    """首次调用前建立 MCP 会话; 认证失败抛 IfindAuthError"""
    if server_type in _sessions:
        return
    payload = {
        "jsonrpc": "2.0",
        "id": _next_id(server_type),
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "simulatedStockSpeculation", "version": "1.0.0"},
        },
    }
    resp = _post(server_type, payload, INIT_TIMEOUT)
    if resp.status_code == 401:
        raise IfindAuthError("网关认证失败 (401 Gateway authentication failed)")
    if resp.status_code >= 400:
        raise IfindProtocolError(f"initialize HTTP {resp.status_code}: {resp.text.strip()[:200]}")
    sid = resp.headers.get("Mcp-Session-Id")
    if not sid:
        raise IfindProtocolError(f"initialize 未返回 Mcp-Session-Id: {resp.text.strip()[:200]}")
    _sessions[server_type] = sid
    try:                   # 通知握手完成 (失败不阻断, 下次调用仍可继续)
        requests.post(
            SERVERS[server_type],
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers=_headers(server_type),
            verify=False,
            timeout=10,
        )
    except requests.RequestException:
        pass


def _unwrap(data: Any) -> Any:
    """剥掉 JSON-RPC 外壳, 取出业务数据

    MCP tools/call 的标准返回为 ``result.content:[{type:'text', text:'...'}]``,
    text 通常是一段 JSON 字符串; 若解析失败则原样返回文本。
    """
    if not isinstance(data, dict):
        return data
    result = data.get("result", data)
    content = result.get("content") if isinstance(result, dict) else None
    if isinstance(content, list) and content:
        texts = [c.get("text") for c in content if isinstance(c, dict) and c.get("text")]
        if len(texts) == 1:
            try:
                return json.loads(texts[0])
            except (ValueError, TypeError):
                return texts[0]
        if texts:
            return texts
    return result


class IfindError(RuntimeError):
    """iFinD 调用失败的基类"""


class IfindAuthError(IfindError):
    """密钥缺失或网关认证失败 (401)"""


class IfindProtocolError(IfindError):
    """协议层异常 (握手失败 / 工具不存在 / 响应格式异常)"""


def call_ifind(server_type: str, tool_name: str, params: Dict) -> Tuple[bool, Any]:
    """调用 iFinD MCP 工具

    返回 ``(ok, data)``: ok=True 时 data 为已剥壳的业务数据; ok=False 时
    data 为可直接展示的中文错误说明。任何异常都被捕获, 不会向上抛出。
    """
    if server_type not in SERVERS:
        return False, f"未知服务类型: {server_type}"
    if not is_configured():
        return False, "未配置 iFinD 密钥 (backend/.env 的 IFIND_AUTH_TOKEN)"

    try:
        _ensure_session(server_type)
        payload = {
            "jsonrpc": "2.0",
            "id": _next_id(server_type),
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": params},
        }
        resp = _post(server_type, payload, CALL_TIMEOUT)
    except IfindError as e:
        _sessions.pop(server_type, None)         # 会话可能已失效, 下次重建
        return False, str(e)
    except requests.RequestException as e:
        _sessions.pop(server_type, None)
        return False, f"网络异常: {e}"

    if resp.status_code == 401:
        _sessions.pop(server_type, None)
        return False, "网关认证失败 (401 Gateway authentication failed)"
    if resp.status_code >= 400:
        return False, f"HTTP {resp.status_code}: {resp.text.strip()[:200]}"

    try:
        data = resp.json()
    except ValueError:
        return False, f"响应解析失败 (HTTP {resp.status_code})"

    if isinstance(data, dict) and "error" in data:
        err = data["error"]
        msg = err.get("message") if isinstance(err, dict) else str(err)
        return False, f"工具调用失败: {msg}"

    return True, _unwrap(data)
