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
import time
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


# 需求101603: 日K拉取缓存 —— akshare 单只全量日K 冷取 0.8~2.0s (最多 8000+ 根),
# 是「开始训练」接口的主要耗时来源; 加进程内 TTL 缓存 + 单飞(同 key 并发只拉一次),
# 命中时返回浅拷贝, 避免训练会话改写缓存内的 K 线字典。
_KLINE_TTL = 600
_KLINE_CACHE: Dict[str, tuple] = {}
_KLINE_LOCKS: Dict[str, threading.Lock] = {}
_KLINE_GUARD = threading.Lock()


def _fetch_stock_kline(code: str) -> List[dict]:
    """A股日 K (前复权, 新浪: volume=股, 含真实 amount=元); 带 TTL 缓存 + 单飞"""
    hit = _KLINE_CACHE.get(code)
    if hit and time.time() - hit[0] < _KLINE_TTL:
        return [dict(b) for b in hit[1]]
    with _KLINE_GUARD:
        lk = _KLINE_LOCKS.setdefault(code, threading.Lock())
    with lk:
        hit = _KLINE_CACHE.get(code)              # 双重检查: 等锁期间可能已被填好
        if hit and time.time() - hit[0] < _KLINE_TTL:
            return [dict(b) for b in hit[1]]
        import akshare as ak
        prefix = "sh" if code.startswith(("6", "9")) else "sz"
        bars = _norm_en(ak.stock_zh_a_daily(symbol=f"{prefix}{code}", adjust="qfq"))
        if bars:
            _KLINE_CACHE[code] = (time.time(), bars)
        return [dict(b) for b in bars]


def warm_klines(codes: List[str], workers: int = 6) -> int:
    """需求101603: 并发预热若干标的的日K缓存 (供后端预加载调用), 返回成功预热数"""
    from concurrent.futures import ThreadPoolExecutor

    codes = [c for c in (codes or []) if c]
    if not codes:
        return 0

    def _one(c: str) -> bool:
        try:
            return bool(_fetch_stock_kline(c))
        except Exception:
            return False

    with ThreadPoolExecutor(max_workers=max(2, min(workers, len(codes)))) as ex:
        return sum(1 for r in ex.map(_one, codes) if r)


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

# 单局最少根数 (与前端滑块下限同口径)
MIN_BARS = 20

# 指标预热日K上限: 训练窗口之前额外保留的日K (仅用于周/月/年线 MACD 的 EMA 收敛, 不展示)
# 年线 span=240, 需 ≥26 根年K (=6240 根日K) 才能让 EMA(26)/DEA(9) 出值, 故上限放宽到
# 单只 A 股全部日K的量级; 这些日K本就在同一次拉取中拿到, 只是多切一段、不产生额外请求。
WARMUP_CAP = 8000

# 需求101604: 「预生成会话」(点击开始训练前备好数据) 的保鲜期与缓存上限
# 需求101605: 保鲜期 300 → 900s (用户可能在设置面板停留较久; 过期会导致开局退回冷构建)
_PRIME_TTL = 900      # 预生成数据 15 分钟内有效 (用户改参数会立即重新预生成)
_PRIME_MAX = 40       # 单进程最多保留的预生成会话数 (超出丢弃最旧的)


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
# 周期长度固定为「交易日数」(周=5/月=20/年=240), 以训练窗口起点为锚点连续切分,
# 不再按 ISO 自然周 / 自然月切分。原先首桶可能是 2~3 天的残周, 推进时会被补全,
# 从而导致「一次推进出现多根 K 线变化」并且改写已展示 K 线的 OHLC。
PERIOD_SPAN = {"day": 1, "week": 5, "month": 20, "year": 240}


def bucket_ranges(bars: List[dict], period: str = "day",
                  offset: int = 0) -> List[tuple]:
    """把日K序列按「固定交易日长度」切分为区间列表 [(start, end), ...] (半开区间)

    offset: 该序列在全局周期网格中的起点偏移 (0 ~ span-1)。前置背景K线按 span
            截整后 offset=0, 保证训练首根刚好落在周期边界上 (锚点即背景首根K线)。
    """
    if not bars:
        return []
    if period == "day":
        return [(i, i + 1) for i in range(len(bars))]
    span = PERIOD_SPAN.get(period, 1)
    n = len(bars)
    off = offset % span
    first = span - off if off else span
    ranges: List[tuple] = []
    i, end = 0, min(first, n)
    while i < n:
        ranges.append((i, end))
        i, end = end, min(end + span, n)
    return ranges


