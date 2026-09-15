import logging
import asyncio
from datetime import date
from core.events import RiskEvent
from core.event_bus import bus

logger = logging.getLogger(__name__)


class RiskManager:
    """
    参考 tingxifa/okx-grid-bot 的风险管理设计：
    - 最大回撤限制 (MAX_DRAWDOWN)
    - 每日亏损限制 (DAILY_LOSS_LIMIT)
    - 最大仓位比例限制 (MAX_POSITION_RATIO)
    """

    def __init__(self, config=None):
        cfg = config or {}
        self.max_drawdown = cfg.get("max_drawdown", 0.10)         # 10%
        self.daily_loss_limit = cfg.get("daily_loss_limit", 50.0)  # 50 USDT
        self.max_position_ratio = cfg.get("max_position_ratio", 0.5)  # 50%

        self.initial_capital = 0.0
        self.peak_capital = 0.0
        self.current_capital = 0.0
        self.daily_pnl = 0.0
        self.today = date.today()
        self._risk_triggered = False

    def set_initial_capital(self, capital: float):
        self.initial_capital = capital
        self.peak_capital = capital
        self.current_capital = capital

    def update_capital(self, capital: float):
        self.current_capital = capital
        if capital > self.peak_capital:
            self.peak_capital = capital
        self._check_risk()

    def add_pnl(self, pnl: float):
        """记录盈亏"""
        self.daily_pnl += pnl
        self.current_capital += pnl
        if self.current_capital > self.peak_capital:
            self.peak_capital = self.current_capital
        self._check_risk()

    def _check_risk(self):
        today = date.today()
        if today != self.today:
            self.today = today
            self.daily_pnl = 0.0
            self._risk_triggered = False

        # 每日亏损检查
        if self.daily_pnl <= -self.daily_loss_limit:
            if not self._risk_triggered:
                logger.warning(f"触发每日亏损限制: {self.daily_pnl:.2f}")
                asyncio.create_task(bus.publish(RiskEvent(
                    event_type="daily_loss",
                    detail=f"今日亏损 {self.daily_pnl:.2f} USDT，超过限制 {self.daily_loss_limit}"
                )))
                self._risk_triggered = True

        # 最大回撤检查
        if self.peak_capital > 0:
            drawdown = (self.peak_capital - self.current_capital) / self.peak_capital
            if drawdown >= self.max_drawdown:
                if not self._risk_triggered:
                    logger.warning(f"触发最大回撤限制: {drawdown*100:.2f}%")
                    asyncio.create_task(bus.publish(RiskEvent(
                        event_type="max_drawdown",
                        detail=f"回撤 {drawdown*100:.2f}%，超过限制 {self.max_drawdown*100:.0f}%"
                    )))
                    self._risk_triggered = True

    def is_risk_triggered(self) -> bool:
        return self._risk_triggered

    def reset(self):
        self._risk_triggered = False
        self.daily_pnl = 0.0
        self.today = date.today()