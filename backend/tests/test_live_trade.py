"""模拟交易 (实时撮合) 单元测试

运行: cd backend && .venv/bin/python3 -m pytest tests/test_live_trade.py -q
"""
from __future__ import annotations
import copy
import sys
from datetime import datetime, date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from app.models import KLine
from app.core.config import settings
from app.services.live_trade import LiveTradeManager


@pytest.fixture(autouse=True)
def _allow_trading_anytime(monkeypatch):
    """默认关闭交易时间门禁, 使既有撮合用例可在任意时刻运行

    门禁本身的用例在 TestTradingGate 中显式开启并单独验证。
    """
    monkeypatch.setattr(settings, "ENFORCE_TRADING_TIME", False, raising=False)


# ---------- 测试替身 ----------

def mk(date_str: str, close: float, volume: int, amount: float, o=None) -> KLine:
    o = close if o is None else o
    return KLine(open=o, high=max(o, close), low=min(o, close), close=close,
                 volume=volume, amount=amount,
                 timestamp=datetime.strptime(date_str, "%Y-%m-%d"))


class FakeFeed:
    def __init__(self, klines, days, market_open=True):
        self.klines = klines
        self.days = days
        self.market_open = market_open

    def get_kline(self, code):
        return self.klines.get(code, [])

    def get_trading_days(self):
        return self.days

    def market_ref_day(self):
        """返回 (基准日, 是否开市); 供交易时间门禁判定"""
        return (self.days[-1] if self.days else None), self.market_open


class FakeRegistry:
    def get(self, code):
        return {"code": code, "name": "测试股" + code[-2:], "is_kcb": 0}


D1, D2, D3 = date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)

BARS = {
    # 大成交额: 12 亿 → 5% 上限 6000 万
    "600519": [
        mk("2026-01-05", 100.0, 1_000_000, 1_000_000_000.0),
        mk("2026-01-06", 105.0, 1_200_000, 1_200_000_000.0),
    ],
    # 小成交额: 10 万 → 5% 上限 5000
    "000001": [
        mk("2026-01-05", 50.0, 2_000, 100_000.0),
        mk("2026-01-06", 50.0, 2_000, 100_000.0),
    ],
    # 停牌 (成交量为 0)
    "600000": [
        mk("2026-01-05", 10.0, 100_000, 1_000_000.0),
        mk("2026-01-06", 10.0, 0, 0.0),
    ],
}


@pytest.fixture
def feed():
    return FakeFeed(copy.deepcopy(BARS), [D1, D2])


def advance_day(feed, code="600519", close=105.0):
    """交易日前进到 D3 并补上当日 K 线 (触发 T+1 懒结算)"""
    feed.days.append(D3)
    feed.klines[code].append(mk("2026-01-07", close, 1_200_000, 1_200_000_000.0))


@pytest.fixture
def mgr(tmp_path, feed):
    LiveTradeManager._instance = None
    m = LiveTradeManager(feed, FakeRegistry(), db_path=tmp_path / "live.db")
    return m


# ================= 资金体系 =================

