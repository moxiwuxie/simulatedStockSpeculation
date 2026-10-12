"""行情基准日 (开市/休市) + 日K缓存 TTL 单测

运行: cd backend && .venv/bin/python3 -m pytest tests/test_market_feed.py -q
"""
from __future__ import annotations
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.models import KLine
from app.services.replay_feed import ReplayFeed


def mk(d: date, close: float = 10.0) -> KLine:
    return KLine(open=close, high=close, low=close, close=close,
                 volume=100, amount=1000.0,
                 timestamp=datetime(d.year, d.month, d.day))


class TestMarketRefDay:
    """market_ref_day: 今日开市(交易日)→当日; 不开市→不晚于今日的最近交易日"""

    def _feed(self, days):
        f = ReplayFeed()
        f.get_trading_days = lambda: days     # 覆写为受控交易日历
        return f

    def test_open_when_today_is_trading_day(self):
        today = date.today()
        days = [today - timedelta(days=3), today, today + timedelta(days=1)]
        ref, is_open = self._feed(days).market_ref_day()
        assert is_open is True
        assert ref == today

    def test_closed_falls_back_to_prev_trading_day(self):
        today = date.today()
        prev = today - timedelta(days=2)
        # 日历含未来日期 (akshare 延伸到年末的场景), 不得被选中
        days = [prev, today + timedelta(days=1), today + timedelta(days=5)]
        ref, is_open = self._feed(days).market_ref_day()
        assert is_open is False
        assert ref == prev

    def test_empty_calendar_returns_today_closed(self):
        ref, is_open = self._feed([]).market_ref_day()
        assert is_open is False
        assert ref == date.today()


class TestKlineCacheTTL:
    """日K缓存带 TTL: 保证行情模块能刷新到「当天」数据"""

    def test_cache_hit_within_ttl(self):
        f = ReplayFeed(kline_ttl=3600.0)
        calls = {"n": 0}

        def fake_fetch(code):
            calls["n"] += 1
            return [mk(date.today())]

        f._fetch_kline = fake_fetch
        f.get_kline("600519")
        f.get_kline("600519")
        assert calls["n"] == 1                     # TTL 内命中缓存, 不重复拉取

    def test_cache_refresh_after_ttl(self):
        f = ReplayFeed(kline_ttl=0.0)               # TTL=0 → 每次过期重新拉取
        calls = {"n": 0}

        def fake_fetch(code):
            calls["n"] += 1
            return [mk(date.today(), close=float(calls["n"]))]

        f._fetch_kline = fake_fetch
        assert f.get_kline("600519")[0].close == 1.0
        assert f.get_kline("600519")[0].close == 2.0   # 过期刷新
        assert calls["n"] == 2

    def test_invalidate_clears_cache(self):
        f = ReplayFeed(kline_ttl=3600.0)
        calls = {"n": 0}

        def fake_fetch(code):
            calls["n"] += 1
            return [mk(date.today())]

        f._fetch_kline = fake_fetch
        f.get_kline("600519")
        f.invalidate("600519")
        f.get_kline("600519")
        assert calls["n"] == 2


class TestBoardIntradayEmpty:
    """板块分时 (A3): 指定历史日期无真实分钟数据 → 返回空结构, 不回退最新日"""

    def _patch(self, monkeypatch, minute_rows):
        from app.services import boards
        monkeypatch.setattr(boards, "board_stocks", lambda kind, label: [{"code": "600519"}])
        monkeypatch.setattr(boards, "_top_codes", lambda cons, n: ["600519"])
        monkeypatch.setattr(boards, "_fetch_minute", lambda codes: {"600519": minute_rows})

    def test_missing_date_returns_empty(self, monkeypatch):
        from app.services import boards
        self._patch(monkeypatch, [{"day": "2026-01-05", "time": "09:31",
                                   "price": 10.0, "volume": 100}])
        out = boards.board_intraday(object(), "industry", "白酒", date="2020-01-01")
        assert out["empty"] is True
        assert out["points"] == []
        assert out["date"] == "2020-01-01"      # 不静默回退最新交易日
        assert out["is_board"] is True

    def test_no_minute_data_returns_empty(self, monkeypatch):
        from app.services import boards
        self._patch(monkeypatch, [])
        out = boards.board_intraday(object(), "industry", "白酒", date="2026-01-05")
        assert out["empty"] is True
        assert out["points"] == []
