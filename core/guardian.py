"""
guardian.py - 保护层（补齐四项缺口中的三项）
=============================================

本模块补齐此前长期缺失的三类保护：

  1. PriceGuard —— 价格突变保护 + 滑点保护
  2. Retirement —— 策略退役线
  3. （日报在 reporter.py）

────────────────────────────────────────────
一、PriceGuard：价格突变与滑点
────────────────────────────────────────────

价格突变（下单前拦截）
----------------------
场景：交易所 API 抽风、瞬时闪崩/插针、WS 推送错值。
此时若照常按这个价格下单，会以离谱的价格成交。

原代码只有"行情陈旧"检测（ws_manager 时间戳），
无法识别【数据是新鲜的错误数据】这种情况。

做法：为每个币种维护近期价格滑动窗口，
用中位数作基准（不用均值 —— 均值会被异常值本身带偏），
新价格偏离中位数超过阈值即判定异常，暂停该币种一段时间。

为什么用中位数：
  窗口 [100, 101, 100, 99, 500]
    均值   = 180   ← 被 500 带偏，误判
    中位数 = 100   ← 稳健

滑点保护（成交后检测）
----------------------
市价单没有价格保护机制，成交价完全由盘口深度决定。
小额账户下单量小问题不大，但遇到剧烈行情，
实际成交均价可能明显偏离下单时看到的价格。

做法：下单前记录预期价格，成交后比对实际均价，
超出阈值即告警。这是"事后发现"而非"事前阻止"——
因为市价单无法事前限价，只能事后暴露问题。

────────────────────────────────────────────
二、Retirement：策略退役线
────────────────────────────────────────────

原有风控全是【局部】的：
  单笔止损、日内亏损上限、连续亏损冷却、回撤熔断

缺一条【全局】的底线：
  "这套策略从开始到现在，最多允许亏多少钱？"

没有这条线，可能出现：
  每天亏一点，每天都"没触发任何风控"，
  但一个月下来累计亏损已经很可观。

做法：累计净利跌破阈值 → 自动停止交易并告警，
需人工 /resume 才能恢复（不自动恢复 ——
自动恢复等于没有底线）。

状态必须持久化，否则 Render 重启后清零，
退役线形同虚设。
"""
import logging
import time
from collections import deque

logger = logging.getLogger(__name__)