class TestTiers:
    def test_tiers_include_large(self, mgr):
        tiers = {t["key"]: t for t in mgr.list_tiers()}
        assert set(tiers) == {"basic", "pro", "vip", "large"}
        assert tiers["large"]["initial_cash"] == 50_000_000.0
        assert tiers["basic"]["initial_cash"] == 1_000_000.0

    def test_default_tier_is_basic(self, mgr):
        assert mgr.get_tier("u1") == "basic"
        assert mgr.get_account("u1")["cash"] == 1_000_000.0

    def test_bind_tier_does_not_reset_account(self, mgr):
        """切换资金体系不重置: 各体系账户独立且数据保留"""
        # 基础体系买入
        mgr.place_order("u1", "600519", "BUY", "MARKET", quantity=100)
        basic_before = mgr.get_account("u1")
        assert basic_before["tier"] == "basic"
        assert len(basic_before["positions"]) == 1
        assert basic_before["cash"] < 1_000_000.0

        # 切到大户 → 5000 万且无基础体系持仓 (独立)
        mgr.bind_tier("u1", "large")
        large = mgr.get_account("u1")
        assert large["tier"] == "large"
        assert large["cash"] == 50_000_000.0
        assert large["positions"] == []

        # 大户体系独立买入
        mgr.place_order("u1", "600519", "BUY", "MARKET", quantity=200)
        assert len(mgr.get_account("u1")["positions"]) == 1

        # 切回基础 → 基础体系数据仍在 (未重置)
        mgr.bind_tier("u1", "basic")
        basic_after = mgr.get_account("u1")
        assert basic_after["cash"] == basic_before["cash"]
        assert len(basic_after["positions"]) == 1
        assert basic_after["positions"][0]["quantity"] == 100

    def test_bind_unknown_tier_rejected(self, mgr):
        with pytest.raises(ValueError):
            mgr.bind_tier("u1", "huge")


# ================= 行情快照 =================

class TestSnapshot:
    def test_snapshot_fields(self, mgr):
        s = mgr.market_snapshot("600519")
        assert s["price"] == 105.0
        assert s["prev_close"] == 100.0
        assert s["limit_up"] == 110.0
        assert s["limit_down"] == 90.0
        # 单笔上限基准 = 上一交易日(D1)成交额 10 亿 → 5% = 5000 万
        assert s["basis_amount"] == 1_000_000_000.0
        assert s["basis_date"] == "2026-01-05"
        assert s["max_order_amount"] == round(1_000_000_000.0 * 0.05, 2)
        assert s["at_limit_up"] is False
        assert s["at_limit_down"] is False
        assert s["suspended"] is False

    def test_at_limit_up_flag(self, mgr, feed):
        """收盘价 = 涨停价 → at_limit_up 为真"""
        feed.klines["600519"][-1].close = 110.0     # 100 * 1.1
        s = mgr.market_snapshot("600519")
        assert s["at_limit_up"] is True

    def test_basis_falls_back_when_prev_amount_zero(self, mgr):
        """上一交易日成交额为 0 时回退到末根成交额"""
        feed = FakeFeed({"600519": [
            mk("2026-01-05", 100.0, 0, 0.0),
            mk("2026-01-06", 105.0, 1_200_000, 1_200_000_000.0),
        ]}, [D1, D2])
        LiveTradeManager._instance = None
        m = LiveTradeManager(feed, FakeRegistry(), db_path=mgr.db_path.parent / "b.db")
        assert m.market_snapshot("600519")["max_order_amount"] == round(1_200_000_000.0 * 0.05, 2)

    def test_suspended_flag(self, mgr):
        assert mgr.market_snapshot("600000")["suspended"] is True

    def test_amount_fallback_to_recent_average(self, mgr):
        """末根成交额为 0 时回退近 5 根均值"""
        s = mgr.market_snapshot("600000")
        assert s["amount"] == 1_000_000.0      # 回退到近 5 根

    def test_missing_code_returns_none(self, mgr):
        assert mgr.market_snapshot("999999") is None


# ================= 下单 / 撮合 =================

