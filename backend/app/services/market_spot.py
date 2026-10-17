"""全市场即时快照服务 —— 动态分组指标的数据底座

数据源:
  akshare.stock_zh_a_spot()  (新浪, 一次返回全市场 ≈5500 只, 单次约 19s)

派生字段 (优先集指标全部可由快照直接算出, 无需历史序列):
  - 涨跌停价 / 是否涨停 / 是否跌停 / 是否炸板(曾涨停后开板)
  - 上影线比例% = (最高 - max(今开, 最新)) / 昨收 * 100
  - 涨跌幅 / 成交量 / 成交额 / 最新价 / 今开 / 收盘价

缓存 (需求101605): 默认 600s TTL + 单飞 + stale-while-revalidate + 磁盘快照, 避免 20~35s 冷拉
阻塞「自选模块进入 / 切换分组」; 拉取失败时回退上一份缓存。
"""
from __future__ import annotations
import json
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

_CODE_PREFIXES = ("bj", "sh", "sz")


def _norm_code(raw: str) -> str:
    c = str(raw or "").strip().lower()
    for p in _CODE_PREFIXES:
        if c.startswith(p):
            return c[len(p):]
    return c


def _split_prefix(raw: str) -> str:
    low = str(raw or "").strip().lower()
    for p in _CODE_PREFIXES:
        if low.startswith(p):
            return p
    return ""


def _limit_pct(prefix: str, code: str, name: str) -> float:
    """涨跌停幅度: 北交所 30% / 创业板·科创板 20% / ST 5% / 其余 10%"""
    if prefix == "bj":
        return 0.30
    if code.startswith(("300", "301", "688", "689")):
        return 0.20
    if "ST" in (name or "").upper():
        return 0.05
    return 0.10


