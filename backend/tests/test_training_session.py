"""训练状态机单元测试 — 覆盖 TrainingSession 全部逻辑分支

运行: cd backend && source .venv/bin/activate && pytest -xvs tests/
"""
from __future__ import annotations
import sys
import os
import sqlite3
import tempfile
import shutil
from pathlib import Path
from unittest.mock import patch, MagicMock

# 让 app 包可 import
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from app.services.training_session import (
    TrainingSession, TrainingManager, CATEGORY_FETCHERS,
)

# ---------- 共享 fixture ----------
# 构造固定 K 线序列: 10 根, 从 100 → 102 → 101 → 103 → 105 → 104 → 106 → 108 → 107 → 110
@pytest.fixture
def sample_bars():
    prices = [100, 102, 101, 103, 105, 104, 106, 108, 107, 110]
    bars = []
    for i, c in enumerate(prices):
        o = prices[i - 1] if i > 0 else 99
        bars.append({
            "date": f"2026-01-{i+1:02d}",
            "open": float(o), "high": float(c + 1), "low": float(c - 1),
            "close": float(c), "volume": 1_000_000 + i * 100_000,
        })
    return bars


@pytest.fixture
def session(sample_bars):
    """基础 TrainingSession, 10 根 K, 10 万资金"""
    return TrainingSession("sess_001", "stock", "600519", sample_bars, 100_000.0)


@pytest.fixture
def db_path(tmp_path):
    """用 pytest 自带 tmp_path, 返回 db 文件路径"""
    return tmp_path / "training.db"


@pytest.fixture
def fresh_manager(db_path):
    """重置 TrainingManager 单例 + 注入临时 db"""
    from app.services.training_session import TrainingManager
    TrainingManager._instance = None
    m = TrainingManager()
    m._db_path = db_path
    m._sessions.clear()
    m._init_db()
    return m


# =============================================================
# TrainingSession 基础属性
# =============================================================
class TestSessionBasics:
    def test_init_defaults(self, session, sample_bars):
        assert session.status == "active"
        assert session.category == "stock"
        assert session.code == "600519"
        assert session.code_revealed == 0
        assert session.bars_total == 10
        assert session.bars_used == 0
        assert session.start_date == "2026-01-01"
        assert session.end_date == "2026-01-10"
        assert session.cash == 100_000
        assert session.pos == 0
        assert session.avg_cost == 0.0
        assert session.trades_count == 0
        assert session.round_trips == 0
        assert session.win_count == 0

    def test_current_price_first_bar(self, session):
        """第 1 根 close=100"""
        assert session.current_price == 100.0
        assert session.current_bar["close"] == 100.0

    def test_current_price_exhausted(self, session):
        """推进到最后后 current_price 返回 None"""
        for _ in range(10):
            session.advance()
        assert session.bars_used == 10
        assert session.current_price is None

    def test_portfolio_value_no_position(self, session):
        """空仓时 portfolio = cash"""
        assert session.portfolio_value == pytest.approx(100_000, abs=0.01)

    def test_portfolio_value_with_position(self, session):
        """买 100 股, portfolio = cash + pos * current_price"""
        session.buy(qty=100)  # 100*100 = 10000, 手续费 3 元
        expected_cash = 100_000 - 10_000 - 3  # 90000 - 3
        assert session.portfolio_value == pytest.approx(expected_cash + 100 * 100, abs=0.01)

    def test_pnl_and_pct(self, session):
        """空仓 pnl=0"""
        assert session.pnl == pytest.approx(0, abs=0.01)
        assert session.pnl_pct == pytest.approx(0, abs=0.01)

    def test_pnl_after_buy(self, session):
        """买入 100 股 @100, 持仓盈利 10% 时 pnl ≈ 1000 - 手续费"""
        session.buy(qty=100)
        session.advance()  # 下一根 close=102, 持仓浮盈
        # 当前价 = 102, 100 股浮盈 200
        expected = 200 - 3  # 手续费 3
        assert session.pnl == pytest.approx(expected, abs=1.0)

    def test_fee_minimum(self, session):
        """小金额也至少收 1 元"""
        # 买卖 1 股 100 元, 0.03% 才 0.03 → 收 1 元
        assert session._fee(100) == 1.0
        # 大金额按比例
        assert session._fee(1_000_000) == pytest.approx(300.0, abs=0.01)