class TestPlaceOrder:
    def test_market_buy_creates_position(self, mgr):
        mgr.place_order("u1", "600519", "BUY", "MARKET", quantity=100)
        acct = mgr.get_account("u1")
        assert len(acct["positions"]) == 1
        p = acct["positions"][0]
        assert p["quantity"] == 100
        assert p["available"] == 0          # T+1: 当日买入不可卖
        assert p["pending_in"] == 100
        assert p["price"] == 105.0
        assert acct["cash"] < 1_000_000.0
        assert acct["market_value"] == 10500.0

    def test_lot_size_must_be_100(self, mgr):
        with pytest.raises(ValueError, match="100"):
            mgr.place_order("u1", "600519", "BUY", "MARKET", quantity=150)

    def test_limit_price_out_of_range(self, mgr):
        with pytest.raises(ValueError, match="涨跌停"):
            mgr.place_order("u1", "600519", "BUY", "LIMIT", price=120.0, quantity=100)

    def test_suspended_rejected(self, mgr):
        with pytest.raises(ValueError, match="停牌"):
            mgr.place_order("u1", "600000", "BUY", "MARKET", quantity=100)

    def test_ratio_converts_to_lots(self, mgr):
        """ratio=1 (满仓) 用全部可用资金换算手数"""
        o = mgr.place_order("u1", "600519", "BUY", "MARKET", ratio=1.0)
        assert o["quantity"] > 0 and o["quantity"] % 100 == 0
        assert o["status"] == "FILLED"

    def test_insufficient_cash(self, mgr):
        with pytest.raises(ValueError, match="资金不足"):
            mgr.place_order("u1", "600519", "BUY", "MARKET", quantity=100_000)


class TestFivePercentRule:
    def test_single_order_over_5pct_rejected(self, mgr):
        """10 万成交额 → 上限 5000 元; 200 股 × 50 = 10000 > 5000"""
        with pytest.raises(ValueError, match="5%"):
            mgr.place_order("u1", "000001", "BUY", "MARKET", quantity=200)

    def test_order_at_cap_allowed(self, mgr):
        o = mgr.place_order("u1", "000001", "BUY", "MARKET", quantity=100)
        assert o["status"] == "FILLED"
        assert o["amount"] == 5000.0

    def test_cumulative_same_direction_rejected(self, mgr):
        """防拆单: 当日同码同向累计成交额 + 本单 > 上限则拒绝"""
        mgr.place_order("u1", "000001", "BUY", "MARKET", quantity=100)
        with pytest.raises(ValueError, match="累计"):
            mgr.place_order("u1", "000001", "BUY", "MARKET", quantity=100)

    def test_zero_amount_rejected(self, mgr):
        """成交额原样为 0 且无历史可兜底 → 拒单"""
        feed = FakeFeed({"000002": [mk("2026-01-05", 10.0, 0, 0.0)]}, [D1, D2])
        LiveTradeManager._instance = None
        m = LiveTradeManager(feed, FakeRegistry(), db_path=mgr.db_path.parent / "z.db")
        with pytest.raises(ValueError):
            m.place_order("u1", "000002", "BUY", "MARKET", quantity=100)


# ================= 交易时间门禁 (需求1) =================

