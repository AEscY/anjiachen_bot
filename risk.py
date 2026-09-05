"""
风控。单一职责：给定当前权益，回答"能不能开仓"。

与旧版的关键区别：
  旧版有两套风控（_check_risk_limits 给单次模式、risk.can_open 给网格），
  共享状态却各写一遍判定逻辑，导致"暂停"在单次模式下形同虚设。
  这里只有一份判定，两个模式共用。

另一个坑：权益口径必须唯一。
  旧版网格循环传 min(实际, cap)，风控任务传真实总资产，
  两者更新同一个 peak_equity，设了 cap 后回撤瞬间算成 99.99%，
  永久熔断且无任何日志。这里 cap 概念已被移除。
"""
import logging
import time

logger = logging.getLogger(__name__)


class Risk:
    def __init__(self, state: dict, params):
        self.s = state
        self.p = params
        self.reason = ""          # 最近一次拦截原因
        self.paused_until = 0.0   # 冷却截止时间（epoch）

    # ─────────── 权益记录 ───────────

    def update(self, equity: float) -> None:
        """每轮调用。equity 必须是【真实总权益】，不允许传其他口径。"""
        if equity <= 0:
            return

        r = self.s
        r["peak_equity"] = max(r.get("peak_equity", 0.0), equity)

        today = time.strftime("%Y-%m-%d", time.localtime())
        if r.get("day_start_date") != today:
            r["day_start_date"] = today
            r["day_start_equity"] = equity
            logger.info(f"新的一天，日基准权益 {equity:.2f}U")

    # ─────────── 判定 ───────────

    def can_open(self, equity: float) -> bool:
        """返回 True 表示可以开仓。拦截时把原因写进 self.reason。"""
        r = self.s
        self.reason = ""

        if r.get("retired"):
            self.reason = "已触发退役线，永久停机"
            return False

        if time.time() < self.paused_until:
            left = int((self.paused_until - time.time()) / 60)
            total = max(1, int(self.pause_minutes))
            # 剩余时间不可能超过总时长；出现即说明时间基准异常
            left = max(0, min(left, total))
            self.reason = f"冷却中（剩 {left}/{total} 分钟）"
            return False

        if equity <= 0:
            self.reason = "权益为 0"
            return False

        # 日亏损
        base = r.get("day_start_equity", 0.0)
        if base > 0:
            loss_pct = (base - equity) / base
            if loss_pct >= self.p.get("daily_loss"):
                self.pause(60)
                self.reason = (
                    f"日亏损 {loss_pct*100:.2f}% 达上限 "
                    f"{self.p.get('daily_loss')*100:.0f}%，暂停 1 小时")
                return False

        # 回撤
        peak = r.get("peak_equity", 0.0)
        if peak > 0:
            dd = (peak - equity) / peak
            if dd >= self.p.get("max_drawdown"):
                self.pause(60)
                self.reason = (
                    f"回撤 {dd*100:.2f}% 达上限 "
                    f"{self.p.get('max_drawdown')*100:.0f}%，暂停 1 小时")
                return False

        return True

    def pause(self, minutes: float) -> None:
        """暂停。记录总时长，供显示剩余时间用。"""
        self.pause_minutes = minutes
        self.paused_until = max(self.paused_until, time.time() + minutes * 60)

    def resume(self, equity: float = 0.0) -> None:
        """
        解除冷却。

        必须重置回撤峰值，否则会永久停摆：

            peak_equity 是【历史最高】。账户从 1000 跌到 850 后，
            回撤恒为 15% > 上限 12%，每轮都被拦。
            resume 只清冷却标志，下一轮算回撤又立刻重新暂停 ——
            用户点 resume 永远没用，且没有任何提示说明原因。

        重置 peak = 当前权益，表示"用户确认从当前位置继续"。
        日基准同理重置，避免日亏损判定也卡死。
        """
        self.paused_until = 0.0
        self.reason = ""
        if equity > 0:
            self.s["peak_equity"] = equity
            self.s["day_start_equity"] = equity

    # ─────────── 已实现盈亏 ───────────

    def add_realized(self, pnl: float) -> None:
        """
        记录已实现盈亏，并检查退役线。

        注意：退役线看【已实现】亏损，不看浮亏。
        浮亏会随行情波动，用它判定会在低谷误杀。
        """
        r = self.s
        r["realized_pnl"] = r.get("realized_pnl", 0.0) + pnl

        if not self.p.get("retire_on"):
            return
        limit = self.p.get("retire_loss")
        if r["realized_pnl"] <= -abs(limit):
            r["retired"] = True
            logger.error(
                f"退役线触发：累计已实现亏损 {r['realized_pnl']:.2f}U，永久停机")

    # ─────────── 展示 ───────────

    def summary(self, equity: float) -> str:
        r = self.s
        peak = r.get("peak_equity", 0.0)
        dd = ((peak - equity) / peak * 100) if peak > 0 else 0.0
        base = r.get("day_start_equity", 0.0)
        today_pct = ((equity - base) / base * 100) if base > 0 else 0.0

        lines = [
            f"权益      {equity:.2f}U",
            f"今日      {today_pct:+.2f}%",
            f"回撤      {dd:.2f}%（上限 {self.p.get('max_drawdown')*100:.0f}%）",
            f"已实现    {r.get('realized_pnl', 0.0):+.2f}U",
        ]
        if r.get("retired"):
            lines.append("🚨 已退役（永久停机）")
        elif time.time() < self.paused_until:
            left = max(0, int((self.paused_until - time.time()) / 60))
            lines.append(f"⏸ 冷却中（剩 {left} 分钟）：{self.reason}")
        return "\n".join(lines)