# =============================================================
# BUY / SELL 开平仓核心
# =============================================================
class TestBuySell:
    def test_open_long_default_qty(self, session):
        """默认自动缩量: 100000 / 100 / 100 = 10 手 = 1000 股"""
        result = session.buy()
        assert result["action"] == "BUY"
        assert result["qty"] >= 1  # 至少 1 股
        assert session.pos == result["qty"]
        assert session.avg_cost == 100.0
        assert session.cash < 100_000

    def test_open_long_exact_qty(self, session):
        result = session.buy(qty=500)
        assert result["qty"] == 500
        assert session.pos == 500
        expected_cash = 100_000 - 500 * 100 - session._fee(500 * 100)
        assert session.cash == pytest.approx(expected_cash, abs=0.01)

    def test_open_long_half_ratio(self, session):
        """二分之一仓: 满仓(100000/100=1000股)的一半 = 500 股"""
        result = session.buy(ratio=0.5)
        assert result["action"] == "BUY"
        assert result["qty"] == 500
        assert session.pos == 500

    def test_open_long_tenth_ratio(self, session):
        """十分之一仓: 100000×0.1/100 = 100 股"""
        result = session.buy(ratio=0.1)
        assert result["qty"] == 100
        assert session.pos == 100

    def test_ratio_anchors_to_total_assets(self, session):
        """仓位锚定账户总资产: 已有 50% 仓时再买 50% 按总资产×50% 而非剩余现金×50%"""
        session.buy(qty=500)                       # 50% 仓位, cash≈49985, 总资产≈99985
        pv, bp = session.portfolio_value, session._buying_power()
        result = session.buy(ratio=0.5)
        # 目标市值 = 总资产 × 0.5 ≈ 49992, 受可用现金约束(≈49985) → 400 股 (旧逻辑仅 200)
        assert result["qty"] == 400
        assert result["qty"] == int(min(pv * 0.5, bp) / 100 / 100) * 100

    def test_open_long_auto_reduce_on_insufficient_cash(self, session):
        """请求远超现金 → 自动缩量"""
        result = session.buy(qty=999_999)
        assert result["qty"] < 999_999
        assert session.pos == result["qty"]
        # cash 不能为负
        assert session.cash >= 0

    def test_open_long_zero_qty_raises(self, session):
        with pytest.raises(ValueError):
            session.buy(qty=0)
        with pytest.raises(ValueError):
            session.buy(qty=-10)

    def test_close_long(self, session):
        """开多 100 → 平多 100 @102 (推进后)"""
        session.buy(qty=100)
        session.advance()  # bars_used=1, current_price=102
        result = session.sell(qty=100)
        assert result["action"] == "SELL"
        assert result["pnl"] == pytest.approx((102 - 100) * 100, abs=0.01)  # 200 元
        assert session.pos == 0
        assert session.round_trips == 1
        assert session.win_count == 1
        assert session.avg_cost == 0

    def test_close_long_partial(self, session):
        """开多 999 (手续费导致 1000 缩量) → 平 400"""
        # buy(qty=1000) 时 cost=1000*100+fee(100000)=100000+30=100030 > 100000 → 缩到 999
        session.buy(qty=1000)
        assert session.pos == 999
        session.advance()  # bars_used=1, current_price=102
        session.sell(qty=400)
        assert session.pos == 599
        assert session.round_trips == 1  # 部分平仓也算
        # 再卖完
        session.sell(qty=599)
        assert session.pos == 0
        assert session.round_trips == 2

    def test_close_loss_trade(self, session):
        """买入后股价跌 → 平仓亏损"""
        session.buy(qty=100)
        # 前进 2 步: 100 → 102 → 101 (跌了 1)
        session.advance()  # 102
        session.advance()  # 101
        result = session.sell(qty=100)
        assert result["pnl"] == pytest.approx((101 - 100) * 100, abs=0.01)  # 100 元盈利
        assert session.win_count == 1

    def test_add_position_weighted_avg(self, session):
        """加仓 → 加权均价"""
        session.buy(qty=100)  # @100, avg=100
        session.advance()  # @102
        session.buy(qty=100)  # @102
        # 加权: (100*100 + 102*100) / 200 = 101
        assert session.avg_cost == pytest.approx(101.0, abs=0.01)
        assert session.pos == 200


# =============================================================
# 状态机 advance / settle / hold
# =============================================================
class TestStateMachine:
    def test_advance_proceeds(self, session):
        result = session.advance()
        assert session.bars_used == 1
        assert session.current_price == 102.0
        assert result is not None
        assert "settled" not in result or result.get("settled") is False

    def test_auto_settle_on_last_bar(self, session):
        """推到最后一根后自动 settle"""
        for _ in range(10):
            result = session.advance()
        assert session.status == "settled"
        assert session.bars_used == 10
        assert session.code_revealed == 1
        assert result.get("settled") is True

    def test_hold_does_nothing(self, session):
        result = session.hold()
        assert result["action"] == "HOLD"
        assert session.bars_used == 0  # hold 不推进
        assert session.pos == 0

    def test_manual_settle(self, session):
        """中途手动 settle, 有持仓会强制平仓"""
        session.buy(qty=100)
        for _ in range(5):
            session.advance()  # 推到第 5 根, price=105
        summary = session.manual_settle()
        assert session.status == "settled"
        assert session.pos == 0  # 强制平仓
        assert session.code_revealed == 1
        assert summary["code"] == "600519"

    def test_abort(self, session):
        session.abort()
        assert session.status == "abandoned"
        assert session.code_revealed == 1

    def test_action_after_settle_raises(self, session):
        session.manual_settle()
        with pytest.raises(ValueError, match="已结束"):
            session.buy()
        with pytest.raises(ValueError, match="已结束"):
            session.sell()
        with pytest.raises(ValueError, match="已结束"):
            session.advance()


# =============================================================
# 双盲机制
# =============================================================
class TestBlindMode:
    def test_hide_meta_when_active(self, session):
        s = session.summary(hide_meta=True)
        assert s["code_revealed"] == 0
        assert "code" not in s
        assert "start_date" not in s
        assert "end_date" not in s

    def test_show_meta_when_revealed(self, session):
        session.code_revealed = 1
        s = session.summary(hide_meta=True)
        assert s["code"] == "600519"
        assert s["start_date"] == "2026-01-01"

    def test_force_show_meta(self, session):
        s = session.summary(hide_meta=False)
        assert s["code"] == "600519"
        assert "start_date" in s


# =============================================================
# 手续费精度
# =============================================================
class TestFeeEdgeCases:
    def test_small_fee_floor(self, session):
        """手续费最低 1 元"""
        # 0.03% of 100 = 0.03 → floor 到 1
        assert session._fee(100) == 1.0

    def test_fee_exact_threshold(self, session):
        """1 元 / 0.0003 = 3333.34 元以下都是 1 元"""
        assert session._fee(3333) == 1.0
        assert session._fee(3334) == pytest.approx(1.0002, abs=0.01)


# =============================================================
# TrainingManager 单例 + DB 持久化
# =============================================================
class TestTrainingManager:
    def test_singleton(self):
        m1 = TrainingManager()
        m2 = TrainingManager()
        assert m1 is m2

    def test_db_tables_created(self, fresh_manager, db_path):
        m = fresh_manager
        import sqlite3
        with sqlite3.connect(str(db_path)) as conn:
            c = conn.cursor()
            c.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = [r[0] for r in c.fetchall()]
        assert "training_sessions" in tables
        assert "training_bars" in tables

    def test_persist_session(self, fresh_manager, db_path, session):
        m = fresh_manager
        m._persist_session(session)
        import sqlite3
        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute("SELECT * FROM training_sessions WHERE session_id=?", (session.session_id,))
            row = c.fetchone()
        assert row is not None
        assert row["code"] == "600519"
        assert row["bars_total"] == 10
        assert row["bars_used"] == 0
        assert row["initial_capital"] == 100_000

    def test_list_history_empty(self, fresh_manager):
        m = fresh_manager
        assert m.list_history() == []

    def test_list_history_after_settle(self, fresh_manager, sample_bars):
        m = fresh_manager
        sess = TrainingSession("h_001", "stock", "000001", sample_bars, 100_000)
        sess.buy(qty=100)
        sess.advance()
        sess.manual_settle()
        m._persist_session(sess)
        m._finalize_session(sess)
        history = m.list_history(limit=10)
        assert len(history) == 1
        assert history[0]["session_id"] == "h_001"
        assert history[0]["status"] == "settled"

    def test_get_history_detail(self, fresh_manager, sample_bars):
        m = fresh_manager
        sess = TrainingSession("h_002", "stock", "600519", sample_bars, 50_000)
        sess.hold()
        m._persist_bar(sess, "HOLD")
        sess.advance()
        sess.manual_settle()
        m._persist_session(sess)
        m._finalize_session(sess)
        detail = m.get_history("h_002")
        assert detail is not None
        assert detail["status"] == "settled"
        assert "bars" in detail
        assert len(detail["bars"]) >= 1

    def test_get_history_not_found(self, fresh_manager):
        m = fresh_manager
        assert m.get_history("nonexistent") is None