class TestTradingGate:
    def test_closed_day_blocks_order(self, tmp_path, monkeypatch):
        """非交易日 → 下单失败, 不撮合"""
        monkeypatch.setattr(settings, "ENFORCE_TRADING_TIME", True, raising=False)
        feed = FakeFeed(copy.deepcopy(BARS), [D1, D2], market_open=False)
        LiveTradeManager._instance = None
        m = LiveTradeManager(feed, FakeRegistry(), db_path=tmp_path / "gate.db")
        with pytest.raises(ValueError, match="非交易时间"):
            m.place_order("u1", "600519", "BUY", "MARKET", quantity=100)
        assert m.get_account("u1")["positions"] == []

    def test_outside_session_blocks_order(self, tmp_path, monkeypatch):
        """交易日但当前不在交易时段 → 下单失败"""
        monkeypatch.setattr(settings, "ENFORCE_TRADING_TIME", True, raising=False)
        monkeypatch.setattr(settings, "MORNING_OPEN", "09:30", raising=False)
        monkeypatch.setattr(settings, "MORNING_CLOSE", "09:31", raising=False)
        monkeypatch.setattr(settings, "AFTERNOON_OPEN", "13:00", raising=False)
        monkeypatch.setattr(settings, "AFTERNOON_CLOSE", "13:01", raising=False)
        feed = FakeFeed(copy.deepcopy(BARS), [D1, D2], market_open=True)
        LiveTradeManager._instance = None
        m = LiveTradeManager(feed, FakeRegistry(), db_path=tmp_path / "gate2.db")
        if m._is_market_open():     # 恰好在测试时段内运行时跳过 (极低概率)
            pytest.skip("当前处于交易时段")
        with pytest.raises(ValueError, match="非交易时间"):
            m.place_order("u1", "600519", "BUY", "MARKET", quantity=100)

    def test_in_session_allows_order(self, tmp_path, monkeypatch):
        """交易日且在交易时段内 → 正常成交"""
        monkeypatch.setattr(settings, "ENFORCE_TRADING_TIME", True, raising=False)
        monkeypatch.setattr(settings, "MORNING_OPEN", "00:00", raising=False)
        monkeypatch.setattr(settings, "MORNING_CLOSE", "23:59", raising=False)
        feed = FakeFeed(copy.deepcopy(BARS), [D1, D2], market_open=True)
        LiveTradeManager._instance = None
        m = LiveTradeManager(feed, FakeRegistry(), db_path=tmp_path / "gate3.db")
        o = m.place_order("u1", "600519", "BUY", "MARKET", quantity=100)
        assert o["status"] == "FILLED"

    def test_closed_day_no_pending_fill(self, mgr, monkeypatch, feed):
        """闭市不撮合挂单"""
        o = mgr.place_order("u1", "600519", "BUY", "LIMIT", price=95.0, quantity=100)
        assert o["status"] == "PENDING"
        monkeypatch.setattr(settings, "ENFORCE_TRADING_TIME", True, raising=False)
        feed.market_open = False
        feed.klines["600519"][-1].close = 93.0
        assert mgr.try_fill_pending("u1") == []

    def test_gate_disabled_returns_true(self, mgr, monkeypatch, feed):
        monkeypatch.setattr(settings, "ENFORCE_TRADING_TIME", False, raising=False)
        feed.market_open = False
        assert mgr._is_market_open() is True


# ================= 涨停板 (需求3) =================

class TestLimitUp:
    def test_buy_at_limit_up_rejected(self, mgr, feed):
        feed.klines["600519"][-1].close = 110.0        # 相对昨收 100 封涨停
        assert mgr.market_snapshot("600519")["at_limit_up"] is True
        with pytest.raises(ValueError, match="涨停"):
            mgr.place_order("u1", "600519", "BUY", "MARKET", quantity=100)

    def test_sell_at_limit_up_not_blocked(self, mgr, feed):
        """涨停仅限制买入; 持仓仍可正常卖出"""
        mgr.place_order("u1", "600519", "BUY", "MARKET", quantity=100)
        advance_day(feed)                              # D3 close 105, available=100
        feed.klines["600519"][-1].close = 115.5        # D3 相对 D2(105) 封涨停
        assert mgr.market_snapshot("600519")["at_limit_up"] is True
        o = mgr.place_order("u1", "600519", "SELL", "MARKET", quantity=100)
        assert o["status"] == "FILLED"


# ================= 停牌 (需求2) =================

class TestSuspendedSell:
    def test_sell_suspended_rejected(self, mgr):
        """收盘停牌个股不可卖出"""
        assert mgr.market_snapshot("600000")["suspended"] is True
        with pytest.raises(ValueError, match="停牌"):
            mgr.place_order("u1", "600000", "SELL", "MARKET", quantity=100)


# ================= 限价挂单 / 懒撮合 / 撤单 =================