def aggregate_bars(bars: List[dict], period: str = "day",
                   offset: int = 0) -> tuple:
    """按周期聚合 dict K线, 返回 (聚合K线, 每根覆盖的日K数量)

    - period=day 原样返回
    - 聚合桶的 date 取该桶最后一根日K的日期 (保证分时联动有确定日期)
    - counts 用于周期切换时进度(已覆盖日K)映射
    """
    if period == "day" or not bars:
        return list(bars), [1] * len(bars)
    out, counts = [], []
    for s0, e0 in bucket_ranges(bars, period, offset):
        g = bars[s0:e0]
        out.append({
            "date": g[-1]["date"],
            "open": g[0]["open"],
            "high": max(x["high"] for x in g),
            "low": min(x["low"] for x in g),
            "close": g[-1]["close"],
            "volume": sum(x["volume"] for x in g),
            "amount": sum(x.get("amount") or 0 for x in g),
        })
        counts.append(len(g))
    return out, counts


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
                 limit_up_count: int = 0, limit_up_bucket: str = "",
                 warmup_bars: Optional[List[dict]] = None,
                 non_blind: bool = False):
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
        self.non_blind = bool(non_blind)             # 非双盲(指定个股+起始日期): 名称与日期均可见
        self.code_revealed = 0
        self.period = period if period in ("day", "week", "month", "year") else "day"
        # 原始日K (训练窗口) —— 进度与聚合的唯一基准
        self._daily = list(bars)
        self.days_total = len(self._daily)
        self.days_used = 0                 # 已推进(消费)的交易日数, 周期切换时进度以此为准
        self._p_ranges: List[tuple] = []   # 当前周期每根K线覆盖的日K区间 [(start,end), ...]
        self.bars_total = 0
        self.bars_used = 0                 # 已决策的「周期K线」根数 (当前周期下)
        # 训练窗口之前的历史K线: pre_bars=市场背景(展示), warmup=指标预热(仅算MACD不展示)
        self._daily_pre = list(pre_bars or [])
        self._warmup_daily = list(warmup_bars or [])
        # 性能优化(101701): 周期聚合/背景/MACD预热的缓存 —— 网格不变时避免重复 O(n) 计算
        self._full_bar_cache: Dict[tuple, dict] = {}
        self._all_bars_cache: Optional[List[dict]] = None
        self._pre_view_cache: Dict[str, List[dict]] = {}
        self._warm_closes_cache: Dict[str, List[float]] = {}
        # MACD 全量结果缓存: 每个周期的「闭合收盘价序列」(预热+背景+全部训练K) 在一次会话内恒定不变,
        # 仅切片长度随进度变化 → 按周期缓存 _macd_js 结果, 推进/切周期时只做切片。
        self._macd_full_cache: Dict[tuple, tuple] = {}
        self._bucket_ends: List[int] = []
        self._recompute_progress()
        self.pre_bars = self._pre_view()   # 背景K线按当前周期聚合 (锚点=背景首根日K)
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
        # 买入委托 (需求4): 决策在 K 线 N, 于下一根 K 线开盘价成交 (挂单模型)
        self._pending_buy: Optional[dict] = None
        self.last_fill: Optional[dict] = None   # 最近一次成交/失败记录 (前端买点与提示)
        # T+1: 当日买入的开多数量被锁定, 当日不可卖出, 下一交易日推进后解锁
        self._locked_qty = 0

    @property
    def pending_buy(self) -> Optional[dict]:
        """待成交买入委托 (对外只读视图, 与 summary()['pending_buy'] 同源)"""
        return dict(self._pending_buy) if self._pending_buy else None

    # ---------- 周期视图 (进度按交易日映射, 不提前展示未走到的行情) ----------
    def _pre_daily_view(self) -> List[dict]:
        """背景日K按当前周期截整: 取最近的 span 整数倍根

        使周期网格在「训练首根」处刚好对齐 (锚点即背景首根日K), 训练段与背景段
        的周期K线首尾相接、互不跨桶。
        """
        pre = self._daily_pre
        if self.period == "day" or not pre:
            return list(pre)
        span = PERIOD_SPAN.get(self.period, 1)
        k = len(pre) - (len(pre) % span)
        return list(pre[len(pre) - k:]) if k else list(pre)

    def _pre_view(self) -> List[dict]:
        """背景K线的周期聚合序列 (与训练段同一网格锚点)

        性能优化(101701): 结果只依赖周期与 _daily_pre, 按周期缓存 (推进时不变)。
        """
        hit = self._pre_view_cache.get(self.period)
        if hit is not None:
            return list(hit)
        daily = self._pre_daily_view()
        out = daily if self.period == "day" else aggregate_bars(daily, self.period)[0]
        self._pre_view_cache[self.period] = out
        return list(out)

    def _recompute_progress(self):
        """按当前周期重算每根K线覆盖的日K区间 (仅周期/网格变化时调用)

        性能优化(101701): 周期网格只随「切换周期」变化, 推进一根K线并不改变网格。
        因此把「重建网格」与「更新已走完根数」拆开: 推进时只走 _update_progress,
        网格与聚合缓存 (_full_bar_cache / _all_bars_cache) 得以跨推进复用,
        消除长周期(1250根)下 _full_bar 的 O(n²) 重复聚合。
        """
        off = 0
        if self.period != "day":
            off = len(self._pre_daily_view()) % PERIOD_SPAN.get(self.period, 1)
        self._p_ranges = bucket_ranges(self._daily, self.period, offset=off)
        self.bars_total = len(self._p_ranges)
        self._bucket_ends = [e for _s, e in self._p_ranges]   # 升序, 供二分
        # 网格变化 → 失效聚合缓存 (背景/MACD预热缓存不依赖训练网格, 保留)
        self._full_bar_cache = {}
        self._all_bars_cache = None
        self._update_progress()

    def _update_progress(self):
        """仅更新「已走完的周期根数」(网格不变, 推进时调用)"""
        import bisect
        self.bars_used = bisect.bisect_right(self._bucket_ends, self.days_used)

    def _full_bar(self, s0: int, e0: int) -> dict:
        """把 _daily[s0:e0] 聚合为一根周期K线 (结果按区间缓存)

        性能优化(101701): 同一区间在一次会话内会被 _visible_bars/_all_bars/
        current_bar/summary/period_macd 反复读取, 改为按 (s0, e0) 记忆化。
        返回浅拷贝, 避免调用方改写缓存对象。
        """
        key = (s0, e0)
        hit = self._full_bar_cache.get(key)
        if hit is not None:
            return dict(hit)
        g = self._daily[s0:e0]
        bar = {
            "date": g[-1]["date"],
            "open": g[0]["open"],
            "high": max(x["high"] for x in g),
            "low": min(x["low"] for x in g),
            "close": g[-1]["close"],
            "volume": sum(x["volume"] for x in g),
            "amount": sum(x.get("amount") or 0 for x in g),
        }
        self._full_bar_cache[key] = bar
        return dict(bar)

    def _visible_bars(self) -> List[dict]:
        """可见K线 = 「已完整走完」的周期桶 + 「当前正在形成」的部分周期桶

        需求101606: 周期切换时 K线计算必须与「已走到的交易日」一致。
        例: 日线推进到第2天(已显示 day1/day2)时切周线, 周K仅按 day1+day2 聚合,
        不提前把 day1~day5 整周算作一根周K。因此在「已完成桶」之后追加
        「当前部分周期桶」(_daily[s0:days_used+1]); 日线 span=1, 该桶即当前交易日。
        """
        out: List[dict] = [self._full_bar(s0, e0)
                           for s0, e0 in self._p_ranges if e0 <= self.days_used]
        # 当前部分周期桶: bars_used 恰好指向「首个未走完的桶」(桶区间连续)
        if self.days_used < self.days_total and self.bars_used < len(self._p_ranges):
            s0, e0 = self._p_ranges[self.bars_used]
            cut = min(e0, self.days_used + 1)
            if cut > s0:
                out.append(self._full_bar(s0, cut))
        return out

    def _all_bars(self) -> List[dict]:
        """完整周期的全部K线 (仅结算揭晓后展示)

        性能优化(101701): 完整周期K线在一次会话内不变, 结果按网格缓存。
        """
        if self._all_bars_cache is None:
            self._all_bars_cache = [self._full_bar(s0, e0) for s0, e0 in self._p_ranges]
        return list(self._all_bars_cache)

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
        """周期视图 MACD: 直接在「周期K线序列」上独立计算 (不再由日线采样)

        - 日线: 在 (背景日K + 训练日K) 上计算, 与前端 indicators.js 同口径
        - 周/月/年线: 在 (预热周期 + 背景周期 + 训练周期) 的收盘价上独立计算 MACD,
          预热段仅用于让 EMA(26)/DEA(9) 收敛、不展示。MACD 是因果指标(只依赖过去),
          因此用完整序列计算后再切片, 与只用可见前缀计算的结果完全一致。

        返回与前端合并序列 (pre_bars + 可见 bars) 一一对齐的 {dif, dea, hist}。
        """
        disp = self._display_bars()
        # 性能优化(101701): 每个周期的收盘价序列(预热+背景+全部训练K)在一次会话内恒定,
        # 因此 _macd_js 全量结果可缓存, 推进/切周期时只做切片 —— 消除每步重复计算 MACD 的开销。
        key = (self.period, fast, slow, signal)
        full = self._macd_full_cache.get(key)
        if full is None:
            if self.period == "day":
                pre_closes = [b.get("close") for b in (self._daily_pre or [])]
                closes = pre_closes + [b.get("close") for b in (self._daily or [])]
                base = 0
            else:
                # 周期序列 = 预热(仅EMA) + 背景 + 训练(完整周期)
                warm = self._warmup_daily or []
                warm_closes = self._warm_closes_cache.get(self.period)
                if warm_closes is None:
                    warm_closes = [b.get("close") for b in (aggregate_bars(warm, self.period)[0] if warm else [])]
                    self._warm_closes_cache[self.period] = warm_closes
                pre_closes = [b.get("close") for b in self._pre_view()]
                train_closes = [b.get("close") for b in self._all_bars()]
                closes = warm_closes + pre_closes + train_closes
                base = len(warm_closes)
            computed = _macd_js(closes, fast, slow, signal) if closes else ([], [], [])
            full = (computed[0], computed[1], computed[2], base, len(pre_closes))
            self._macd_full_cache[key] = full
        dif, dea, hist, base, pre_len = full
        n = pre_len + len(disp)
        return {"dif": dif[base:base + n], "dea": dea[base:base + n], "hist": hist[base:base + n]}

    # 当前 bar 的 close_price (撮合价) —— 取「当前交易日」收盘价
    @property
    def current_price(self) -> Optional[float]:
        if self.days_used >= self.days_total:
            return None
        return self._daily[self.days_used]["close"]

    # 当前「正在形成」的周期K线 OHLC (日线=当前交易日; 周/月/年=已走过部分的聚合)
    @property
    def current_bar(self) -> Optional[dict]:
        if self.days_used >= self.days_total:
            return None
        if self.period == "day":
            return self._daily[self.days_used]
        if self.bars_used >= len(self._p_ranges):
            return None
        s0, e0 = self._p_ranges[self.bars_used]
        cut = min(e0, self.days_used + 1)
        return self._full_bar(s0, cut) if cut > s0 else None

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
        """挂出买入委托 (需求4): 决策在 K 线 N, 于下一根 K 线开盘价成交

        - qty 指定时按股数买入
        - qty 为空且 ratio 给定时按「总资产 × 杠杆 × ratio」换算手数 (仓位选择)
        - 都不传 = 满仓买入
        实际成交发生在 advance() 走完新一根 K 线时, 以该根开盘价执行 (见 _fill_buy)。
        """
        if self.status != "active": raise ValueError("会话已结束")
        if self.current_price is None: raise ValueError("无可操作 K 线")
        self._pending_buy = {"kind": "COVER" if self.pos < 0 else "BUY",
                             "qty": qty, "ratio": ratio, "decided_index": self.bars_used}
        return {"action": "BUY_PENDING", "qty": int(qty or 0), "ratio": ratio,
                "price": None, "pending": True, "decided_index": self.bars_used}

    def _fill_buy(self, price: float, qty: Optional[int] = None,
                  ratio: Optional[float] = None) -> dict:
        """买入委托成交执行体 (价格由调用方给定: 成交根的开盘价)"""
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
            # 仓位模式按「手」(100股)取整: 不足 1 手即视为买不动。
            # 不再用 max(1, ...) 兜底强买 1 股 —— 满仓后再点买入会把可用资金打成负数。
            qty = int(amount / price / 100) * 100
            if qty <= 0:
                raise ValueError("资金不足 / 仓位已满, 无法买入")
        if qty <= 0: raise ValueError("数量必须 > 0")
        cost = qty * price + self._fee(qty * price)
        if cost > bp:
            qty = int(bp / (price * 1.0003))
            if qty <= 0: raise ValueError("资金不足 / 仓位已满, 无法买入")
        self.cash -= qty * price + self._fee(qty * price)
        if self.pos > 0:
            # 加仓: 加权均价
            self.avg_cost = (self.avg_cost * self.pos + price * qty) / (self.pos + qty)
        else:
            self.avg_cost = price
        self.pos += qty
        self._locked_qty += qty          # T+1: 当日买入锁定, 次日开盘后才可卖出
        self.trades_count += 1
        return {"action": "BUY", "qty": qty, "price": price}

    # ---------- 涨停板判定 (需求3/4: 涨停板无法买入) ----------
    @staticmethod
    def _limit_pct(code: str) -> float:
        """涨跌幅限制: 科创板/创业板 20%, 其余 10%"""
        return 0.20 if str(code).startswith(("688", "300", "301")) else 0.10

    def _is_limit_one_word(self, day_idx: int) -> bool:
        """成交日(日K _daily[day_idx])是否为「缩量一字涨停板」→ 买不到

        一字(open==high==low==close) 且 收盘为涨停价 且 成交量 < 前 5 日均量 × 50%,
        三者同时满足才判定买不到; 普通涨停 / 放量一字板均可买。
        """
        if day_idx < 0 or day_idx >= len(self._daily):
            return False
        bar = self._daily[day_idx]
        o, h, l, c = bar["open"], bar["high"], bar["low"], bar["close"]
        if not (o == h == l == c):                       # 非一字
            return False
        if day_idx > 0:
            prev = self._daily[day_idx - 1]["close"]
        elif self._daily_pre:
            prev = self._daily_pre[-1]["close"]
        else:
            return False
        limit = round(prev * (1 + self._limit_pct(self.code)), 2)
        if abs(c - limit) > 0.011:                       # 非涨停一字
            return False
        vols = [self._daily[i]["volume"] for i in range(max(0, day_idx - 5), day_idx)
                if self._daily[i].get("volume")]
        if not vols:                                     # 无历史量能 → 不判缩量
            return False
        return self._daily[day_idx].get("volume", 0) < (sum(vols) / len(vols)) * 0.5

    def sell(self, qty: Optional[int] = None, allow_short: bool = False) -> dict:
        """平多 / 开空 (即时成交 @ 当前 close)"""
        if self.status != "active": raise ValueError("会话已结束")
        price = self.current_price
        if price is None: raise ValueError("无可操作 K 线")
        if self.pos > 0:
            # T+1: 当日买入的部分不可卖出 (sellable = 持仓 - 当日买入锁定)
            sellable = self.pos - self._locked_qty
            if sellable <= 0:
                raise ValueError("当日买入(T+1), 次日开盘后才能卖出")
            close_qty = min(qty or sellable, sellable)
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
            if c["side"] == "long":
                avail = self.pos - self._locked_qty     # T+1: 当日买入不可平
            else:
                avail = abs(self.pos) if self.pos < 0 else 0
            operable = avail > 0
            if not operable:
                c["status"] = "skipped"
                c["triggered_index"] = bar_index
                c["triggered_date"] = bar["date"]
                c["triggered_price"] = round(exec_price, 2)
                c["message"] = ("条件单触发, 但当日买入(T+1)不可平仓, 已跳过"
                                if c["side"] == "long" and self.pos > 0
                                else "条件单触发, 但当前无可操作持仓, 已跳过")
                skipped.append(dict(c))
                continue
            qty = max(1, int(avail * c["ratio"]))
            qty = min(qty, avail)
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
        self._locked_qty = 0        # T+1: 新的一日推进, 昨日买入解锁
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
        # 需求4: 待成交买入委托在该「新走完那根K线之后」的开盘价成交
        # (下一根周期K线的首日 = _daily[cut]; 缩量一字板 → 买不到, 委托失效)
        fill_info = None
        if self._pending_buy:
            intent, self._pending_buy = self._pending_buy, None
            if cut >= self.days_total:
                fill_info = {"action": "BUY_FAILED", "reason": "已到最后一根K线, 委托失效",
                             "bar_index": bar_idx + 1, "date": done["date"],
                             "price": None, "qty": 0}
            elif self._is_limit_one_word(cut):
                fill_info = {"action": "BUY_FAILED", "reason": "缩量一字板, 无法买入",
                             "bar_index": bar_idx + 1, "date": self._daily[cut]["date"],
                             "price": None, "qty": 0}
            else:
                nxt = self._daily[cut]
                r = self._fill_buy(float(nxt["open"]), intent["qty"], intent["ratio"])
                fill_info = {"action": r["action"], "price": r["price"], "qty": r["qty"],
                             "bar_index": bar_idx + 1, "date": nxt["date"]}
            self.last_fill = fill_info
        self.days_used = cut
        self._update_progress()      # 性能优化(101701): 网格未变, 仅更新已走完根数
        # 权益按「刚走完那根K线」的收盘价计, 与决策价保持一致 (最大回撤才准确)
        self.equity_curve.append(self._equity(done["close"]))
        # 条件单: 按刚走完的 K 线高低价判定, 触发即尝试操作 (有持仓则平仓, 无则提示)
        triggered, skipped = self._check_conditionals(done, bar_idx)
        result = self._bar_snapshot(prev_bar)
        result["pending_filled"] = fill_info
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
        self._pending_buy = None   # 未成交委托作废
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
        self._pending_buy = None   # 未成交委托作废
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
        # 市场背景同样按周期聚合展示 (合并绘制需同一周期, 且与训练段同一网格锚点)
        self.pre_bars = self._pre_view()
        return self.summary()

    # ---------- 对外输出 ----------
    def _intraday(self, code: str, b: dict, prev_close: Optional[float], label: str) -> dict:
        """训练分时: 优先真实 1 分钟数据, 不可得则返回空数据样式 (双盲时掩码日期)"""
        return build_intraday(code, b, prev_close, label, bool(self.code_revealed or self.non_blind))

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
        # 非双盲(需求7) 与揭晓后同样展示名称/日期; bars 仍按是否揭晓决定是否回传完整序列
        reveal_meta = bool(self.code_revealed or not hide_meta or self.non_blind)
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
            # 需求101701: 前端分时预加载缓存的版本令牌需要它 —— 周/月/年线「当前部分周期桶」
            # 的 OHLC 随已推进交易日数变化(bars_used 可能在桶内多日不变), 用 days_used 区分。
            "days_used": self.days_used,
            "days_total": self.days_total,
            "initial_capital": self.initial_capital,
            "cash": round(self.cash, 2),
            "pos": self.pos,
            "locked_qty": self._locked_qty,          # T+1: 当日买入锁定, 当日不可卖
            "sellable_qty": max(0, self.pos - self._locked_qty) if self.pos > 0 else 0,
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
            # 需求4: 待成交买入委托 (挂单模型) + 最近一次成交/失败记录
            "pending_buy": (dict(self._pending_buy) if self._pending_buy else None),
            "last_fill": self.last_fill,
            # 需求7: 非双盲(指定个股+起始日期) —— 名称与日期均可见
            "non_blind": self.non_blind,
            # 双盲: 只回传「已走完 + 当前部分周期」的 K 线, 避免把未来行情泄漏到前端
            "bars": self._display_bars(),
            # 周期视图 MACD (在日线上计算后按周期桶采样, 与 bars 一一对齐; 修复周/月/年线空洞)
            "macd": self.period_macd(),
            # 市场背景: 训练窗口之前的历史K线 (双盲时日期掩码为 #N)
            "pre_count": len(self.pre_bars),
            "pre_bars": (self.pre_bars if reveal_meta
                         else [{**b, "date": f"#{i + 1}"} for i, b in enumerate(self.pre_bars)]),
        }
        if reveal_meta:
            # 训练结束揭晓 / 非双盲: 标的名称 + 训练时间段
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
        # 需求101604: 预生成会话缓存 (key → (ts, session)) —— 点击「开始训练」前就把
        # 抽标/K线切片/首根分时备好, 点击时直接取用, 使 start 退化为「注册 + 落库」
        self._primed: Dict[str, tuple] = {}
        self._prime_lock = threading.Lock()
        self._db_path = Path(__file__).resolve().parent.parent / "data" / "training.db"
        self._db_path.parent.mkdir(exist_ok=True)
        self._init_db()

    def _connect(self):
        """打开训练库连接 (性能优化101701: WAL + synchronous=NORMAL 大幅降低每步落库的 fsync 开销)"""
        conn = sqlite3.connect(str(self._db_path), timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_db(self):
        with self._connect() as conn:
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
            # 迁移: 旧「默认」战法改名为「副本」
            # 注意: 不再自动创建/绑定「副本」——训练未指定战法时 strategy_id 保持 NULL,
            # 界面按「默认(未归类)」展示, 避免战法库凭空冒出一个用户没建过的战法。
            c.execute("UPDATE strategies SET name='副本' WHERE name='默认'")
            # 收益曲线重置记录: 每个账号一行, 记录重置次数与最近一次重置时间
            c.execute("""CREATE TABLE IF NOT EXISTS review_resets (
                user_id TEXT PRIMARY KEY, reset_count INTEGER DEFAULT 0, last_reset_at TEXT
            )""")
            # 收益曲线重置锚点明细: 每次重置留一条记录, 曲线「保留历史 + 收益率归 0」靠它定位
            c.execute("""CREATE TABLE IF NOT EXISTS review_reset_log (
                user_id TEXT, seq INTEGER, reset_at TEXT, PRIMARY KEY (user_id, seq)
            )""")
            # 训练划线数据: 按会话持久化, 供复盘历史对局回放时同步渲染 (需求3)
            c.execute("""CREATE TABLE IF NOT EXISTS training_annos (
                session_id TEXT PRIMARY KEY, payload TEXT, updated_at TEXT
            )""")
            conn.commit()

    # ---------- 启动 ----------
    @staticmethod
    def _norm_args(category: str, bars_count: int, code: Optional[str],
                   initial_capital: float, leverage: Optional[float]) -> tuple:
        """归一化开局参数 (start/prime 共用, 保证两边缓存键一致)

        注意: code 保持原值 (None 表示随机抽标), 不做 None→"" 转换 —— 构建路径依赖
        ``code is None`` 判定是否随机抽取。
        """
        if category not in CATEGORY_FETCHERS:
            raise ValueError(f"未知品类 {category}, 支持 {list(CATEGORY_FETCHERS.keys())}")
        bars_count = min(max(bars_count, MIN_BARS), CATEGORY_BARS_CAP.get(category, 250))
        if leverage is None:
            leverage = CATEGORY_LEVERAGE.get(category, 1.0)
        return bars_count, code, float(initial_capital), float(leverage)

    def _prime_key(self, category: str, bars_count: int, code: Optional[str],
                   initial_capital: float, leverage: Optional[float],
                   strategy_id: Optional[int], user_id: str, period: str,
                   start_date: Optional[str]) -> str:
        """预生成会话的缓存键 (同参数即可复用同一份预生成数据)"""
        bn, cd, cap, lev = self._norm_args(category, bars_count, code, initial_capital, leverage)
        return json.dumps([category, bn, (cd or ""), cap, lev, strategy_id,
                           user_id or "default", period or "day", (start_date or "")],
                          sort_keys=True)

    def _take_primed(self, key: str) -> Optional[TrainingSession]:
        """取用预生成会话 (超过保鲜期即丢弃)"""
        with self._prime_lock:
            hit = self._primed.pop(key, None)
        if not hit:
            return None
        ts, session = hit
        if time.time() - ts > _PRIME_TTL:
            return None
        return session

    def prime(self, category: str, bars_count: int = 100,
              code: Optional[str] = None, initial_capital: float = 100_000.0,
              leverage: Optional[float] = None, strategy_id: Optional[int] = None,
              user_id: str = "default", period: str = "day",
              limit_up_weights: Optional[List[float]] = None,
              start_date: Optional[str] = None) -> Optional[TrainingSession]:
        """需求101604: 预生成一局训练「备用」, 点击开始训练时直接取用

        与 start() 走同一构建路径, 但 **不注册活动会话、不写库** —— 未被消费的预生成
        数据随 TTL/上限自然淘汰, 不会污染训练历史。返回 None 表示预生成失败 (不影响 start)。
        """
        key = self._prime_key(category, bars_count, code, initial_capital, leverage,
                              strategy_id, user_id, period, start_date)
        with self._prime_lock:                       # 同参数已在预热中 → 直接复用
            hit = self._primed.get(key)
        if hit and time.time() - hit[0] <= _PRIME_TTL:
            return hit[1]
        try:
            session = self._build(category, bars_count=bars_count, code=code,
                                  initial_capital=initial_capital, leverage=leverage,
                                  strategy_id=strategy_id, user_id=user_id, period=period,
                                  limit_up_weights=limit_up_weights, start_date=start_date)
        except Exception as e:
            print(f"[Training] 预生成失败 {category}: {e}")
            return None
        try:                                          # 预热首根 K 线的当日分时
            session.bar_intraday(0)
        except Exception:
            pass
        with self._prime_lock:
            self._primed[key] = (time.time(), session)
            if len(self._primed) > _PRIME_MAX:        # 超上限: 丢弃最旧的一条
                oldest = min(self._primed, key=lambda k: self._primed[k][0])
                self._primed.pop(oldest, None)
        return session

    def start(self, category: str, bars_count: int = 100,
              code: Optional[str] = None, initial_capital: float = 100_000.0,
              leverage: Optional[float] = None, strategy_id: Optional[int] = None,
              user_id: str = "default", period: str = "day",
              limit_up_weights: Optional[List[float]] = None,
              start_date: Optional[str] = None) -> TrainingSession:
        """开始一局训练 —— 优先取用预生成会话 (需求101604), 未命中才现场构建

        code=None → 随机挑一只符合品类的标的, 从全量 K 线末尾切 bars_count 根
        code 有值 → 用指定 code 的全量 K 线末尾切 bars_count 根
        code + start_date → 非双盲训练: 从 start_date 起切 bars_count 根 (名称与日期可见);
                            起始日期之后不足 bars_count 时按可用根数收缩 (最少 MIN_BARS 根)

        limit_up_weights: 随机抽标的时的「涨停次数档位权重」(无/低/中/高, 长度4),
                          缺省等权; 仅 code=None 时生效
        """
        key = self._prime_key(category, bars_count, code, initial_capital, leverage,
                              strategy_id, user_id, period, start_date)
        session = self._take_primed(key)
        if session is None:
            session = self._build(category, bars_count=bars_count, code=code,
                                  initial_capital=initial_capital, leverage=leverage,
                                  strategy_id=strategy_id, user_id=user_id, period=period,
                                  limit_up_weights=limit_up_weights, start_date=start_date)
        self._sessions[session.session_id] = session
        # 立即存一份骨架
        self._persist_session(session)
        return session

    def _build(self, category: str, bars_count: int = 100,
               code: Optional[str] = None, initial_capital: float = 100_000.0,
               leverage: Optional[float] = None, strategy_id: Optional[int] = None,
               user_id: str = "default", period: str = "day",
               limit_up_weights: Optional[List[float]] = None,
               start_date: Optional[str] = None) -> TrainingSession:
        """纯构建一局训练会话 (抽标 → 切窗 → 组装), 不注册活动会话、不写库"""
        bars_count, code, initial_capital, leverage = self._norm_args(
            category, bars_count, code, initial_capital, leverage)
        user_specified = bool(code)          # 用户指定标的 (训练中可展示名称)
        # strategy_id 保持 None 时不绑定任何战法 → 复盘/统计按「默认(未归类)」归集

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
            # 指定起始日期时窗口由 start_date 决定, 不要求尾部再留 30 根背景
            if not start_date and len(klines) < bars_count + 30:
                raise ValueError(f"{code} K线只有 {len(klines)} 条, 不够训练")
        # 起始窗口: 指定起始日期(非双盲) 或 随机切一段 (开头保留 ≥30 根市场背景, 末尾留足训练根数)
        non_blind = False
        if code is not None and start_date:
            sd = str(start_date)[:10]
            try:
                datetime.strptime(sd, "%Y-%m-%d")
            except ValueError:
                raise ValueError("起始日期格式须为 YYYY-MM-DD")
            idx = next((i for i, k in enumerate(klines) if str(k["date"])[:10] >= sd), None)
            if idx is None:
                raise ValueError(f"起始日期 {sd} 晚于该标的全部K线, 请调整日期")
            available = len(klines) - idx          # 起始日期之后实际可用根数
            if available < MIN_BARS:
                raise ValueError(
                    f"起始日期 {sd} 之后仅 {available} 根K线, 不足单局最少 {MIN_BARS} 根, 请调整日期或改选标的")
            bars_count = min(bars_count, available)   # 自动收缩训练窗口, 不再直接报错
            start_idx = idx
            non_blind = True
        else:
            import random as _rnd
            start_idx = _rnd.randint(30, len(klines) - bars_count)
        bars = klines[start_idx:][:bars_count]

        # 市场背景: 训练窗口之前的近 250 根历史K线 (双盲时显示掩码日期)
        pre_count = min(250, start_idx)
        pre_start = max(0, start_idx - pre_count)
        pre_bars = _augment_amount(klines[pre_start:start_idx], category)
        # 指标预热: 背景之前再多留一段日K (仅用于周/月/年线 MACD 的 EMA 收敛, 不展示)
        warm_count = min(WARMUP_CAP, pre_start)
        warmup_bars = _augment_amount(klines[pre_start - warm_count:pre_start], category) if warm_count else []

        session_id = str(uuid.uuid4())[:12]
        session = TrainingSession(session_id, category, code, bars, initial_capital,
                                  leverage=leverage, strategy_id=strategy_id, pre_bars=pre_bars,
                                  user_id=user_id, period=period,
                                  code_name=self._resolve_code_name(code),
                                  user_specified=user_specified,
                                  limit_up_count=lu_count, limit_up_bucket=lu_bucket,
                                  warmup_bars=warmup_bars, non_blind=non_blind)
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

    

    # ---------- 动作 ----------
    def get(self, session_id: str) -> Optional[TrainingSession]:
        return self._sessions.get(session_id)

    def buy(self, session_id: str, qty: Optional[int] = None,
            ratio: Optional[float] = None) -> TrainingSession:
        """挂出买入委托 (需求4): 决策根不落库, 待 advance 于次日开盘成交后回写成交根"""
        s = self.get(session_id); assert s is not None
        s.buy(qty, ratio); self._persist_session(s); return s

    def sell(self, session_id: str, qty: Optional[int] = None, allow_short: bool = False) -> TrainingSession:
        s = self.get(session_id); assert s is not None
        s.sell(qty, allow_short); s.last_action = "SELL"; self._persist_bar(s); self._persist_session(s); return s

    def hold(self, session_id: str) -> TrainingSession:
        s = self.get(session_id); assert s is not None
        s.hold(); s.last_action = "HOLD"
        # 性能优化(101701): bar 快照 + 会话行合并为一次事务
        conn = self._connect()
        try:
            self._persist_bar(s, conn=conn)
            self._persist_session(s, conn=conn)
            conn.commit()
        finally:
            conn.close()
        return s

    def hold_and_advance(self, session_id: str) -> dict:
        """观望并立即推进一根K线 (合并 hold + advance)

        性能优化(101701): 前端「观望」原先串行发 hold→advance 两次请求, 且同一根K
        的决策快照会被两次落库。合并后只需一次请求、一次落库, 返回与 advance 同结构。
        """
        s = self.get(session_id); assert s is not None
        s.hold()                      # 校验会话可决策 (终态直接抛错)
        s.last_action = "HOLD"
        return self.advance(session_id)

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
        """推进一根 K, 同时决定 auto-settle / 条件单触发 / 买入委托成交"""
        s = self.get(session_id); assert s is not None
        # 性能优化(101701): 一次连接一次提交完成本步全部落库 (原先 2~3 次 connect+commit)
        conn = self._connect()
        try:
            self._persist_bar(s, conn=conn)      # 落库当前这根 K 的决策 (含纯观望), 保证权益曲线完整
            result = s.advance()
            # 条件单触发 → 用触发动作(TP/SL)覆盖该根 K 的快照, 保证复盘与内存一致
            if result.get("triggered"):
                self._persist_bar(s, conn=conn, action=result["last_action"], bar_index=result["bar_index"])
            # 买入委托于该根K线开盘价成交 → 回写成交根 (BUY/COVER + 成交价)
            pf = result.get("pending_filled")
            if pf and pf.get("action") in ("BUY", "COVER"):
                self._persist_bar(s, conn=conn, action=pf["action"], bar_index=pf["bar_index"],
                                  action_price=pf.get("price"))
            self._persist_session(s, conn=conn)
            if s.status in TERMINAL_STATUSES:   # settled / blown_up 均归档 (并入同一事务)
                self._finalize_session(s, conn=conn)
            conn.commit()
        finally:
            conn.close()
        return {"session": s.summary(), "auto_settled": result.get("settled", False),
                "triggered": result.get("triggered", []),
                "pending_filled": pf,
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
    def _persist_session(self, s: TrainingSession, conn=None):
        """写入会话行; conn 传入时复用外部连接且不提交 (供一次事务批量落库)"""
        own = conn is None
        conn = conn if conn is not None else self._connect()
        try:
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
            if own:
                conn.commit()
        finally:
            if own:
                conn.close()

    _TRADE_ACTIONS = ("BUY", "SELL", "COVER", "SHORT", "SHORT_ADD", "TP", "SL")

    def _persist_bar(self, s: TrainingSession, action: Optional[str] = None,
                     bar_index: Optional[int] = None, action_price: Optional[float] = None,
                     conn=None):
        """持久化当前 bar 的决策快照 (action 缺省取本根 K 的决策动作)

        bar_index 显式指定时用于「条件单触发后回写该根 K 的实际持仓变化」,
        以及「买入委托于次日开盘成交后回写成交根」(action_price = 成交价)。
        已落库的成交记录不会被后续的观望(HOLD)覆盖。
        conn 传入时复用外部连接且不提交 (供一次事务批量落库)。
        """
        vis = s.bars                              # 当前周期的可见K线 (周/月/年=已走完的桶)
        idx = s.bars_used if bar_index is None else bar_index
        allb = s._all_bars()
        if idx < len(vis):
            bar = vis[idx]
        elif idx < len(allb):
            # 周/月/年线: 当前桶尚未走完, 该桶不在可见序列里 → 取完整桶存档 (决策价另取实时价)
            bar = allb[idx]
        else:
            return
        action = action or getattr(s, "last_action", "HOLD")
        # 决策(买卖)成交价 = 决策时刻的「当前交易日」收盘价; 并未用整桶收盘价
        price = action_price if action_price is not None else (
            s.current_price if s.current_price is not None else bar["close"])
        own = conn is None
        conn = conn if conn is not None else self._connect()
        try:
            c = conn.cursor()
            if action == "HOLD":
                # 该根已有成交记录(BUY/SELL/条件单) → 不用后续观望覆盖
                row = c.execute("SELECT action FROM training_bars WHERE session_id=? AND bar_index=?",
                                (s.session_id, idx)).fetchone()
                if row and row[0] in self._TRADE_ACTIONS:
                    return
            c.execute("""INSERT OR REPLACE INTO training_bars
                (session_id, bar_index, date, open, high, low, close, volume,
                 action, action_price, pos_before, cash_before, pnl_before,
                 pos_after, cash_after, pnl_after, equity)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (s.session_id, idx, bar["date"], bar["open"], bar["high"], bar["low"], bar["close"], bar["volume"],
                 action, price, s.pos, s.cash, s.pnl, s.pos, s.cash, s.pnl,
                 round(s._equity(price), 2)))
            if own:
                conn.commit()
        finally:
            if own:
                conn.close()

    def _finalize_session(self, s: TrainingSession, conn=None):
        own = conn is None
        conn = conn if conn is not None else self._connect()
        try:
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
            if own:
                conn.commit()
        finally:
            if own:
                conn.close()
        # 从内存移除 (已 settle/abandoned)
        self._sessions.pop(s.session_id, None)

    # ---------- 训练划线 (需求3: 持久化, 供复盘历史对局同步) ----------
    def save_annos(self, session_id: str, payload) -> int:
        """保存某会话的划线数据 (整体覆盖; payload 为划线数组)"""
        data = payload if isinstance(payload, list) else []
        with sqlite3.connect(str(self._db_path)) as conn:
            conn.execute("""INSERT OR REPLACE INTO training_annos (session_id, payload, updated_at)
                            VALUES (?,?,?)""",
                         (session_id, json.dumps(data, ensure_ascii=False),
                          datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            conn.commit()
        return len(data)

    def get_annos(self, session_id: str) -> List[dict]:
        """读取某会话的划线数据 (不存在返回空数组)"""
        with sqlite3.connect(str(self._db_path)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT payload FROM training_annos WHERE session_id=?",
                               (session_id,)).fetchone()
        if not row or not row["payload"]:
            return []
        try:
            data = json.loads(row["payload"])
            return data if isinstance(data, list) else []
        except Exception:
            return []

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

    def _reset_points(self, conn, user_id: str) -> List[str]:
        """该账号的全部重置时间点 (按重置先后正序)"""
        rows = conn.execute("SELECT reset_at FROM review_reset_log WHERE user_id=? ORDER BY seq",
                            (user_id,)).fetchall()
        return [r["reset_at"] for r in rows if r["reset_at"]]

    @staticmethod
    def _anchor(reset_at: str, label: str) -> dict:
        """曲线锚点: 净值 100 / 收益率 0% (起点 或 一次重置)"""
        return {"session_id": None, "created_at": reset_at, "code": "", "code_name": "",
                "pnl_pct": 0.0, "nav": 100.0, "is_reset": True, "label": label}

    def equity_nav(self, user_id: str = "default") -> dict:
        """账号收益曲线: 净值比例复利加权 (基准100)

        - 曲线完整保留历史: 首部固定「起点」锚点(100/0%), 每次重置再插入一个「重置」锚点(100/0%),
          即「重置只把收益率归 0, 不清理之前的曲线数据」
        - nav = 最近一次重置锚点之后的复利净值; nav <= 0 (出现 -100% 爆仓) 时 can_start=False
        """
        with sqlite3.connect(str(self._db_path)) as conn:
            conn.row_factory = sqlite3.Row
            st = self._reset_state(conn, user_id)
            resets = self._reset_points(conn, user_id)
            rows = conn.execute(
                """SELECT session_id, code, code_name, category, created_at, pnl_pct
                   FROM training_sessions s
                   WHERE s.status IN ('settled','blown_up') AND s.user_id=?
                     AND s.pnl_pct IS NOT NULL
                   ORDER BY s.created_at ASC""", (user_id,)).fetchall()
        nav = 1.0
        curve: List[dict] = [self._anchor("", "起点")]
        ri = 0
        for r in rows:
            # 该局之前发生的重置 → 先落锚点并把收益率归 0 (历史点保留在曲线上)
            while ri < len(resets) and resets[ri] < (r["created_at"] or ""):
                nav = 1.0
                curve.append(self._anchor(resets[ri], f"重置#{ri + 1}"))
                ri += 1
            nav *= (1 + (float(r["pnl_pct"]) or 0) / 100)
            if nav < 0:
                nav = 0.0
            curve.append({
                "session_id": r["session_id"], "created_at": r["created_at"],
                "code": r["code"], "code_name": r["code_name"] or "",
                "pnl_pct": round(float(r["pnl_pct"]) or 0, 2),
                "nav": round(nav * 100, 2), "is_reset": False,
            })
        # 全部记录之后发生的重置 → 落到曲线末尾 (归零后重置可立即恢复可训练)
        while ri < len(resets):
            nav = 1.0
            curve.append(self._anchor(resets[ri], f"重置#{ri + 1}"))
            ri += 1
        return {
            "nav": round(nav * 100, 2),
            "curve": curve,
            "can_start": nav > 0,
            "reset_count": st["reset_count"],
            "last_reset_at": st["last_reset_at"],
        }

    def reset_equity(self, user_id: str = "default") -> dict:
        """重置收益曲线: 收益率归 0 (保留全部训练记录与历史曲线), 并累加重置次数"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with sqlite3.connect(str(self._db_path)) as conn:
            conn.execute(
                """INSERT INTO review_resets (user_id, reset_count, last_reset_at) VALUES (?,1,?)
                   ON CONFLICT(user_id) DO UPDATE SET
                     reset_count = reset_count + 1, last_reset_at = excluded.last_reset_at""",
                (user_id, now))
            seq = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM review_reset_log WHERE user_id=?",
                               (user_id,)).fetchone()[0]
            conn.execute("INSERT OR REPLACE INTO review_reset_log (user_id, seq, reset_at) VALUES (?,?,?)",
                         (user_id, seq, now))
            conn.commit()
        return self.equity_nav(user_id)

    def get_history(self, session_id: str) -> Optional[dict]:
        """单局完整回看数据: 训练K线(含每根决策) + 训练前市场背景K线 + 权益曲线

        训练K线以「完整序列」为基准, 再用落库的决策快照按 bar_index 覆盖:
        - 已结算/爆仓/放弃 → 结算快照 settlement_json["bars"] (与结算瞬间展示一致)
        - 进行中 → 内存会话的可见K线; 内存已释放时退回落库稀疏K线 (老数据兜底)
        这样「整局没有任何操作」也能看到完整K线 (原先只返回有决策的稀疏行, 前端不渲染)。
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
            decisions = {r["bar_index"]: dict(r) for r in c.fetchall()}
            result = dict(sess)
            result["status_label"] = STATUS_LABELS.get(result.get("status"), result.get("status"))
            if not result.get("code_name"):     # 老记录回填股票名称
                result["code_name"] = self._resolve_code_name(result.get("code") or "")
        # 结算快照 (含完整K线 + 训练前背景K线)
        snap = None
        raw = result.get("settlement_json")
        if raw:
            try:
                snap = json.loads(raw)
            except (ValueError, TypeError):
                snap = None
        # 1) 完整K线基准: 结算快照 → 内存会话 → 落库稀疏行
        base = [dict(b) for b in ((snap or {}).get("bars") or [])]
        if not base:
            mem = self.get(session_id)
            if mem is not None:
                base = [dict(b) for b in mem._display_bars()]
        if not base:
            base = [decisions[i] for i in sorted(decisions)]
        # 2) 决策快照按 bar_index 覆盖 (含纯观望), 无决策的K线补 HOLD 默认值
        bars = []
        for i, b in enumerate(base):
            d = decisions.get(i) or {}
            row = dict(b)
            row["action"] = d.get("action") or "HOLD"
            row["action_price"] = d.get("action_price")
            row["pos_before"] = d.get("pos_before")
            row["cash_before"] = d.get("cash_before")
            row["pnl_before"] = d.get("pnl_before")
            row["pos_after"] = d.get("pos_after")
            row["cash_after"] = d.get("cash_after")
            row["pnl_after"] = d.get("pnl_after")
            row["equity"] = d.get("equity")
            bars.append(row)
        result["bars"] = bars
        # 权益曲线: 优先用落库的每根权益快照 (空头/杠杆均准确), 缺值回退旧公式
        result["equity_curve"] = [
            b["equity"] if b.get("equity") is not None
            else (b["cash_after"] or 0) + (b["pos_after"] or 0) * b["close"]
            for b in bars
        ]
        result["actions_count"] = sum(1 for b in bars if b["action"] in ("BUY", "SELL"))
        # 训练前市场背景K线 (来自结算快照)
        result["pre_bars"] = list((snap or {}).get("pre_bars") or [])
        result["pre_count"] = len(result["pre_bars"])
        # 需求3: 训练划线数据随对局同步, 复盘历史回看时一并渲染
        result["annos"] = self.get_annos(session_id)
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
            # 过滤系统遗留「副本」: 仅在它没有任何训练记录关联时隐藏 (有记录则保留标签, 不丢数据)
            rows = conn.execute("""
                SELECT * FROM strategies s
                WHERE NOT (s.name='副本' AND s.description='系统默认战法'
                           AND NOT EXISTS (SELECT 1 FROM training_sessions t WHERE t.strategy_id = s.id))
                ORDER BY id""").fetchall()
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
            items = grouped.pop(st["id"], [])
            # 系统遗留「副本」且无任何记录 → 不展示 (避免凭空出现一个用户没建过的战法)
            if st["name"] == "副本" and st.get("description") == "系统默认战法" and not items:
                continue
            out.append({**st, **_agg(items, st["id"])})
        # 未归类 (strategy_id=NULL) → 展示为「默认」(不可删除)
        unassigned = grouped.pop(None, [])
        if unassigned:
            out.append({"id": None, "name": "默认", "color": "#8892b0",
                        **_agg(unassigned, None)})
        return out


# 单例
training_mgr = TrainingManager()