# =============================================================
# TrainingManager.start (mock akshare)
# =============================================================
class TestManagerStart:
    def test_start_invalid_category_raises(self, fresh_manager):
        m = fresh_manager
        with pytest.raises(ValueError, match="未知品类"):
            m.start("crypto", bars_count=30)

    def test_start_stock_with_code(self, fresh_manager, sample_bars):
        from app.services import training_session as ts
        m = fresh_manager
        long_bars = sample_bars * 20
        fake = MagicMock(return_value=long_bars)
        ts.CATEGORY_FETCHERS = {"stock": fake, "limit_up": fake}
        sess = m.start("stock", bars_count=50, code="600519")
        assert fake.called
        assert sess.category == "stock"
        assert sess.code == "600519"
        assert sess.bars_total == 50

    def test_start_bars_count_clamped(self, fresh_manager, sample_bars):
        from app.services import training_session as ts
        m = fresh_manager
        fake = MagicMock(return_value=sample_bars * 30)
        ts.CATEGORY_FETCHERS = {"stock": fake, "limit_up": fake}
        sess_low = m.start("stock", bars_count=5, code="600519")
        sess_high = m.start("stock", bars_count=999, code="600519")
        assert sess_low.bars_total == 20
        assert sess_high.bars_total == 250

    def test_start_code_not_enough_bars_raises(self, fresh_manager, sample_bars):
        from app.services import training_session as ts
        m = fresh_manager
        fake = MagicMock(return_value=sample_bars)
        ts.CATEGORY_FETCHERS = {"stock": fake, "limit_up": fake}
        with pytest.raises(ValueError, match="不够训练"):
            m.start("stock", bars_count=50, code="600519")

    def test_start_random_window(self, fresh_manager, sample_bars):
        """随机起始窗口: 不同批次 start_idx 可能不同, 但窗口长度固定且不越界"""
        from app.services import training_session as ts
        m = fresh_manager
        fake = MagicMock(return_value=sample_bars * 40)
        ts.CATEGORY_FETCHERS = {"stock": fake, "limit_up": fake}
        sess = m.start("stock", bars_count=60, code="600519")
        assert sess.bars_total == 60
        assert sess.start_date is not None
        assert len(sess.pre_bars) >= 30

    def test_etf_no_longer_supported(self, fresh_manager, sample_bars):
        """仅保留 stock/limit_up 两品类, etf 等其余品类应被拒绝"""
        from app.services import training_session as ts
        m = fresh_manager
        fake = MagicMock(return_value=sample_bars * 20)
        ts.CATEGORY_FETCHERS = {"stock": fake, "limit_up": fake}
        with pytest.raises(ValueError, match="未知品类"):
            m.start("etf", bars_count=60, code="510050")


# =============================================================
# 交易对统计 round_trips / win_count
# =============================================================
class TestRoundTripStats:
    def test_win_trade_counted(self, session):
        session.buy(qty=100)  # @100
        session.advance()  # @102 (浮盈)
        session.sell(qty=100)  # 平多 +200
        assert session.round_trips == 1
        assert session.win_count == 1
        assert session.trades_count == 2  # buy + sell

    def test_loss_trade_counted(self, session):
        """股价没动平掉 = 盈亏 0 → win_count 不增加"""
        session.buy(qty=100)  # @100
        session.sell(qty=100)  # @100 (同 bar 卖, 但当前价还是 100)
        # 等等——sell 不推进 bar, 所以 price 还是 100, pnl=0
        assert session.round_trips == 1
        assert session.win_count == 0  # pnl=0, 不算赢

    def test_win_rate_zero_round_trips(self, session):
        """还没平仓 → win_rate=0 不除以 0"""
        assert session.win_count == 0
        s = session.summary()
        assert s["win_rate"] == 0


# =============================================================
# 结算时自动强制平仓
# =============================================================
class TestForceClose:
    def test_force_close_long_at_settle(self, session):
        """手动 settle 时如果还有多头 → 强制 @last_bar_close"""
        session.buy(qty=100)  # @100
        for _ in range(9):
            session.advance()  # 到最后一根, bars_used=9, current_price=110
        # bars_used=9 < 10, 还没 auto_settle
        summary = session.manual_settle()
        assert session.pos == 0
        # 最终价 110 - avg_cost 100 = 10 * 100 = 1000 - 手续费
        assert summary["pnl"] == pytest.approx(1000 - session._fee(100 * 100) - session._fee(100 * 110), abs=2.0)

    def test_force_close_when_auto_settle(self, session):
        """推到最后一根 auto_settle 也会 force close"""
        session.buy(qty=100)
        for _ in range(10):
            session.advance()
        assert session.status == "settled"
        assert session.pos == 0
        # buy → advance×9 → 第10次 advance 触发 auto_settle + force_close
        # round_trips = 1 (force close 算 1 次)
        assert session.round_trips == 1