class TestPendingOrders:
    def test_limit_buy_below_market_stays_pending(self, mgr):
        o = mgr.place_order("u1", "600519", "BUY", "LIMIT", price=95.0, quantity=100)
        assert o["status"] == "PENDING"
        acct = mgr.get_account("u1")
        assert acct["frozen_cash"] > 0
        assert acct["cash"] == round(1_000_000.0 - o["frozen_amount"], 2)

    def test_pending_fills_when_price_drops(self, mgr, feed):
        o = mgr.place_order("u1", "600519", "BUY", "LIMIT", price=95.0, quantity=100)
        assert o["status"] == "PENDING"
        feed.klines["600519"][-1].close = 93.0      # 现价跌到限价内
        filled = mgr.try_fill_pending("u1")
        assert len(filled) == 1
        assert filled[0]["price"] == 93.0
        assert mgr.get_account("u1")["frozen_cash"] == 0.0

    def test_cancel_refunds_frozen_cash(self, mgr):
        o = mgr.place_order("u1", "600519", "BUY", "LIMIT", price=95.0, quantity=100)
        assert mgr.cancel_order("u1", o["order_id"]) is True
        acct = mgr.get_account("u1")
        assert acct["frozen_cash"] == 0.0
        assert acct["cash"] == 1_000_000.0

    def test_limit_sell_locks_available(self, mgr, feed):
        mgr.place_order("u1", "600519", "BUY", "MARKET", quantity=200)
        advance_day(feed)                           # 次日: 解锁
        o = mgr.place_order("u1", "600519", "SELL", "LIMIT", price=108.0, quantity=100)
        assert o["status"] == "PENDING"             # 现价 105 < 108 不成交
        p = mgr.get_account("u1")["positions"][0]
        assert p["available"] == 100                # 被挂单锁定
        assert p["quantity"] == 200


# ================= T+1 =================

class TestTPlusOne:
    def test_same_day_sell_blocked(self, mgr):
        mgr.place_order("u1", "600519", "BUY", "MARKET", quantity=100)
        with pytest.raises(ValueError, match="可卖数量不足"):
            mgr.place_order("u1", "600519", "SELL", "MARKET", quantity=100)

    def test_sell_allowed_next_trading_day(self, mgr, feed):
        mgr.place_order("u1", "600519", "BUY", "MARKET", quantity=100)
        advance_day(feed)                           # 交易日前进 → 懒结算解锁
        acct = mgr.get_account("u1")
        assert acct["positions"][0]["available"] == 100
        o = mgr.place_order("u1", "600519", "SELL", "MARKET", quantity=100)
        assert o["status"] == "FILLED"
        assert mgr.get_account("u1")["positions"] == []


# ================= 复盘回合 =================

class TestRoundTrip:
    def _open_and_close(self, mgr, feed):
        mgr.place_order("u1", "600519", "BUY", "MARKET", quantity=200)
        advance_day(feed)
        mgr.place_order("u1", "600519", "SELL", "MARKET", quantity=200)

    def test_trip_recorded_with_review_fields(self, mgr, feed):
        self._open_and_close(mgr, feed)
        rows = mgr.list_history("u1")
        assert len(rows) == 1
        r = rows[0]
        assert r["kind"] == "live"
        assert r["category_label"] == "模拟交易"
        assert r["code"] == "600519"
        assert r["start_date"] == "2026-01-06"
        assert r["end_date"] == "2026-01-07"
        assert r["trades_count"] == 2

    def test_detail_normalized_for_review(self, mgr, feed):
        self._open_and_close(mgr, feed)
        trip_id = mgr.list_history("u1")[0]["session_id"]
        d = mgr.get_history("u1", trip_id)
        assert d["trip_id"] == trip_id
        assert d["session_id"] == trip_id
        assert isinstance(d["pre_bars"], list)
        assert len(d["bars"]) >= 2
        assert d["bars"][0]["action"] == "BUY"
        assert d["bars"][-1]["action"] == "SELL"
        for key in ("pnl", "pnl_pct", "win_rate", "max_drawdown", "leverage"):
            assert key in d

    def test_partial_close_then_full_trip_once(self, mgr, feed):
        mgr.place_order("u1", "600519", "BUY", "MARKET", quantity=200)
        advance_day(feed)
        mgr.place_order("u1", "600519", "SELL", "MARKET", quantity=100)
        assert mgr.list_history("u1") == []          # 未清仓, 不生成回合
        mgr.place_order("u1", "600519", "SELL", "MARKET", quantity=100)
        rows = mgr.list_history("u1")
        assert len(rows) == 1
        assert rows[0]["trades_count"] == 3

    def test_trip_meta_strategy_and_note(self, mgr, feed):
        self._open_and_close(mgr, feed)
        trip_id = mgr.list_history("u1")[0]["session_id"]
        assert mgr.set_trip_meta("u1", trip_id, strategy_id=7, note="测试笔记") is True
        d = mgr.get_history("u1", trip_id)
        assert d["strategy_id"] == 7 and d["note"] == "测试笔记"
        assert mgr.set_trip_meta("u1", "nope", note="x") is False

    def test_history_isolated_by_user(self, mgr, feed):
        self._open_and_close(mgr, feed)
        assert len(mgr.list_history("u1")) == 1
        assert mgr.list_history("u2") == []


