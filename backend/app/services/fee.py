"""费率计算模块"""
from decimal import Decimal, ROUND_HALF_UP
from ..core.config import settings
from ..models import OrderSide


class FeeCalculator:
    """
    费率计算：
    - 佣金：成交额 * 0.025%，最低 5 元（买卖双向）
    - 印花税：成交额 * 0.05%（仅卖出）
    - 过户费：成交额 * 0.001%（买卖双向）
    """

    @staticmethod
    def calculate(amount: float, side: OrderSide) -> float:
        """计算交易费用"""
        # 佣金
        commission = max(amount * settings.COMMISSION_RATE, settings.MIN_COMMISSION)
        # 过户费
        transfer_fee = amount * settings.TRANSFER_FEE_RATE
        # 印花税（仅卖出）
        stamp_duty = amount * settings.STAMP_DUTY_RATE if side == OrderSide.SELL else 0.0

        total = commission + transfer_fee + stamp_duty
        return round(total, 2)

    @staticmethod
    def commission(amount: float) -> float:
        return round(max(amount * settings.COMMISSION_RATE, settings.MIN_COMMISSION), 2)

    @staticmethod
    def stamp_duty(amount: float) -> float:
        return round(amount * settings.STAMP_DUTY_RATE, 2)

    @staticmethod
    def transfer_fee(amount: float) -> float:
        return round(amount * settings.TRANSFER_FEE_RATE, 2)
