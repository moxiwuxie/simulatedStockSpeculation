"""实时行情数据源 - 新浪财经接口（批量+轻量+稳定）"""
from __future__ import annotations
import threading
import time
import random
import requests
from typing import Dict, Optional, List
from dataclasses import dataclass

from ..models import Stock, KLine


@dataclass
class RealtimeQuote:
    """一次拉取到的股票实时行情"""
    code: str           # 纯数字代码 600519
    name: str           # 名称
    price: float        # 最新价
    prev_close: float   # 昨收
    open_price: float   # 今开
    high: float
    low: float
    volume: int         # 成交量（股）
    amount: float       # 成交额（元）


class RealTimeFeed:
    """
    新浪财经批量实时行情接入
    - 接口: https://hq.sinajs.cn/list=sh600519,sz000001,...
    - 特点: 稳定、批量、轻量、免费
    - 间隔: 2-3 秒/批，避免触发限流
    """

    # 纯代码 → 新浪前缀
    SH_PREFIX = "sh"  # 沪市 (60xxxx, 68xxxx 科创板)
    SZ_PREFIX = "sz"  # 深市 (00xxxx, 30xxxx 创业板)

    def __init__(self, refresh_interval: float = 3.0):
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": random.choice([
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            ]),
            "Referer": "https://finance.sina.com.cn/",
        })
        self._interval = refresh_interval
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._callbacks: List = []
        self._lock = threading.Lock()

    # ========== 订阅 ==========

    def subscribe(self, callback):
        """注册行情更新回调: callback(List[RealtimeQuote])"""
        with self._lock:
            self._callbacks.append(callback)

    def subscribe_stock(self, code: str) -> str:
        """纯代码 → 新浪前缀代码"""
        if code.startswith(("6", "9")):
            return f"{self.SH_PREFIX}{code}"
        return f"{self.SZ_PREFIX}{code}"

    # ========== 拉取 ==========

    def fetch_once(self, codes: List[str]) -> Dict[str, RealtimeQuote]:
        """拉取一批股票的实时行情"""
        # 构建新浪代码
        sina_codes = [self.subscribe_stock(c) for c in codes]
        url = f"https://hq.sinajs.cn/list={','.join(sina_codes)}"

        try:
            r = self._session.get(url, timeout=8)
            r.encoding = "gbk"
            return self._parse(r.text, codes)
        except Exception as e:
            print(f"[RealTimeFeed] 拉取失败: {e}")
            return {}

    def _parse(self, text: str, expected_codes: List[str]) -> Dict[str, RealtimeQuote]:
        """解析新浪返回的 gb2312 文本"""
        result = {}
        for line in text.strip().split(";"):
            if "=" not in line:
                continue
            # var hq_str_sh600519="贵州茅台,1262.99,..."
            body = line.split("=", 1)[1].strip('"')
            if not body:
                continue
            parts = body.split(",")
            if len(parts) < 10:
                continue
            # 从 line 里提取代码
            import re
            m = re.search(r"hq_str_(s[hz])(\d+)", line)
            if not m:
                continue
            pure_code = m.group(2)

            result[pure_code] = RealtimeQuote(
                code=pure_code,
                name=parts[0],
                price=float(parts[3]) if parts[3] else 0,
                prev_close=float(parts[2]) if parts[2] else 0,
                open_price=float(parts[1]) if parts[1] else 0,
                high=float(parts[4]) if parts[4] else 0,
                low=float(parts[5]) if parts[5] else 0,
                volume=int(float(parts[8])) if parts[8] else 0,
                amount=float(parts[9]) if parts[9] else 0,
            )
        return result

    # ========== 后台推送 ==========

    def start(self, codes_provider):
        """
        启动后台线程定时拉取
        codes_provider: callable → List[str] (当前需要监控的代码)
        """
        self._running = True
        self._codes_provider = codes_provider
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False

    def _run_loop(self):
        import time
        while self._running:
            try:
                codes = self._codes_provider()
                if codes:
                    quotes = self.fetch_once(codes)
                    if quotes:
                        with self._lock:
                            for cb in self._callbacks:
                                try:
                                    cb(list(quotes.values()))
                                except Exception as e:
                                    print(f"[RealTimeFeed] 回调异常: {e}")
            except Exception as e:
                print(f"[RealTimeFeed] 循环异常: {e}")
            time.sleep(self._interval + random.uniform(0, 0.5))