# ================= 资金体系隔离 =================

class TestTierIsolation:
    def test_history_and_orders_isolated_by_tier(self, mgr, feed):
        """同一用户在 basic 完成的回合, 切到 large 不可见, 切回 basic 仍在"""
        mgr.place_order("u1", "600519", "BUY", "MARKET", quantity=100)
        advance_day(feed)
        mgr.place_order("u1", "600519", "SELL", "MARKET", quantity=100)
        assert len(mgr.list_history("u1")) == 1          # 当前体系 basic

        # 切到大户 → 该体系无回合/委托
        mgr.bind_tier("u1", "large")
        assert mgr.list_history("u1") == []
        assert mgr.list_orders("u1") == []
        assert mgr.list_trades("u1") == []

        # 切回基础 → 回合仍在, 且 user_id 不暴露 #tier
        mgr.bind_tier("u1", "basic")
        rows = mgr.list_history("u1")
        assert len(rows) == 1
        assert rows[0]["code"] == "600519"
        assert rows[0]["user_id"] == "u1"


# ================= 账户重置 =================

class TestReset:
    def test_reset_clears_positions_keeps_history(self, mgr, feed):
        self_open = mgr.place_order("u1", "600519", "BUY", "MARKET", quantity=100)
        assert self_open["status"] == "FILLED"
        advance_day(feed)
        mgr.place_order("u1", "600519", "SELL", "MARKET", quantity=100)
        assert len(mgr.list_history("u1")) == 1
        acct = mgr.reset("u1")
        assert acct["positions"] == []
        assert acct["cash"] == 1_000_000.0
        assert mgr.list_orders("u1") == []
        assert len(mgr.list_history("u1")) == 1       # 复盘记录保留


# ================= 条件单 (止盈 / 止损) =================

def add_bar(feed, code, ds, close, amount=1_200_000_000.0):
    """追加一个交易日并补上当日 K 线"""
    d = datetime.strptime(ds, "%Y-%m-%d").date()
    if d not in feed.days:
        feed.days.append(d)
    feed.klines[code].append(mk(ds, close, 1_200_000, amount))