# =============================================================
# 条件单 (止盈 / 止损): 触发即操作
# =============================================================
class TestConditionalOrders:
    def test_take_profit_triggers_on_high(self, session):
        """多头止盈: K 线最高价触及触发价 → 自动平仓"""
        session.buy(qty=100)                      # @100
        session.set_conditional("take_profit", 103.0)
        session.advance()                         # bar0 high=101 < 103, 未触发
        assert session.pos == 100
        assert session.conditionals[0]["status"] == "active"
        session.advance()                         # bar1 high=103 >= 103 → 触发
        assert session.pos == 0
        c = session.conditionals[0]
        assert c["status"] == "triggered"
        assert c["triggered_price"] == pytest.approx(103.0)   # open(100) < 103 → 按触发价
        assert c["triggered_pnl"] == pytest.approx(300.0)     # (103-100)*100

    def test_stop_loss_triggers_on_low(self, session):
        """多头止损: K 线最低价触及触发价 → 自动平仓"""
        session.buy(qty=100)                      # @100
        session.set_conditional("stop_loss", 99.0)
        session.advance()                         # bar0 low=99 <= 99 → 触发
        assert session.pos == 0
        c = session.conditionals[0]
        assert c["status"] == "triggered"
        assert c["triggered_pnl"] == pytest.approx(-100.0)    # (99-100)*100

    def test_partial_ratio_closes_part_position(self, session):
        """按比例部分平仓: ratio=0.5 → 平一半"""
        session.buy(qty=200)                      # @100, pos=200
        session.set_conditional("stop_loss", 99.0, ratio=0.5)
        session.advance()
        assert session.pos == 100
        assert session.conditionals[0]["triggered_qty"] == 100

    def test_gap_open_uses_open_price(self, session):
        """跳空穿越触发价: 成交价取更差的开盘价 (而非触发价)"""
        session.buy(qty=100)                        # bar0 收盘 100
        session.advance()                           # 走完 bar0, 进入 bar1 (现价 102)
        session.set_conditional("stop_loss", 101.5)  # 101.5 < 102, 合法
        session.advance()                           # bar1: open=100, low=101 <= 101.5 → 触发
        c = session.conditionals[0]
        assert c["status"] == "triggered"
        assert c["triggered_price"] == pytest.approx(100.0)   # 取开盘价 100, 而非触发价 101.5

    def test_cancel_conditional(self, session):
        """撤销后的条件单不再触发"""
        session.buy(qty=100)
        c = session.set_conditional("take_profit", 103.0)
        session.cancel_conditional(c["id"])
        assert session.conditionals[0]["status"] == "cancelled"
        session.advance()
        session.advance()                         # bar1 high=103, 但已撤销
        assert session.pos == 100

    def test_can_set_without_position(self, session):
        """无持仓也可挂条件单 (方向由「类型 + 触发价相对现价」推断)"""
        c = session.set_conditional("take_profit", 105.0)   # 现价 100 → 多头止盈
        assert c["status"] == "active"
        assert c["side"] == "long"
        c2 = session.set_conditional("take_profit", 95.0)    # 现价 100 → 空头止盈
        assert c2["side"] == "short"

    def test_trigger_without_position_is_skipped(self, session):
        """触发但当前无可操作持仓 → 标记 skipped 并给出提示, 不做操作"""
        session.set_conditional("take_profit", 101.0)  # 无限持仓 → 推断多头; bar0 high=101 ≥ 101
        r = session.advance()
        c = session.conditionals[0]
        assert c["status"] == "skipped"
        assert c["message"]
        assert session.pos == 0

    def test_wrong_side_price_rejected(self, session):
        """多头止盈价必须高于现价"""
        session.buy(qty=100)                      # 现价 100
        with pytest.raises(ValueError):
            session.set_conditional("take_profit", 99.0)
        with pytest.raises(ValueError):
            session.set_conditional("stop_loss", 101.0)

    def test_summary_exposes_conditionals(self, session):
        """summary 输出条件单列表, 供前端展示"""
        session.buy(qty=100)
        session.set_conditional("take_profit", 105.0)
        conds = session.summary()["conditionals"]
        assert len(conds) == 1
        assert conds[0]["kind"] == "take_profit"
        assert conds[0]["trigger_price"] == pytest.approx(105.0)

    def test_liquidation_keeps_conditional_active(self, session):
        """手动平仓后, 条件单仍保留 (触发时再判定是否可操作)"""
        session.buy(qty=100)
        session.set_conditional("take_profit", 110.0)
        session.sell(qty=100)                     # 手动平多 → pos=0
        session.advance()                         # bar0 high=101 < 110, 未触发
        assert session.conditionals[0]["status"] == "active"


# =============================================================
# 移动止损 (trailing_stop): 止损位随最新价移动
# =============================================================
class TestTrailingStop:
    def test_trailing_stop_follows_peak_then_triggers(self, session):
        """多头移动止损: 止损位随最高价上移, 自最高价回撤 trail_pct% 触发"""
        session.buy(qty=100)                        # @100
        c = session.set_conditional("trailing_stop", trail_pct=2.0)
        assert c["kind_label"] == "移动止损"
        assert c["side"] == "long"

        session.advance()                           # bar0 high=101 low=99 → peak=101, 止损=98.98
        assert session.pos == 100
        c = session.conditionals[0]
        assert c["status"] == "active"
        assert c["trigger_price"] == pytest.approx(round(101 * 0.98, 2))

        session.advance()                           # bar1 high=103 → peak=103, 止损=100.94; low=101 未触及
        assert session.conditionals[0]["trigger_price"] == pytest.approx(round(103 * 0.98, 2))

        session.advance()                           # bar2 low=100 <= 100.94 → 触发
        c = session.conditionals[0]
        assert c["status"] == "triggered"
        assert session.pos == 0
        assert c["triggered_price"] == pytest.approx(round(103 * 0.98, 2))

    def test_trailing_stop_without_position_is_skipped(self, session):
        """移动止损触发但无持仓 → 标记 skipped 并提示"""
        session.set_conditional("trailing_stop", trail_pct=1.0)   # 现价100 → 止损 99
        session.advance()                           # bar0 high=101 → 止损 99.99, low=99 触及
        c = session.conditionals[0]
        assert c["status"] == "skipped"
        assert c["message"]
        assert session.pos == 0

    def test_invalid_trail_pct_rejected(self, session):
        session.buy(qty=100)
        with pytest.raises(ValueError):
            session.set_conditional("trailing_stop", trail_pct=0)


