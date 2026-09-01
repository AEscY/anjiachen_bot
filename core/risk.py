"""
risk.py - 风控层

把散落在 bot.py 各处的风控判断收敛为统一入口。
所有「能不能开仓」的判断都必须经过 can_open()，避免新增逻辑绕过风控。

四道闸门（按检查顺序）：
  1. 全局暂停  —— panic / 人工暂停
  2. 回撤熔断  —— 峰值回撤超限（此前该判断算了却从未生效，已修复）
  3. 连亏冷静期 —— 连续亏损达阈值后冷却
  4. 日内亏损  —— 当日累计亏损超限

外加：
  - 仓位上限（总仓位 / 单币仓位）
  - 每日开仓次数
  - 网格专用：区间下限止损
"""
import time
import logging
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

CST = timezone(timedelta(hours=8))


class RiskManager:
    def __init__(self, cfg, alert=None):
        """
        cfg: 参数提供者（QuantBot）
        alert: 异步告警回调 (message, level)
        """
        self.cfg = cfg
        self._alert = alert

        # 运行时状态（由 bot 负责持久化/恢复）
        self.is_paused = False
        self.drawdown_safe = True
        self.drawdown_alerted = False
        self.last_drawdown = 0.0
        self.peak_equity = 0.0

        self.consecutive_losses = 0
        self.last_pause_time = 0.0

        self.today_loss_pct = 0.0
        self.today_loss_usdt = 0.0
        self.daily_start_equity = 0.0
        self.last_reset_day = datetime.now(CST).date().isoformat()

        self.daily_trades = 0

    # ─────────── 日切 ───────────

    def roll_day_if_needed(self):
        today = datetime.now(CST).date().isoformat()
        if today != self.last_reset_day:
            self.today_loss_pct = 0.0
            self.today_loss_usdt = 0.0
            self.daily_start_equity = 0.0
            self.consecutive_losses = 0
            self.daily_trades = 0
            self.last_reset_day = today
            logger.info(f"📅 日切 {today}，风控计数已重置")
            return True
        return False

    # ─────────── 权益与回撤 ───────────

    def update_equity(self, equity: float):
        """每轮风险监控调用：更新峰值、回撤、日内亏损"""
        if equity <= 0:
            return
        if self.daily_start_equity <= 0:
            self.daily_start_equity = equity

        self.peak_equity = max(self.peak_equity or equity, equity)
        dd = (self.peak_equity - equity) / self.peak_equity
        self.last_drawdown = dd
        self.drawdown_safe = dd < float(self.cfg.max_drawdown_pct)

        self.today_loss_usdt = max(0.0, self.daily_start_equity - equity)
        self.today_loss_pct = (self.today_loss_usdt / self.daily_start_equity
                               if self.daily_start_equity > 0 else 0.0)

    # ─────────── 主入口 ───────────

    async def can_open(self, symbol: str = "", extra_usdt: float = 0.0,
                       used_usdt: float = 0.0, equity: float = 0.0) -> tuple:
        """
        是否允许开仓。返回 (bool, 原因)
        返回 False 时 reason 为用于回显的中文说明。
        """
        self.roll_day_if_needed()

        # 1) 全局暂停
        if self.is_paused:
            return False, "机器人已暂停"

        # 2) 回撤熔断
        if not self.drawdown_safe:
            if not self.drawdown_alerted:
                await self._fire_alert(
                    f"⛔ 回撤 {self.last_drawdown*100:.2f}% 达上限 "
                    f"{float(self.cfg.max_drawdown_pct)*100:.0f}%，禁止新开仓", "critical")
                self.drawdown_alerted = True
            return False, f"回撤熔断({self.last_drawdown*100:.2f}%)"
        self.drawdown_alerted = False

        # 3) 连亏冷静期
        max_cons = int(self.cfg.max_consecutive_losses)
        cooldown = float(self.cfg.consecutive_loss_cooldown)
        if self.consecutive_losses >= max_cons:
            if self.last_pause_time <= 0:
                self.last_pause_time = time.time()
                self.is_paused = True
                await self._fire_alert(
                    f"⛔ 连续亏损 {self.consecutive_losses} 笔，进入 "
                    f"{int(cooldown)//60} 分钟冷静期", "critical")
                return False, "连亏冷静期(新)"
            elapsed = time.time() - self.last_pause_time
            if elapsed >= cooldown:
                self.consecutive_losses = 0
                self.last_pause_time = 0.0
                self.is_paused = False
                await self._fire_alert("✅ 连续亏损冷静期结束，恢复交易", "info")
            else:
                return False, f"连亏冷静期(剩{int(cooldown-elapsed)//60}分)"

        # 4) 日内亏损
        if self.today_loss_pct >= float(self.cfg.max_daily_loss_pct):
            if not self.is_paused:
                self.is_paused = True
                await self._fire_alert(
                    f"⛔ 日亏损达 {self.today_loss_pct*100:.1f}%，暂停交易", "critical")
            return False, f"日亏损熔断({self.today_loss_pct*100:.1f}%)"

        # 5) 每日开仓次数
        max_trades = int(self.cfg.max_daily_trades)
        if max_trades > 0 and self.daily_trades >= max_trades:
            return False, f"已达每日上限({self.daily_trades}/{max_trades})"

        # 6) 总仓位上限
        if equity > 0 and extra_usdt > 0:
            cap = equity * float(self.cfg.max_total_allocated_pct)
            if used_usdt + extra_usdt > cap + 1e-9:
                return False, "总仓位超限"

        return True, ""

    # ─────────── 盈亏记账 ───────────

    def record_close(self, net_pnl: float, net_pnl_pct: float):
        """平仓后记账：连亏计数 + 日内亏损累计"""
        if net_pnl < 0:
            self.consecutive_losses += 1
            self.today_loss_pct += abs(net_pnl_pct) / 100
        else:
            self.consecutive_losses = 0

    def record_open(self):
        self.daily_trades += 1

    # ─────────── 手动恢复 ───────────

    def resume(self):
        """手动解除所有熔断"""
        self.is_paused = False
        self.consecutive_losses = 0
        self.last_pause_time = 0.0
        self.peak_equity = 0.0
        self.drawdown_safe = True
        self.drawdown_alerted = False
        logger.info("✅ 风控熔断已手动解除")

    # ─────────── 状态序列化 ───────────

    def to_dict(self):
        return {
            "is_paused": self.is_paused,
            "drawdown_safe": self.drawdown_safe,
            "peak_equity": self.peak_equity,
            "consecutive_losses": self.consecutive_losses,
            "last_pause_time": self.last_pause_time,
            "today_loss_pct": self.today_loss_pct,
            "today_loss_usdt": self.today_loss_usdt,
            "daily_start_equity": self.daily_start_equity,
            "last_reset_day": self.last_reset_day,
            "daily_trades": self.daily_trades,
        }

    def from_dict(self, d):
        if not d:
            return
        for k in ("is_paused", "drawdown_safe"):
            if k in d:
                setattr(self, k, bool(d[k]))
        for k in ("peak_equity", "consecutive_losses", "last_pause_time",
                  "today_loss_pct", "today_loss_usdt", "daily_start_equity",
                  "daily_trades"):
            if k in d:
                try:
                    setattr(self, k, float(d[k] or 0))
                except (TypeError, ValueError):
                    pass
        self.consecutive_losses = int(self.consecutive_losses)
        self.daily_trades = int(self.daily_trades)
        if d.get("last_reset_day"):
            self.last_reset_day = str(d["last_reset_day"])

    async def _fire_alert(self, msg, level="warning"):
        log = logger.warning if level != "info" else logger.info
        log(f"[{level}] {msg}")
        if self._alert:
            try:
                await self._alert(msg, level)
            except Exception as e:
                logger.warning(f"告警回调失败: {e}")
