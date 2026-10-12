"""K线训练会话状态机 — 核心闭环

生命周期: start → (advance + decision)*N → settle

状态字段:
  session_id, status (active/settled/abandoned), category (stock/etf)
  code, code_revealed (0/1), start_date, end_date, bars_total, bars_used
  initial_capital, cash, pos, avg_cost
  pnl, pnl_pct, trades_count, win_count
"""
from __future__ import annotations
import json
import sqlite3
import threading
import uuid
from datetime import datetime, date
from pathlib import Path
from typing import Optional, List, Dict, Any

from ..core.config import settings
from . import stock_pool as _pool


# ---------- K 线适配器 ----------
# 不同品类 akshare 接口名/字段不同, 这里统一成 List[{date,open,high,low,close,volume}]

def _norm_en(df) -> List[dict]:
    """把 akshare 英文列名 DataFrame (date/open/high/low/close/volume) 归一化为标准 K 线"""
    out = []
    for _, r in df.iterrows():
        bar = {"date": str(r["date"])[:10], "open": float(r["open"]), "high": float(r["high"]),
               "low": float(r["low"]), "close": float(r["close"]), "volume": int(float(r["volume"]))}
        if "amount" in r.index and r["amount"] is not None:
            try:
                bar["amount"] = float(r["amount"])
            except (TypeError, ValueError):
                pass
        out.append(bar)
    return out


def _fetch_stock_kline(code: str) -> List[dict]:
    """A股日 K (前复权, 新浪: volume=股, 含真实 amount=元)"""
    import akshare as ak
    prefix = "sh" if code.startswith(("6", "9")) else "sz"
    return _norm_en(ak.stock_zh_a_daily(symbol=f"{prefix}{code}", adjust="qfq"))


# 品类随机标的池 (start 时未指定 code 时使用) — 仅 A 股, 由注册表随机
CATEGORY_POOLS = {}

# 品类默认杠杆
CATEGORY_LEVERAGE = {}

# 品类展示名
CATEGORY_LABELS = {
    "stock": "股票双盲", "long": "长周期训练",
}

# 训练品类 → 数据源 (A 股日 K: 股票双盲 + 长周期 共用)
CATEGORY_FETCHERS = {
    "stock": _fetch_stock_kline,
    "long": _fetch_stock_kline,           # 单标的长周期 (双盲), 仅放宽单局根数上限
}

# 单局 K 线根数上限 (长周期训练放宽到 1250 根, 其余沿用 250)
CATEGORY_BARS_CAP = {"stock": 250, "long": 1250}

# 品类默认单局根数 (前端滑块初始值)
CATEGORY_DEFAULT_BARS = {"stock": 250, "long": 1250}


def _augment_amount(klines: List[dict], category: str = "stock") -> List[dict]:
    """为 K 线补齐「成交额」字段: 优先用数据源真实 amount, 缺失时估算
    amount_est = 均价 × 成交量
    """
    for k in klines:
        if k.get("amount") is None:
            avg = (k["open"] + k["high"] + k["low"] + k["close"]) / 4.0
            k["amount"] = round(avg * k["volume"], 2)
    return klines


def build_intraday(code: str, bar: dict, prev_close: Optional[float],
                   label: str, reveal: bool) -> dict:
    """从单根 K 线生成当日分时 (真实 1 分钟数据优先, 不可得则空数据样式)

    抽为模块函数: 训练会话在内存时由 TrainingSession._intraday 调用; 已结算/已归档
    的会话内存已释放, 由 main 层从 DB 历史 K 线复用本函数, 保证复盘分时仍可查看。
    reveal=False (双盲未揭晓) 时用 label 掩码真实日期。
    """
    from .intraday import generate_intraday_real

    def _empty(suspended: bool) -> dict:
        d = {
            "date": bar["date"], "prev_close": prev_close,
            "open": bar["open"], "high": bar["high"], "low": bar["low"], "close": bar["close"],
            "amplitude": round((bar["high"] - bar["low"]) / prev_close * 100, 2) if prev_close else None,
            "suspended": suspended, "source": "empty",
            "points": [], "empty": True,
            "hint": "该日无真实分钟数据, 仅展示收盘信息",
        }
        if not reveal:
            d["date"] = label
        return d

    if not bar.get("volume"):
        return _empty(True)
    real = None
    try:
        real = generate_intraday_real(code, bar["date"], bar["open"], bar["high"], bar["low"],
                                     bar["close"], bar["volume"], prev_close)
    except Exception:
        real = None
    if not real:
        return _empty(False)
    if not reveal:
        real["date"] = label
    return real


# ---------- 周期聚合 ----------

def _period_key(b, period: str):
    """抽取 K 线所属周期桶 key: week=(ISO年,周) / month=(年,月) / year=(年,) / day=None"""
    from datetime import datetime
    d = b.get("date")
    if isinstance(d, str):
        dt = datetime.strptime(d[:10], "%Y-%m-%d")
    else:
        dt = d
    if period == "week":
        iso = dt.isocalendar()
        return (iso[0], iso[1])
    if period == "month":
        return (dt.year, dt.month)
    if period == "year":
        return (dt.year,)
    return None


def aggregate_bars(bars: List[dict], period: str = "day") -> tuple:
    """按周期聚合 dict K线, 返回 (聚合K线, 每根覆盖的日K数量)

    - period=day 原样返回
    - 聚合桶的 date 取该桶最后一根日K的日期 (保证分时联动有确定日期)
    - counts 用于周期切换时进度(已覆盖日K)映射
    """
    if period == "day" or not bars:
        return list(bars), [1] * len(bars)
    groups, order, key_of, counts = {}, [], {}, []
    for b in bars:
        key = _period_key(b, period)
        idx = key_of.get(key)
        if idx is None:
            key_of[key] = len(order)
            order.append(key)
            groups[key] = []
            counts.append(0)
        groups[key].append(b)
        counts[key_of[key]] += 1
    out = []
    for key in order:
        g = groups[key]
        out.append({
            "date": g[-1]["date"],
            "open": g[0]["open"],
            "high": max(x["high"] for x in g),
            "low": min(x["low"] for x in g),
            "close": g[-1]["close"],
            "volume": sum(x["volume"] for x in g),
            "amount": sum(x.get("amount") or 0 for x in g),
        })
    return out, counts


def bucket_ranges(bars: List[dict], period: str = "day") -> List[tuple]:
    """把日K序列按周期切分为区间列表 [(start, end), ...] (半开区间, end 不含)

    用于「进度按交易日映射」: 周期K线的第 i 根覆盖 bars[start:end] 这段日K。
    """
    if not bars:
        return []
    if period == "day":
        return [(i, i + 1) for i in range(len(bars))]
    ranges, start = [], 0
    for i in range(1, len(bars)):
        if _period_key(bars[i], period) != _period_key(bars[i - 1], period):
            ranges.append((start, i))
            start = i
    ranges.append((start, len(bars)))
    return ranges


# ---------- MACD (与前端 indicators.js 同口径, 供「日线算 + 周期桶采样」使用) ----------

def _ema_js(vals: List[Optional[float]], n: int) -> List[Optional[float]]:
    """EMA, 与前端 indicators.js ema() 完全一致 (前 n-1 个为 None)"""
    k = 2.0 / (n + 1)
    out: List[Optional[float]] = [None] * len(vals)
    prev = None
    for i, v in enumerate(vals):
        x = 0.0 if v is None else v
        prev = x if prev is None else x * k + prev * (1 - k)
        out[i] = prev if i >= n - 1 else None
    return out


def _macd_js(closes: List[Optional[float]], fast: int = 12, slow: int = 26,
             signal: int = 9) -> tuple:
    """MACD(12,26,9): DIF=EMA12-EMA26, DEA=EMA(DIF,9), 柱=(DIF-DEA)*2"""
    ef = _ema_js(closes, fast)
    es = _ema_js(closes, slow)
    dif = [(ef[i] - es[i]) if (ef[i] is not None and es[i] is not None) else None
           for i in range(len(closes))]
    dea = _ema_js(dif, signal)
    hist = [((dif[i] - dea[i]) * 2) if (dif[i] is not None and dea[i] is not None) else None
            for i in range(len(closes))]
    return dif, dea, hist



# ---------- 训练记录状态机 ----------
# active ──结算/推到末根──> settled
# active ──权益归零──────> blown_up
# active ──用户放弃──────> abandoned
# settled / blown_up / abandoned 为终态, 不接受任何决策
STATUS_ACTIVE = "active"
STATUS_SETTLED = "settled"
STATUS_BLOWN_UP = "blown_up"
STATUS_ABANDONED = "abandoned"
TERMINAL_STATUSES = (STATUS_SETTLED, STATUS_BLOWN_UP, STATUS_ABANDONED)
STATUS_TRANSITIONS = {
    STATUS_ACTIVE: (STATUS_SETTLED, STATUS_BLOWN_UP, STATUS_ABANDONED),
    STATUS_SETTLED: (),
    STATUS_BLOWN_UP: (),
    STATUS_ABANDONED: (),
}
STATUS_LABELS = {
    STATUS_ACTIVE: "进行中",
    STATUS_SETTLED: "已结算",
    STATUS_BLOWN_UP: "已爆仓",
    STATUS_ABANDONED: "已放弃",
}


