"""系统配置"""
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # 初始资金
    INITIAL_CASH: float = 1_000_000.0

    # 账户分级初始资金 (模拟交易资金体系)
    TIER_CASH: dict = {
        "basic": 1_000_000.0,      # 基础
        "pro":   5_000_000.0,      # 进阶
        "vip":  10_000_000.0,      # 专业
        "large": 50_000_000.0,     # 大户
    }

    # 模拟交易: 单笔委托金额不得超过该股当日成交额的比例
    MAX_ORDER_AMOUNT_RATIO: float = 0.05

    # 费率
    COMMISSION_RATE: float = 0.00025  # 佣金率 0.025%
    MIN_COMMISSION: float = 5.0  # 最低佣金
    STAMP_DUTY_RATE: float = 0.0005  # 印花税 0.05% (仅卖出)
    TRANSFER_FEE_RATE: float = 0.00001  # 过户费 0.001%

    # 涨跌停
    LIMIT_UP_PCT: float = 0.10
    LIMIT_UP_PCT_KCB: float = 0.20

    # 行情刷新间隔(秒)
    TICK_INTERVAL: float = 1.0

    # 交易时间
    MORNING_OPEN: str = "09:30"
    MORNING_CLOSE: str = "11:30"
    AFTERNOON_OPEN: str = "13:00"
    AFTERNOON_CLOSE: str = "15:00"

    # 模拟模式 - 跳过交易时间校验
    SIMULATION_MODE: bool = True

    model_config = {"env_file": ".env"}


settings = Settings()
