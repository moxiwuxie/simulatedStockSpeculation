"""示例股票数据"""
from ..models import Stock


DEFAULT_STOCKS = [
    Stock(code="600519", name="贵州茅台", price=1680.50, prev_close=1680.50),
    Stock(code="000001", name="平安银行", price=12.35, prev_close=12.35),
    Stock(code="601318", name="中国平安", price=48.90, prev_close=48.90),
    Stock(code="600036", name="招商银行", price=35.60, prev_close=35.60),
    Stock(code="000858", name="五粮液", price=148.20, prev_close=148.20),
    Stock(code="601888", name="中国中免", price=78.50, prev_close=78.50),
    Stock(code="300750", name="宁德时代", price=186.30, prev_close=186.30, is_kcb=True),
    Stock(code="688981", name="中芯国际", price=58.20, prev_close=58.20, is_kcb=True),
]
