from dataclasses import dataclass, field
from typing import Any, Optional
import time


@dataclass
class MarketEvent:
    """行情事件"""
    inst_id: str
    price: float
    raw: dict
    ts: float = field(default_factory=time.time)


@dataclass
class OrderEvent:
    """订单事件"""
    inst_id: str
    ord_id: str
    side: str
    state: str
    price: float
    size: float
    algo_id: Optional[str] = None
    ts: float = field(default_factory=time.time)


@dataclass
class SignalEvent:
    """信号事件"""
    inst_id: str
    signal_type: str   # buy / sell / stop_loss / take_profit / trailing
    score: float = 0.0
    reason: str = ""
    ts: float = field(default_factory=time.time)


@dataclass
class RiskEvent:
    """风控事件"""
    event_type: str    # daily_loss / max_drawdown / position_limit
    inst_id: str = ""
    detail: str = ""
    ts: float = field(default_factory=time.time)


@dataclass
class GridSubOrderEvent:
    """网格子订单事件"""
    algo_id: str
    inst_id: str
    ord_id: str
    side: str
    price: float
    size: float
    state: str
    pnl: float = 0.0
    fee: float = 0.0
    ts: float = field(default_factory=time.time)