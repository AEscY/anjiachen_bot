"""
参数表。单一来源：新增参数只需在此登记，无需改其他地方。

设计原则（与旧版的关键区别）：
  旧版把参数写成 self.xxx 散落在 __init__ 里，漏写一个就在
  运行到那行时才崩溃。这里用表驱动，读取统一走 Params.get()。
"""

DEFAULTS = {
    # 网格
    "levels": 2,           # 档位数
    "spacing": 0.02,       # 档间距（2%）
    "capital_pct": 0.8,    # 用可用现金的比例
    "min_order": 1.0,      # 单档最小金额（USDT）
    "reserve": 1.0,        # 保留现金底线（USDT）

    # 卖单跟随（防单边下跌死挂）
    "follow_hours": 24.0,  # 持仓老化多少小时后允许下移卖单价
    "follow_max_loss": 0.06,  # 下移底线：不低于成本×(1-此值)

    # 风控
    "stop_loss": 0.15,     # 跌破中枢此比例 → 清仓重置
    "max_drawdown": 0.12,  # 回撤达此比例 → 暂停
    "daily_loss": 0.05,    # 日亏损达此比例 → 暂停
    "retire_loss": 5.0,    # 累计已实现亏损达此金额 → 永久停机
    "retire_on": 0,        # 退役线开关（1=启用）

    # 运行
    "poll": 10,            # 主循环间隔（秒）
}


LABELS = {
    "levels": "网格层数",
    "spacing": "档间距",
    "capital_pct": "资金使用率",
    "min_order": "单档最小金额",
    "reserve": "保留现金底线",
    "follow_hours": "卖单跟随老化时间",
    "follow_max_loss": "卖单让利底线",
    "stop_loss": "区间止损",
    "max_drawdown": "最大回撤",
    "daily_loss": "日亏损上限",
    "retire_loss": "退役线累计亏损",
    "retire_on": "退役线开关",
    "poll": "轮询间隔",
}


class Params:
    """参数容器。未登记的名字直接抛错，不静默返回默认值。"""

    def __init__(self, saved: dict | None = None):
        self._v = dict(DEFAULTS)
        for k, v in (saved or {}).items():
            if k not in DEFAULTS:
                continue                      # 忽略已废弃的旧参数
            try:
                self._v[k] = type(DEFAULTS[k])(v)
            except (TypeError, ValueError):
                pass                          # 类型不符则用默认值

    def get(self, key: str):
        if key not in DEFAULTS:
            raise KeyError(f"未登记的参数: {key}")
        return self._v[key]

    def set(self, key: str, raw: str):
        """从字符串设置，失败抛 ValueError（由调用方转成提示）"""
        if key not in DEFAULTS:
            raise KeyError(f"未登记的参数: {key}")
        t = type(DEFAULTS[key])
        if t is bool:
            self._v[key] = raw.strip().lower() in ("1", "true", "on", "yes")
        else:
            self._v[key] = t(raw)

    def dump(self) -> dict:
        return dict(self._v)

    def diff(self) -> dict:
        """返回与默认值不同的项，用于 /status 精简显示"""
        return {k: v for k, v in self._v.items() if v != DEFAULTS[k]}