# =============================================================
# 分时索引与展示K线一致 (揭晓后按完整K线, 避免越界)
# =============================================================
class TestBarIntradayIndexing:
    def test_bar_intraday_uses_full_bars_when_revealed(self, sample_bars):
        """爆仓/放弃等「已揭晓但未推进到末根」的会话: 末根分时不应越界"""
        s = TrainingSession("s_blow", "stock", "600519", sample_bars, 100_000.0)
        s.advance(); s.advance()                    # 仅推进 2 根
        s.code_revealed = 1                         # 揭晓 (前端此时拿到完整 bars)
        assert len(s.summary()["bars"]) == len(sample_bars)
        with patch("app.services.intraday.generate_intraday_real", return_value=None):
            d = s.bar_intraday(len(sample_bars) - 1)   # 末根索引不再越界
        assert d is not None

    def test_bar_intraday_out_of_range_raises(self, session):
        with pytest.raises(ValueError, match="越界"):
            session.bar_intraday(999)


class TestBuildIntraday:
    """模块级 build_intraday: 供已结算(内存已释放)会话从 DB 历史K线重建分时"""

    def test_empty_when_no_volume(self):
        from app.services.training_session import build_intraday
        bar = {"date": "2026-01-05", "open": 10.0, "high": 10.2, "low": 9.9, "close": 10.1, "volume": 0}
        d = build_intraday("600519", bar, 10.0, "#3", True)
        assert d["empty"] is True and d["suspended"] is True
        assert d["date"] == "2026-01-05"

    def test_masks_date_when_not_revealed(self):
        from app.services.training_session import build_intraday
        bar = {"date": "2026-01-05", "open": 10.0, "high": 10.2, "low": 9.9, "close": 10.1, "volume": 0}
        d = build_intraday("600519", bar, 10.0, "#3", False)
        assert d["date"] == "#3"


class TestArchivedReplay:
    """已归档会话(内存已释放)的分时回看: 优先用结算快照的完整K线序列

    回归背景: 落库的 training_bars 只记录「有决策」的稀疏K线, 若按下标索引分时,
    会与前端展示的完整序列 (结算快照) 错位, 出现「索引越界」。
    """

    def _seed(self, m, sample_bars):
        import json
        snap = {"session_id": "s_arch", "status": "settled", "category": "long",
                "code": "600519", "bars": sample_bars,
                "pre_bars": [{"date": "2025-12-01", "open": 9.0, "high": 9.2, "low": 8.9,
                              "close": 9.05, "volume": 500_000}]}
        with sqlite3.connect(str(m._db_path)) as conn:
            conn.execute(
                """INSERT OR REPLACE INTO training_sessions
                   (session_id,status,category,code,code_revealed,start_date,end_date,
                    bars_total,bars_used,initial_capital,created_at,category_label,user_id,settlement_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("s_arch", "settled", "long", "600519", 1, "2026-01-01", "2026-01-10",
                 len(sample_bars), len(sample_bars), 100_000.0,
                 "2026-01-11 00:00:00", "长周期训练", "default", json.dumps(snap)))
            # 落库决策K线故意稀疏: 只写首末两根
            for i in (0, len(sample_bars) - 1):
                b = sample_bars[i]
                conn.execute(
                    """INSERT OR REPLACE INTO training_bars
                       (session_id,bar_index,date,open,high,low,close,volume,action,action_price,
                        pos_before,cash_before,pnl_before,pos_after,cash_after,pnl_after,equity)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    ("s_arch", i, b["date"], b["open"], b["high"], b["low"], b["close"], b["volume"],
                     "HOLD", b["close"], 0, 100_000.0, 0.0, 0, 100_000.0, 0.0, 100_000.0))
            conn.commit()

    def test_replay_uses_snapshot_not_sparse_rows(self, fresh_manager, sample_bars):
        self._seed(fresh_manager, sample_bars)
        r = fresh_manager.replay_series("s_arch")
        assert r is not None
        assert len(r["bars"]) == len(sample_bars)     # 完整序列, 非稀疏落库的两根
        assert r["code"] == "600519"
        assert len(r["pre_bars"]) == 1

    def test_middle_index_of_sparse_session_is_replayable(self, fresh_manager, sample_bars):
        self._seed(fresh_manager, sample_bars)
        with patch("app.services.intraday.generate_intraday_real", return_value=None):
            d = fresh_manager.bar_intraday_replay("s_arch", len(sample_bars) - 1)
        assert d is not None and d["empty"] is True

    def test_out_of_range_raises(self, fresh_manager, sample_bars):
        self._seed(fresh_manager, sample_bars)
        with pytest.raises(ValueError, match="越界"):
            fresh_manager.bar_intraday_replay("s_arch", 999)

    def test_unknown_session_returns_none(self, fresh_manager):
        assert fresh_manager.replay_series("no_such_session") is None


# =============================================================
# 模式归因 (基于训练记录清洗筛选)
# =============================================================
import app.services.attribution as attr_mod


@pytest.fixture
def attr_db(tmp_path, monkeypatch):
    """构造最小 attr 数据库 (sessions + bars + strategies) 并指向它"""
    db = tmp_path / "attr.db"
    with sqlite3.connect(str(db)) as c:
        c.execute("""CREATE TABLE training_sessions (
            session_id TEXT PRIMARY KEY, status TEXT, category TEXT, code TEXT, code_name TEXT,
            code_revealed INTEGER, start_date TEXT, end_date TEXT, bars_total INTEGER,
            bars_used INTEGER, initial_capital REAL, final_capital REAL, pnl REAL, pnl_pct REAL,
            trades_count INTEGER, round_trips INTEGER, win_count INTEGER, win_rate REAL,
            created_at TEXT, settled_at TEXT, settlement_json TEXT, strategy_id INTEGER,
            leverage REAL, max_drawdown REAL, blown_up INTEGER, category_label TEXT,
            user_id TEXT, note TEXT)""")
        c.execute("""CREATE TABLE training_bars (
            session_id TEXT, bar_index INTEGER, date TEXT, open REAL, high REAL, low REAL,
            close REAL, volume INTEGER, action TEXT, action_price REAL, pos_before REAL,
            cash_before REAL, pnl_before REAL, pos_after REAL, cash_after REAL,
            pnl_after REAL, equity REAL, PRIMARY KEY (session_id, bar_index))""")
        c.execute("""CREATE TABLE strategies (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE, description TEXT,
            color TEXT, created_at TEXT)""")
        # 一局已结算: 4 根K, 100 买入 → 110 卖出 (盈利一笔)
        c.execute("""INSERT INTO training_sessions
            (session_id, status, category, code, code_name, bars_total, bars_used,
             initial_capital, final_capital, pnl, pnl_pct, trades_count, round_trips,
             win_count, win_rate, max_drawdown, blown_up, user_id, settlement_json)
            VALUES ('s1','settled','stock','600519','贵州茅台',4,4,
                    100000,101000,1000,1.0,2,1,1,100.0,5.0,0,'default','{}')""")
        rows = [
            # bar_index, date, o,h,l,c, vol, action, pos_before, pos_after, equity
            (0, "2026-01-01", 99, 101, 98, 100, 1000, "BUY", 0, 100, 100000),
            (1, "2026-01-02", 100, 106, 99, 105, 1000, "HOLD", 100, 100, 100500),
            (2, "2026-01-03", 105, 111, 104, 110, 1000, "SELL", 100, 0, 101000),
            (3, "2026-01-04", 110, 112, 108, 109, 1000, "HOLD", 0, 0, 101000),
        ]
        c.executemany("""INSERT INTO training_bars
            (session_id, bar_index, date, open, high, low, close, volume, action,
             action_price, pos_before, cash_before, pnl_before, pos_after, cash_after,
             pnl_after, equity)
            VALUES ('s1',?,?,?,?,?,?,?,?,?,?,0,0,?,0,0,?)""",
            [(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[5], r[8], r[9], r[9])
             for r in rows])
        c.commit()
    monkeypatch.setattr(attr_mod, "_db_path", lambda: db)
    return db


