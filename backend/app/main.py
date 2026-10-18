"""FastAPI 主入口 - 历史回放交易系统 v3

数据源: 全 A 股 + akshare 真实日 K
撮合: 延迟撮合 (OPEN 买单 / CLOSE 卖单)
时钟: ReplayEngine 全局交易日时钟
"""
from __future__ import annotations
import json
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime, date
from typing import Literal, Optional, List
from fastapi import FastAPI, HTTPException, Query, Header, Depends, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from pydantic import BaseModel, Field

from .models import Account, Order, OrderSide, OrderType, OrderStatus, Stock
from .services.replay_feed import ReplayFeed
from .services.replay_engine import ReplayEngine
from .services.matching import MatchingEngine
from .services.account import AccountManager
from .services.live_trade import LiveTradeManager
from .services.auth import auth_svc
from .data.stock_registry import StockRegistry
from .core.config import settings


# ========== 数据模型 ==========

class OrderRequest(BaseModel):
    stock_code: str = Field(..., min_length=6, max_length=6)
    side: Literal["BUY", "SELL", "buy", "sell"]
    type: Literal["LIMIT", "MARKET", "limit", "market"] = "LIMIT"
    price: float = Field(0.0, ge=0)
    quantity: int = Field(..., gt=0)


class AdvanceRequest(BaseModel):
    days: int = Field(1, ge=1, le=500)


class SeekRequest(BaseModel):
    date: str = Field(..., description="YYYY-MM-DD")


class AuthRegisterRequest(BaseModel):
    # 账号格式(4-20位字母开头)与密码复杂度由服务层校验, 以便返回中文提示
    username: str = Field(..., min_length=1, max_length=40)
    password: str = Field(..., min_length=1, max_length=64)
    phone: str = Field("", max_length=20)      # 必填, 交服务层校验并给中文提示
    email: str = Field("", max_length=80)


class AuthLoginRequest(BaseModel):
    username: str = Field(..., min_length=2, max_length=40)
    password: str = Field(..., min_length=1, max_length=64)


class AuthBindPhoneRequest(BaseModel):
    phone: str = Field("", max_length=20)
    email: str = Field("", max_length=80)


class WatchGroupRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=20)


class WatchItemRequest(BaseModel):
    group_id: Optional[int] = None
    # 需求101506-2: 板块名/指数代码均可能超过 6 字符, 放宽长度; kind 区分个股/板块/指数
    code: str = Field(..., min_length=1, max_length=64)
    name: str = ""
    kind: str = "stock"                      # stock | board | index
    meta: str = ""                           # JSON: 板块 {boardKind,label} / 指数 {code,symbol}


class WatchImportRequest(BaseModel):
    content: str = ""
    group_id: Optional[int] = None


class WatchItemsRequest(BaseModel):
    """批量加入自选/分类: items 为 [{code, name}] 列表 (板块成分股多选/全选后提交)"""
    group_id: Optional[int] = None
    items: List[dict] = Field(default_factory=list)


class DynGroupRequest(BaseModel):
    """动态分组: 规则驱动, 多条件 AND"""
    name: str = Field("", max_length=20)
    rules: dict = Field(default_factory=dict)


class DynPreviewRequest(BaseModel):
    rules: dict = Field(default_factory=dict)
    limit: int = Field(50, ge=1, le=500)


def get_current_user_id(authorization: str = Header(default="")) -> str:
    """从 Bearer token 解析当前登录用户, 未登录返回 'default' (游客)"""
    token = (authorization or "").removeprefix("Bearer ").strip()
    if not token:
        return "default"
    user = auth_svc.get_user(token)
    return user["user_id"] if user else "default"


def require_user_id(authorization: str = Header(default="")) -> str:
    """受限接口: 必须登录且已绑定手机号, 否则 401(未登录) / 403(需补绑手机号)"""
    token = (authorization or "").removeprefix("Bearer ").strip()
    user = auth_svc.get_user(token) if token else None
    if not user:
        raise HTTPException(401, "请先登录")
    if not user.get("phone_bound"):
        raise HTTPException(403, "请先绑定手机号后再使用")
    return user["user_id"]


def require_user_id_or_login(authorization: str = Header(default="")) -> str:
    """仅要求登录 (供补绑手机号等「登录后即可」的接口使用)"""
    token = (authorization or "").removeprefix("Bearer ").strip()
    user = auth_svc.get_user(token) if token else None
    if not user:
        raise HTTPException(401, "请先登录")
    return user["user_id"]


# ========== 全局单例 ==========

registry = StockRegistry()
feed = ReplayFeed()
account_mgr = AccountManager()
matching = MatchingEngine()
replay = ReplayEngine(feed, registry)
live_mgr = LiveTradeManager(feed, registry)


def _wl_quote(item: dict) -> dict:
    """需求101506-1/2: 自选项统一报价 (个股/板块/指数), 供自选分组批量返回与概览复用"""
    kind = str(item.get("kind") or "stock")
    meta: dict = {}
    raw = item.get("meta") or ""
    if raw:
        try:
            meta = json.loads(raw) or {}
        except Exception:
            meta = {}
    if kind == "board":
        try:
            from .services.boards import board_index
            rows = board_index(feed, meta.get("boardKind") or "industry",
                               meta.get("label") or item.get("name") or "", days=3)
            if rows:
                k = rows[-1]
                return {"price": k.get("close"), "change_pct": k.get("change_pct"),
                        "prev_close": (rows[-2].get("close") if len(rows) > 1 else None),
                        "suspended": False}
        except Exception:
            pass
        return {}
    if kind == "index":
        try:
            from .services.index_feed import index_quote
            return index_quote(meta.get("code") or item.get("code") or "")
        except Exception:
            return {}
    try:
        from .services.market_spot import market_spot
        spot = market_spot.get_spot() or {}
    except Exception:
        spot = {}
    return spot.get(item.get("code")) or {}


def _spot_map() -> dict:
    """全市场实时快照字典 (code → 报价), 失败或整体不可用时返回空

    需求101607: wait=False —— 批量列表接口不应等待在途的全市场刷新 (20~35s)。
    """
    try:
        from .services.market_spot import market_spot
        return market_spot.get_spot(wait=False) or {}
    except Exception:
        return {}


def _quick_quote(code: str, spot: dict) -> dict:
    """最新真实行情概况 {price, prev_close, change_pct, suspended}

    需求101607: 优先复用「全市场快照」(一次拉取即覆盖全部标的, 查表 O(1)), 替代逐只
    ``feed.get_kline()`` 冷取 akshare —— 后者单只 0.2~0.5s, 使批量接口 (搜索 30 只 /
    未分组 300 只) 冷启动 2.8s~60s+ 超时。快照整体不可用时不回落逐只拉取 (避免退化),
    仅当快照存在但缺该标的 (极少数) 才回落单只日K。
    """
    if not spot:
        return {"suspended": True}
    row = spot.get(code)
    if row and row.get("price"):
        return {"price": row.get("price"), "prev_close": row.get("prev_close"),
                "change_pct": row.get("change_pct"),
                "suspended": int(row.get("volume") or 0) == 0}
    try:
        kl = feed.get_kline(code)
    except Exception:
        kl = []
    if kl:
        k = kl[-1]
        prev_close = kl[-2].close if len(kl) > 1 else k.open
        return {"price": k.close, "prev_close": prev_close,
                "change_pct": (round((k.close - prev_close) / prev_close * 100, 2)
                               if prev_close else 0.0),
                "suspended": k.volume == 0}
    return {"suspended": True}


def _install_watchlist_quote_provider():
    from .services.watchlist import watchlist_svc
    watchlist_svc.quote_provider = _wl_quote


_install_watchlist_quote_provider()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1) 确保股票注册表就绪
    registry.ensure_ready()
    print(f"[Startup] 股票池 {registry.count()} 只")

    # 2) 恢复账户
    account_mgr.restore_from_db()

    # 3) 注入依赖
    replay.set_account_manager(account_mgr)
    replay.set_matching_engine(matching)

    # 4) 启动回放时钟 (默认从 2024-01-02 开始)
    replay.start()

    # 5) 需求101602: 后台预热重接口 (>0.5s), 消除前端首次进入模块的冷启动等待
    try:
        from .services.preload import warm_async
        warm_async(feed, registry)
    except Exception as e:
        print(f"[Startup] 预加载跳过: {e}")

    # 6) 需求101801: 百人以内并发 —— 同步端点由 anyio 线程池托管, 默认上限仅 40,
    #    提高上限以支撑 ~100 并发在线训练 (配合 SQLite WAL + busy_timeout 已足够)
    try:
        from anyio import to_thread
        to_thread.current_default_thread_limiter().total_tokens = 96
    except Exception as e:
        print(f"[Startup] 线程池调整跳过: {e}")

    yield
    # 回放系统无后台线程, 无需 stop