class TestConditionals:
    def test_requires_correct_side(self, mgr):
        """止盈价须高于现价 (A 股做多)"""
        with pytest.raises(ValueError):
            mgr.set_conditional("u1", "600519", "take_profit", 100.0)   # 现价 105
        c = mgr.set_conditional("u1", "600519", "take_profit", 110.0)
        assert c["status"] == "active"
        assert c["side"] == "long"

    def test_can_set_without_position(self, mgr):
        """不要求先有持仓即可挂条件单"""
        c = mgr.set_conditional("u1", "600519", "stop_loss", 100.0)   # 现价 105
        assert c["status"] == "active"
        assert mgr.get_account("u1")["positions"] == []

    def test_trigger_without_position_skipped(self, mgr, feed):
        """触发但无持仓 → 标记 skipped 并提示, 不产生委托"""
        mgr.set_conditional("u1", "600519", "take_profit", 108.0)
        add_bar(feed, "600519", "2026-01-07", 110.0)      # 现价 110 ≥ 108 → 触发
        c = mgr.list_conditionals("u1")[0]
        assert c["status"] == "skipped"
        assert c["message"]
        assert mgr.get_account("u1")["positions"] == []
        assert mgr.list_trades("u1") == []

    def test_trigger_with_position_sells(self, mgr, feed):
        """有可卖持仓 → 触发即卖出"""
        mgr.place_order("u1", "600519", "BUY", "MARKET", quantity=100)
        advance_day(feed, close=110.0)                     # T+1 → available=100, 现价 110
        mgr.set_conditional("u1", "600519", "take_profit", 120.0)
        add_bar(feed, "600519", "2026-01-08", 125.0)       # 触发止盈
        c = mgr.list_conditionals("u1")[0]
        assert c["status"] == "triggered"
        assert c["triggered_qty"] == 100
        sells = [t for t in mgr.list_trades("u1") if t["side"] == "SELL"]
        assert len(sells) == 1
        assert mgr.get_account("u1")["positions"] == []

    def test_trigger_with_locked_position_skipped(self, mgr, feed):
        """持仓当日买入不可卖 (T+1) → 触发时跳过"""
        mgr.place_order("u1", "600519", "BUY", "MARKET", quantity=100)   # 当日买入 pending_in
        mgr.set_conditional("u1", "600519", "take_profit", 120.0)
        # 仅推进行情 (不推进交易日) → 仍为 T+0, 持仓不可卖
        feed.klines["600519"].append(mk("2026-01-07", 125.0, 1_200_000, 1_200_000_000.0))
        c = mgr.list_conditionals("u1")[0]
        assert c["status"] == "skipped"
        assert "T+1" in c["message"] or "不可卖" in c["message"]

    def test_cancel_conditional(self, mgr, feed):
        c = mgr.set_conditional("u1", "600519", "take_profit", 108.0)
        assert mgr.cancel_conditional("u1", c["cond_id"]) is True
        add_bar(feed, "600519", "2026-01-07", 110.0)
        assert mgr.list_conditionals("u1")[0]["status"] == "cancelled"


# ================= 交割单 (模拟交易复盘) =================

class TestDeliveries:
    def test_each_fill_is_one_record(self, mgr, feed):
        """买一笔 + 卖一笔 = 两条交割单记录"""
        mgr.place_order("u1", "600519", "BUY", "MARKET", quantity=100)
        advance_day(feed)
        mgr.place_order("u1", "600519", "SELL", "MARKET", quantity=100)
        d = mgr.list_deliveries("u1")
        assert d["summary"]["total"] == 2
        assert d["summary"]["buy_count"] == 1
        assert d["summary"]["sell_count"] == 1
        assert {x["side"] for x in d["items"]} == {"BUY", "SELL"}
        assert all(x["side_label"] in ("买入", "卖出") for x in d["items"])
        assert d["summary"]["fee_total"] == round(sum(x["fee"] for x in d["items"]), 2)

    def test_shows_before_position_closed(self, mgr):
        """未平仓也要有交割单 (修复: 模拟交易复盘此前只认已平仓回合)"""
        mgr.place_order("u1", "600519", "BUY", "MARKET", quantity=100)
        d = mgr.list_deliveries("u1")
        assert d["summary"]["total"] == 1
        assert d["summary"]["buy_count"] == 1
        assert mgr.list_history("u1") == []          # 回合表仍为空, 但交割单有数据

    def test_isolated_by_tier(self, mgr):
        mgr.place_order("u1", "600519", "BUY", "MARKET", quantity=100)
        assert mgr.list_deliveries("u1")["summary"]["total"] == 1
        mgr.bind_tier("u1", "large")
        assert mgr.list_deliveries("u1")["summary"]["total"] == 0

    def test_empty_for_new_user(self, mgr):
        d = mgr.list_deliveries("nobody")
        assert d["items"] == [] and d["summary"]["total"] == 0