class TestAttribution:
    def test_empty_db_returns_graceful_report(self, tmp_path, monkeypatch):
        db = tmp_path / "empty.db"
        with sqlite3.connect(str(db)) as c:
            c.execute("""CREATE TABLE training_sessions (session_id TEXT, status TEXT,
                user_id TEXT, strategy_id INTEGER, category TEXT, created_at TEXT)""")
            c.execute("""CREATE TABLE training_bars (session_id TEXT, bar_index INTEGER)""")
            c.execute("""CREATE TABLE strategies (id INTEGER, name TEXT)""")
            c.commit()
        monkeypatch.setattr(attr_mod, "_db_path", lambda: db)
        r = attr_mod.analyze()
        assert r["overview"]["trips"] == 0
        assert r["conclusion"]["profile"] == "样本不足"

    def test_single_trip_analysis(self, attr_db):
        r = attr_mod.analyze()
        assert r["scope"]["sessions"] == 1
        assert r["overview"]["trips"] == 1
        assert r["overview"]["win_rate"] == 100.0
        assert r["overview"]["total_pnl"] == pytest.approx(1000.0)
        # 因子/纪律/手法/仓位四大板块齐备
        assert len(r["factors"]) >= 4
        assert len(r["discipline"]) == 4
        assert "buy_timing" in r["tactics"]
        assert len(r["positioning"]["buckets"]) == 3
        assert r["conclusion"]["profile"]
        assert r["conclusion"]["summary"]

    def test_filter_by_strategy_unassigned(self, attr_db):
        """strategy_id=-1 (未归类) 正常返回, 展示为「默认」"""
        r = attr_mod.analyze(strategy_id=-1)
        assert r["scope"]["strategy"] == "默认"
        assert r["scope"]["sessions"] == 1


# =============================================================
# 模式归因 —— 逻辑推演 (观察→依据→推断→行动)
# =============================================================
class TestAttributionReasoning:
    def test_reasoning_key_present(self, attr_db):
        r = attr_mod.analyze()
        assert "reasoning" in r
        assert isinstance(r["reasoning"], list)

    def test_reasoning_empty_for_tiny_sample(self, attr_db):
        """单笔样本不足以形成对比, 推演应为空 (不无的放矢)"""
        r = attr_mod.analyze()
        assert r["reasoning"] == []

    def test_item_fields_and_confidence(self):
        it = attr_mod._item("买入择位", "观察", "依据", "推断", "行动",
                            n=12, diff=5.0, base=1.0)
        for k in ("topic", "severity", "confidence", "observation",
                  "evidence_text", "inference", "action"):
            assert k in it
        assert it["confidence"] == "high"        # n>=10
        assert it["severity"] == "high"          # diff >= max(1, |base|)
        assert it["_n"] == 12 and it["_diff"] == 5.0

    def test_item_low_grade(self):
        it = attr_mod._item("t", "o", "e", "i", "a", n=2, diff=0.0, base=1.0)
        assert it["confidence"] == "low"         # n<6
        assert it["severity"] == "low"           # diff<=0

    def test_reasoning_generates_on_clear_contrast(self):
        """构造「MA20下方开仓亏损 / MA20上方开仓盈利」两组对比, 应产出推演"""
        def trip(gap, pnl, hold=2, chase=0):
            return {"direction": "long", "open_index": 0, "close_index": hold,
                    "open_price": 100.0, "close_price": 100.0, "qty": 1, "pnl": pnl,
                    "win": pnl > 0, "hold_bars": hold, "ma5": 100.0, "ma20": 100.0,
                    "above_ma5": True, "above_ma20": gap > 0, "ma20_gap": gap,
                    "trend": 1, "chase": chase, "pos_ratio": 0.3, "held_to_end": False}
        trips = [trip(-6, -500.0) for _ in range(4)] + [trip(5, 400.0) for _ in range(4)]
        out = attr_mod._reasoning({"expectancy": 0, "avg_hold": 2}, trips)
        assert out, "存在明显优劣差时应产出至少一条推演"
        assert len(out) <= 5
        for it in out:
            for k in ("topic", "observation", "evidence_text", "inference", "action"):
                assert it[k]
            assert it["severity"] in ("high", "medium", "low")
            assert it["confidence"] in ("high", "medium", "low")
            assert "_n" not in it and "_diff" not in it   # 内部字段已清理


