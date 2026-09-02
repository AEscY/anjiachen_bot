"""
趋势性退化检测 —— 发现"慢慢变差"。

与看门狗的区别
--------------
看门狗看【状态】：有没有卡住、有没有断线。
        判定方式是"超过某个阈值"。

本模块看【趋势】：是不是在持续恶化。
        每一步变化都太小，够不着任何阈值，
        所以永远不会"触发"任何事件 ——
        等你某天想起来查 /stats，已经亏了很久。

典型场景：
  手续费占比 8% → 11% → 15% → 19%  (间距被手续费侵蚀)
  胜率       61% → 55% → 48% → 38%  (行情变了，策略不再适配)

严格约束：同样只观测、只提议，绝不自动改参数。
"""

import time


class TrendWatcher:
    """绩效趋势检测"""

    # ── 判定阈值 ──
    MIN_SAMPLES = 20          # 样本太少不判断（噪声太大）
    RECENT_N = 30             # 近期窗口笔数
    BASELINE_N = 60           # 基线窗口笔数

    # 手续费占比：相对涨幅 + 绝对下限，双重条件防误报
    FEE_REL_RISE = 0.5        # 相对上涨 50%
    FEE_ABS_FLOOR = 0.15      # 且绝对值 ≥ 15%（低于此值不值得管）

    # 胜率：相对下滑幅度
    WIN_REL_DROP = 0.25       # 下滑 25%
    WIN_ABS_FLOOR = 0.50      # 且基线胜率曾 ≥ 50%（否则本就不赚钱）

    # 单笔平均净利：相对下滑
    PNL_REL_DROP = 0.5        # 下滑 50%

    # 节流
    REPEAT_INTERVAL = 24 * 3600

    def __init__(self, bot, logger=None):
        self.bot = bot
        self.logger = logger
        self._sent = {}
        self._last_snapshot = None

    def _can_send(self, key):
        last = self._sent.get(key, 0)
        if time.time() - last < self.REPEAT_INTERVAL:
            return False
        self._sent[key] = time.time()
        return True

    async def check(self, windows):
        """
        windows: get_performance_windows() 的返回值
                 {'recent': {...}, 'baseline': {...}}
        返回告警文本列表
        """
        if not windows:
            return []
        recent = windows.get("recent") or {}
        base = windows.get("baseline") or {}

        # 样本量门槛：噪声太大时不做判断
        if recent.get("count", 0) < self.MIN_SAMPLES:
            return []
        if base.get("count", 0) < self.MIN_SAMPLES:
            return []

        self._last_snapshot = windows
        out = []
        for fn in (self._check_fee, self._check_winrate, self._check_pnl):
            try:
                r = fn(recent, base)
                if r:
                    out.append(r)
            except Exception as e:
                if self.logger:
                    self.logger.debug(f"趋势检测 {fn.__name__} 异常: {e}")
        return out

    # ---------- 三项检测 ----------

    def _check_fee(self, recent, base):
        """手续费占比持续上升 —— 间距正在被侵蚀"""
        if not self._can_send("fee"):
            return None
        b = float(base.get("fee_ratio", 0))
        n = float(recent.get("fee_ratio", 0))
        if b <= 0:
            return None
        if n < self.FEE_ABS_FLOOR:
            return None                      # 绝对值还很低，不值得管
        if n < b * (1 + self.FEE_REL_RISE):
            return None                      # 涨幅不够

        spacing = float(getattr(self.bot, "grid_spacing_pct", 0) or 0)
        lines = [
            f"📉 手续费占比持续上升",
            f"   {b*100:.1f}% → {n*100:.1f}%"
            f"（+{(n-b)*100:.1f} 个百分点）",
            f"   样本: 近 {recent.get('count')} 笔 vs 基线 {base.get('count')} 笔",
        ]
        if spacing > 0:
            # 往返手续费约 0.16%（限价）
            net = spacing - 0.16
            lines.append(f"   当前间距 {spacing:.2f}%，净利约 {net:.2f}%")
            if net < spacing * 0.3:
                lines.append(f"   → 间距已被手续费吃掉大半")
                sug = round(max(spacing * 1.5, 3.0), 1)
                lines.append(f"   → 建议: /set grid_spacing_pct {sug}")
            else:
                lines.append(f"   → 建议: 适当放大间距，或换波动更大的币种")
        else:
            lines.append("   → 建议: 放大网格间距，降低交易频率")
        return "\n".join(lines)

    def _check_winrate(self, recent, base):
        """胜率下滑 —— 行情可能变了"""
        if not self._can_send("winrate"):
            return None
        b = float(base.get("win_rate", 0))
        n = float(recent.get("win_rate", 0))
        if b < self.WIN_ABS_FLOOR:
            return None                      # 基线本就不赚钱，不是"退化"
        if n >= b * (1 - self.WIN_REL_DROP):
            return None

        thr = float(getattr(self.bot, "auto_min_score", 0) or 0)
        lines = [
            f"📉 胜率明显下滑",
            f"   {b*100:.0f}% → {n*100:.0f}%"
            f"（-{((b-n))*100:.0f} 个百分点）",
            f"   样本: 近 {recent.get('count')} 笔 vs 基线 {base.get('count')} 笔",
            f"   单笔均值: {base.get('avg_pnl_pct',0):+.2f}% → "
            f"{recent.get('avg_pnl_pct',0):+.2f}%",
            f"   → 行情特征可能已改变，原策略不再适配",
        ]
        if thr > 0:
            lines.append(f"   → 可先提高门槛: /set auto_min_score {int(min(90, thr+5))}")
        lines.append(f"   → 或降档运行: /preset safe")
        return "\n".join(lines)

    def _check_pnl(self, recent, base):
        """单笔平均净利下滑"""
        if not self._can_send("pnl"):
            return None
        b = float(base.get("avg_pnl_pct", 0))
        n = float(recent.get("avg_pnl_pct", 0))
        if b <= 0:
            return None                      # 基线本就亏，不算退化
        if n >= b * (1 - self.PNL_REL_DROP):
            return None
        if n > 0:
            return None                      # 还在赚，只是赚少了（交给上面两项报）

        lines = [
            f"🚨 单笔平均净利由正转负",
            f"   {b:+.2f}% → {n:+.2f}%",
            f"   样本: 近 {recent.get('count')} 笔 vs 基线 {base.get('count')} 笔",
            f"   → 继续按当前参数运行，预期是稳定亏损",
            f"   → 建议: /preset safe 降档，或 /autotrade off 暂停观察",
        ]
        return "\n".join(lines)

    # ---------- 持久化 ----------

    def to_dict(self):
        return {"sent": self._sent}

    def from_dict(self, d):
        if not d:
            return
        self._sent = dict(d.get("sent") or {})

    def summary(self):
        w = self._last_snapshot
        if not w:
            return "尚未采样"
        r = w.get("recent") or {}
        return (f"近{r.get('count', 0)}笔 胜率{r.get('win_rate', 0)*100:.0f}% "
                f"均{r.get('avg_pnl_pct', 0):+.2f}% "
                f"费占比{r.get('fee_ratio', 0)*100:.1f}%")
