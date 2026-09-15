from dataclasses import dataclass, field
from typing import Optional
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
    ts: float = field(default_factory=time.time)


@dataclass
class SignalEvent:
    """信号事件"""
    inst_id: str
    signal_type: str
    score: float = 0.0
    reason: str = ""
    ts: float = field(default_factory=time.time)


@dataclass
class RiskEvent:
    """风控事件"""
    event_type: str
    inst_id: str = ""
    detail: str = ""
    ts: float = field(default_factory=time.time)