app = FastAPI(title="历史回放交易系统", version="3.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])
# 需求101702: 经 ngrok 等低速隧道访问时, 大体积 JSON (全市场快照 795KB / 看盘全景 / 板块K线)
# 不压缩导致传输极慢。此处对 /api 响应启用 gzip; 需客户端带 Accept-Encoding (代理不再剥离该头)。
app.add_middleware(GZipMiddleware, minimum_size=1024)


# ========== 回放控制 ==========

@app.get("/api/replay/status")
def replay_status():
    return {
        "clock_date": str(replay.clock_date) if replay.clock_date else None,
        "clock_index": replay.clock_index,
        "is_running": replay.is_running,
        "days_total": len(feed.get_trading_days()),
    }


@app.post("/api/replay/advance")
def replay_advance(req: AdvanceRequest):
    new_dates = replay.advance_days(req.days)
    return {
        "advanced": len(new_dates),
        "dates": [str(d) for d in new_dates],
        "clock_date": str(replay.clock_date) if replay.clock_date else None,
    }


@app.post("/api/replay/seek")
def replay_seek(req: SeekRequest):
    try:
        d = datetime.strptime(req.date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(400, "日期格式 YYYY-MM-DD")
    result = replay.seek_to(d)
    if result is None:
        raise HTTPException(404, f"未找到交易日 {req.date}")
    return {"clock_date": str(result)}


@app.post("/api/replay/start")
def replay_start():
    replay.start()
    return replay_status()


# ========== 股票搜索 ==========

@app.get("/api/stocks/search")
def stocks_search(keyword: str = Query(..., min_length=1), limit: int = 30):
    results = registry.search(keyword, limit)
    # 附加最新真实行情概况 (不受回放时钟影响); 需求101607: 复用全市场快照, 避免逐只冷取日K
    spot = _spot_map()
    enriched = []
    for r in results:
        item = {k: v for k, v in r.items()}
        item.update(_quick_quote(r["code"], spot))
        enriched.append(item)
    return enriched


@app.get("/api/stocks")
def stocks_list(market: Optional[str] = None, board: Optional[str] = None):
    return registry.list_all(market, board)


@app.get("/api/stocks/{code}")
def stock_detail(code: str):
    row = registry.get(code)
    if not row:
        raise HTTPException(404, "股票不存在")
    result = dict(row)
    # 最新真实日K (不受回放时钟截断), 与 /stocks/{code}/kline 口径一致
    kl = feed.get_kline(code)
    if kl:
        k = kl[-1]
        prev_close = kl[-2].close if len(kl) > 1 else k.open
        result.update({
            "price": k.close, "open": k.open, "high": k.high, "low": k.low,
            "volume": k.volume, "amount": k.amount,
            "prev_close": prev_close,
            "change_pct": round((k.close - prev_close) / prev_close * 100, 2) if prev_close else 0.0,
            "suspended": k.volume == 0,
            "trade_date": k.timestamp.strftime("%Y-%m-%d"),
        })
    else:
        result["suspended"] = True
    return result


@app.get("/api/stocks/{code}/kline")
def stock_kline(code: str, days: int = 120, period: Literal["day", "week", "month", "year"] = "day"):
    """最新市场 K 线: 不受回放时钟截断, 支持日/周/月/年周期, 每根附涨跌幅"""
    kl = feed.get_kline(code)
    if not kl:
        raise HTTPException(404, "股票无 K 线数据")
    kl = ReplayFeed.aggregate(kl, period)
    result = []
    prev_close = None
    for k in kl[-days:]:
        result.append({
            "date": k.timestamp.strftime("%Y-%m-%d"),
            "open": k.open, "high": k.high, "low": k.low,
            "close": k.close, "volume": k.volume, "amount": k.amount,
            "change_pct": round((k.close - prev_close) / prev_close * 100, 2) if prev_close else None,
        })
        prev_close = k.close
    return result


@app.get("/api/stocks/{code}/intraday")
def stock_intraday(code: str, date: Optional[str] = None):
    """查询当日分时 (优先真实分钟数据, 失败回退 OHLC 确定性重建)

    - 不传 date: 按「开市」规则取基准日 —— 今日开市(交易日)显示当日, 不开市显示上一交易日
      (该股在基准日无 K 线时回退到其最近一根日K)
    - 传 date=YYYY-MM-DD: 查看历史任一天的分时 (K线点击联动)
    响应附 market_open / is_today, 供前端标注「当日 / 上一交易日」
    """
    from datetime import datetime as _dt
    from .services.intraday import (generate_intraday, generate_intraday_real,
                                    build_intraday_from_minutes, minute_series)

    today = _dt.now().date()
    ref_day = None
    # 确定目标日期
    if date:
        try:
            day = _dt.strptime(date, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(400, "date 格式应为 YYYY-MM-DD")
        market_open = None                                   # 指定历史日期: 不标注开市状态
    else:
        ref_day, market_open = feed.market_ref_day()          # 开市→当日, 不开市→上一交易日
        kl = feed.get_kline(code)
        # 需求101601: 当日开市且已有当日真实分钟数据 → 优先「当日」
        # (akshare 日K通常收盘后才更新, 不这样处理会退到上一交易日的分时)
        if market_open and minute_series(code).get(str(ref_day)):
            day = ref_day
        elif feed.get_kline_on(code, ref_day) is not None:
            day = ref_day
        elif kl:
            day = kl[-1].timestamp.date()                     # 停牌/未更新 → 回退该股最近日K
        elif replay.clock_date:
            day = replay.clock_date
        else:
            raise HTTPException(400, "无法确定日期")
    is_today = (day == today)

    k = feed.get_kline_on(code, day)
    prev = feed._prev_kline(code, day)
    if not k:
        # 需求101601: 当日日K尚未更新, 但已有当日真实分钟数据 → 用分钟序列构造当日分时
        if day == today:
            data = build_intraday_from_minutes(
                day, minute_series(code).get(str(day)), prev.close if prev else None,
                {"market_open": market_open, "is_today": is_today})
            if data:
                return data
        return {"date": str(day), "prev_close": prev.close if prev else None,
                "suspended": True, "points": [],
                "market_open": market_open, "is_today": is_today,
                "hint": "该日无K线 (停牌或未上市)"}
    # 真实分钟数据优先, 失败回退布朗桥重建 (保证不空窗)
    data = None
    if k.volume:
        data = generate_intraday_real(code, day, k.open, k.high, k.low, k.close,
                                      k.volume, prev.close if prev else None)
    if data is None:
        data = generate_intraday(code, day, k.open, k.high, k.low, k.close,
                                 k.volume, prev.close if prev else None)
    data = dict(data)                                        # 浅拷贝: 避免污染分钟缓存
    data["market_open"] = market_open
    data["is_today"] = is_today
    return data


# ========== 板块查询 ==========

@app.get("/api/boards")
def boards_list(kind: str = Query("industry", pattern="^(industry|concept)$")):
    """行业/概念板块列表"""
    from .services.boards import list_boards
    try:
        rows = list_boards(kind)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"板块数据获取失败: {e}")
    return {"kind": kind, "boards": rows}


@app.get("/api/boards/search")
def boards_search(kw: str = Query(..., min_length=1), limit: int = Query(20, ge=1, le=50)):
    """板块模糊搜索 (一次搜「行业 + 概念」, 名称/拼音/label)"""
    from .services.boards import search_all_boards
    try:
        rows = search_all_boards(kw, limit)
    except Exception as e:
        raise HTTPException(502, f"板块搜索失败: {e}")
    return {"kw": kw, "boards": rows}


@app.get("/api/boards/{board_name}/stocks")
def boards_stocks(board_name: str, kind: str = Query("industry", pattern="^(industry|concept)$")):
    """板块成分股 (按涨跌幅正序)"""
    from .services.boards import board_stocks
    try:
        rows = board_stocks(kind, board_name)
    except Exception as e:
        raise HTTPException(502, f"成分股获取失败: {e}")
    return {"kind": kind, "board": board_name, "stocks": rows}


@app.get("/api/boards/{board_name}/kline")
def boards_kline(board_name: str, kind: str = Query("industry", pattern="^(industry|concept)$"),
                 days: int = Query(240, ge=20, le=1000),
                 period: Literal["day", "week", "month", "year"] = "day"):
    """板块 K 线 (成分股等权复合指数, 基准 1000)"""
    from .services.boards import board_index
    try:
        rows = board_index(feed, kind, board_name, days=days, period=period)
    except Exception as e:
        raise HTTPException(502, f"板块K线获取失败: {e}")
    if not rows:
        raise HTTPException(404, "该板块暂无K线数据")
    return rows


@app.get("/api/boards/{board_name}/intraday")
def boards_intraday(board_name: str, kind: str = Query("industry", pattern="^(industry|concept)$"),
                    date: Optional[str] = None):
    """板块分时 (成分股真实分钟按成交量加权合成; 无可得分钟数据时返回空数据样式)"""
    from datetime import datetime as _dt
    from .services.boards import board_intraday
    try:
        data = board_intraday(feed, kind, board_name, date=date)
    except Exception as e:
        raise HTTPException(502, f"板块分时获取失败: {e}")
    # 标注是否为「当日」分时 (板块分时取可得分钟数据的最新交易日)
    try:
        d = _dt.strptime(str(data.get("date"))[:10], "%Y-%m-%d").date()
        data["is_today"] = (d == _dt.now().date())
    except (ValueError, TypeError):
        data["is_today"] = False
    return data


# ========== 大盘指数行情 (需求5: 看盘点击指数跳转图表模块) ==========

@app.get("/api/index/list")
def index_list():
    """常用大盘指数清单 (上证/深证/创业板/科创50/沪深300/北证50)"""
    from .services.index_feed import list_indices
    return {"indices": list_indices()}


@app.get("/api/index/{code}/kline")
def index_kline(code: str, days: int = Query(240, ge=20, le=1000),
                period: Literal["day", "week", "month", "year"] = "day"):
    """指数 K 线 (新浪日K, 支持日/周/月/年周期)"""
    from .services.index_feed import index_kline as _index_kline
    try:
        rows = _index_kline(code, days=days, period=period)
    except Exception as e:
        raise HTTPException(502, f"指数K线获取失败: {e}")
    if not rows:
        raise HTTPException(404, "该指数暂无K线数据")
    return rows


@app.get("/api/index/{code}/intraday")
def index_intraday(code: str, date: Optional[str] = None):
    """指数分时 (真实 1 分钟优先, 缺失时按当日 OHLC 确定性重建)"""
    from .services.index_feed import index_intraday as _index_intraday
    try:
        return _index_intraday(code, date=date)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(502, f"指数分时获取失败: {e}")


# ========== 聚合搜索 (个股 + 板块, 支持代码/名称/首字母/模糊) ==========

@app.get("/api/search")
def unified_search(kw: str = Query(..., min_length=1), limit: int = Query(12, ge=1, le=50)):
    """个股 + 板块聚合搜索: 代码 / 名称 / 拼音首字母, 前缀优先 (价格为最新真实日K)"""
    from .services.boards import search_all_boards
    text = kw.strip()
    stocks = registry.search(text, limit)
    spot = _spot_map()                     # 需求101607: 复用全市场快照
    enriched = []
    for r in stocks:
        item = {k: v for k, v in r.items()}
        item.update(_quick_quote(r["code"], spot))
        enriched.append(item)
    try:
        boards = search_all_boards(text, limit)
    except Exception:
        boards = []
    return {"stocks": enriched, "boards": boards[:limit]}


# ========== 交易 ==========

@app.post("/api/orders")
def place_order(req: OrderRequest, user_id: str = Depends(get_current_user_id)):
    order = Order(
        stock_code=req.stock_code,
        side=OrderSide(req.side.upper()),
        order_type=OrderType(req.type.upper()),
        price=req.price,
        quantity=req.quantity,
        user_id=user_id,
    )

    # 构建 Stock 对象做校验 (用今日 K 线), 自动加入 watch
    replay.add_watch(req.stock_code)
    stock = replay.get_stock(req.stock_code)

    valid, msg = account_mgr.validate_and_freeze(order, stock)
    if not valid:
        order.status = OrderStatus.REJECTED
        order._reject_reason = msg
        return HTTPException(400, {"order_id": order.order_id, "reason": msg})

    # 撮合引擎只挂单, 等回放时钟 OPEN/CLOSE 触发
    order = matching.submit_order(order)

    return {
        "order_id": order.order_id,
        "status": order.status.value,
        "message": "委托已挂单",
    }


@app.delete("/api/orders/{order_id}")
def cancel_order(order_id: str):
    order = matching.get_order(order_id)
    if not order:
        raise HTTPException(404, "订单不存在")
    ok = matching.cancel_order(order_id)
    if not ok:
        raise HTTPException(400, f"订单已{order.status.value}")
    account_mgr.cancel_order(order)
    return {"success": True}


@app.get("/api/orders")
def list_orders():
    return [
        {
            "order_id": o.order_id,
            "stock_code": o.stock_code,
            "side": o.side.value,
            "type": o.order_type.value,
            "price": o.price,
            "quantity": o.quantity,
            "filled": o.filled_quantity,
            "status": o.status.value,
            "created_at": o.created_at.strftime("%Y-%m-%d %H:%M:%S"),
        }
        for o in matching.get_all_orders()
    ]


@app.get("/api/trades")
def list_trades(limit: int = 500):
    return [
        {
            "trade_id": t.trade_id,
            "stock_code": t.stock_code,
            "side": t.side.value,
            "price": t.price,
            "quantity": t.quantity,
            "amount": t.amount,
            "buyer_fee": t.buyer_fee,
            "seller_fee": t.seller_fee,
            "buyer_id": t.buyer_id,
            "seller_id": t.seller_id,
            "created_at": t.created_at.strftime("%Y-%m-%d %H:%M:%S"),
        }
        for t in matching.get_trades(limit)
    ]


# ========== 账户 ==========

@app.get("/api/account")
def get_account(user_id: str = Depends(get_current_user_id)):
    a = account_mgr.get_account(user_id)
    return {
        "tier": a.tier,
        "cash": round(a.cash, 2),
        "frozen_cash": round(a.frozen_cash, 2),
        "total_assets": a.total_assets,
        "positions_count": len(a.positions),
    }


@app.get("/api/account/profit")
def get_profit(user_id: str = Depends(get_current_user_id)):
    summary = account_mgr.get_profit_summary(user_id)
    clock = replay.clock_date
    for p in summary["positions"]:
        code = p["code"]
        row = registry.get(code)
        p["name"] = row.get("name", "") if row else ""
        if clock:
            k = feed.get_kline_on(code, clock)
            if k:
                p["price"] = k.close
                p["profit"] = round((k.close - p["avg_cost"]) * p["quantity"], 2)
                p["profit_pct"] = round((k.close - p["avg_cost"]) / p["avg_cost"] * 100, 2) if p["avg_cost"] > 0 else 0
    tier = account_mgr.get_account(user_id).tier
    init_cash = settings.TIER_CASH.get(tier, settings.INITIAL_CASH)
    summary["total_profit_pct"] = round(summary["total_profit"] / init_cash * 100, 2)
    return summary
# ========== 账户分级 ==========

@app.get("/api/account/tiers")
def list_tiers():
    return {
        "tiers": [
            {"key": "basic", "name": "基础", "initial_cash": settings.TIER_CASH["basic"]},
            {"key": "pro",   "name": "进阶", "initial_cash": settings.TIER_CASH["pro"]},
            {"key": "vip",   "name": "专业", "initial_cash": settings.TIER_CASH["vip"]},
        ]
    }


@app.post("/api/account/tier/{tier}")
def set_account_tier(tier: str, user_id: str = Depends(get_current_user_id)):
    ok, msg = account_mgr.set_tier(tier, user_id)
    if not ok:
        raise HTTPException(400, msg)
    matching.clear()
    return {"success": True, "message": msg, "account": {
        "tier": account_mgr.get_account(user_id).tier,
        "cash": account_mgr.get_account(user_id).cash,
    }}


# ========== 重置账户 ==========

@app.post("/api/account/reset")
def reset_account(user_id: str = Depends(get_current_user_id)):
    matching.clear()
    account_mgr._accounts[user_id] = Account(
        user_id=user_id, tier="basic",
        cash=settings.TIER_CASH.get("basic", settings.INITIAL_CASH), frozen_cash=0.0)
    account_mgr._persist()
    return {"success": True}


# ========== 模拟交易 (实时撮合) ==========

class LiveOrderRequest(BaseModel):
    code: str = Field(..., min_length=6, max_length=6)
    side: Literal["BUY", "SELL", "buy", "sell"]
    type: Literal["LIMIT", "MARKET", "limit", "market"] = "LIMIT"
    price: float = Field(0.0, ge=0)
    quantity: Optional[int] = Field(None, gt=0)
    ratio: Optional[float] = Field(None, gt=0, le=1)


class LiveMetaRequest(BaseModel):
    strategy_id: Optional[int] = None
    note: Optional[str] = None


class LiveConditionalRequest(BaseModel):
    code: str
    kind: str = "take_profit"          # take_profit / stop_loss
    trigger_price: float
    ratio: float = 1.0


@app.get("/api/live/tiers")
def live_tiers():
    """资金体系档位 (基础/进阶/专业/大户)"""
    return {"tiers": live_mgr.list_tiers()}


@app.get("/api/live/account")
def live_account(user_id: str = Depends(get_current_user_id)):
    """资金 + 持仓明细 (含可用/不可用股数与可用资金)"""
    live_mgr.try_fill_pending(user_id)
    return live_mgr.get_account(user_id)


@app.post("/api/live/tier/{tier}")
def live_bind_tier(tier: str, user_id: str = Depends(get_current_user_id)):
    """切换资金体系 (各体系账户完全独立, 切换不重置任何账户)"""
    try:
        return live_mgr.bind_tier(user_id, tier)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/live/reset")
def live_reset(user_id: str = Depends(get_current_user_id)):
    """重置「当前体系」的资金/持仓/委托/成交 (其他体系与历史回合保留)"""
    return live_mgr.reset(user_id)


@app.get("/api/live/quote/{code}")
def live_quote(code: str, user_id: str = Depends(get_current_user_id)):
    """盘口快照 + 单笔委托上限 (当日成交额 5%)"""
    live_mgr.try_fill_pending(user_id)
    snap = live_mgr.market_snapshot(code)
    if not snap:
        raise HTTPException(404, "该股暂无行情数据")
    return snap


@app.post("/api/live/orders")
def live_place_order(req: LiveOrderRequest, user_id: str = Depends(get_current_user_id)):
    try:
        return live_mgr.place_order(user_id, req.code, req.side, req.type,
                                    price=req.price, quantity=req.quantity, ratio=req.ratio)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/live/orders/{order_id}")
def live_cancel_order(order_id: str, user_id: str = Depends(get_current_user_id)):
    if not live_mgr.cancel_order(user_id, order_id):
        raise HTTPException(404, "委托不存在或已结束")
    return {"success": True}


@app.get("/api/live/orders")
def live_orders(limit: int = Query(100, ge=1, le=500), user_id: str = Depends(get_current_user_id)):
    live_mgr.try_fill_pending(user_id)
    return {"orders": live_mgr.list_orders(user_id, limit)}


@app.get("/api/live/trades")
def live_trades(limit: int = Query(100, ge=1, le=500), user_id: str = Depends(get_current_user_id)):
    return {"trades": live_mgr.list_trades(user_id, limit)}


@app.get("/api/live/deliveries")
def live_deliveries(limit: int = Query(200, ge=1, le=500), user_id: str = Depends(get_current_user_id)):
    """交割单: 每一笔成交独立成行 (买一笔 / 卖一笔), 供模拟交易复盘"""
    return live_mgr.list_deliveries(user_id, limit)


# ---------- 条件单 (止盈 / 止损, 触发即尝试操作) ----------

@app.get("/api/live/conditionals")
def live_conditionals(limit: int = Query(50, ge=1, le=500),
                      user_id: str = Depends(get_current_user_id)):
    return {"conditionals": live_mgr.list_conditionals(user_id, limit)}


@app.post("/api/live/conditionals")
def live_set_conditional(req: LiveConditionalRequest,
                         user_id: str = Depends(get_current_user_id)):
    """挂出止盈/止损条件单 (不要求先有持仓, 触发时再尝试操作)"""
    try:
        return live_mgr.set_conditional(user_id, req.code, req.kind, req.trigger_price, req.ratio)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/live/conditionals/{cond_id}")
def live_cancel_conditional(cond_id: str, user_id: str = Depends(get_current_user_id)):
    if not live_mgr.cancel_conditional(user_id, cond_id):
        raise HTTPException(404, "条件单不存在或已失效")
    return {"success": True}


@app.get("/api/live/history")
def live_history(limit: int = Query(50, ge=1, le=500), user_id: str = Depends(get_current_user_id)):
    """复盘: 已完成交易回合 (归一化为训练同形字段)"""
    from .services.training_session import training_mgr
    if user_id == "default":            # 未登录: 复盘记录绑定账号, 不展示
        return {"sessions": []}
    names = {s["id"]: s["name"] for s in training_mgr.list_strategies()}
    rows = live_mgr.list_history(user_id, limit)
    for r in rows:
        r["strategy_name"] = names.get(r.get("strategy_id")) if r.get("strategy_id") else None
    return {"sessions": rows}


@app.get("/api/live/history/{trip_id}")
def live_history_detail(trip_id: str, user_id: str = Depends(get_current_user_id)):
    if user_id == "default":
        raise HTTPException(404, "模拟交易记录不存在")
    d = live_mgr.get_history(user_id, trip_id)
    if not d:
        raise HTTPException(404, "模拟交易记录不存在")
    return d


@app.patch("/api/live/history/{trip_id}")
def live_history_meta(trip_id: str, req: LiveMetaRequest,
                      user_id: str = Depends(get_current_user_id)):
    ok = live_mgr.set_trip_meta(user_id, trip_id, req.strategy_id, req.note)
    if not ok:
        raise HTTPException(404, "模拟交易记录不存在")
    return live_mgr.get_history(user_id, trip_id)


# ========== 账号认证 ==========

@app.post("/api/auth/register")
def auth_register(req: AuthRegisterRequest):
    try:
        user = auth_svc.register(req.username, req.password, req.phone, req.email)
    except ValueError as e:
        raise HTTPException(400, str(e))
    token = auth_svc.login(req.username, req.password)["token"]
    account_mgr.ensure_account(user["user_id"])
    return {"token": token, "user_id": user["user_id"], "username": user["username"],
            "phone": user["phone"], "email": user.get("email", ""),
            "need_bind_phone": False}


@app.post("/api/auth/login")
def auth_login(req: AuthLoginRequest):
    try:
        result = auth_svc.login(req.username, req.password)
    except ValueError as e:
        raise HTTPException(400, str(e))
    account_mgr.ensure_account(result["user_id"])
    return result


@app.post("/api/auth/bind-phone")
def auth_bind_phone(req: AuthBindPhoneRequest, user_id: str = Depends(require_user_id_or_login)):
    """补绑手机号 (旧账号/未绑定账号): 登录即可调用, 不要求已绑手机号"""
    try:
        return auth_svc.bind_phone(user_id, req.phone, req.email)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/auth/logout")
def auth_logout(authorization: str = Header(default="")):
    auth_svc.logout((authorization or "").removeprefix("Bearer ").strip())
    return {"success": True}


@app.get("/api/auth/me")
def auth_me(user_id: str = Depends(get_current_user_id)):
    if user_id == "default":
        return {"user_id": "default", "username": "游客", "phone": "", "email": "",
                "phone_bound": False, "logged_in": False}
    p = auth_svc.profile(user_id) or {"user_id": user_id, "username": user_id,
                                      "phone": "", "email": "", "phone_bound": False}
    return {**p, "logged_in": True}


# ========== 自选股分组 ==========

@app.get("/api/watchlist")
def watchlist_list(user_id: str = Depends(get_current_user_id)):
    from .services.watchlist import watchlist_svc
    if user_id == "default":            # 未登录: 自选绑定账号, 不展示任何自选
        return {"groups": []}
    return {"groups": watchlist_svc.list_groups(user_id)}


@app.get("/api/watchlist/overview")
def watchlist_overview(user_id: str = Depends(get_current_user_id)):
    """需求9: 各分组涨跌幅概览 (顶部标签栏展示)"""
    from .services.watchlist import watchlist_svc
    if user_id == "default":
        return {"groups": []}
    return {"groups": watchlist_svc.groups_overview(user_id)}


@app.post("/api/watchlist/group")
def watchlist_add_group(req: WatchGroupRequest, user_id: str = Depends(require_user_id)):
    from .services.watchlist import watchlist_svc
    try:
        return watchlist_svc.add_group(user_id, req.name)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/watchlist/group/{group_id}")
def watchlist_del_group(group_id: int, user_id: str = Depends(require_user_id)):
    from .services.watchlist import watchlist_svc
    try:
        if not watchlist_svc.delete_group(user_id, group_id):
            raise HTTPException(404, "分组不存在")
        return {"success": True}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/watchlist/item")
def watchlist_add_item(req: WatchItemRequest, user_id: str = Depends(require_user_id)):
    from .services.watchlist import watchlist_svc
    try:
        return watchlist_svc.add_item(user_id, req.code, req.name, req.group_id,
                                      kind=req.kind, meta=req.meta)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/watchlist/quotes")
def watchlist_quotes(group_id: Optional[int] = None,
                     user_id: str = Depends(get_current_user_id)):
    """需求101506-1/2: 分组自选项 + 报价一次性批量返回 (个股/板块/指数)"""
    from .services.watchlist import watchlist_svc
    if user_id == "default":
        return {"group_id": None, "items": []}
    return watchlist_svc.group_quotes(user_id, group_id)


@app.delete("/api/watchlist/item/{item_id}")
def watchlist_del_item(item_id: int, user_id: str = Depends(require_user_id)):
    from .services.watchlist import watchlist_svc
    watchlist_svc.remove_item(user_id, item_id)
    return {"success": True}


# 分时缩略图缓存: code → (时间戳, 序列|None), 60s 内复用
_SPARK_CACHE: dict = {}
_SPARK_TTL = 60.0
_SPARK_MAX = 20          # 单次请求上限 (前端一屏个股数量级)


@app.get("/api/watchlist/sparklines")
def watchlist_sparklines(codes: str = Query("", max_length=1200)):
    """需求101508-2: 图表左侧个股列表的当日分时缩略图

    返回 ``{"items": {code: [价格序列...]}}`` (等距下采样至 ≤48 点);
    无真实分钟数据的代码不出现在 items 中 (前端回落展示涨跌幅文本)。
    """
    want = [c.strip() for c in (codes or "").split(",") if c.strip()][:_SPARK_MAX]
    if not want:
        return {"items": {}}
    now = time.time()
    out: dict = {}
    todo: List[str] = []
    for c in want:
        hit = _SPARK_CACHE.get(c)
        if hit and now - hit[0] < _SPARK_TTL:
            if hit[1]:
                out[c] = hit[1]
        else:
            todo.append(c)
    if todo:
        from concurrent.futures import ThreadPoolExecutor
        from .services.intraday import minute_series

        def _one(code: str):
            try:
                days = minute_series(code) or {}
                if not days:
                    return code, None
                pts = [p for _, p, _ in days[max(days)]]
                if len(pts) < 5:
                    return code, None
                step = max(1, len(pts) // 48)
                return code, [round(p, 2) for p in pts[::step][:48]]
            except Exception:                 # noqa: BLE001 - 缩略图非关键路径
                return code, None

        with ThreadPoolExecutor(max_workers=6) as ex:
            for code, series in ex.map(_one, todo):
                _SPARK_CACHE[code] = (now, series)
                if series:
                    out[code] = series
    return {"items": out}


@app.post("/api/watchlist/items")
def watchlist_add_items(req: WatchItemsRequest, user_id: str = Depends(require_user_id)):
    """批量加入自选/分类 (板块成分股多选/全选): 同代码去重, 返回 added/skipped"""
    from .services.watchlist import watchlist_svc
    added = skipped = 0
    seen = set()
    for it in req.items:
        code = str(it.get("code") or "").strip()
        name = str(it.get("name") or "").strip()
        kind = str(it.get("kind") or "stock").strip() or "stock"
        meta = str(it.get("meta") or "")
        if not code or code in seen:
            skipped += 1
            continue
        seen.add(code)
        if not name and kind == "stock":
            row = registry.get(code)
            name = row["name"] if row else code
        try:
            r = watchlist_svc.add_item(user_id, code, name, req.group_id,
                                       kind=kind, meta=meta)
        except ValueError:            # 分组不存在等
            skipped += 1
            continue
        skipped += 1 if r.get("exists") else 0
        added += 0 if r.get("exists") else 1
    return {"total": len(req.items), "added": added, "skipped": skipped}


def _parse_watchlist_tokens(content: str) -> List[str]:
    """导入文本 → token 列表: 按行切分, 行内再按 逗号/制表符/分号/空白 切分"""
    tokens: List[str] = []
    for line in (content or "").splitlines():
        line = line.strip()
        if not line:
            continue
        tokens.extend(t for t in re.split(r"[,;\t\s]+", line) if t)
    return tokens


def _extract_import_tokens(content: str) -> List[str]:
    """解析导入内容 → token 列表 (兼容格式):
    - JSON 数组: [{"code":"600519","name":"贵州茅台"}] 或 ["600519", ...]
    - CSV / TSV / 纯文本: 按行 + 分隔符切分
    """
    raw = (content or "").strip()
    if not raw:
        return []
    if raw.startswith("["):
        try:
            arr = json.loads(raw)
        except Exception:
            arr = None
        if isinstance(arr, list):
            toks: List[str] = []
            for it in arr:
                if isinstance(it, str):
                    toks.append(it)
                elif isinstance(it, dict):
                    c = it.get("code") or it.get("symbol") or it.get("代码")
                    if c:
                        toks.append(str(c))
            if toks:
                return toks
    return _parse_watchlist_tokens(content)


# 前缀/后缀市场标识 (归一化时剥离)
_SUFFIX_MARKETS = ("SH", "SZ", "BJ", "SS", "HK", "NYSE", "NASDAQ")
_HEADER_RE = re.compile(r"(?i)^(代码|名称|序号|code|name|symbol|no\.?)$")


def _normalize_token(tok: str) -> str:
    """归一化市场标记: sh600519 / 600519.SH / SH.600519 / 00700.HK → 纯代码"""
    t = (tok or "").strip().strip("\"'").strip()
    if not t:
        return ""
    # 前缀+点: SH.600519 / SZ.000001
    m = re.fullmatch(r"(?i)(sh|sz|bj)\.(\d{5,6})", t)
    if m:
        return m.group(2)
    # 后缀: 600519.SH / 000001.SZ / 00700.HK / AAPL.NASDAQ
    if "." in t:
        head, _, tail = t.rpartition(".")
        if tail.upper() in _SUFFIX_MARKETS:
            t = head
    # 前缀: sh600519 / SZ000001 / bj430047
    m = re.fullmatch(r"(?i)(sh|sz|bj)(\d{5,6})", t)
    if m:
        return m.group(2)
    return t.strip()


def _token_to_stock(token: str) -> Optional[dict]:
    """单个 token → 股票 {code, name}; 匹配不到返回 None

    规则: 归一化后 6 位纯数字按代码精确匹配; 含非数字字符的 token 按名称/拼音走 search() 取最佳。
    (纯数字 token 不走模糊搜索, 避免行号/数量等误命中代码)
    """
    tok = _normalize_token(token)
    if not tok:
        return None
    if re.fullmatch(r"\d{6}", tok):
        row = registry.get(tok)
        return {"code": row["code"], "name": row["name"]} if row else None
    if tok.isdigit():
        return None
    hits = registry.search(tok, 1)
    return {"code": hits[0]["code"], "name": hits[0]["name"]} if hits else None


@app.post("/api/watchlist/import")
def watchlist_import(req: WatchImportRequest, user_id: str = Depends(require_user_id)):
    """解析上传内容中的股票代码/名称, 批量加入当前自选分组 (同一只去重)

    兼容: JSON 数组 / CSV / TSV / 纯文本; 支持 sh600519 / 600519.SH / SH.600519 / 00700.HK / AAPL
    """
    from .services.watchlist import watchlist_svc
    tokens = _extract_import_tokens(req.content)
    # 跳过 CSV 表头行 (代码 / 名称 / code / name ...)
    while tokens and _HEADER_RE.match(tokens[0]):
        tokens.pop(0)
    matched: List[dict] = []          # 去重后的候选股票
    seen = set()
    for tok in tokens:
        hit = _token_to_stock(tok)
        if hit and hit["code"] not in seen:
            seen.add(hit["code"])
            matched.append(hit)
    added = skipped = 0
    for it in matched:
        try:
            r = watchlist_svc.add_item(user_id, it["code"], it["name"], req.group_id)
        except ValueError:            # 分组不存在等
            skipped += 1
            continue
        skipped += 1 if r.get("exists") else 0
        added += 0 if r.get("exists") else 1
    return {"total": len(matched), "added": added, "skipped": skipped, "items": matched}


@app.get("/api/watchlist/ungrouped")
def watchlist_ungrouped(limit: int = Query(300, ge=1, le=1000),
                        user_id: str = Depends(get_current_user_id)):
    """注册表里有、但未被该用户加入任何自选分组的股票 (按代码排序, 附最新价/涨跌幅)"""
    from .services.watchlist import watchlist_svc
    watched = {it["code"]
               for g in watchlist_svc.list_groups(user_id)
               for it in g.get("items", [])}
    rows = sorted((r for r in registry.list_all() if r["code"] not in watched),
                  key=lambda x: x["code"])[:limit]
    spot = _spot_map()               # 需求101607: 复用全市场快照 (原逐只 get_kline 冷取会 >60s 超时)
    items = []
    for r in rows:                    # 最新真实行情概况 (口径同 /api/stocks/search)
        item = {"code": r["code"], "name": r["name"], "market": r.get("market")}
        item.update(_quick_quote(r["code"], spot))
        items.append(item)
    return {"total": len(items), "items": items}


# ========== 动态分组 (规则驱动 + 全市场实时求值) ==========

@app.get("/api/dyn-groups/metrics")
def dyn_group_metrics():
    """动态分组可用指标元数据 (前端据此渲染条件编辑器)"""
    from .services.dyn_group import METRICS
    return {"metrics": METRICS}


@app.get("/api/dyn-groups")
def dyn_groups_list(user_id: str = Depends(require_user_id)):
    """内置预设(只读) + 当前用户自定义动态分组"""
    from .services.dyn_group import dyn_group_svc
    return {"groups": dyn_group_svc.list_groups(user_id)}


@app.post("/api/dyn-groups")
def dyn_groups_add(req: DynGroupRequest, user_id: str = Depends(require_user_id)):
    from .services.dyn_group import dyn_group_svc
    try:
        return dyn_group_svc.add_group(user_id, req.name, req.rules)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.put("/api/dyn-groups/{group_id}")
def dyn_groups_update(group_id: str, req: DynGroupRequest, user_id: str = Depends(require_user_id)):
    from .services.dyn_group import dyn_group_svc
    gid: object = int(group_id) if group_id.isdigit() else group_id
    try:
        return dyn_group_svc.update_group(user_id, gid, req.name or None, req.rules or None)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/dyn-groups/{group_id}")
def dyn_groups_delete(group_id: str, user_id: str = Depends(require_user_id)):
    from .services.dyn_group import dyn_group_svc
    gid: object = int(group_id) if group_id.isdigit() else group_id
    try:
        ok = dyn_group_svc.delete_group(user_id, gid)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not ok:
        raise HTTPException(404, "动态分组不存在")
    return {"success": True}


@app.get("/api/dyn-groups/{group_id}/stocks")
def dyn_groups_stocks(group_id: str, limit: int = Query(200, ge=1, le=1000),
                      refresh: int = Query(0, ge=0, le=1),
                      user_id: str = Depends(require_user_id)):
    """每次调用按规则对全市场快照重新求值, 动态返回命中个股"""
    from .services.dyn_group import dyn_group_svc
    gid: object = int(group_id) if group_id.isdigit() else group_id
    hits = dyn_group_svc.stocks(user_id, gid, limit=limit, force=bool(refresh))
    if hits is None:
        raise HTTPException(404, "动态分组不存在")
    return {"group_id": group_id, "total": len(hits), "stocks": hits}


@app.get("/api/dyn-groups/recent-metrics")
def dyn_groups_recent_metrics(user_id: str = Depends(require_user_id)):
    """当前用户最近使用过的指标 (供配置页「最近使用」分类快速复用)"""
    from .services.dyn_group import dyn_group_svc
    return {"metrics": dyn_group_svc.recent_metrics(user_id)}


@app.post("/api/dyn-groups/preview")
def dyn_groups_preview(req: DynPreviewRequest, user_id: str = Depends(require_user_id)):
    """未保存规则的即时预览 (配置页查看命中数/样例)"""
    from .services.dyn_group import dyn_group_svc, evaluate, normalize_rules
    from .services.market_spot import market_spot
    try:
        rules = normalize_rules(req.rules)
    except ValueError as e:
        raise HTTPException(400, str(e))
    dyn_group_svc.record_recent(user_id, [c["metric"] for c in rules["conditions"]])
    hits = evaluate(rules, market_spot.get_spot(), limit=req.limit)
    return {"total": len(hits), "stocks": hits}


# ========== 训练系统 ==========

class TrainStartRequest(BaseModel):
    category: Literal["stock", "long", "demon"] = "stock"
    bars: int = Field(100, ge=20, le=1250)
    code: Optional[str] = None
    initial_capital: float = Field(100_000.0, ge=10_000)
    leverage: Optional[float] = Field(None, ge=1, le=100)
    strategy_id: Optional[int] = None
    period: Literal["day", "week", "month", "year"] = "day"
    # 随机抽标的时的「涨停次数档位权重」(无/低/中/高, 长度4); 仅 code 为空时生效
    limit_up_weights: Optional[List[float]] = Field(None, min_length=4, max_length=4)
    # 指定起始日期 (YYYY-MM-DD): 与 code 同时给出 → 非双盲训练(名称与日期可见)
    start_date: Optional[str] = None


class TrainActionRequest(BaseModel):
    qty: Optional[int] = None
    ratio: Optional[float] = Field(None, gt=0, le=1)
    allow_short: bool = False


class StrategyRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=40)
    description: str = ""
    color: str = "#00b5ff"


class TrainAnnosRequest(BaseModel):
    """训练划线数据 (需求3): 整体覆盖保存, 复盘历史对局同步渲染"""
    annos: List[dict] = Field(default_factory=list)


class TrainConditionalRequest(BaseModel):
    kind: Literal["take_profit", "stop_loss", "trailing_stop"]
    trigger_price: Optional[float] = Field(None, gt=0)   # 移动止损不需要固定触发价
    ratio: float = Field(1.0, gt=0, le=1)
    trail_pct: Optional[float] = Field(None, gt=0, le=100)   # 移动止损回撤幅度(%)
    side: Optional[Literal["long", "short"]] = None


class SessionMetaRequest(BaseModel):
    strategy_id: Optional[int] = None
    note: Optional[str] = None


class TrainPeriodRequest(BaseModel):
    period: Literal["day", "week", "month", "year"] = "day"


@app.get("/api/training/categories")
def train_categories(response: Response):
    """训练品类元数据 (股票双盲 + 长周期训练)

    需求101701-2: 移动端曾因浏览器缓存旧响应而看不到「长周期训练」品类。
    该接口不使用任何缓存 (no-store), 保证每次进入训练模块都拿到最新品类列表。
    """
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    from .services.training_session import (CATEGORY_LABELS, CATEGORY_LEVERAGE,
                                            CATEGORY_BARS_CAP, CATEGORY_DEFAULT_BARS)
    from .services.stock_pool import bucket_meta
    buckets = bucket_meta()
    return {"categories": [
        {"key": k, "label": v, "leverage": CATEGORY_LEVERAGE.get(k, 1.0),
         "bars_max": CATEGORY_BARS_CAP.get(k, 250),
         "bars_default": CATEGORY_DEFAULT_BARS.get(k, 250),
         "limit_up_buckets": buckets,
         "limit_up_weights_default": [1] * len(buckets)}
        for k, v in CATEGORY_LABELS.items()
    ]}


@app.get("/api/training/pool")
def train_pool(filter: str = Query("all"), limit: int = Query(50, ge=1, le=200),
               offset: int = Query(0, ge=0)):
    """训练标的池 (需求101802-1): 展示当前池内个股 + 筛选维度

    filter: all / demon(妖股) / limit_up(有涨停) / vol_anomaly(量能异常)
    仅返回未过期行 (与训练候选口径一致), 供前端弹窗列表展示与选为训练标的。
    """
    from .services.stock_pool import pool_query
    return pool_query(filter, limit, offset)


@app.post("/api/training/start")
def train_start(req: TrainStartRequest, user_id: str = Depends(require_user_id)):
    from .services.training_session import training_mgr
    # 收益曲线归零(-100%)后不可开启训练, 需先在复盘模块重置收益曲线
    if not training_mgr.equity_nav(user_id).get("can_start", True):
        raise HTTPException(400, "收益曲线已归零(-100%)，无法开启新训练，请先在复盘模块重置收益曲线")
    try:
        s = training_mgr.start(req.category, req.bars, req.code, req.initial_capital,
                               leverage=req.leverage, strategy_id=req.strategy_id,
                               user_id=user_id, period=req.period,
                               limit_up_weights=req.limit_up_weights,
                               start_date=req.start_date)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return s.summary(hide_meta=True)


@app.post("/api/training/prime")
def train_prime(req: TrainStartRequest, user_id: str = Depends(require_user_id)):
    """需求101604: 预生成一局训练数据 —— 在点击「开始训练」之前就把数据备好

    返回 {"ready": 0/1}: ready=1 表示同参数的可复用数据已就绪, 点击开始训练近乎瞬时。
    预生成不写库、不注册会话, 未被消费的数据随 TTL 自然失效 (不污染训练历史、不泄露标的)。
    """
    from .services.training_session import training_mgr
    # 收益曲线归零时开局会被拦截 → 没必要预生成
    if not training_mgr.equity_nav(user_id).get("can_start", True):
        return {"ready": 0, "reason": "equity_reset"}
    # 池内候选不足 → 后台扩容 (不阻塞本次响应), 避免随机抽标退化为现场探测慢路径
    try:
        from .services import stock_pool as pool
        from .services.preload import warm_pool_async, POOL_MIN
        if len(pool.candidates(req.bars + 30, only_intraday=True)) < POOL_MIN:
            warm_pool_async(POOL_MIN)
    except Exception:
        pass
    try:
        s = training_mgr.prime(req.category, req.bars, req.code, req.initial_capital,
                               leverage=req.leverage, strategy_id=req.strategy_id,
                               user_id=user_id, period=req.period,
                               limit_up_weights=req.limit_up_weights,
                               start_date=req.start_date)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ready": 1 if s else 0}


@app.get("/api/training/session/{session_id}")
def train_session(session_id: str):
    from .services.training_session import training_mgr
    s = training_mgr.get(session_id)
    if s:
        return s.summary(hide_meta=True)
    # 已结算的会话: 回落到 DB 记录 (揭晓复盘用)
    d = training_mgr.get_history(session_id)
    if not d: raise HTTPException(404, "会话不存在")
    return d


@app.get("/api/training/session/{session_id}/pre-intraday")
def train_pre_intraday(session_id: str, idx: int = 0):
    """市场背景中指定索引 K 线的当日分时 (双盲时掩码日期)"""
    from .services.training_session import training_mgr
    s = training_mgr.get(session_id)
    if s:
        try:
            return s.prebar_intraday(idx)
        except ValueError as e:
            raise HTTPException(400, str(e))
    # 已结算/已归档会话内存已释放: 从 DB 回看数据重建 (复盘分时仍可查看)
    try:
        d = training_mgr.prebar_intraday_replay(session_id, idx)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if d is None: raise HTTPException(404, "会话不存在")
    return d


@app.get("/api/training/session/{session_id}/bar-intraday")
def train_bar_intraday(session_id: str, idx: int = 0):
    """训练窗口中指定索引 K 线的当日分时 (同花顺式, 双盲时掩码日期)"""
    from .services.training_session import training_mgr
    s = training_mgr.get(session_id)
    if s:
        try:
            return s.bar_intraday(idx)
        except ValueError as e:
            raise HTTPException(400, str(e))
    # 已结算/已归档会话内存已释放: 从 DB 回看数据重建 (复盘分时仍可查看)
    try:
        d = training_mgr.bar_intraday_replay(session_id, idx)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if d is None: raise HTTPException(404, "会话不存在")
    return d


@app.post("/api/training/{session_id}/buy")
def train_buy(session_id: str, req: TrainActionRequest):
    from .services.training_session import training_mgr
    try:
        s = training_mgr.buy(session_id, req.qty, req.ratio)
    except (ValueError, AssertionError) as e:
        raise HTTPException(400, str(e) or "会话不存在或已结束")
    return s.summary(hide_meta=True)


@app.post("/api/training/{session_id}/sell")
def train_sell(session_id: str, req: TrainActionRequest):
    from .services.training_session import training_mgr
    try:
        s = training_mgr.sell(session_id, req.qty, req.allow_short)
    except (ValueError, AssertionError) as e:
        raise HTTPException(400, str(e) or "会话不存在或已结束")
    return s.summary(hide_meta=True)


@app.post("/api/training/{session_id}/hold")
def train_hold(session_id: str, advance: bool = Query(False)):
    """观望; advance=true 时合并为「观望并立即推进一根」, 一次请求完成 (性能优化101701)"""
    from .services.training_session import training_mgr
    try:
        if advance:
            return training_mgr.hold_and_advance(session_id)
        s = training_mgr.hold(session_id)
    except (ValueError, AssertionError) as e:
        raise HTTPException(400, str(e) or "会话不存在或已结束")
    return s.summary(hide_meta=True)


@app.post("/api/training/{session_id}/conditional")
def train_set_conditional(session_id: str, req: TrainConditionalRequest):
    """挂止盈/止损/移动止损条件单 (触发即操作: 推进时按 K 线高低价判定并自动平仓)"""
    from .services.training_session import training_mgr
    s = training_mgr.get(session_id)
    if not s:
        raise HTTPException(404, "会话不存在")
    try:
        training_mgr.set_conditional(session_id, req.kind, req.trigger_price, req.ratio,
                                     trail_pct=req.trail_pct, side=req.side)
    except (ValueError, AssertionError) as e:
        raise HTTPException(400, str(e) or "条件单设置失败")
    return s.summary(hide_meta=True)


@app.delete("/api/training/{session_id}/conditional/{cond_id}")
def train_cancel_conditional(session_id: str, cond_id: str):
    """撤销止盈/止损条件单"""
    from .services.training_session import training_mgr
    s = training_mgr.get(session_id)
    if not s:
        raise HTTPException(404, "会话不存在")
    try:
        training_mgr.cancel_conditional(session_id, cond_id)
    except (ValueError, AssertionError) as e:
        raise HTTPException(400, str(e) or "条件单撤销失败")
    return s.summary(hide_meta=True)


@app.post("/api/training/{session_id}/advance")
def train_advance(session_id: str, steps: int = Query(1, ge=1, le=10)):
    """推进 K 线; steps=N 表示连续观望 N 个交易日 (批量推进)"""
    from .services.training_session import training_mgr
    s = training_mgr.get(session_id)
    if not s:
        raise HTTPException(404, "会话不存在")
    first_fill = None
    result = None
    try:
        for _ in range(steps):
            result = training_mgr.advance(session_id)
            # 买入委托在推进后首根成交 → 保留首次成交结果 (批量推进时前端据此补买点)
            if first_fill is None and result.get("pending_filled"):
                first_fill = result["pending_filled"]
            if result.get("auto_settled") or result["session"].get("status") != "active":
                break
    except (ValueError, AssertionError) as e:
        raise HTTPException(400, str(e) or "会话不存在或已结束")
    if result is None:      # 兜底 (steps>=1 时不会走到)
        result = {"session": s.summary(), "auto_settled": False}
    result["pending_filled"] = first_fill
    return result


@app.post("/api/training/{session_id}/period")
def train_switch_period(session_id: str, req: TrainPeriodRequest):
    """训练运行中切换周期 (日/周/月/年), 不重置 K 线推进进度"""
    from .services.training_session import training_mgr
    try:
        return training_mgr.switch_period(session_id, req.period)
    except (ValueError, AssertionError) as e:
        raise HTTPException(400, str(e) or "会话不存在或已结束")


@app.post("/api/training/{session_id}/settle")
def train_settle(session_id: str):
    from .services.training_session import training_mgr
    try:
        return training_mgr.manual_settle(session_id)
    except (ValueError, AssertionError) as e:
        raise HTTPException(400, str(e) or "会话不存在或已结束")


@app.post("/api/training/{session_id}/abandon")
def train_abandon(session_id: str):
    from .services.training_session import training_mgr
    s = training_mgr.abort(session_id)
    return s.summary() if s else {"success": True}


@app.get("/api/training/history")
def train_history(limit: int = Query(50, ge=1, le=500), user_id: str = Depends(get_current_user_id)):
    from .services.training_session import training_mgr
    if user_id == "default":            # 未登录: 训练记录绑定账号, 不展示
        return {"sessions": []}
    return {"sessions": training_mgr.list_history(limit, user_id=user_id)}


@app.get("/api/training/history/{session_id}")
def train_history_detail(session_id: str, user_id: str = Depends(get_current_user_id)):
    from .services.training_session import training_mgr
    if user_id == "default":            # 未登录: 不展示任何复盘记录
        raise HTTPException(404, "训练记录不存在")
    d = training_mgr.get_history(session_id)
    if not d: raise HTTPException(404, "训练记录不存在")
    # 进行中的会话尚无结算快照 → 从内存会话补齐前置K线, 保证复盘也能完整展示
    if not d.get("pre_bars"):
        s = training_mgr.get(session_id)
        if s and s.pre_bars:
            d["pre_bars"] = list(s.pre_bars)
            d["pre_count"] = len(s.pre_bars)
    return d


@app.post("/api/training/{session_id}/annos")
def train_save_annos(session_id: str, req: TrainAnnosRequest,
                     user_id: str = Depends(get_current_user_id)):
    """保存训练划线 (需求3): 按会话整体覆盖, 供复盘历史对局同步展示"""
    from .services.training_session import training_mgr
    if user_id == "default":
        raise HTTPException(401, "请先登录")
    n = training_mgr.save_annos(session_id, req.annos)
    return {"saved": n}


@app.get("/api/training/{session_id}/annos")
def train_get_annos(session_id: str, user_id: str = Depends(get_current_user_id)):
    from .services.training_session import training_mgr
    if user_id == "default":
        raise HTTPException(401, "请先登录")
    return {"annos": training_mgr.get_annos(session_id)}


# ========== 战法库 ==========

@app.get("/api/strategies")
def strategy_list():
    from .services.training_session import training_mgr
    return {"strategies": training_mgr.list_strategies()}


@app.post("/api/strategies")
def strategy_create(req: StrategyRequest):
    from .services.training_session import training_mgr
    try:
        return training_mgr.create_strategy(req.name, req.description, req.color)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/strategies/{strategy_id}")
def strategy_delete(strategy_id: int):
    from .services.training_session import training_mgr
    return {"success": training_mgr.delete_strategy(strategy_id)}


@app.get("/api/strategies/stats")
def strategy_stats(user_id: str = Depends(get_current_user_id)):
    from .services.training_session import training_mgr
    if user_id == "default":            # 未登录: 战法统计绑定账号, 不展示
        return {"stats": []}
    return {"stats": training_mgr.strategy_stats(user_id=user_id)}


@app.get("/api/review/equity")
def review_equity(user_id: str = Depends(get_current_user_id)):
    """复盘收益曲线: 累计净值(基准100) + 重置次数 + 是否可开启训练"""
    from .services.training_session import training_mgr
    if user_id == "default":            # 未登录: 收益曲线绑定账号, 返回空曲线
        return {"nav": 100.0, "curve": [], "can_start": True,
                "reset_count": 0, "last_reset_at": None}
    return training_mgr.equity_nav(user_id)


@app.post("/api/review/equity/reset")
def review_equity_reset(user_id: str = Depends(require_user_id)):
    """重置收益曲线起点(保留全部训练记录), 累加并返回重置次数"""
    from .services.training_session import training_mgr
    return training_mgr.reset_equity(user_id)


@app.get("/api/watch/panorama")
def watch_panorama():
    """真实看盘全景: 优先接入同花顺 iFinD (逐板块容错), 失败回落占位快照"""
    from .services.watch import real_panorama, mock_panorama
    try:
        return real_panorama()
    except Exception:                     # 兜底: 任何异常都返回完整占位结构, 保证接口不 5xx
        return mock_panorama()


# 指数分时缩略图缓存: code → (时间戳, 序列|None), 300s 内复用 (指数分时本身缓存 15 分钟)
_INDEX_SPARK_CACHE: dict = {}
_INDEX_SPARK_TTL = 300.0
_INDEX_SPARK_MAX = 8


@app.get("/api/watch/index-sparklines")
def watch_index_sparklines(codes: str = Query("", max_length=400)):
    """需求101805-0: 看盘指数条当日分时缩略图

    返回 ``{"items": {code: [价格序列...]}}`` (等距下采样至 ≤48 点);
    取不到分时数据的指数不出现在 items 中 (前端回落仅展示涨跌幅文本)。
    """
    want = [c.strip() for c in (codes or "").split(",") if c.strip()][:_INDEX_SPARK_MAX]
    if not want:
        return {"items": {}}
    from .services.index_feed import index_intraday
    now = time.time()
    out: dict = {}
    for code in want:
        hit = _INDEX_SPARK_CACHE.get(code)
        if hit and now - hit[0] < _INDEX_SPARK_TTL:
            if hit[1]:
                out[code] = hit[1]
            continue
        series = None
        try:
            data = index_intraday(code) or {}
            pts = [p.get("price") for p in (data.get("points") or [])
                   if p.get("price") is not None]
            if len(pts) >= 5:
                step = max(1, len(pts) // 48)
                series = [round(float(p), 2) for p in pts[::step][:48]]
        except Exception:                 # noqa: BLE001 - 缩略图非关键路径
            series = None
        _INDEX_SPARK_CACHE[code] = (now, series)
        if series:
            out[code] = series
    return {"items": out}


@app.get("/api/watch/list")
def watch_list(key: str = Query("", max_length=40), limit: int = Query(30, ge=1, le=60)):
    """需求101807-0: 盘面榜单卡片「查看更多」—— 返回更长的板块/个股榜单

    ``key`` 为盘面 section 键 (hot_boards / gain_stocks / ... / zhaban);
    实时取数失败时回落全景缓存中的同名短榜 (附 error 说明), 保证弹层不空白。
    """
    from .services.watch import section_list
    try:
        return section_list(key, limit)
    except Exception as e:                # noqa: BLE001 - 兜底: 不 5xx
        return {"key": key, "limit": limit, "kind": "", "items": [], "mode": "",
                "error": str(e)}


@app.get("/api/preload")
def trigger_preload(force: bool = False):
    """需求101602: 触发后端预加载 —— 后台预热各重接口缓存, 立即返回进度

    幂等: 预热进行中或已完成过则直接返回当前快照; force=true 时强制重新预热。
    """
    from .services.preload import warm_async
    return warm_async(feed, registry, force=force)


@app.get("/api/review/attribution")
def review_attribution(strategy_id: Optional[int] = None,
                       category: Optional[str] = None,
                       limit: int = Query(200, ge=1, le=500),
                       user_id: str = Depends(get_current_user_id)):
    """模式归因: 清洗筛选训练记录, 输出因子/纪律/手法/仓位完整交易模式"""
    from .services.attribution import analyze
    try:
        # 未登录: 用不存在的隔离 user_id 得到空报告 (复用现有 graceful 结构)
        return analyze(user_id=user_id if user_id != "default" else "__guest__",
                       strategy_id=strategy_id, category=category, limit=limit)
    except Exception as e:
        raise HTTPException(500, f"归因分析失败: {e}")


@app.patch("/api/training/history/{session_id}")
def train_session_meta(session_id: str, req: SessionMetaRequest):
    """复盘归档: 设置战法归类 / 复盘笔记"""
    from .services.training_session import training_mgr
    ok = False
    if req.strategy_id is not None:
        ok = training_mgr.set_session_strategy(session_id, req.strategy_id) or ok
    if req.note is not None:
        ok = training_mgr.set_note(session_id, req.note) or ok
    if not ok: raise HTTPException(404, "训练记录不存在")
    return training_mgr.get_history(session_id)


# ========== 费率工具 ==========

@app.get("/api/utils/fee")
def calc_fee(amount: float, side: Literal["BUY", "SELL"]):
    from .services.fee import FeeCalculator
    fee = FeeCalculator.calculate(amount, OrderSide(side.upper()))
    return {"amount": amount, "side": side.upper(), "fee": fee}


# ========== 兼容直接运行 `python app/main.py` ==========
# Python 直接把本文件当脚本跑时, `from .xxx import` 相对导入会炸。
# 这里追加 absolute import fallback + __main__ uvicorn 启动。
if __package__ in (None, ""):
    import sys as _sys, os as _os
    _parent = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    if _parent not in _sys.path:
        _sys.path.insert(0, _parent)
    from app.models import Order, OrderSide, OrderType, OrderStatus, Stock  # noqa: F401
    from app.services.fee import FeeCalculator  # noqa: F401
    print("💡 提示: 直接运行 main.py 属开发调试用法, 推荐根目录 `python run.py` 一键启动")
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8010, reload=False)