# ---------- 会话类 ----------
class TrainingSession:
    def __init__(self, session_id: str, category: str, code: str,
                 bars: List[dict], initial_capital: float = 100_000.0,
                 leverage: float = 1.0, strategy_id: Optional[int] = None,
                 pre_bars: Optional[List[dict]] = None, user_id: str = "default",
                 period: str = "day", code_name: str = "", user_specified: bool = False,
                 limit_up_count: int = 0, limit_up_bucket: str = ""):
        self.session_id = session_id
        self.user_id = user_id
        self.status = STATUS_ACTIVE
        self.category = category
        self.code = code
        self.code_name = code_name or ""
        # 标的特性: 近期(近250根日K)涨停次数与分档 —— 双盲下「特性」不泄露「身份」
        self.limit_up_count = int(limit_up_count or 0)
        self.limit_up_bucket = limit_up_bucket or ""
        self.user_specified = bool(user_specified)   # 用户指定标的: 训练中可展示名称
        self.code_revealed = 0
        self.period = period if period in ("day", "week", "month", "year") else "day"
        # 原始日K (训练窗口) —— 进度与聚合的唯一基准
        self._daily = list(bars)
        self.days_total = len(self._daily)
        self.days_used = 0                 # 已推进(消费)的交易日数, 周期切换时进度以此为准
        self._p_ranges: List[tuple] = []   # 当前周期每根K线覆盖的日K区间 [(start,end), ...]
        self.bars_total = 0
        self.bars_used = 0                 # 已决策的「周期K线」根数 (当前周期下)
        self._recompute_progress()
        self.pre_bars = pre_bars or []   # 训练窗口之前的历史K线 (市场背景, 日K)
        self._daily_pre = list(pre_bars or [])
        self.start_date = self._daily[0]["date"] if self._daily else None
        self.end_date = self._daily[-1]["date"] if self._daily else None
        self.leverage = max(1.0, float(leverage or 1.0))
        self.strategy_id = strategy_id   # 关联战法 (复盘归类用)
        self.initial_capital = initial_capital
        self.cash = initial_capital
        self.pos = 0              # 当前持仓 (股数, 正=多, 负=空)
        self.avg_cost = 0.0       # 持仓均价
        self.trades_count = 0     # 累计买卖次数 (平仓才算完成一笔)
        self.round_trips = 0      # 已完成的完整交易对 (开+平)
        self.win_count = 0
        self._round_pnl = 0.0     # 平仓盈亏累加
        self.equity_curve = [initial_capital]   # 权益曲线 (仅已推进的 K 线, 用于最大回撤)
        self.last_action = "HOLD"   # 当前 bar 的决策动作 (advance 时落库)
        # 条件单 (止盈/止损): 触发即操作 —— 推进时按 K 线高低价判定, 自动平仓
        self.conditionals: List[dict] = []
        self._cond_seq = 0

    # ---------- 周期视图 (进度按交易日映射, 不提前展示未走到的行情) ----------
    def _recompute_progress(self):
        """按当前周期重算每根K线覆盖的日K区间, 以及「已走完的周期根数」"""
        self._p_ranges = bucket_ranges(self._daily, self.period)
        self.bars_total = len(self._p_ranges)
        self.bars_used = sum(1 for _s, e in self._p_ranges if e <= self.days_used)

    def _full_bar(self, s0: int, e0: int) -> dict:
        """把 _daily[s0:e0] 聚合为一根周期K线"""
        g = self._daily[s0:e0]
        return {
            "date": g[-1]["date"],
            "open": g[0]["open"],
            "high": max(x["high"] for x in g),
            "low": min(x["low"] for x in g),
            "close": g[-1]["close"],
            "volume": sum(x["volume"] for x in g),
            "amount": sum(x.get("amount") or 0 for x in g),
        }

    def _visible_bars(self) -> List[dict]:
        """可见K线 = 已完整走完的周期 + 当前「部分」周期

        当前周期K线只聚合「已走过 + 当前交易日」的日K, 绝不提前用未来的交易日聚合,
        否则切到周线会立刻显示一整周的K线 (信息泄漏)。
        """
        out: List[dict] = []
        for s0, e0 in self._p_ranges:
            if e0 <= self.days_used:
                out.append(self._full_bar(s0, e0))
            else:
                cut = min(e0, self.days_used + 1)
                if cut > s0:
                    out.append(self._full_bar(s0, cut))
                break
        return out

    def _all_bars(self) -> List[dict]:
        """完整周期的全部K线 (仅结算揭晓后展示)"""
        return [self._full_bar(s0, e0) for s0, e0 in self._p_ranges]

    def _display_bars(self) -> List[dict]:
        """当前对外展示的K线: 揭晓后为完整K线, 否则为「已走完 + 当前部分周期」

        分时接口(bar_intraday)必须与 summary() 使用同一序列, 否则爆仓/放弃等
        「已揭晓但未推进到末根」的会话会出现索引越界。
        """
        return self._all_bars() if self.code_revealed else self._visible_bars()

    @property
    def bars(self) -> List[dict]:
        """当前周期的可见K线序列"""
        return self._visible_bars()

    def period_macd(self, fast: int = 12, slow: int = 26, signal: int = 9) -> dict:
        """周期视图 MACD: 在「日线」上算 MACD, 再按周期桶取桶末值采样

        周/月/年线若直接对聚合序列算 MACD, 周期根数远少于 EMA(26)+DEA(9) 的预热需求,
        会导致大量 null (年线几乎全空)。这里改为在完整日线上计算, 再按与 bars 一致的
        周期切片采样, 保证与日线同口径且无空洞。

        返回与前端合并序列 (pre_bars + bars) 一一对齐的 {dif, dea, hist}。
        """
        pre_daily = self._daily_pre or []
        closes = [b.get("close") for b in pre_daily] + [b.get("close") for b in (self._daily or [])]
        if not closes:
            return {"dif": [], "dea": [], "hist": []}
        dif, dea, hist = _macd_js(closes, fast, slow, signal)
        pre_off = len(pre_daily)

        def sample(daily_idx: int):
            li = pre_off + daily_idx
            if 0 <= li < len(closes):
                return dif[li], dea[li], hist[li]
            return None, None, None

        out = {"dif": [], "dea": [], "hist": []}

        def push(d, e, h):
            out["dif"].append(d)
            out["dea"].append(e)
            out["hist"].append(h)

        # 1) 前置背景段 (与 pre_bars 同一套周期分桶)
        for _s0, e0 in bucket_ranges(pre_daily, self.period):
            push(*sample(e0 - 1))
        # 2) 训练段: 与 _display_bars() 完全一致的切片逻辑
        if self.code_revealed:
            for _s0, e0 in self._p_ranges:
                push(*sample(e0 - 1))
        else:
            for s0, e0 in self._p_ranges:
                if e0 <= self.days_used:
                    push(*sample(e0 - 1))
                else:
                    cut = min(e0, self.days_used + 1)
                    if cut > s0:
                        push(*sample(cut - 1))
                    break
        return out

    # 当前 bar 的 close_price (撮合价) —— 取「当前交易日」收盘价
    @property
    def current_price(self) -> Optional[float]:
        if self.days_used >= self.days_total:
            return None
        return self._daily[self.days_used]["close"]

    # 当前 bar 的 OHLC (当前周期中的部分K线)
    @property
    def current_bar(self) -> Optional[dict]:
        vis = self._visible_bars()
        if self.bars_used >= len(vis):
            return None
        return vis[self.bars_used]

    # ---------- 状态机 ----------
    def _transition(self, new_status: str):
        """按状态机流转; 非法流转直接报错 (终态不可再进入决策)"""
        if new_status not in STATUS_TRANSITIONS.get(self.status, ()):
            raise ValueError(f"非法的状态流转: {self.status} → {new_status}")
        self.status = new_status

    # 账户实时价值
    @property
    def portfolio_value(self) -> float:
        p = self.current_price
        if p is None:
            p = self._daily[-1]["close"] if self._daily else 0.0
        return self._equity(p)

    def _equity(self, price: float) -> float:
        """给定价格下的账户权益

        - 多头: 现金 + 持仓市值
        - 空头: 现金 + 锁定保证金(|持仓|×开仓均价) + 浮动盈亏(开仓价-现价)×|持仓|
          (开空时现金已扣除保证金, 故此处需把保证金计回)
        """
        if self.pos >= 0:
            return self.cash + self.pos * price
        qty = -self.pos
        margin = qty * self.avg_cost
        float_pnl = (self.avg_cost - price) * qty
        return self.cash + margin + float_pnl

    @property
    def pnl(self) -> float:
        return self.portfolio_value - self.initial_capital

    @property
    def pnl_pct(self) -> float:
        return self.pnl / self.initial_capital * 100

    @property
    def has_position(self) -> bool:
        return self.pos != 0

    @property
    def max_drawdown(self) -> float:
        """最大回撤 (%) — 基于权益曲线的峰值回落"""
        peak = float("-inf")
        mdd = 0.0
        for v in self.equity_curve:
            peak = max(peak, v)
            if peak > 0:
                mdd = max(mdd, (peak - v) / peak)
        return round(mdd * 100, 2)

    @property
    def blown_up(self) -> bool:
        """是否爆仓 (权益归零或为负)"""
        return self.portfolio_value <= 0

    def _fee(self, amount: float) -> float:
        """简化手续费: 每笔 0.03% (佣金+印花税+过户费合并)"""
        return max(amount * 0.0003, 1.0)

    def _buying_power(self) -> float:
        """可用买入力 = 现金 + 杠杆放大额度 (leverage=1 时等于现金, 保持原有行为)"""
        return self.cash + self.initial_capital * (self.leverage - 1.0)

    # ---------- 决策 ----------
    def buy(self, qty: Optional[int] = None, ratio: Optional[float] = None) -> dict:
        """开多 / 平空 (即时成交 @ 当前 close)

        - qty 指定时按股数买入
        - qty 为空且 ratio 给定时按「买入力 × ratio」换算手数 (仓位选择)
        - 都不传 = 满仓买入
        """
        if self.status != "active": raise ValueError("会话已结束")
        price = self.current_price
        if price is None: raise ValueError("无可操作 K 线")
        if self.pos < 0:
            # 平空: 返还锁定保证金 + 结算盈亏 (盈亏 = (开仓价-平仓价) × 数量)
            close_qty = min(qty or -self.pos, -self.pos)
            entry = self.avg_cost                     # 空头均价 = 开仓价 (正数)
            pnl = (entry - price) * close_qty
            self.cash += close_qty * entry + pnl - self._fee(close_qty * price)
            self.pos += close_qty
            self._round_pnl += pnl
            self.round_trips += 1
            self.win_count += 1 if pnl > 0 else 0
            if self.pos == 0: self.avg_cost = 0
            self.trades_count += 1
            return {"action": "COVER", "qty": close_qty, "price": price, "pnl": pnl}
        # 开多 (仓位锚定账户总资产: 目标市值 = 总资产 × 杠杆 × 仓位比例, 上限为可用买入力)
        bp = self._buying_power()
        if qty is None:
            ratio_val = ratio if ratio is not None else 1.0
            target_value = self.portfolio_value * self.leverage * ratio_val
            amount = min(target_value, bp)
            qty = max(1, int(amount / price / 100) * 100 or int(amount / price))
        if qty <= 0: raise ValueError("数量必须 > 0")
        cost = qty * price + self._fee(qty * price)
        if cost > bp: qty = max(1, int(bp / (price * 1.0003)))
        if qty <= 0: raise ValueError("资金不足")
        self.cash -= qty * price + self._fee(qty * price)
        if self.pos > 0:
            # 加仓: 加权均价
            self.avg_cost = (self.avg_cost * self.pos + price * qty) / (self.pos + qty)
        else:
            self.avg_cost = price
        self.pos += qty
        self.trades_count += 1
        return {"action": "BUY", "qty": qty, "price": price}

    def sell(self, qty: Optional[int] = None, allow_short: bool = False) -> dict:
        """平多 / 开空 (即时成交 @ 当前 close)"""
        if self.status != "active": raise ValueError("会话已结束")
        price = self.current_price
        if price is None: raise ValueError("无可操作 K 线")
        if self.pos > 0:
            close_qty = min(qty or self.pos, self.pos)
            self.cash += close_qty * price - self._fee(close_qty * price)
            self.pos -= close_qty
            pnl = (price - self.avg_cost) * close_qty
            self._round_pnl += pnl
            self.round_trips += 1
            self.win_count += 1 if pnl > 0 else 0
            if self.pos == 0: self.avg_cost = 0
            self.trades_count += 1
            return {"action": "SELL", "qty": close_qty, "price": price, "pnl": pnl}
        if self.pos < 0:
            # 已有空仓 → 加仓空 (追加保证金)
            if not allow_short: raise ValueError("已有空仓, 开空需 allow_short=True")
            bp = self._buying_power()
            if qty is None: qty = int(bp / price / 100) * 100 or int(bp / price)
            if qty <= 0: raise ValueError("保证金不足")
            old_qty = -self.pos
            if qty * price > bp: qty = int(bp / (price * 1.0003) / 100) * 100
            if qty <= 0: raise ValueError("保证金不足")
            self.cash -= qty * price + self._fee(qty * price)         # 追加保证金
            self.avg_cost = (self.avg_cost * old_qty + price * qty) / (old_qty + qty)
            self.pos -= qty
            self.trades_count += 1
            return {"action": "SHORT_ADD", "qty": qty, "price": price}
        # 空仓 → 开空 (保证金模式: 现金扣除保证金)
        if not allow_short: raise ValueError("当前无空仓, 需 allow_short=True 开空")
        bp = self._buying_power()
        if qty is None: qty = int(bp / price / 100) * 100
        if qty <= 0: raise ValueError("数量必须 > 0")
        if qty * price > bp: qty = int(bp / (price * 1.0003) / 100) * 100
        if qty <= 0: raise ValueError("保证金不足")
        self.cash -= qty * price + self._fee(qty * price)
        self.avg_cost = price                    # 空头均价 = 开仓价 (正数, 供 _equity/平空使用)
        self.pos -= qty
        self.trades_count += 1
        return {"action": "SHORT", "qty": qty, "price": price}

    def hold(self) -> dict:
        """观望: 直接推进 (终态会话不接受任何决策)"""
        if self.status != STATUS_ACTIVE: raise ValueError("会话已结束")
        return {"action": "HOLD", "qty": 0, "price": self.current_price}

    # ---------- 条件单 (止盈 / 止损 / 移动止损, 触发即操作) ----------
    def set_conditional(self, kind: str, trigger_price: Optional[float] = None, ratio: float = 1.0,
                        trail_pct: Optional[float] = None, side: Optional[str] = None) -> dict:
        """挂出止盈/止损/移动止损条件单, 推进时按 K 线高低价判定并尝试平仓

        - 不再要求先有持仓: 有持仓时按持仓方向校验触发价; 无持仓时由「类型 + 触发价相对现价」推断方向
        - 触发时若无可操作持仓, 不做操作并给出提示 (见 _check_conditionals)
        - ratio: 触发后平仓的持仓比例 (1.0 = 全平)
        - trailing_stop (移动止损): 不设固定触发价, 而是给「回撤幅度 trail_pct(%)」,
          止损位随最高价(多)/最低价(空)动态上移(下移), 自最新价回撤 trail_pct% 即触发
        """
        if self.status != STATUS_ACTIVE: raise ValueError("会话已结束")
        if kind not in ("take_profit", "stop_loss", "trailing_stop"):
            raise ValueError("kind 必须为 take_profit / stop_loss / trailing_stop")
        price = self.current_price or 0.0
        if kind == "trailing_stop":
            pct = float(trail_pct or 0.0)
            if pct <= 0: raise ValueError("移动止损的回撤幅度必须大于 0")
            if side in ("long", "short"):
                cside = side
            elif self.pos > 0:
                cside = "long"
            elif self.pos < 0:
                cside = "short"
            else:
                cside = "long"            # 无持仓: 移动止损默认保护多头
            peak = price if cside == "long" else None
            trough = price if cside == "short" else None
            stop = price * (1 - pct / 100) if cside == "long" else price * (1 + pct / 100)
            self._cond_seq += 1
            vis = self.bars
            cur_date = vis[self.bars_used]["date"] if self.bars_used < len(vis) else None
            cond = {
                "id": f"c{self._cond_seq}",
                "kind": kind,
                "kind_label": "移动止损",
                "trigger_price": round(stop, 2),
                "trail_pct": round(pct, 2),
                "peak": peak,
                "trough": trough,
                "ratio": max(0.01, min(float(ratio or 1.0), 1.0)),
                "status": "active",
                "side": cside,
                "created_index": self.bars_used,
                "created_date": cur_date,
                "created_price": round(price, 2),
                "created_pos": self.pos,
                "triggered_index": None,
                "triggered_date": None,
                "triggered_price": None,
                "triggered_qty": None,
                "triggered_pnl": None,
                "message": "",
            }
            self.conditionals.append(cond)
            return dict(cond)
        tp = float(trigger_price or 0.0)
        if tp <= 0: raise ValueError("触发价必须大于 0")
        if self.pos > 0:
            side = "long"
            if kind == "take_profit" and tp <= price:
                raise ValueError(f"多头止盈价应高于现价 {price}")
            if kind == "stop_loss" and tp >= price:
                raise ValueError(f"多头止损价应低于现价 {price}")
        elif self.pos < 0:
            side = "short"
            if kind == "take_profit" and tp >= price:
                raise ValueError(f"空头止盈价应低于现价 {price}")
            if kind == "stop_loss" and tp <= price:
                raise ValueError(f"空头止损价应高于现价 {price}")
        else:
            # 无持仓: 由「类型 + 触发价相对现价」推断方向 (止盈=顺势, 止损=逆势)
            if abs(tp - price) < 1e-9:
                raise ValueError("触发价不能等于现价")
            if kind == "take_profit":
                side = "long" if tp > price else "short"
            else:
                side = "long" if tp < price else "short"
        self._cond_seq += 1
        vis = self.bars
        cur_date = vis[self.bars_used]["date"] if self.bars_used < len(vis) else None
        cond = {
            "id": f"c{self._cond_seq}",
            "kind": kind,
            "kind_label": "止盈" if kind == "take_profit" else "止损",
            "trigger_price": round(tp, 2),
            "ratio": max(0.01, min(float(ratio or 1.0), 1.0)),
            "status": "active",
            "side": side,
            "created_index": self.bars_used,
            "created_date": cur_date,
            "created_price": round(price, 2),
            "created_pos": self.pos,
            "triggered_index": None,
            "triggered_date": None,
            "triggered_price": None,
            "triggered_qty": None,
            "triggered_pnl": None,
            "message": "",
        }
        self.conditionals.append(cond)
        return dict(cond)

    def cancel_conditional(self, cond_id: str) -> dict:
        """撤销条件单 (标记 cancelled, 保留历史)"""
        for c in self.conditionals:
            if c["id"] == cond_id and c["status"] == "active":
                c["status"] = "cancelled"
                return dict(c)
        raise ValueError("条件单不存在或已失效")

    def _close_at(self, qty: int, price: float) -> Optional[dict]:
        """按指定价平仓 (条件单触发用; 不改变 last_action)"""
        qty = min(int(qty), abs(self.pos))
        if qty <= 0: return None
        if self.pos > 0:
            self.cash += qty * price - self._fee(qty * price)
            pnl = (price - self.avg_cost) * qty
            self.pos -= qty
        else:
            entry = self.avg_cost
            pnl = (entry - price) * qty
            self.cash += qty * entry + pnl - self._fee(qty * price)
            self.pos += qty
        self._round_pnl += pnl
        self.round_trips += 1
        self.win_count += 1 if pnl > 0 else 0
        self.trades_count += 1
        if self.pos == 0: self.avg_cost = 0.0
        return {"qty": qty, "price": round(price, 2), "pnl": round(pnl, 2)}

    def _check_conditionals(self, bar: dict, bar_index: int) -> tuple:
        """按刚走完的 K 线 high/low 判定条件单, 返回 (成交列表, 触发但不可操作列表)

        触发即尝试操作: 有匹配方向持仓 → 以触发价(跳空取开盘价)平仓;
        无匹配方向持仓 → 标记 skipped 并给出提示 (不再静默取消)。
        """
        fired: List[dict] = []
        skipped: List[dict] = []
        if not self.conditionals:
            return fired, skipped
        hi, lo, op = bar["high"], bar["low"], bar["open"]
        for c in self.conditionals:
            if c["status"] != "active":
                continue
            kind = c["kind"]
            hit, exec_price = False, c["trigger_price"]
            if kind == "trailing_stop":
                # 移动止损: 止损位随最高价(多)/最低价(空)动态移动, 回撤 trail_pct% 即触发
                pct = float(c.get("trail_pct") or 0.0)
                if c["side"] == "long":
                    peak = max(float(c.get("peak") or hi), hi)
                    c["peak"] = round(peak, 2)
                    stop = peak * (1 - pct / 100)
                    c["trigger_price"] = round(stop, 2)
                    if lo <= stop:
                        hit = True; exec_price = min(op, stop)   # 跳空低开 → 更差价成交
                else:
                    trough = min(float(c.get("trough") or lo), lo)
                    c["trough"] = round(trough, 2)
                    stop = trough * (1 + pct / 100)
                    c["trigger_price"] = round(stop, 2)
                    if hi >= stop:
                        hit = True; exec_price = max(op, stop)
            elif c["side"] == "long":
                tp = c["trigger_price"]
                if kind == "take_profit" and hi >= tp:
                    hit = True; exec_price = max(op, tp)   # 跳空高开 → 更优价成交
                elif kind == "stop_loss" and lo <= tp:
                    hit = True; exec_price = min(op, tp)   # 跳空低开 → 更差价成交
            else:
                tp = c["trigger_price"]
                if kind == "take_profit" and lo <= tp:
                    hit = True; exec_price = min(op, tp)
                elif kind == "stop_loss" and hi >= tp:
                    hit = True; exec_price = max(op, tp)
            if not hit:
                continue
            operable = (c["side"] == "long" and self.pos > 0) or (c["side"] == "short" and self.pos < 0)
            if not operable:
                c["status"] = "skipped"
                c["triggered_index"] = bar_index
                c["triggered_date"] = bar["date"]
                c["triggered_price"] = round(exec_price, 2)
                c["message"] = "条件单触发, 但当前无可操作持仓, 已跳过"
                skipped.append(dict(c))
                continue
            qty = max(1, int(abs(self.pos) * c["ratio"]))
            res = self._close_at(qty, exec_price)
            if not res:
                continue
            c["status"] = "triggered"
            c["triggered_index"] = bar_index
            c["triggered_date"] = bar["date"]
            c["triggered_price"] = res["price"]
            c["triggered_qty"] = res["qty"]
            c["triggered_pnl"] = res["pnl"]
            fired.append(dict(c))
        return fired, skipped

    def advance(self) -> dict:
        """推进 1 根 K 线 (当前周期), 返回 bar_index 与当前账户状态

        推进一步 = 走完当前周期K线覆盖的全部交易日 (日线=1天, 周线=当周剩余天数)。
        """
        if self.status != STATUS_ACTIVE: raise ValueError("会话已结束")
        if self.days_used >= self.days_total:
            return {"message": "已到最后一根, 请 settle 结算"}
        s0, e0 = self._p_ranges[self.bars_used]
        bar_idx = self.bars_used
        cut = min(e0, self.days_total)
        done = self._full_bar(s0, cut)          # 走完后的完整周期K线
        prev_bar = {
            "session_id": self.session_id,
            "bar_index": self.bars_used,
            "date": done["date"],
            "open": done["open"],
            "high": done["high"],
            "low": done["low"],
            "close": done["close"],
            "volume": done["volume"],
        }
        self.days_used = cut
        self._recompute_progress()
        # 权益按「刚走完那根K线」的收盘价计, 与决策价保持一致 (最大回撤才准确)
        self.equity_curve.append(self._equity(done["close"]))
        # 条件单: 按刚走完的 K 线高低价判定, 触发即尝试操作 (有持仓则平仓, 无则提示)
        triggered, skipped = self._check_conditionals(done, bar_idx)
        result = self._bar_snapshot(prev_bar)
        self.last_action = "HOLD"   # 新的一根 K 线, 决策重置为观望
        if triggered:
            result["triggered"] = triggered
            result["bar_index"] = bar_idx
            result["last_action"] = "TP" if triggered[0]["kind"] == "take_profit" else "SL"
        if skipped:
            result["conditions_skipped"] = skipped
            result["bar_index"] = bar_idx
        # 爆仓: 权益归零/为负 → 强制平仓并终结 (优先级高于走到末根)
        if self.portfolio_value <= 0:
            self._force_close()
            self._transition(STATUS_BLOWN_UP)
            self.code_revealed = 1
            self.equity_curve.append(self.portfolio_value)
            result["session"] = self.summary()
            result["settled"] = True
            result["blown_up"] = True
            return result
        # 自动 settle (推到末尾)
        if self.days_used >= self.days_total:
            self._force_close()
            self._transition(STATUS_SETTLED)
            self.code_revealed = 1
            self.equity_curve.append(self.portfolio_value)
            result["session"] = self.summary()
            result["settled"] = True
        return result

    def _bar_snapshot(self, bar: dict, action: str = "HOLD") -> dict:
        """构造 training_bars 表的一行快照"""
        return {
            **bar,
            "action": action, "action_price": bar["close"],
            "pos_before": self.pos, "cash_before": self.cash,
            "pnl_before": self.pnl,
            "pos_after": self.pos, "cash_after": self.cash,
            "pnl_after": self.pnl,
            "equity": round(self._equity(bar["close"]), 2),
        }

    def _force_close(self):
        """结算时强制平仓 (多/空全部 @ 当前交易日收盘价; 已到末尾则取最后一根)"""
        p = self.current_price
        if p is None:
            p = self._daily[-1]["close"] if self._daily else 0.0
        if self.pos > 0:
            self.cash += self.pos * p - self._fee(self.pos * p)
            pnl = (p - self.avg_cost) * self.pos
            self._round_pnl += pnl
            self.round_trips += 1
            self.win_count += 1 if pnl > 0 else 0
            self.pos = 0; self.avg_cost = 0; self.trades_count += 1
        elif self.pos < 0:
            # 平空: 返还保证金 + 结算盈亏
            qty = -self.pos
            entry = self.avg_cost
            pnl = (entry - p) * qty
            self.cash += qty * entry + pnl - self._fee(qty * p)
            self._round_pnl += pnl
            self.round_trips += 1
            self.win_count += 1 if pnl > 0 else 0
            self.pos = 0; self.avg_cost = 0; self.trades_count += 1

    def manual_settle(self):
        if self.status != STATUS_ACTIVE: return self.summary()
        self._force_close()
        # 结算后权益归零/为负 → 记为爆仓, 否则正常结算
        self._transition(STATUS_BLOWN_UP if self.portfolio_value <= 0 else STATUS_SETTLED)
        self.code_revealed = 1
        self.equity_curve.append(self.portfolio_value)
        return self.summary()

    def abort(self):
        if self.status == STATUS_ACTIVE:
            self._transition(STATUS_ABANDONED)
        self.code_revealed = 1

    def switch_period(self, period: str) -> dict:
        """运行中切换周期 (日/周/月/年), 进度不重置

        进度基准是「已走过的交易日数」days_used, 切换只改变每根K线覆盖的交易日跨度,
        当前所处的时间点不变 → 切到周线不会退回、也不会提前显示一整周的K线。
        """
        if period not in ("day", "week", "month", "year"):
            raise ValueError(f"不支持的周期 {period}")
        if self.status != STATUS_ACTIVE: raise ValueError("会话已结束")
        if period == self.period:
            return self.summary()
        self.period = period
        self._recompute_progress()
        # 市场背景同样按周期聚合展示 (合并绘制需同一周期)
        self.pre_bars = list(self._daily_pre) if period == "day" else aggregate_bars(self._daily_pre, period)[0]
        return self.summary()

    # ---------- 对外输出 ----------
    def _intraday(self, code: str, b: dict, prev_close: Optional[float], label: str) -> dict:
        """训练分时: 优先真实 1 分钟数据, 不可得则返回空数据样式 (双盲时掩码日期)"""
        return build_intraday(code, b, prev_close, label, bool(self.code_revealed))

    def prebar_intraday(self, idx: int) -> dict:
        """市场背景中第 idx 根历史K线的当日分时 (真实数据, 双盲时掩码日期)"""
        if not 0 <= idx < len(self.pre_bars):
            raise ValueError(f"背景K线索引 {idx} 越界 (0 ~ {len(self.pre_bars) - 1})")
        p = self.pre_bars[idx]
        prev_close = self.pre_bars[idx - 1]["close"] if idx > 0 else None
        return self._intraday(self.code, p, prev_close, f"#{idx + 1}")

    def bar_intraday(self, idx: int) -> dict:
        """训练窗口中第 idx 根K线的当日分时 (真实数据, 双盲时掩码日期)

        前收盘与前置背景最后一日/前一训练日连续衔接, 保证分时基准价正确
        索引基于 _display_bars() (与前端展示的K线序列一致)
        """
        disp = self._display_bars()
        if not 0 <= idx < len(disp):
            raise ValueError(f"训练K线索引 {idx} 越界 (0 ~ {len(disp) - 1})")
        b = disp[idx]
        if idx > 0:
            prev_close = disp[idx - 1]["close"]
        elif self.pre_bars:
            prev_close = self.pre_bars[-1]["close"]
        else:
            prev_close = None
        return self._intraday(self.code, b, prev_close, f"#{len(self.pre_bars) + idx + 1}")

    def summary(self, hide_meta: bool = True) -> dict:
        """会话状态快照, hide_meta=True 时隐藏 code/date (双盲)"""
        price = self.current_price or (self.bars[-1]["close"] if self.bars else 0)
        out = {
            "session_id": self.session_id,
            "status": self.status,
            "status_label": STATUS_LABELS.get(self.status, self.status),
            "terminal": self.status in TERMINAL_STATUSES,
            "category": self.category,
            "category_label": CATEGORY_LABELS.get(self.category, self.category),
            "period": self.period,
            "leverage": self.leverage,
            "strategy_id": self.strategy_id,
            "bars_total": self.bars_total,
            "bars_used": self.bars_used,
            "initial_capital": self.initial_capital,
            "cash": round(self.cash, 2),
            "pos": self.pos,
            "avg_cost": round(self.avg_cost, 2),
            "margin": round(-self.pos * self.avg_cost, 2) if self.pos < 0 else 0.0,
            "current_price": round(price, 2),
            "pos_value": round(self.pos * price, 2),
            "portfolio_value": round(self.portfolio_value, 2),
            "pnl": round(self.pnl, 2),
            "pnl_pct": round(self.pnl_pct, 2),
            "max_drawdown": self.max_drawdown,
            "blown_up": self.blown_up,
            "trades_count": self.trades_count,
            "round_trips": self.round_trips,
            "win_count": self.win_count,
            "win_rate": round(self.win_count / self.round_trips * 100, 1) if self.round_trips else 0,
            "code_revealed": self.code_revealed,
            "user_specified": self.user_specified,
            # 标的特性 (双盲下也展示: 只体现活跃度, 不泄露名称/代码/日期)
            "limit_up_count": self.limit_up_count,
            "limit_up_bucket": self.limit_up_bucket,
            "limit_up_label": _pool.bucket_label(self.limit_up_bucket) if _pool else "",
            # 条件单 (止盈/止损): 前端展示 active / triggered / cancelled 全量
            "conditionals": [dict(c) for c in self.conditionals],
            # 双盲: 只回传「已走完 + 当前部分周期」的 K 线, 避免把未来行情泄漏到前端
            "bars": self._display_bars(),
            # 周期视图 MACD (在日线上计算后按周期桶采样, 与 bars 一一对齐; 修复周/月/年线空洞)
            "macd": self.period_macd(),
            # 市场背景: 训练窗口之前的历史K线 (双盲时日期掩码为 #N)
            "pre_count": len(self.pre_bars),
            "pre_bars": (self.pre_bars if self.code_revealed
                         else [{**b, "date": f"#{i + 1}"} for i, b in enumerate(self.pre_bars)]),
        }
        if self.code_revealed or not hide_meta:
            # 训练结束揭晓: 标的名称 + 训练时间段
            out["code"] = self.code
            out["code_name"] = self.code_name
            out["start_date"] = self.start_date
            out["end_date"] = self.end_date
        elif self.user_specified:
            # 用户指定标的: 训练中正常展示代码与名称 (便于确认), 时间段仍在结束时揭晓
            out["code"] = self.code
            out["code_name"] = self.code_name
        return out

    def summary_mini(self) -> dict:
        """训练面板用的精简状态 (双盲隐藏)"""
        return {
            "session_id": self.session_id,
            "status": self.status,
            "bars_used": self.bars_used,
            "bars_total": self.bars_total,
            "pos": self.pos,
            "cash": round(self.cash, 2),
            "avg_cost": round(self.avg_cost, 2),
            "current_price": round(self.current_price or 0, 2),
            "pnl": round(self.pnl, 2),
            "pnl_pct": round(self.pnl_pct, 2),
            "max_drawdown": self.max_drawdown,
        }