class PriceGuard:
    """价格突变拦截 + 滑点检测"""

    def __init__(self, bot, alert=None, logger=None):
        self.bot = bot
        self._alert = alert
        self.logger = logger or logging.getLogger(__name__)

        # 每个币种的价格滑动窗口
        self._history = {}
        # sym -> 暂停到什么时候（时间戳）
        self._halt_until = {}
        # 告警节流，避免同一币种反复刷屏
        self._last_alert = {}

    # ─────────── 价格突变 ───────────

    def _window_size(self):
        return max(5, int(getattr(self.bot, "price_guard_window", 20) or 20))

    def _max_deviation(self):
        """允许的最大偏离比例，默认 8%"""
        v = getattr(self.bot, "price_guard_max_dev", None)
        return float(v) if v else 0.08

    def _halt_seconds(self):
        v = getattr(self.bot, "price_guard_halt_sec", None)
        return int(v) if v else 300

    def record(self, sym, price):
        """记录最新价格到滑动窗口"""
        try:
            p = float(price)
        except (TypeError, ValueError):
            return
        if p <= 0:
            return
        hist = self._history.setdefault(
            sym, deque(maxlen=self._window_size()))
        hist.append(p)

    @staticmethod
    def _median(values):
        """中位数 —— 对异常值稳健，不会被插针本身带偏"""
        if not values:
            return 0.0
        s = sorted(values)
        n = len(s)
        mid = n // 2
        if n % 2:
            return float(s[mid])
        return (float(s[mid - 1]) + float(s[mid])) / 2.0

    def is_halted(self, sym):
        until = self._halt_until.get(sym)
        if until is None:
            return False
        if time.time() >= until:
            self._halt_until.pop(sym, None)
            return False
        return True

    async def check(self, sym, price):
        """
        下单前调用。返回 True 表示价格可信，可以继续；
        False 表示判定异常，应跳过本轮。

        判定逻辑：
          · 样本不足（窗口未填满）→ 放行，先攒数据
          · 偏离中位数超过阈值 → 暂停该币种并告警
        """
        if not getattr(self.bot, "price_guard_enabled", True):
            return True

        # ⚠️ 必须先检查暂停状态。
        # 原实现只在【判定异常时】写入 _halt_until，
        # 却从未在入口处检查它 —— 于是下一轮价格恢复正常后
        # 偏离小于阈值，直接 return True 放行。
        # 暂停机制形同虚设，只在触发的那一轮生效过一次。
        if self.is_halted(sym):
            return False

        try:
            p = float(price)
        except (TypeError, ValueError):
            return False
        if p <= 0:
            return False

        self.record(sym, p)

        hist = self._history.get(sym)
        if not hist or len(hist) < self._window_size():
            # 样本不足，先放行攒数据
            return True

        med = self._median(hist)
        if med <= 0:
            return True

        dev = abs(p - med) / med
        if dev <= self._max_deviation():
            return True

        # 判定异常：暂停该币种
        self._halt_until[sym] = time.time() + self._halt_seconds()

        now = time.time()
        last = self._last_alert.get(sym, 0)
        if now - last > 600:          # 同一币种 10 分钟内只报一次
            self._last_alert[sym] = now
            direction = "高于" if p > med else "低于"
            msg = (
                f"⚠️ 价格异常，已暂停 {sym}\n"
                f"   当前价: {p:.6f}\n"
                f"   近期中位: {med:.6f}\n"
                f"   偏离: {dev*100:.1f}%（{direction}中位数，"
                f"上限 {self._max_deviation()*100:.0f}%）\n"
                f"   可能原因: 行情剧烈波动 / 交易所数据异常\n"
                f"   已暂停 {self._halt_seconds()} 秒，期间不下单"
            )
            self.logger.warning(f"[价格守卫] {sym} 偏离 {dev*100:.1f}%")
            if self._alert:
                await self._alert(msg, "warning")
        return False

    # ─────────── 滑点 ───────────

    def _slip_threshold(self):
        v = getattr(self.bot, "slippage_max_pct", None)
        return float(v) if v else 0.01      # 默认 1%

    async def check_slippage(self, sym, expected, actual, side="buy"):
        """
        成交后调用。实际成交均价与预期偏离过大时告警。

        expected: 下单时看到的价格
        actual  : 实际成交均价
        """
        if not getattr(self.bot, "slippage_guard_enabled", True):
            return True
        try:
            exp = float(expected)
            act = float(actual)
        except (TypeError, ValueError):
            return True
        if exp <= 0 or act <= 0:
            return True

        # 只关心【不利方向】的偏离。
        # 滑点的定义就是"成交比预期差"：
        #   买入 → 实际高于预期（买贵了）
        #   卖出 → 实际低于预期（卖便宜了）
        # 有利方向的偏离不是损失，不属滑点范畴
        #（而且市价单几乎不可能出现，若出现说明价格数据有问题，
        #  那由价格突变保护负责，不在这里重复告警）。
        if side == "buy":
            diff = act - exp          # 买贵了为正
        else:
            diff = exp - act          # 卖便宜了为正

        slip = diff / exp
        if slip <= self._slip_threshold():
            return True

        now = time.time()
        key = f"slip_{sym}"
        last = self._last_alert.get(key, 0)
        if now - last < 600:
            return False
        self._last_alert[key] = now

        direction = "买贵了" if side == "buy" else "卖便宜了"
        msg = (
            f"⚠️ 滑点超出阈值 [{sym}]\n"
            f"   预期: {exp:.6f}\n"
            f"   实际: {act:.6f}（{direction}）\n"
            f"   滑点: {slip*100:.2f}%（上限 "
            f"{self._slip_threshold()*100:.2f}%）\n"
            f"   可能原因: 盘口深度不足 / 行情剧烈波动\n"
            f"   → 可改用限价单: /set order_type limit"
        )
        self.logger.warning(f"[滑点] {sym} {slip*100:.2f}%")
        if self._alert:
            await self._alert(msg, "warning")
        return False

    # ─────────── 持久化 ───────────

    def to_dict(self):
        return {
            "halt_until": dict(self._halt_until),
            "history": {k: list(v) for k, v in self._history.items()},
        }

    def from_dict(self, d):
        if not isinstance(d, dict):
            return
        now = time.time()
        for k, v in (d.get("halt_until") or {}).items():
            try:
                if float(v) > now:
                    self._halt_until[k] = float(v)
            except (TypeError, ValueError):
                continue
        # 价格历史不恢复：重启后行情可能已变，用旧数据判定反而危险
        self._history = {}

    def summary(self):
        if not self._halt_until:
            return "价格守卫: 正常"
        now = time.time()
        active = {k: int(v - now) for k, v in self._halt_until.items()
                  if v > now}
        if not active:
            return "价格守卫: 正常"
        return "价格守卫: " + ", ".join(
            f"{k} 暂停{ s}s" for k, s in active.items())


