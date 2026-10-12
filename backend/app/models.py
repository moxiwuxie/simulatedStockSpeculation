"""数据模型定义"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional, List
import uuid


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"  # 市价单
    LIMIT = "LIMIT"  # 限价单


class OrderStatus(str, Enum):
    PENDING = "PENDING"  # 待撮合成交
    PARTIAL = "PARTIAL"  # 部分成交
    FILLED = "FILLED"  # 全部成交
    CANCELED = "CANCELED"  # 已取消
    REJECTED = "REJECTED"  # 已拒绝


@dataclass
class Stock:
    code: str
    name: str
    price: float  # 当前价
    prev_close: float  # 昨收
    open_price: float = 0.0  # 开盘价
    high: float = 0.0
    low: float = 0.0
    volume: int = 0  # 成交量（股）
    amount: float = 0.0  # 成交额
    is_kcb: bool = False  # 是否科创板/创业板(±20%)

    def limit_up(self) -> float:
        """涨停价"""
        pct = 0.20 if self.is_kcb else 0.10
        return round(self.prev_close * (1 + pct), 2)

    def limit_down(self) -> float:
        """跌停价"""
        pct = 0.20 if self.is_kcb else 0.10
        return round(self.prev_close * (1 - pct), 2)

    def change_pct(self) -> float:
        """涨跌幅"""
        if self.prev_close == 0:
            return 0.0
        return round((self.price - self.prev_close) / self.prev_close * 100, 2)


@dataclass
class Order:
    order_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    stock_code: str = ""
    side: OrderSide = OrderSide.BUY
    order_type: OrderType = OrderType.LIMIT
    price: float = 0.0
    quantity: int = 0  # 委托数量
    filled_quantity: int = 0  # 已成交数量
    status: OrderStatus = OrderStatus.PENDING
    created_at: datetime = field(default_factory=datetime.now)
    user_id: str = "default"

    @property
    def remaining(self) -> int:
        return self.quantity - self.filled_quantity


@dataclass
class Trade:
    trade_id: str = field(default_factory=lambda: str(uuid.uuid4))
    order_id: str = ""
    stock_code: str = ""
    side: OrderSide = OrderSide.BUY
    price: float = 0.0
    quantity: int = 0
    amount: float = 0.0
    fee: float = 0.0              # 单边手续费（本order侧）
    buyer_fee: float = 0.0        # 买方承担的手续费（双边撮合时）
    seller_fee: float = 0.0       # 卖方承担的手续费（双边撮合时）
    created_at: datetime = field(default_factory=datetime.now)
    buyer_id: str = ""
    seller_id: str = ""


@dataclass
class Position:
    stock_code: str
    quantity: int = 0  # 持仓数量
    available: int = 0  # 可卖数量（T+1）
    avg_cost: float = 0.0  # 平均成本
    market_value: float = 0.0  # 市值


@dataclass
class Account:
    user_id: str = "default"
    tier: str = "basic"   # basic / pro / vip
    cash: float = 0.0
    frozen_cash: float = 0.0
    total_assets: float = 0.0
    positions: dict = field(default_factory=dict)

    @property
    def total_position_value(self) -> float:
        return sum(p.market_value for p in self.positions.values())


@dataclass
class Tick:
    """行情快照"""
    stock_code: str
    price: float
    volume: int
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class KLine:
    open: float
    high: float
    low: float
    close: float
    volume: int
    amount: float
    timestamp: datetime = field(default_factory=datetime.now)