# ---------- 管理器 (单例 + DB) ----------
class TrainingManager:
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None: cls._instance = super().__new__(cls)
            return cls._instance

    def __init__(self):
        if hasattr(self, "_initialized"): return
        self._initialized = True
        self._sessions: Dict[str, TrainingSession] = {}   # 内存活动会话
        self._db_path = Path(__file__).resolve().parent.parent / "data" / "training.db"
        self._db_path.parent.mkdir(exist_ok=True)
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(str(self._db_path)) as conn:
            c = conn.cursor()
            c.execute("""CREATE TABLE IF NOT EXISTS training_sessions (
                session_id TEXT PRIMARY KEY, status TEXT, category TEXT, code TEXT,
                code_revealed INTEGER, start_date TEXT, end_date TEXT,
                bars_total INTEGER, bars_used INTEGER,
                initial_capital REAL, final_capital REAL, pnl REAL, pnl_pct REAL,
                trades_count INTEGER, round_trips INTEGER, win_count INTEGER, win_rate REAL,
                created_at TEXT, settled_at TEXT, settlement_json TEXT
            )""")
            c.execute("""CREATE TABLE IF NOT EXISTS training_bars (
                session_id TEXT, bar_index INTEGER, date TEXT,
                open REAL, high REAL, low REAL, close REAL, volume INTEGER,
                action TEXT, action_price REAL,
                pos_before REAL, cash_before REAL, pnl_before REAL,
                pos_after REAL, cash_after REAL, pnl_after REAL,
                PRIMARY KEY (session_id, bar_index)
            )""")
            # 迁移: 每根K线的权益快照 (最大回撤/成长曲线的准确数据源)
            bar_cols = {r[1] for r in c.execute("PRAGMA table_info(training_bars)").fetchall()}
            if "equity" not in bar_cols:
                c.execute("ALTER TABLE training_bars ADD COLUMN equity REAL")
            # 战法库
            c.execute("""CREATE TABLE IF NOT EXISTS strategies (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL,
                description TEXT DEFAULT '', color TEXT DEFAULT '#00b5ff',
                created_at TEXT
            )""")
            # 迁移: 老库补齐 战法关联 / 杠杆 / 复盘笔记 / 最大回撤(均为幂等)
            cols = {r[1] for r in c.execute("PRAGMA table_info(training_sessions)").fetchall()}
            for col, ddl in (
                ("strategy_id", "ADD COLUMN strategy_id INTEGER"),
                ("leverage", "ADD COLUMN leverage REAL DEFAULT 1.0"),
                ("note", "ADD COLUMN note TEXT DEFAULT ''"),
                ("max_drawdown", "ADD COLUMN max_drawdown REAL DEFAULT 0"),
                ("blown_up", "ADD COLUMN blown_up INTEGER DEFAULT 0"),
                ("category_label", "ADD COLUMN category_label TEXT DEFAULT ''"),
                ("user_id", "ADD COLUMN user_id TEXT DEFAULT 'default'"),
                ("code_name", "ADD COLUMN code_name TEXT DEFAULT ''"),
                ("limit_up_count", "ADD COLUMN limit_up_count INTEGER DEFAULT 0"),
                ("limit_up_bucket", "ADD COLUMN limit_up_bucket TEXT DEFAULT ''"),
            ):
                if col not in cols:
                    c.execute(f"ALTER TABLE training_sessions {ddl}")
            # 默认战法: 训练默认归属的标签 (战法只是记录标签, 删除不影响记录)
            # 迁移: 旧「默认」战法改名为「副本」(「默认」一词保留给未归类展示)
            c.execute("UPDATE strategies SET name='副本' WHERE name='默认'")
            c.execute("INSERT OR IGNORE INTO strategies (name, description, color, created_at) VALUES (?,?,?,?)",
                      ("副本", "系统默认战法", "#00b5ff", datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            # 收益曲线重置记录: 每个账号一行, 记录重置次数与最近一次重置时间
            c.execute("""CREATE TABLE IF NOT EXISTS review_resets (
                user_id TEXT PRIMARY KEY, reset_count INTEGER DEFAULT 0, last_reset_at TEXT
            )""")
            conn.commit()

    # ---------- 启动 ----------
    def start(self, category: str, bars_count: int = 100,
              code: Optional[str] = None, initial_capital: float = 100_000.0,
              leverage: Optional[float] = None, strategy_id: Optional[int] = None,
              user_id: str = "default", period: str = "day",
              limit_up_weights: Optional[List[float]] = None) -> TrainingSession:
        """
        code=None → 随机挑一只符合品类的标的, 从全量 K 线末尾切 bars_count 根
        code 有值 → 用指定 code 的全量 K 线末尾切 bars_count 根

        limit_up_weights: 随机抽标的时的「涨停次数档位权重」(无/低/中/高, 长度4),
                          缺省等权; 仅 code=None 时生效
        """
        if category not in CATEGORY_FETCHERS:
            raise ValueError(f"未知品类 {category}, 支持 {list(CATEGORY_FETCHERS.keys())}")
        bars_count = min(max(bars_count, 20), CATEGORY_BARS_CAP.get(category, 250))
        if leverage is None:
            leverage = CATEGORY_LEVERAGE.get(category, 1.0)
        user_specified = bool(code)          # 用户指定标的 (训练中可展示名称)
        if strategy_id is None:              # 默认归属「默认」战法
            strategy_id = self.default_strategy_id()

        fetcher = CATEGORY_FETCHERS[category]
        lu_count, lu_bucket = 0, ""
        if code is None:
            # 随机抽取: 内部分级匹配 (优先周期内有涨停 / 有量能异常 / 时间较近)
            # 并避开同账号最近 50 局已用标的 (50 局以内不重复)
            exclude = self._recent_codes(user_id, 50)
            for _ in range(15):
                picked = self._pick_random_code(bars_count + 30, exclude=exclude)
                if not picked:
                    break
                cand = picked["code"]
                k = picked.get("klines") or _augment_amount(fetcher(cand), category)
                if len(k) >= bars_count + 30:
                    code, klines = cand, k
                    lu_count = picked.get("limit_up_count") or 0
                    lu_bucket = picked.get("limit_up_bucket") or ""
                    break
            if code is None:
                raise ValueError(
                    f"未找到历史足够长的标的 (至少需 {bars_count + 30} 条日K), 请重试或指定标的")
        else:
            klines = _augment_amount(fetcher(code), category)
            if len(klines) < bars_count + 30:
                raise ValueError(f"{code} K线只有 {len(klines)} 条, 不够训练")
        # 随机起始窗口: 在全历史中随机切一段 (开头保留 ≥30 根市场背景, 末尾留足训练根数)
        import random as _rnd
        start_idx = _rnd.randint(30, len(klines) - bars_count)
        bars = klines[start_idx:][:bars_count]

        # 市场背景: 训练窗口之前的近 250 根历史K线 (双盲时显示掩码日期)
        pre_count = min(250, start_idx)
        pre_bars = _augment_amount(klines[max(0, start_idx - pre_count):start_idx], category)

        session_id = str(uuid.uuid4())[:12]
        session = TrainingSession(session_id, category, code, bars, initial_capital,
                                  leverage=leverage, strategy_id=strategy_id, pre_bars=pre_bars,
                                  user_id=user_id, period=period,
                                  code_name=self._resolve_code_name(code),
                                  user_specified=user_specified,
                                  limit_up_count=lu_count, limit_up_bucket=lu_bucket)
        self._sessions[session_id] = session
        # 立即存一份骨架
        self._persist_session(session)
        return session

    def _pick_random_code(self, bars_needed: int = 0,
                          weights: Optional[List[float]] = None,
                          avoid: Optional[List[str]] = None,
                          exclude: Optional[List[str]] = None) -> Optional[dict]:
        """随机挑一只 A 股 —— 内部分级匹配 (周期内有涨停 / 量能异常 / 时间较近) + 去重

        两级策略:
          1) 标的池 (stock_pool.pick_tiered): 池内已确认「有分时数据」的候选按分级优先抽取,
             并避开 exclude (同账号最近 N 局已用代码); 池不足时按 MAX_PROBE 现场探测写池
          2) 兜底: 池不可用/抽不到 → 纯随机探测若干只「有分时数据」的标的

        weights/avoid 为历史遗留参数, 已不再生效 (权重逻辑内部化)。

        返回 {"code", "klines", "limit_up_count", "limit_up_bucket", "limit_up_label"}
        或 None (调用方回退到自己的随机逻辑)。
        """
        try:
            hit = _pool.pick_tiered(bars_needed, exclude=exclude)
            if hit:
                return hit
        except Exception as e:
            print(f"[Train] 标的池抽取失败, 回退纯随机: {e}")

        import random
        from ..data.stock_registry import StockRegistry
        from .intraday import has_minute_data
        try:
            reg = StockRegistry()
            reg.ensure_ready()
            codes = [r["code"] for r in reg.list_all() if r.get("market") in ("sh", "sz")]
        except Exception:
            return None
        if not codes:
            raise ValueError("股票注册表为空, 请先刷新")
        ex = set(exclude or [])
        filtered = [c for c in codes if c not in ex]
        if filtered:
            codes = filtered
        random.shuffle(codes)
        for code in codes[:8]:
            try:
                if has_minute_data(code):
                    k = _augment_amount(_fetch_stock_kline(code), "stock")
                    if bars_needed and len(k) < bars_needed:
                        continue                      # 历史不足, 顺带写池后继续探下一只
                    info = _pool.update_stat(code, "", "", "", 0, bars=k, has_intraday=True)
                    return {"code": code, "klines": k,
                            "limit_up_count": (info or {}).get("limit_up_count", 0),
                            "limit_up_bucket": (info or {}).get("limit_up_bucket", ""),
                            "limit_up_label": (info or {}).get("limit_up_label", "")}
            except Exception:
                continue
        # 全部探测失败: 退回纯随机 (不校验分时/长度), 由 start() 的重试循环继续筛
        return {"code": random.choice(codes), "klines": [],
                "limit_up_count": 0, "limit_up_bucket": "", "limit_up_label": ""}

    def _recent_codes(self, user_id: str, n: int = 50) -> List[str]:
        """该用户最近 n 局已用过的标的代码 (随机抽标时作为排除集, 50 局以内不重复)"""
        try:
            with sqlite3.connect(str(self._db_path)) as conn:
                rows = conn.execute(
                    """SELECT DISTINCT code FROM training_sessions
                       WHERE user_id=? AND code IS NOT NULL AND code!=''
                       ORDER BY created_at DESC LIMIT ?""",
                    (user_id or "default", n)).fetchall()
            return [r[0] for r in rows if r[0]]
        except Exception:
            return []

    def _recent_buckets(self, user_id: str, n: int = 5) -> List[str]:
        """该用户最近 n 局已用过的涨停档位 (供随机时避开, 保证每局涨停次数不同)"""
        try:
            with sqlite3.connect(str(self._db_path)) as conn:
                rows = conn.execute(
                    """SELECT limit_up_bucket FROM training_sessions
                       WHERE user_id=? AND limit_up_bucket!=''
                       ORDER BY created_at DESC LIMIT ?""",
                    (user_id or "default", n)).fetchall()
            return [r[0] for r in rows if r[0]]
        except Exception:
            return []

    def _resolve_code_name(self, code: str) -> str:
        """按代码查注册表中的股票名称 (查不到返回空串)"""
        try:
            from ..data.stock_registry import StockRegistry
            reg = StockRegistry()
            reg.ensure_ready()
            for r in reg.list_all():
                if r.get("code") == code:
                    return r.get("name") or ""
        except Exception:
            pass
        return ""

    def default_strategy_id(self) -> Optional[int]:
        """「副本」战法的 id (用于训练默认归属)"""
        with sqlite3.connect(str(self._db_path)) as conn:
            row = conn.execute("SELECT id FROM strategies WHERE name='副本'").fetchone()
        return row[0] if row else None

    # ---------- 动作 ----------
    def get(self, session_id: str) -> Optional[TrainingSession]:
        return self._sessions.get(session_id)

    def buy(self, session_id: str, qty: Optional[int] = None,
            ratio: Optional[float] = None) -> TrainingSession:
        s = self.get(session_id); assert s is not None
        s.buy(qty, ratio); s.last_action = "BUY"; self._persist_bar(s); self._persist_session(s); return s

    def sell(self, session_id: str, qty: Optional[int] = None, allow_short: bool = False) -> TrainingSession:
        s = self.get(session_id); assert s is not None
        s.sell(qty, allow_short); s.last_action = "SELL"; self._persist_bar(s); self._persist_session(s); return s

    def hold(self, session_id: str) -> TrainingSession:
        s = self.get(session_id); assert s is not None
        s.hold(); s.last_action = "HOLD"; self._persist_bar(s); self._persist_session(s); return s

    def set_conditional(self, session_id: str, kind: str,
                        trigger_price: Optional[float] = None, ratio: float = 1.0,
                        trail_pct: Optional[float] = None,
                        side: Optional[str] = None) -> dict:
        s = self.get(session_id); assert s is not None
        c = s.set_conditional(kind, trigger_price, ratio, trail_pct=trail_pct, side=side)
        self._persist_session(s)
        return c

    def cancel_conditional(self, session_id: str, cond_id: str) -> dict:
        s = self.get(session_id); assert s is not None
        c = s.cancel_conditional(cond_id)
        self._persist_session(s)
        return c

    def advance(self, session_id: str) -> dict:
        """推进一根 K, 同时决定 auto-settle / 条件单触发"""
        s = self.get(session_id); assert s is not None
        self._persist_bar(s)          # 落库当前这根 K 的决策 (含纯观望), 保证权益曲线完整
        result = s.advance()
        # 条件单触发 → 用触发动作(TP/SL)覆盖该根 K 的快照, 保证复盘与内存一致
        if result.get("triggered"):
            self._persist_bar(s, action=result["last_action"], bar_index=result["bar_index"])
        self._persist_session(s)
        if s.status in TERMINAL_STATUSES:   # settled / blown_up 均归档
            self._finalize_session(s)
        return {"session": s.summary(), "auto_settled": result.get("settled", False),
                "triggered": result.get("triggered", []),
                "conditions_skipped": result.get("conditions_skipped", [])}

    def switch_period(self, session_id: str, period: str) -> dict:
        """运行中切换周期, 进度不重置"""
        s = self.get(session_id); assert s is not None
        s.switch_period(period)
        self._persist_session(s)
        return s.summary()

    def manual_settle(self, session_id: str) -> dict:
        s = self.get(session_id); assert s is not None
        s.manual_settle(); self._finalize_session(s)
        return s.summary()

    def abort(self, session_id: str) -> Optional[TrainingSession]:
        s = self.get(session_id)
        if s and s.status == "active":
            s.abort(); self._finalize_session(s)
        return s

    # ---------- 存档 ----------
    def _persist_session(self, s: TrainingSession):
        with sqlite3.connect(str(self._db_path)) as conn:
            c = conn.cursor()
            c.execute("""INSERT OR REPLACE INTO training_sessions
                (session_id, status, category, code, code_revealed, start_date, end_date,
                 bars_total, bars_used, initial_capital, final_capital, pnl, pnl_pct,
                 trades_count, round_trips, win_count, win_rate, created_at, settled_at, settlement_json,
                 strategy_id, leverage, max_drawdown, blown_up, category_label, user_id, code_name,
                 limit_up_count, limit_up_bucket)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (s.session_id, s.status, s.category, s.code, s.code_revealed,
                 s.start_date, s.end_date, s.bars_total, s.bars_used,
                 s.initial_capital, round(s.portfolio_value, 2), round(s.pnl, 2), round(s.pnl_pct, 2),
                 s.trades_count, s.round_trips, s.win_count,
                 round(s.win_count / s.round_trips * 100, 1) if s.round_trips else 0,
                 datetime.now().strftime("%Y-%m-%d %H:%M:%S"), None, None,
                 s.strategy_id, s.leverage,
                 s.max_drawdown, 1 if s.blown_up else 0,
                 CATEGORY_LABELS.get(s.category, s.category),
                 s.user_id, s.code_name,
                 s.limit_up_count, s.limit_up_bucket))
            conn.commit()

    def _persist_bar(self, s: TrainingSession, action: Optional[str] = None,
                     bar_index: Optional[int] = None):
        """持久化当前 bar 的决策快照 (action 缺省取本根 K 的决策动作)

        bar_index 显式指定时用于「条件单触发后回写该根 K 的实际持仓变化」。
        """
        vis = s.bars                              # 当前周期的可见K线 (含部分周期)
        idx = s.bars_used if bar_index is None else bar_index
        if idx >= len(vis): return
        action = action or getattr(s, "last_action", "HOLD")
        bar = vis[idx]
        with sqlite3.connect(str(self._db_path)) as conn:
            c = conn.cursor()
            c.execute("""INSERT OR REPLACE INTO training_bars
                (session_id, bar_index, date, open, high, low, close, volume,
                 action, action_price, pos_before, cash_before, pnl_before,
                 pos_after, cash_after, pnl_after, equity)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (s.session_id, idx, bar["date"], bar["open"], bar["high"], bar["low"], bar["close"], bar["volume"],
                 action, bar["close"], s.pos, s.cash, s.pnl, s.pos, s.cash, s.pnl,
                 round(s._equity(bar["close"]), 2)))
            conn.commit()

    def _finalize_session(self, s: TrainingSession):
        with sqlite3.connect(str(self._db_path)) as conn:
            c = conn.cursor()
            final_pnl_pct = round(s.pnl_pct, 2)
            final_win_rate = round(s.win_count / s.round_trips * 100, 1) if s.round_trips else 0
            c.execute("""UPDATE training_sessions SET status=?, code_revealed=1,
                final_capital=?, pnl=?, pnl_pct=?, win_rate=?,
                max_drawdown=?, blown_up=?, leverage=?, strategy_id=?, category_label=?,
                settled_at=?, settlement_json=?
                WHERE session_id=?""",
                (s.status, round(s.portfolio_value, 2), round(s.pnl, 2), final_pnl_pct, final_win_rate,
                 s.max_drawdown, 1 if s.blown_up else 0, s.leverage, s.strategy_id,
                 CATEGORY_LABELS.get(s.category, s.category),
                 datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                 json.dumps(s.summary(hide_meta=False), ensure_ascii=False), s.session_id))
            conn.commit()
        # 从内存移除 (已 settle/abandoned)
        self._sessions.pop(s.session_id, None)

    # ---------- 历史 ----------
    def list_history(self, limit: int = 50, user_id: Optional[str] = None) -> List[dict]:
        """历史对局 —— 与训练模块实时同步

        - 状态机: 进行中(active) / 已结算(settled) / 已爆仓(blown_up) / 已放弃(abandoned)
          含 active 是为了「训练后立即在复盘可见」; 仅开始未推进(bars_used=0)的骨架不展示
        - 用户: 登录后只看本人记录; 历史遗留的 default(游客)记录一并可见, 避免旧数据消失
        """
        where = ("s.status IN ('active','settled','blown_up','abandoned') "
                 "AND NOT (s.status='active' AND s.bars_used=0)")
        params: list = []
        if user_id and user_id != "default":
            where += " AND (s.user_id=? OR s.user_id='default')"
            params.append(user_id)
        params.append(limit)
        with sqlite3.connect(str(self._db_path)) as conn:
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute(f"""SELECT s.session_id, s.status, s.category, s.category_label,
                    s.code, s.code_name, s.code_revealed, s.start_date, s.end_date,
                    s.bars_total, s.bars_used, s.initial_capital, s.final_capital,
                    s.pnl, s.pnl_pct, s.trades_count, s.round_trips, s.win_count, s.win_rate,
                    s.created_at, s.settled_at, s.strategy_id, s.leverage,
                    s.max_drawdown, s.blown_up, s.note, s.user_id,
                    st.name AS strategy_name
                FROM training_sessions s
                LEFT JOIN strategies st ON st.id = s.strategy_id
                WHERE {where}
                ORDER BY s.created_at DESC LIMIT ?""", params)
            rows = [dict(r) for r in c.fetchall()]
        for r in rows:
            r["status_label"] = STATUS_LABELS.get(r.get("status"), r.get("status"))
        return rows

    # ---------- 收益曲线 (累计净值) / 重置 ----------
    def _reset_state(self, conn, user_id: str) -> dict:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT reset_count, last_reset_at FROM review_resets WHERE user_id=?",
                           (user_id,)).fetchone()
        if row:
            return {"reset_count": row["reset_count"] or 0, "last_reset_at": row["last_reset_at"] or ""}
        return {"reset_count": 0, "last_reset_at": ""}

    def equity_nav(self, user_id: str = "default") -> dict:
        """账号收益曲线: 净值比例复利加权 (基准100), 仅统计「最近一次重置之后」的结算局

        nav <= 0 (出现 -100% 爆仓) 时 can_start=False, 训练入口据此拦截。
        """
        with sqlite3.connect(str(self._db_path)) as conn:
            conn.row_factory = sqlite3.Row
            st = self._reset_state(conn, user_id)
            where = ("WHERE s.status IN ('settled','blown_up') "
                     "AND s.user_id=? AND s.pnl_pct IS NOT NULL")
            params: list = [user_id]
            if st["last_reset_at"]:
                where += " AND s.created_at > ?"
                params.append(st["last_reset_at"])
            rows = conn.execute(
                f"""SELECT session_id, code, code_name, category, created_at, pnl_pct
                    FROM training_sessions s {where} ORDER BY s.created_at ASC""",
                params).fetchall()
        nav = 1.0
        curve: List[dict] = []
        for r in rows:
            nav *= (1 + (float(r["pnl_pct"]) or 0) / 100)
            if nav < 0:
                nav = 0.0
            curve.append({
                "session_id": r["session_id"], "created_at": r["created_at"],
                "code": r["code"], "code_name": r["code_name"] or "",
                "pnl_pct": round(float(r["pnl_pct"]) or 0, 2),
                "nav": round(nav * 100, 2),
            })
        return {
            "nav": round(nav * 100, 2),
            "curve": curve,
            "can_start": nav > 0,
            "reset_count": st["reset_count"],
            "last_reset_at": st["last_reset_at"],
        }

    def reset_equity(self, user_id: str = "default") -> dict:
        """重置收益曲线起点 (保留全部训练记录, 仅从当前时刻重新起算) 并累加重置次数"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with sqlite3.connect(str(self._db_path)) as conn:
            conn.execute(
                """INSERT INTO review_resets (user_id, reset_count, last_reset_at) VALUES (?,1,?)
                   ON CONFLICT(user_id) DO UPDATE SET
                     reset_count = reset_count + 1, last_reset_at = excluded.last_reset_at""",
                (user_id, now))
            conn.commit()
        return self.equity_nav(user_id)

    def get_history(self, session_id: str) -> Optional[dict]:
        """单局完整回看数据: 训练K线(含每根决策) + 训练前市场背景K线 + 权益曲线

        前置背景K线来自结算快照 settlement_json (仅有结算/放弃后才有), 因此
        进行中的会话需由调用方从内存会话补齐 (见 main.train_history_detail)。
        """
        with sqlite3.connect(str(self._db_path)) as conn:
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute("""SELECT s.*, st.name AS strategy_name FROM training_sessions s
                LEFT JOIN strategies st ON st.id = s.strategy_id
                WHERE s.session_id=?""", (session_id,))
            sess = c.fetchone()
            if not sess: return None
            c.execute("SELECT * FROM training_bars WHERE session_id=? ORDER BY bar_index", (session_id,))
            bars = [dict(r) for r in c.fetchall()]
            result = dict(sess)
            result["bars"] = bars
            result["status_label"] = STATUS_LABELS.get(result.get("status"), result.get("status"))
            if not result.get("code_name"):     # 老记录回填股票名称
                result["code_name"] = self._resolve_code_name(result.get("code") or "")
            # 权益曲线: 优先用落库的每根权益快照 (空头/杠杆均准确), 老数据回退旧公式
            result["equity_curve"] = [
                b["equity"] if b.get("equity") is not None
                else (b["cash_after"] or 0) + (b["pos_after"] or 0) * b["close"]
                for b in bars
            ]
            # 训练前市场背景K线 (来自结算快照)
            pre_bars = []
            raw = result.get("settlement_json")
            if raw:
                try:
                    pre_bars = json.loads(raw).get("pre_bars") or []
                except (ValueError, TypeError):
                    pre_bars = []
            result["pre_bars"] = pre_bars
            result["pre_count"] = len(pre_bars)
            return result

    # ---------- 已归档会话的分时回看 (内存已释放) ----------
    def replay_series(self, session_id: str) -> Optional[dict]:
        """已归档会话的分时回看序列: {code, bars, pre_bars}

        优先取结算快照 settlement_json 的 bars —— 与结算瞬间前端展示的完整K线序列
        (含未产生决策的K线) 完全一致; 落库的 training_bars 只记录「有决策」的稀疏K线,
        直接按下标索引会与前端看到的位置错位。无快照时回退到落库K线。
        """
        h = self.get_history(session_id)
        if not h:
            return None
        pre = list(h.get("pre_bars") or [])
        raw = h.get("settlement_json")
        snap = None
        if raw:
            try:
                snap = json.loads(raw)
            except (ValueError, TypeError):
                snap = None
        if snap and snap.get("bars"):
            return {"code": h.get("code") or snap.get("code") or "",
                    "bars": list(snap["bars"]),
                    "pre_bars": list(snap.get("pre_bars") or pre)}
        return {"code": h.get("code") or "", "bars": list(h.get("bars") or []), "pre_bars": pre}

    def bar_intraday_replay(self, session_id: str, idx: int) -> Optional[dict]:
        r = self.replay_series(session_id)
        if r is None:
            return None
        bars, pre = r["bars"], r["pre_bars"]
        if not 0 <= idx < len(bars):
            raise ValueError(f"训练K线索引 {idx} 越界 (0 ~ {len(bars) - 1})")
        prev_close = bars[idx - 1]["close"] if idx > 0 else (pre[-1]["close"] if pre else None)
        return build_intraday(r["code"], bars[idx], prev_close, f"#{len(pre) + idx + 1}", True)

    def prebar_intraday_replay(self, session_id: str, idx: int) -> Optional[dict]:
        r = self.replay_series(session_id)
        if r is None:
            return None
        pre = r["pre_bars"]
        if not 0 <= idx < len(pre):
            raise ValueError(f"背景K线索引 {idx} 越界 (0 ~ {len(pre) - 1})")
        prev_close = pre[idx - 1]["close"] if idx > 0 else None
        return build_intraday(r["code"], pre[idx], prev_close, f"#{idx + 1}", True)

    # ---------- 战法库 ----------
    def list_strategies(self) -> List[dict]:
        with sqlite3.connect(str(self._db_path)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM strategies ORDER BY id").fetchall()
            return [dict(r) for r in rows]

    def create_strategy(self, name: str, description: str = "", color: str = "#00b5ff") -> dict:
        name = (name or "").strip()
        if not name: raise ValueError("战法名称不能为空")
        with sqlite3.connect(str(self._db_path)) as conn:
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("INSERT INTO strategies (name, description, color, created_at) VALUES (?,?,?,?)",
                             (name, description, color, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                conn.commit()
            except sqlite3.IntegrityError:
                raise ValueError(f"战法「{name}」已存在")
            return dict(conn.execute("SELECT * FROM strategies WHERE name=?", (name,)).fetchone())

    def delete_strategy(self, strategy_id: int) -> bool:
        with sqlite3.connect(str(self._db_path)) as conn:
            conn.execute("UPDATE training_sessions SET strategy_id=NULL WHERE strategy_id=?", (strategy_id,))
            cur = conn.execute("DELETE FROM strategies WHERE id=?", (strategy_id,))
            conn.commit()
            return cur.rowcount > 0

    def set_session_strategy(self, session_id: str, strategy_id: Optional[int]) -> bool:
        """把某局训练归类到战法 (复盘归档)"""
        with sqlite3.connect(str(self._db_path)) as conn:
            cur = conn.execute("UPDATE training_sessions SET strategy_id=? WHERE session_id=?",
                               (strategy_id, session_id))
            conn.commit()
            s = self.get(session_id)
            if s: s.strategy_id = strategy_id
            return cur.rowcount > 0

    def set_note(self, session_id: str, note: str) -> bool:
        """复盘笔记"""
        with sqlite3.connect(str(self._db_path)) as conn:
            cur = conn.execute("UPDATE training_sessions SET note=? WHERE session_id=?", (note or "", session_id))
            conn.commit()
            return cur.rowcount > 0

    def strategy_stats(self, user_id: Optional[str] = None) -> List[dict]:
        """分战法统计: 胜率 / 盈亏比 / 最大回撤 / 爆仓次数 / 平均收益 (按用户隔离)

        仅统计终局且已产生盈亏的对局 (已结算 + 已爆仓), 进行中/已放弃不计入。
        """
        where = "status IN ('settled','blown_up')"
        params: list = []
        if user_id and user_id != "default":
            where += " AND (user_id=? OR user_id='default')"
            params.append(user_id)
        with sqlite3.connect(str(self._db_path)) as conn:
            conn.row_factory = sqlite3.Row
            strategies = [dict(r) for r in conn.execute("SELECT * FROM strategies ORDER BY id").fetchall()]
            rows = [dict(r) for r in conn.execute(
                f"""SELECT strategy_id, pnl, pnl_pct, win_rate, max_drawdown, blown_up, round_trips
                   FROM training_sessions WHERE {where}""", params).fetchall()]
        grouped: Dict[Any, List[dict]] = {}
        for r in rows:
            grouped.setdefault(r["strategy_id"], []).append(r)

        def _agg(items: List[dict], sid) -> dict:
            wins = [r["pnl"] for r in items if (r["pnl"] or 0) > 0]
            losses = [r["pnl"] for r in items if (r["pnl"] or 0) < 0]
            if not items:
                return {"strategy_id": sid, "sessions": 0, "win_rate": 0, "pnl_ratio": 0,
                        "total_pnl": 0, "avg_pnl_pct": 0, "max_drawdown": 0, "blowups": 0}
            avg_win = sum(wins) / len(wins) if wins else 0
            avg_loss = abs(sum(losses) / len(losses)) if losses else 0
            if avg_loss > 0:
                ratio = round(avg_win / avg_loss, 2)
            else:
                ratio = round(avg_win, 2) if avg_win else 0
            return {
                "strategy_id": sid,
                "sessions": len(items),
                "win_rate": round(sum(1 for r in items if (r["pnl"] or 0) > 0) / len(items) * 100, 1),
                "pnl_ratio": ratio,
                "total_pnl": round(sum(r["pnl"] or 0 for r in items), 2),
                "avg_pnl_pct": round(sum(r["pnl_pct"] or 0 for r in items) / len(items), 2),
                "max_drawdown": round(max((r["max_drawdown"] or 0) for r in items), 2),
                "blowups": sum(1 for r in items if r["blown_up"]),
            }

        out = []
        for st in strategies:
            out.append({**st, **_agg(grouped.pop(st["id"], []), st["id"])})
        # 未归类 (strategy_id=NULL) → 展示为「默认」(不可删除)
        unassigned = grouped.pop(None, [])
        if unassigned:
            out.append({"id": None, "name": "默认", "color": "#8892b0",
                        **_agg(unassigned, None)})
        return out


# 单例
training_mgr = TrainingManager()