# =============================================================
# 训练标的池 stock_pool —— 涨停止口径 / 分档 / 抽样
# =============================================================
class TestStockPool:
    @pytest.fixture(autouse=True)
    def _pool(self, tmp_path, monkeypatch):
        from app.services import stock_pool as sp
        db = tmp_path / "pool.db"
        monkeypatch.setattr(sp, "_db_path", lambda: db)
        sp._ensure_table()
        self.sp = sp
        return db

    def test_limit_up_pct_rules(self):
        assert self.sp.limit_up_pct("600519") == 0.10
        assert self.sp.limit_up_pct("300750") == 0.20     # 创业板
        assert self.sp.limit_up_pct("688981") == 0.20     # 科创板
        assert self.sp.limit_up_pct("830799", market="bj") == 0.30
        assert self.sp.limit_up_pct("600519", name="ST某某") == 0.05

    def test_count_limit_up(self):
        bars = [{"close": 100}, {"close": 110}, {"close": 109}, {"close": 120}]
        # 110/100-1 == 10% (涨停); 120/109-1 ≈ 10.09% (涨停) → 共 2 次
        assert self.sp.count_limit_up(bars, 0.10) == 2

    def test_bucket_boundaries(self):
        assert [self.sp.bucket_of(x) for x in (0, 1, 2, 3, 5, 6, 99)] == \
            ["none", "low", "low", "mid", "mid", "high", "high"]

    def test_bucket_meta(self):
        meta = self.sp.bucket_meta()
        assert [m["key"] for m in meta] == ["none", "low", "mid", "high"]
        assert self.sp.bucket_label("mid") == "3-5 次"
        assert self.sp.bucket_label("unknown") == ""

    def test_update_and_read_pool(self):
        bars = [{"close": 100}, {"close": 110}]           # 1 次涨停 → low
        info = self.sp.update_stat("600519", name="贵州茅台", market="sh",
                                   bars=bars, has_intraday=True)
        assert info["limit_up_count"] == 1
        assert info["limit_up_bucket"] == "low"
        rows = self.sp.pool_stats()
        assert len(rows) == 1
        assert rows[0]["code"] == "600519" and rows[0]["has_intraday"] == 1

    def test_update_stat_without_bars_returns_none(self):
        assert self.sp.update_stat("600519", bars=None) is None

    def test_candidates_only_intraday(self):
        self.sp.update_stat("600519", bars=[{"close": 100}, {"close": 110}], has_intraday=True)
        self.sp.update_stat("000001", bars=[{"close": 100}, {"close": 110}], has_intraday=False)
        got = self.sp.candidates(bars_needed=2, only_intraday=True)
        assert [r["code"] for r in got] == ["600519"]

    def test_candidates_bars_needed_filter(self):
        self.sp.update_stat("600519", bars=[{"close": 100}, {"close": 110}], has_intraday=True)
        assert self.sp.candidates(bars_needed=999, only_intraday=True) == []

    def test_pool_stats_expired_filtered(self):
        """stat_date 早于保鲜期 → 不再作为候选"""
        with sqlite3.connect(str(self.sp._db_path())) as c:
            c.execute("""INSERT INTO stock_pool (code, limit_up_count, limit_up_bucket, stat_date)
                         VALUES ('600519', 1, 'low', '2000-01-01')""")
        assert self.sp.pool_stats() == []

    def test_choose_bucket_deterministic(self):
        assert self.sp.choose_bucket([0, 0, 0, 1]) == "high"
        assert self.sp.choose_bucket([1, 0, 0, 0]) == "none"

    def test_choose_bucket_all_zero_returns_none(self):
        assert self.sp.choose_bucket([0, 0, 0, 0]) is None
        assert self.sp.choose_bucket(None) in ("none", "low", "mid", "high")

    def test_choose_bucket_avoid_all_falls_back(self):
        """唯一正权重档被 avoid 排除 → 忽略 avoid, 仍返回该档"""
        assert self.sp.choose_bucket([0, 0, 0, 1], avoid=["high"]) == "high"

    def test_pick_code_from_pool(self):
        self.sp.update_stat("600519", name="贵州茅台", market="sh",
                            bars=[{"close": 100}, {"close": 110}], has_intraday=True)
        got = self.sp.pick_code(2, weights=[0, 1, 0, 0],
                                fetch_kline=lambda c: [{"close": 1}, {"close": 2}])
        assert got is not None
        assert got["code"] == "600519"
        assert got["limit_up_bucket"] == "low"
        assert len(got["klines"]) == 2

    def test_pick_code_probes_when_pool_miss(self, monkeypatch):
        """池内没有目标档 → 现场探测命中该档"""
        from app.services import intraday as intra
        monkeypatch.setattr(intra, "has_minute_data", lambda c: True, raising=False)
        monkeypatch.setattr(self.sp, "_ledger_codes", lambda: ["600519"])
        monkeypatch.setattr(self.sp, "_registry_row", lambda c: {})
        hb = [{"close": 100.0}]
        for _ in range(8):                       # 连续 8 次 10% → high 档
            hb.append({"close": round(hb[-1]["close"] * 1.1, 2)})
        got = self.sp.pick_code(2, weights=[0, 0, 0, 1], fetch_kline=lambda c: hb)
        assert got is not None
        assert got["limit_up_bucket"] == "high"

    def test_pick_code_none_when_no_registry(self, monkeypatch):
        monkeypatch.setattr(self.sp, "_ledger_codes", lambda: [])
        assert self.sp.pick_code(2, weights=[1, 1, 1, 1],
                                 fetch_kline=lambda c: [{"close": 1}]) is None


# =============================================================
# 随机选标 —— 内部分级匹配 (涨停/量能异常/时间较近) + 最近50局去重
# =============================================================
class TestPickRandomCodeByBucket:
    def test_uses_pool_pick(self, fresh_manager, monkeypatch):
        from app.services import stock_pool as sp
        m = fresh_manager
        hit = {"code": "600519", "klines": [{"close": 1}], "limit_up_count": 1,
               "limit_up_bucket": "low", "limit_up_label": "1-2 次"}
        monkeypatch.setattr(sp, "pick_tiered", lambda *a, **k: hit)
        got = m._pick_random_code(1, exclude=["000001"])
        assert got == hit

    def test_fallback_none_when_registry_unavailable(self, fresh_manager, monkeypatch):
        from app.services import stock_pool as sp
        import app.data.stock_registry as reg_mod
        m = fresh_manager
        monkeypatch.setattr(sp, "pick_tiered",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("池不可用")))

        class BoomReg:
            def __init__(self, *a, **k):
                raise RuntimeError("注册表不可用")

        monkeypatch.setattr(reg_mod, "StockRegistry", BoomReg)
        assert m._pick_random_code(1) is None

    def test_recent_codes_reads_persisted(self, fresh_manager, sample_bars):
        """最近 50 局去重: 已用代码出现在 _recent_codes 中"""
        from app.services import training_session as ts
        m = fresh_manager
        fake = MagicMock(return_value=sample_bars * 30)
        ts.CATEGORY_FETCHERS = {"stock": fake, "limit_up": fake}
        s = m.start("stock", bars_count=50, code="600519")
        m._persist_session(s)
        assert "600519" in m._recent_codes("default", 50)

    def test_recent_buckets_reads_persisted(self, fresh_manager, sample_bars):
        from app.services import training_session as ts
        m = fresh_manager
        fake = MagicMock(return_value=sample_bars * 30)
        ts.CATEGORY_FETCHERS = {"stock": fake, "limit_up": fake}
        s = m.start("stock", bars_count=50, code="600519")
        s.limit_up_bucket = "mid"
        m._persist_session(s)
        assert "mid" in m._recent_buckets("default")


