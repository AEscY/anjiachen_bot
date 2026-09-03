"""
reporter.py - 定期日报
========================

补齐的第四项缺口。

为什么需要日报
----------------
原有告警全是【事件驱动】的：出事了才说话。

这带来一个致命盲区：

    "没消息" 有两种可能
      · 一切正常
      · 它死了

    而这两种从外部完全无法区分。

崩溃有日志、对账有告警、巡检能发现"装死"，
但如果 Telegram 断了、或者服务整个被 Render 回收，
用户就是什么都收不到 —— 而且不知道自己是"收不到"
还是"本来就没异常"。

日报的作用不是汇报业绩，而是提供一个【心跳证明】：

    收到日报 = 它活着，且能正常发消息

没收到日报本身就是最强的告警信号。

设计原则
--------
1. 定时发送（默认每天 9 点，UTC+8）
2. 内容包含持仓、今日盈亏、巡检状态、断连情况
3. 无论有无交易都发 —— 这正是"心跳"的意义
4. 持久化上次发送日期，避免重启后重复发送或漏发
5. 发送失败不重试到死，记录日志等下一天
"""
import logging
import time
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

CST = timezone(timedelta(hours=8))


class DailyReporter:
    def __init__(self, bot, alert=None, logger=None):
        self.bot = bot
        self._alert = alert
        self.logger = logger or logging.getLogger(__name__)

        self.last_sent_day = ""      # 上次发送的日期 'YYYY-MM-DD'
        self.last_sent_ts = 0.0
        self.last_error = ""

    def _enabled(self):
        return bool(getattr(self.bot, "daily_report_enabled", True))

    def _hour(self):
        v = getattr(self.bot, "daily_report_hour", None)
        try:
            h = int(v)
        except (TypeError, ValueError):
            return 9
        return max(0, min(23, h))

    def should_send(self):
        """
        是否该发送。满足三个条件：
          1. 启用
          2. 今天还没发过
          3. 当前时间已过设定的小时
        """
        if not self._enabled():
            return False
        now = datetime.now(CST)
        today = now.date().isoformat()
        if self.last_sent_day == today:
            return False
        return now.hour >= self._hour()

    async def build_report(self):
        """构造日报内容"""
        bot = self.bot
        try:
            from storage import (get_today_trades, get_recent_performance,
                                 get_total_fees, get_total_net_profit)
        except Exception as e:
            self.logger.debug(f"日报取数据失败: {e}")
            return None

        lines = []
        now = datetime.now(CST)
        lines.append(f"📊 日报 {now.strftime('%Y-%m-%d')}")

        # 环境标识：这一行很重要，实盘/模拟盘必须一眼可辨
        try:
            tag = bot.env_tag
        except Exception:
            tag = ""
        lines.append(f"{tag}")

        # 今日交易
        try:
            today = await get_today_trades()
        except Exception:
            today = None
        if today and today.get("total"):
            lines.append(
                f"今日: {today['total']} 笔 | "
                f"胜率 {today.get('win_rate', 0)*100:.0f}% | "
                f"盈亏 {today.get('total_pnl_sum', 0):+.2f}%")
        else:
            lines.append("今日: 无交易")

        # 近 20 笔
        try:
            perf = await get_recent_performance(20)
        except Exception:
            perf = None
        if perf and perf.get("total"):
            lines.append(
                f"近20笔: 胜率 {perf.get('win_rate', 0)*100:.0f}%")
        else:
            lines.append("近20笔: 暂无数据")

        # 累计
        try:
            fees = await get_total_fees()
            net = await get_total_net_profit()
            lines.append(f"累计: 净利 {net:+.4f} U（手续费 {fees:.4f} U）")
        except Exception:
            pass

        # 持仓
        lines.append("")
        has_pos = False
        try:
            for sym in bot.symbols:
                amt = bot._bot_position_amount(sym)
                if amt <= 1e-12:
                    continue
                t = bot.ws.get_ticker(sym)
                price = float(t.get("last") or 0) if t else 0.0
                entry = bot._weighted_entry(sym)
                if not has_pos:
                    lines.append("持仓:")
                    has_pos = True
                if entry > 0 and price > 0:
                    pnl = (price - entry) / entry * 100
                    lines.append(
                        f"  {sym.split('/')[0]}  {amt:.6f}  "
                        f"{price:.4f}  {pnl:+.2f}%")
                else:
                    lines.append(f"  {sym.split('/')[0]}  {amt:.6f}")
        except Exception as e:
            self.logger.debug(f"日报持仓读取失败: {e}")
        if not has_pos:
            lines.append("持仓: 空仓")

        # 巡检状态
        lines.append("")
        try:
            wd = getattr(bot, "watchdog", None)
            if wd is not None:
                lines.append(f"巡检: {wd.summary()}")
        except Exception:
            pass
        try:
            tw = getattr(bot, "trend", None)
            if tw is not None:
                lines.append(f"趋势: {tw.summary()}")
        except Exception:
            pass

        # Telegram 断连情况 —— 如果断着，日报也发不出去，
        # 但这一项在 /patrol 和 /status 里能看到
        try:
            down = getattr(bot, "_tg_down_since", None)
            if down:
                lines.append(
                    f"⚠️ Telegram 曾断连 "
                    f"{(time.time()-down)/60:.0f} 分钟")
        except Exception:
            pass

        # 退役线
        try:
            rt = getattr(bot, "retirement", None)
            if rt is not None:
                lines.append(rt.summary())
        except Exception:
            pass

        lines.append("")
        lines.append("—— 收到本条即说明机器人正常运行 ——")
        return "\n".join(lines)

    async def maybe_send(self):
        """到点就发，返回是否发送成功"""
        if not self.should_send():
            return False
        try:
            text = await self.build_report()
        except Exception as e:
            self.last_error = str(e)
            self.logger.warning(f"日报生成失败: {e}")
            return False
        if not text:
            return False
        if self._alert:
            try:
                await self._alert(text, "info")
            except Exception as e:
                self.last_error = str(e)
                self.logger.warning(f"日报发送失败: {e}")
                return False
        self.last_sent_day = datetime.now(CST).date().isoformat()
        self.last_sent_ts = time.time()
        self.last_error = ""
        self.logger.info("📊 日报已发送")
        return True

    def to_dict(self):
        return {
            "last_sent_day": self.last_sent_day,
            "last_sent_ts": self.last_sent_ts,
            "last_error": self.last_error,
        }

    def from_dict(self, d):
        if not isinstance(d, dict):
            return
        self.last_sent_day = d.get("last_sent_day") or ""
        try:
            self.last_sent_ts = float(d.get("last_sent_ts") or 0)
        except (TypeError, ValueError):
            self.last_sent_ts = 0.0
        self.last_error = d.get("last_error") or ""