class MarketSpot:
    """需求101605: 单飞 + stale-while-revalidate

    - 缓存新鲜 (<= ttl) → 直接返回
    - 缓存已过期但有旧值 → **立即返回旧值**, 同时后台单飞刷新 (切分组不再卡 20~35s)
    - 无任何缓存 (进程首次) → 同步拉取, 期间并发请求共享同一次拉取 (单飞, 避免各拉各的)
    """

    def __init__(self, ttl: float = 600.0, fetcher: Optional[Callable[[], Any]] = None):
        self._ttl = ttl                    # 需求101605: 300 → 600 (与后端预热周期对齐)
        self._fetcher = fetcher          # 测试可注入假数据源
        self._cache: Dict[str, dict] = {}
        self._ts = 0.0
        self._lock = threading.RLock()
        self._fetching = False           # 单飞标志: 同一时刻只允许一个拉取在跑
        self._done = threading.Event()   # 等待首次拉取完成的信号
        # 需求101605: 磁盘快照 —— 进程重启后首次进入自选也能「立即有旧值 + 后台刷新」
        self._snap_path = Path(__file__).resolve().parent.parent / "data" / "market_spot.json"
        if self._fetcher is None:        # 测试注入 fetcher 时不读磁盘, 避免串扰
            self._load_snapshot()

    # ---------- 磁盘快照 (跨进程重启复用) ----------
    def _load_snapshot(self):
        try:
            if not self._snap_path.exists():
                return
            obj = json.loads(self._snap_path.read_text(encoding="utf-8"))
            data = obj.get("data") if isinstance(obj, dict) else None
            ts = float(obj.get("ts") or 0) if isinstance(obj, dict) else 0.0
            if data:
                with self._lock:
                    self._cache = data
                    self._ts = ts or self._snap_path.stat().st_mtime
        except Exception:
            pass

    def _save_snapshot(self, data: Dict[str, dict]):
        try:
            self._snap_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._snap_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"ts": time.time(), "data": data},
                                      ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            tmp.replace(self._snap_path)
        except Exception:
            pass


    # ---------- 数据源 ----------
    def _fetch(self):
        if self._fetcher is not None:
            return self._fetcher()
        import akshare as ak
        return ak.stock_zh_a_spot()

    def _parse(self, df) -> Dict[str, dict]:
        out: Dict[str, dict] = {}
        if df is None:
            return out
        try:
            records = df.to_dict("records")
        except Exception:
            return out
        for row in records:
            raw = str(row.get("代码") or "").strip()
            if not raw:
                continue
            prefix = _split_prefix(raw)
            code = _norm_code(raw)
            if len(code) != 6 or not code.isdigit():
                continue
            name = str(row.get("名称") or "")

            def num(key: str) -> float:
                try:
                    return float(row.get(key))
                except (TypeError, ValueError):
                    return 0.0

            price = num("最新价")
            prev = num("昨收")
            op = num("今开")
            high = num("最高")
            low = num("最低")
            vol = num("成交量")
            amt = num("成交额")
            pct = num("涨跌幅")

            lp = _limit_pct(prefix, code, name)
            up = round(prev * (1 + lp), 2) if prev else 0.0
            dn = round(prev * (1 - lp), 2) if prev else 0.0
            active = prev > 0 and price > 0
            out[code] = {
                "code": code,
                "name": name,
                "market": prefix or "",
                "price": price,
                "prev_close": prev,
                "open": op,
                "high": high,
                "low": low,
                "volume": int(vol),
                "amount": amt,
                "change_pct": round(pct, 2),
                "limit_pct": lp,
                "limit_up_price": up,
                "limit_down_price": dn,
                "is_limit_up": bool(active and price >= up - 1e-6),
                "is_limit_down": bool(active and price <= dn + 1e-6),
                "is_zhaban": bool(active and high >= up - 1e-6 and price < up - 1e-6),
                "upper_shadow": (round((high - max(op, price)) / prev * 100, 2) if prev else 0.0),
            }
        return out

    # ---------- 对外 ----------
    def _refresh(self) -> Dict[str, dict]:
        """执行一次拉取并写入缓存 (调用方需保证 _fetching 单飞语义)"""
        with self._lock:
            self._fetching = True
            self._done.clear()
        try:
            data = self._parse(self._fetch())
            with self._lock:
                if data:
                    self._cache = data
                    self._ts = time.time()
            if data and self._fetcher is None:
                self._save_snapshot(data)     # 需求101605: 落盘, 供重启后秒用
        except Exception as e:            # 断连 → 保留旧缓存, 不阻断接口
            print(f"[MarketSpot] 全市场快照获取失败: {e}")
        finally:
            with self._lock:
                self._fetching = False
                self._done.set()
                return self._cache

    def get_spot(self, force: bool = False, wait: bool = True) -> Dict[str, dict]:
        """取全市场快照

        ``wait=False`` (需求101607): 若有在途刷新, 立即返回旧值而不等待 —— 供「批量列表
        接口」(搜索/未分组, 一次要取几百只报价) 使用, 避免被全市场刷新 (20~35s) 阻塞。
        """
        now = time.time()
        with self._lock:
            fresh = bool(self._cache) and not force and now - self._ts < self._ttl
            if fresh:
                return self._cache
            has_stale = bool(self._cache)
            if not self._fetching:
                already = True           # 本线程抢到刷新权
            else:
                already = False
        if already:
            if has_stale:
                # 需求101605: 有旧值 → 后台刷新, 立即返回旧值 (stale-while-revalidate)
                def _bg():
                    self._refresh()
                threading.Thread(target=_bg, name="market-spot-refresh", daemon=True).start()
                with self._lock:
                    return self._cache
            # 无缓存 (进程首次) → 同步拉取
            return self._refresh()
        if not wait and has_stale:
            with self._lock:             # 需求101607: 不等待在途刷新, 直接用旧值
                return self._cache
        # 已有其他线程在拉取 → 等待它完成 (并发共享同一次拉取, 避免各拉各的)
        self._done.wait(timeout=self._ttl)
        with self._lock:
            return self._cache



market_spot = MarketSpot()