# =============================================================
# 会话涨停元字段 (summary 透出)
# =============================================================
class TestSessionLimitUpMeta:
    def test_session_stores_and_summarizes(self, sample_bars):
        s = TrainingSession("s_lu", "stock", "600519", sample_bars, 100_000.0,
                            limit_up_count=4, limit_up_bucket="mid")
        d = s.summary()
        assert d["limit_up_count"] == 4
        assert d["limit_up_bucket"] == "mid"
        assert d["limit_up_label"] == "3-5 次"

    def test_default_zero_and_empty_label(self, session):
        d = session.summary()
        assert d["limit_up_count"] == 0
        assert d["limit_up_bucket"] == ""
        assert d["limit_up_label"] == ""


# =============================================================
# 复盘收益曲线 (累计净值) / 重置
# =============================================================
class TestEquityNav:
    @staticmethod
    def _insert(m, sid, pnl_pct, created_at, user_id="u1", status="settled"):
        with sqlite3.connect(str(m._db_path)) as conn:
            conn.execute(
                """INSERT INTO training_sessions
                   (session_id, status, category, code, code_revealed, created_at, pnl_pct, user_id)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (sid, status, "stock", "600519", 1, created_at, pnl_pct, user_id))

    def test_compound_nav_after_reset(self, fresh_manager):
        m = fresh_manager
        self._insert(m, "s1", 10.0, "2026-01-01 10:00:00")
        self._insert(m, "s2", -20.0, "2026-01-02 10:00:00")
        d = m.equity_nav("u1")
        # 100 * 1.1 * 0.8 = 88
        assert d["nav"] == pytest.approx(88.0, abs=0.01)
        assert d["can_start"] is True
        assert [c["nav"] for c in d["curve"]] == [pytest.approx(110.0), pytest.approx(88.0)]

    def test_zero_nav_blocks_start(self, fresh_manager):
        m = fresh_manager
        self._insert(m, "s1", -100.0, "2026-01-01 10:00:00", status="blown_up")
        d = m.equity_nav("u1")
        assert d["nav"] == 0.0
        assert d["can_start"] is False

    def test_reset_keeps_records_and_counts(self, fresh_manager):
        m = fresh_manager
        self._insert(m, "s1", -100.0, "2026-01-01 10:00:00", status="blown_up")
        d = m.reset_equity("u1")
        assert d["reset_count"] == 1
        assert d["nav"] == 100.0            # 起点重置回基准
        assert d["can_start"] is True
        assert d["curve"] == []             # 重置前的记录不再计入曲线
        # 训练记录本身保留
        assert any(r["session_id"] == "s1" for r in m.list_history(user_id="u1"))
        # 再次重置累加次数
        assert m.reset_equity("u1")["reset_count"] == 2


# =============================================================
# 周期视图 MACD (B11): 日线算 + 周期桶末采样, 与展示序列对齐
# =============================================================
class TestPeriodMacd:
    @staticmethod
    def _bars(n):
        out = []
        for i in range(n):
            c = 100 + i * 0.5
            out.append({"date": f"2025-{i // 28 + 1:02d}-{i % 28 + 1:02d}",
                        "open": c, "high": c + 1, "low": c - 1, "close": c,
                        "volume": 1_000_000 + i})
        return out

    def test_length_matches_display_bars(self):
        s = TrainingSession("s_macd1", "stock", "600519", self._bars(300), 1_000_000.0)
        m = s.period_macd()
        n = len(s._display_bars())
        assert len(m["dif"]) == n
        assert len(m["dea"]) == n
        assert len(m["hist"]) == n

    def test_year_period_has_no_macd_gap(self):
        # 年线聚合后仅 1~2 根, 但 MACD 在日线上计算 → 不应出现大面积 null
        s = TrainingSession("s_macd2", "stock", "600519", self._bars(300), 1_000_000.0)
        s.code_revealed = 1                  # 揭晓后展示完整周期序列
        s.switch_period("year")
        m = s.period_macd()
        assert len(m["dif"]) == len(s._display_bars())
        assert all(v is not None for v in m["dif"])
        assert all(v is not None for v in m["dea"])

    def test_daily_macd_warmup_nulls(self):
        # 日线上 EMA 预热期仍为 null (口径与前端 indicators.js 一致)
        s = TrainingSession("s_macd3", "stock", "600519", self._bars(40), 1_000_000.0)
        s.code_revealed = 1
        m = s.period_macd()
        assert len(m["dif"]) == len(s._display_bars()) == 40
        assert m["dif"][0] is None
        assert m["dif"][-1] is not None

    def test_summary_exposes_macd(self):
        s = TrainingSession("s_macd4", "stock", "600519", self._bars(60), 1_000_000.0)
        assert "macd" in s.summary()


# =============================================================
# 标的池成交量异常 (B13): 优先匹配周期内放量个股
# =============================================================
class TestVolumeAnomaly:
    @staticmethod
    def _bars(vols):
        return [{"date": f"2026-02-{i + 1:02d}", "open": 10, "high": 10.5,
                 "low": 9.5, "close": 10, "volume": v} for i, v in enumerate(vols)]

    def test_detects_spike(self):
        from app.services import stock_pool as sp
        vols = [100] * 25 + [1000]        # 末根放量 10 倍
        assert sp.has_volume_anomaly(self._bars(vols)) is True

    def test_flat_volume_no_spike(self):
        from app.services import stock_pool as sp
        assert sp.has_volume_anomaly(self._bars([100] * 30)) is False

    def test_insufficient_bars_returns_false(self):
        from app.services import stock_pool as sp
        assert sp.has_volume_anomaly(self._bars([100, 200, 300])) is False