class Retirement:
    """
    策略退役线 —— 全局累计亏损底线。

    与现有风控的区别：
      现有风控都是【局部/周期性】的
        · 单笔止损       —— 单笔
        · 日内亏损上限   —— 每天重置
        · 连续亏损冷却   —— 冷却完自动恢复
        · 回撤熔断       —— 回落即恢复

      退役线是【全局、累计、不自动恢复】的：
        从策略开始运行至今，累计净利跌破阈值就彻底停，
        必须人工 /resume。

    没有这条线会出现：
      每天亏一点，每天都"没触发任何风控"，
      但一个月下来累计亏损已经很可观。
    """

    def __init__(self, bot, alert=None, logger=None):
        self.bot = bot
        self._alert = alert
        self.logger = logger or logging.getLogger(__name__)

        self.retired = False
        self.retire_reason = ""
        self.retire_time = 0.0
        # 基线：启用时的累计净利，用于计算"从启用至今亏了多少"
        self.baseline_profit = None

    def _enabled(self):
        return bool(getattr(self.bot, "retire_enabled", False))

    def _max_loss_usdt(self):
        v = getattr(self.bot, "retire_max_loss_usdt", None)
        return float(v) if v else 0.0

    def _max_loss_pct(self):
        v = getattr(self.bot, "retire_max_loss_pct", None)
        return float(v) if v else 0.0

    async def evaluate(self, total_net_profit, current_equity):
        """
        每轮调用。返回 True 表示可以继续交易，False 表示已退役。

        total_net_profit: 累计净利（U），可为负
        current_equity  : 当前权益（U）
        """
        if not self._enabled():
            return True
        if self.retired:
            return False

        # 首次评估时记录基线
        if self.baseline_profit is None:
            self.baseline_profit = float(total_net_profit or 0.0)
            return True

        since = float(total_net_profit or 0.0) - self.baseline_profit

        max_usdt = self._max_loss_usdt()
        max_pct = self._max_loss_pct()

        hit = False
        reason = ""

        if max_usdt > 0 and since <= -abs(max_usdt):
            hit = True
            reason = (f"启用退役线以来累计亏损 {abs(since):.2f} U，"
                      f"达到上限 {max_usdt:.2f} U")

        if not hit and max_pct > 0 and current_equity > 0:
            loss_ratio = abs(since) / current_equity
            if since < 0 and loss_ratio >= max_pct:
                hit = True
                reason = (f"启用退役线以来累计亏损占当前权益 "
                          f"{loss_ratio*100:.1f}%，达到上限 "
                          f"{max_pct*100:.0f}%")

        if not hit:
            return True

        self.retired = True
        self.retire_time = time.time()
        self.retire_reason = reason

        msg = (
            f"🚨 策略已退役，交易已停止\n"
            f"   原因: {reason}\n"
            f"   累计净利: {total_net_profit:+.2f} U\n"
            f"   \n"
            f"   这不是临时熔断，不会自动恢复。\n"
            f"   请人工复盘后再决定是否重启：\n"
            f"     1. 检查策略参数是否适配当前行情\n"
            f"     2. 确认无误后 /resume 恢复交易\n"
            f"     3. 如需调整底线: /set retire_max_loss_usdt 20"
        )
        self.logger.error(f"[退役线] {reason}")
        if self._alert:
            await self._alert(msg, "critical")
        return False

    def reset(self):
        """人工 /resume 时调用，解除退役并重置基线"""
        self.retired = False
        self.retire_reason = ""
        self.retire_time = 0.0
        self.baseline_profit = None

    def to_dict(self):
        return {
            "retired": self.retired,
            "reason": self.retire_reason,
            "time": self.retire_time,
            "baseline": self.baseline_profit,
        }

    def from_dict(self, d):
        if not isinstance(d, dict):
            return
        self.retired = bool(d.get("retired"))
        self.retire_reason = d.get("reason") or ""
        try:
            self.retire_time = float(d.get("time") or 0)
        except (TypeError, ValueError):
            self.retire_time = 0.0
        b = d.get("baseline")
        try:
            self.baseline_profit = float(b) if b is not None else None
        except (TypeError, ValueError):
            self.baseline_profit = None

    def summary(self):
        if not self._enabled():
            return "退役线: 未启用"
        if self.retired:
            return f"退役线: 🚨 已退役（{self.retire_reason}）"
        return "退役线: 正常"
