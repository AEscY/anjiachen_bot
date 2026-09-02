"""
行为看门狗 —— 让机器人主动说出自己的"不适应"。

设计原则
--------
1. **纯观测，零干预**：只读状态、只发告警，绝不改动任何参数或下单。
   交易系统里"改错"的代价远大于"不改"。

2. **区分"没坏但不对劲"与"真坏了"**：
   这是本模块存在的理由。崩溃会立刻被发现，
   "静默地不干活"却能持续好几天。

3. **每条告警必须带原因和建议**：
   "已 41 小时未开仓"没有价值，
   "未开仓，近 24h 信号 0 次（前 7 日均值 6.2），建议 /set autoscore 60"
   才有价值。

4. **强节流**：同一问题不重复推送，避免刷屏导致用户屏蔽。
"""

import time
from collections import deque


def _fmt_dur(sec):
    """秒 → 人类可读时长"""
    if sec < 60:
        return f"{sec:.0f}秒"
    if sec < 3600:
        return f"{sec/60:.0f}分钟"
    if sec < 86400:
        return f"{sec/3600:.1f}小时"
    return f"{sec/86400:.1f}天"


class Watchdog:
    """
    行为巡检。

    三类检测（都是"没坏，但不对劲"型问题）：
      A. 长时间未开仓 —— 区分"真没信号"与"循环卡死"
      B. 挂单长期不成交 —— 网格最容易卡死的地方
      C. 单币长期无数据 —— 订阅悄悄断了
    """

    # 默认阈值（秒）
    NO_OPEN_WARN = 6 * 3600      # 6 小时未开仓
    STALE_ORDER_WARN = 4 * 3600  # 挂单 4 小时未成交
    NO_DATA_WARN = 900           # 15 分钟无行情
    REPEAT_INTERVAL = 12 * 3600  # 同一告警最短重推间隔

    def __init__(self, bot, alert=None, logger=None):
        self.bot = bot
        self._alert = alert
        self.logger = logger

        # ── A. 开仓相关 ──
        self.last_open_time = None      # 上次成功开仓时间戳
        self.open_count = 0             # 累计开仓次数
        self._tick_count = 0            # 主循环轮次（证明循环在跑）
        self._score_seen = 0            # 本轮产生过评分的币种数
        self._score_hist = {}           # {sym: deque([(ts, score)])}
        self._score_maxlen = 200

        # ── B. 挂单相关 ──
        # {sym: {client_id: created_ts}}
        self._order_times = {}

        # ── C. 行情相关 ──
        self._last_data_ts = {}         # {sym: ts}

        # 节流：{key: last_sent_ts}
        self._sent = {}

        self._started = time.time()

    # ---------- 数据接入（由 bot 调用）----------

    def tick(self):
        """主循环每轮调用一次 —— 证明循环还活着"""
        self._tick_count += 1

    def record_score(self, sym, score):
        """记录一次信号评分（不管达不达标）"""
        self._score_seen += 1
        d = self._score_hist.setdefault(sym, deque(maxlen=self._score_maxlen))
        d.append((time.time(), float(score)))
        self._last_data_ts[sym] = time.time()

    def record_open(self, sym):
        """成功开仓"""
        self.last_open_time = time.time()
        self.open_count += 1

    def record_order(self, sym, client_id):
        """网格挂出一张单"""
        if not client_id:
            return
        self._order_times.setdefault(sym, {})[str(client_id)] = time.time()

    def clear_order(self, sym, client_id=None):
        """挂单成交或撤销"""
        d = self._order_times.get(sym)
        if not d:
            return
        if client_id is None:
            d.clear()
        else:
            d.pop(str(client_id), None)

    def record_data(self, sym):
        """该币种拿到了行情"""
        self._last_data_ts[sym] = time.time()

    # ---------- 节流 ----------

    def _can_send(self, key):
        last = self._sent.get(key, 0)
        if time.time() - last < self.REPEAT_INTERVAL:
            return False
        self._sent[key] = time.time()
        return True

    # ---------- 检测 ----------

    async def check_all(self):
        """跑一遍全部检测，返回告警列表"""
        out = []
        for fn in (self._check_no_open,
                   self._check_stale_orders,
                   self._check_no_data):
            try:
                r = await fn()
                if r:
                    out.append(r)
            except Exception as e:
                if self.logger:
                    self.logger.debug(f"巡检 {fn.__name__} 异常: {e}")
        return out

    async def _check_no_open(self):
        """
        A. 长时间未开仓。

        关键：必须区分"真没信号"与"循环卡死"。
          循环在跑(tick 增长) + 有评分但不达标 → 行情问题，调门槛
          循环不跑(tick 停滞)                → 真坏了，看日志
        """
        bot = self.bot
        if not getattr(bot, "auto_trade_enabled", False):
            return None

        ref = self.last_open_time or self._started
        idle = time.time() - ref
        if idle < self.NO_OPEN_WARN:
            return None
        if not self._can_send("no_open"):
            return None

        lines = [f"⚠️ 已连续 {_fmt_dur(idle)} 未开仓"]

        # 循环是否还活着
        if self._tick_count > 0:
            lines.append(f"   主循环: 正常运行（已 {self._tick_count} 轮）")
        else:
            lines.append("   主循环: ⚠️ 未检测到轮次，可能已卡死")

        # 近期信号密度
        recent, baseline = self._signal_density()
        if recent == 0:
            lines.append(f"   近 24h 达标信号: 0 次"
                         + (f"（前 7 日均值 {baseline:.1f}）" if baseline else ""))
            lines.append("   → 行情未触发策略，属正常等待")
        else:
            lines.append(f"   近 24h 达标信号: {recent} 次")

        # 评分分布：离门槛有多远
        gap = self._score_gap()
        if gap is not None:
            lines.append(f"   评分距门槛: 还差 {gap:.1f} 分")
            if gap <= 5:
                lines.append(f"   → 接近临界，可下调门槛:")
                lines.append(f"     /set auto_min_score "
                             f"{max(50, int(self._threshold()) - 5)}")

        if self.last_open_time:
            lines.append(f"   上次开仓: {_fmt_dur(idle)}前")
        else:
            lines.append("   本次启动以来尚未开仓")
            lines.append("   → /brain 可逐项检查是否卡在余额/网格最小额")

        return "\n".join(lines)

    async def _check_stale_orders(self):
        """B. 挂单长期不成交 —— 网格最易卡死处"""
        bot = self.bot
        if not getattr(bot, "grid_enabled", False):
            return None
        now = time.time()
        stale = []
        for sym, orders in list(self._order_times.items()):
            for cid, ts in list(orders.items()):
                age = now - ts
                if age >= self.STALE_ORDER_WARN:
                    stale.append((sym, cid, age))
        if not stale:
            return None
        if not self._can_send("stale_order"):
            return None

        lines = [f"⚠️ {len(stale)} 张挂单长期未成交"]
        ws = getattr(bot, "ws", None)
        for sym, cid, age in stale[:5]:
            # 算挂单价与现价偏离
            extra = ""
            if ws:
                t = ws.get_ticker(sym)
                if t:
                    try:
                        cur = float(t.get("last") or 0)
                        st = getattr(bot, "grid", None)
                        spacing = 0.0
                        if st:
                            g = st.states.get(sym)
                            if g:
                                spacing = float(getattr(g, "spacing", 0)) * 100
                        if cur > 0 and spacing > 0:
                            extra = f"（间距 {spacing:.2f}%）"
                    except Exception:
                        pass
            lines.append(f"   · {sym} 挂了 {_fmt_dur(age)}{extra}")
        if len(stale) > 5:
            lines.append(f"   ...等 {len(stale)} 张")
        lines.append("   → 价格可能已走出网格区间")
        lines.append("     查看: /grid ；重挂: /gridreset <币>")
        return "\n".join(lines)

    async def _check_no_data(self):
        """C. 单币长期无数据 —— 订阅悄悄断了"""
        now = time.time()
        dead = []
        for sym in getattr(self.bot, "symbols", []) or []:
            ts = self._last_data_ts.get(sym)
            if ts is None:
                # 从没拿到过数据，但刚启动不算
                if now - self._started > self.NO_DATA_WARN:
                    dead.append((sym, now - self._started))
                continue
            age = now - ts
            if age >= self.NO_DATA_WARN:
                dead.append((sym, age))
        if not dead:
            return None
        if not self._can_send("no_data"):
            return None

        lines = ["🚨 以下币种长时间无行情数据"]
        for sym, age in dead[:6]:
            lines.append(f"   · {sym} — 已 {_fmt_dur(age)}")
        if len(dead) > 6:
            lines.append(f"   ...等 {len(dead)} 个")
        lines.append("   → 可能是 WebSocket 订阅断开")
        lines.append("     该币种已停止交易，其余币种不受影响")
        return "\n".join(lines)

    # ---------- 辅助 ----------

    def _threshold(self):
        return float(getattr(self.bot, "auto_min_score", 65))

    def _signal_density(self):
        """(近24h达标数, 前7日日均)"""
        now = time.time()
        d24, d7 = 0, 0
        thr = self._threshold()
        for sym, hist in self._score_hist.items():
            for ts, sc in hist:
                age = now - ts
                if age <= 86400 and sc >= thr:
                    d24 += 1
                if age <= 7 * 86400 and sc >= thr:
                    d7 += 1
        return d24, (d7 / 7.0 if d7 else 0.0)

    def _score_gap(self):
        """当前评分与门槛的差距（取最近的样本）"""
        thr = self._threshold()
        best = None
        for sym, hist in self._score_hist.items():
            if not hist:
                continue
            recent = [sc for ts, sc in hist if time.time() - ts <= 3600]
            if not recent:
                continue
            m = max(recent)
            if best is None or m > best:
                best = m
        if best is None:
            return None
        gap = thr - best
        return gap if gap > 0 else None

    # ---------- 持久化 ----------

    def to_dict(self):
        return {
            "last_open_time": self.last_open_time,
            "open_count": self.open_count,
            "started": self._started,
            "sent": self._sent,
            "order_times": {s: dict(o) for s, o in self._order_times.items()},
            "last_data_ts": dict(self._last_data_ts),
        }

    def from_dict(self, d):
        if not d:
            return
        self.last_open_time = d.get("last_open_time")
        self.open_count = int(d.get("open_count", 0) or 0)
        self._started = float(d.get("started") or time.time())
        self._sent = dict(d.get("sent") or {})
        self._order_times = {s: dict(o) for s, o in
                             (d.get("order_times") or {}).items()}
        self._last_data_ts = dict(d.get("last_data_ts") or {})

    def summary(self):
        """给 /status 用的一行摘要"""
        ref = self.last_open_time or self._started
        idle = time.time() - ref
        parts = [f"距上次开仓 {_fmt_dur(idle)}"]
        if self.open_count:
            parts.append(f"累计 {self.open_count} 次")
        pending = sum(len(o) for o in self._order_times.values())
        if pending:
            parts.append(f"在挂 {pending} 单")
        return " | ".join(parts)
