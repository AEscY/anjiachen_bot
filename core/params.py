"""
params.py - 统一参数注册表

设计目的：
  用户要求「全部走默认值，但每个参数都能用 Telegram 命令自定义」。
  若给几十个参数各写一个命令函数，会产生大量重复代码且校验规则不一致。
  这里用声明式注册表描述每个参数的类型/范围/默认值/命令别名，
  由 set/get 命令统一驱动，自动完成解析、校验、持久化与回显。

新增一个可调参数 = 在 PARAMS 里加一行，不需要改命令处理逻辑。
"""
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class ParamSpec:
    key: str                      # 属性名（同时是数据库键）
    default: Any                  # 默认值
    desc: str                     # 中文说明
    ptype: type = float           # 类型：float / int / bool / str
    lo: Optional[float] = None    # 最小值（含）
    hi: Optional[float] = None    # 最大值（含）
    scale: float = 1.0            # 命令输入值 → 实际值的换算（如输入百分比 *0.01）
    choices: Optional[list] = None  # 枚举可选值
    aliases: tuple = field(default_factory=tuple)  # 专用命令别名
    unit: str = ""                # 单位说明，用于回显
    group: str = "其他"            # 分组，用于 /params 分组展示


# ══════════════════════════════════════════════════════════════
#  参数注册表
#  默认值来自回测标定结论，改动前请先跑 core/backtest.py 验证
# ══════════════════════════════════════════════════════════════
PARAMS: dict = {}


def _reg(*specs):
    for s in specs:
        PARAMS[s.key] = s


# ---------- 网格：档位 ----------
_reg(
    ParamSpec("grid_levels", 8, "网格层数（单边档位数）", int, 2, 50,
              aliases=("setlevels",), unit="层", group="网格·档位"),
    ParamSpec("grid_spacing_pct", 0.012, "基础网格间距（每格）", float, 0.001, 0.10,
              scale=0.01, aliases=("setspacing",), unit="%", group="网格·档位"),
    ParamSpec("grid_spacing_mode", "atr", "间距模式：fixed 固定 / atr 随波动伸缩",
              str, choices=["fixed", "atr"], aliases=("setspacingmode",), group="网格·档位"),
    ParamSpec("grid_atr_mult", 0.75, "ATR 间距倍数（间距 = ATR% × 此值）", float, 0.1, 3.0,
              aliases=("setatrmult",), group="网格·档位"),
    ParamSpec("grid_spacing_min", 0.005, "间距下限（防手续费吞噬利润）", float, 0.001, 0.05,
              scale=0.01, aliases=("setspacingmin",), unit="%", group="网格·档位"),
    ParamSpec("grid_spacing_max", 0.035, "间距上限（防档位过疏）", float, 0.005, 0.20,
              scale=0.01, aliases=("setspacingmax",), unit="%", group="网格·档位"),
)

# ---------- 网格：中枢 ----------
_reg(
    ParamSpec("grid_anchor_mode", "anchored", "中枢模式：anchored 锚定 / following 跟随",
              str, choices=["anchored", "following"], aliases=("setanchormode",), group="网格·中枢"),
    ParamSpec("grid_rebalance_drift", 2.0, "中枢漂移多少个间距才重挂网格", float, 0.5, 10.0,
              aliases=("setdrift",), unit="格", group="网格·中枢"),
    ParamSpec("grid_max_drift_pct", 0.20, "中枢相对锚点的最大偏移（防追涨杀跌）", float, 0.02, 1.0,
              scale=0.01, aliases=("setmaxdrift",), unit="%", group="网格·中枢"),
    ParamSpec("grid_rebalance_interval", 60, "重挂检查间隔", int, 10, 3600,
              aliases=("setrebalint",), unit="秒", group="网格·中枢"),
)

# ---------- 网格：资金 ----------
_reg(
    ParamSpec("grid_capital_pct", 0.80, "网格占用总权益比例（每档 = 此值/层数）", float, 0.05, 1.0,
              scale=0.01, aliases=("setgridcapital",), unit="%", group="网格·资金"),
    ParamSpec("grid_min_order_usdt", 5.0, "单档最小下单金额（低于此值该档不挂）", float, 0.0, 10000,
              aliases=("setminorder",), unit="U", group="网格·资金"),
)

# ---------- 网格：风控 ----------
_reg(
    ParamSpec("grid_stop_loss_pct", 0.15, "击穿区间下限后的止损线", float, 0.02, 0.50,
              scale=0.01, aliases=("setgridstop",), unit="%", group="网格·风控"),
    ParamSpec("grid_lower_buffer_pct", 0.06, "区间下限缓冲（最低档之下再加此缓冲）", float, 0.0, 0.30,
              scale=0.01, aliases=("setlowerbuf",), unit="%", group="网格·风控"),
    ParamSpec("grid_upper_buffer_pct", 0.06, "区间上限缓冲（最高档之上）", float, 0.0, 0.30,
              scale=0.01, aliases=("setupperbuf",), unit="%", group="网格·风控"),
    ParamSpec("grid_trend_filter", True, "大跌趋势下暂停新开网格", bool,
              aliases=("setgridfilter",), group="网格·风控"),
    ParamSpec("grid_trend_threshold", 0.02, "趋势过滤阈值（跌幅超此值停开新网）", float, 0.005, 0.30,
              scale=0.01, aliases=("settrendthr",), unit="%", group="网格·风控"),
)

# ---------- 执行 ----------
_reg(
    ParamSpec("order_type", "limit", "下单方式：limit 限价挂单 / market 市价",
              str, choices=["limit", "market"], aliases=("setordertype",), group="执行"),
    ParamSpec("grid_enabled", False, "启用网格模式（关闭则退回单次低吸高卖）", bool,
              aliases=("gridmode",), group="执行"),
    ParamSpec("data_max_age", 90, "行情数据最大容忍陈旧时长", int, 10, 600,
              aliases=("setdataage",), unit="秒", group="执行"),
)

# ---------- 原策略参数（非网格模式，保留兼容）----------
_reg(
    ParamSpec("tp_pct", 0.015, "止盈（单次模式）", float, 0.001, 0.50,
              scale=0.01, aliases=("settp",), unit="%", group="单次模式"),
    ParamSpec("sl_pct", 0.01, "止损（单次模式）", float, 0.001, 0.50,
              scale=0.01, aliases=("setsl",), unit="%", group="单次模式"),
    ParamSpec("trailing_sl_pct", 0.005, "移动止损（单次模式）", float, 0.0, 0.20,
              scale=0.01, aliases=("settsl",), unit="%", group="单次模式"),
    ParamSpec("trailing_tp_pct", 0.003, "移动止盈（单次模式）", float, 0.0, 0.20,
              scale=0.01, aliases=("settmpt",), unit="%", group="单次模式"),
    ParamSpec("auto_min_score", 65, "自动开仓评分阈值（单次模式）", int, 50, 95,
              aliases=("autoscore",), unit="分", group="单次模式"),
    ParamSpec("single_order_pct", 0.02, "单笔占总权益比例（单次模式）", float, 0.001, 0.20,
              scale=0.01, aliases=("setorderpct",), unit="%", group="单次模式"),
    ParamSpec("max_positions_per_coin", 8, "单币最大仓位数（单次模式）", int, 1, 100,
              aliases=("setmaxpos",), unit="个", group="单次模式"),
    ParamSpec("max_per_coin_usdt", 50, "单币最大持仓金额（单次模式）", float, 1, 1000000,
              aliases=("setmaxcoin",), unit="U", group="单次模式"),
)

# ---------- 通用风控 ----------
_reg(
    ParamSpec("max_daily_loss_pct", 0.05, "日内亏损熔断", float, 0.01, 0.50,
              scale=0.01, aliases=("setmaxloss",), unit="%", group="通用风控"),
    ParamSpec("max_drawdown_pct", 0.12, "最大回撤熔断", float, 0.01, 0.50,
              scale=0.01, aliases=("setmaxdd",), unit="%", group="通用风控"),
    ParamSpec("max_total_allocated_pct", 0.80, "总仓位上限", float, 0.1, 1.0,
              scale=0.01, aliases=("setmaxalloc",), unit="%", group="通用风控"),
    ParamSpec("max_daily_trades", 20, "每日最大开仓次数", int, 0, 1000,
              aliases=("setmaxtrades",), unit="次", group="通用风控"),
    ParamSpec("reserve_bottom", 10, "USDT 保留底线", float, 0, 1000000,
              aliases=("setreserve",), unit="U", group="通用风控"),
    ParamSpec("max_consecutive_losses", 4, "连续亏损熔断阈值", int, 2, 20,
              aliases=("setmaxconsloss",), unit="笔", group="通用风控"),
    ParamSpec("consecutive_loss_cooldown", 3600, "连亏冷静期", int, 60, 86400,
              aliases=("setcooldown",), unit="秒", group="通用风控"),
)

# ---------- 自适应 ----------
_reg(
    ParamSpec("adaptive_enabled", True, "启用市场自适应（按波动调阈值/仓位）", bool,
              aliases=("setadaptive",), group="自适应"),
    ParamSpec("adaptive_interval", 300, "自适应参数更新间隔", int, 30, 3600,
              aliases=("setadaptint",), unit="秒", group="自适应"),
)

# ══════════════════════════════════════════════════════════════
#  索引
# ══════════════════════════════════════════════════════════════

# 别名 → 参数键
ALIAS_MAP: dict = {}
for _k, _s in PARAMS.items():
    for _a in _s.aliases:
        ALIAS_MAP[_a] = _k

# 分组
GROUPS: dict = {}
for _k, _s in PARAMS.items():
    GROUPS.setdefault(_s.group, []).append(_s)


def defaults() -> dict:
    """返回 {key: 默认值}"""
    return {k: s.default for k, s in PARAMS.items()}


def parse(key: str, raw: str):
    """
    解析并校验用户输入。
    成功返回 (value, None)；失败返回 (None, 错误说明)
    """
    spec = PARAMS.get(key)
    if spec is None:
        return None, f"未知参数: {key}"

    txt = str(raw).strip()

    if spec.ptype is bool:
        low = txt.lower()
        if low in ("1", "true", "yes", "on", "开", "开启"):
            return True, None
        if low in ("0", "false", "no", "off", "关", "关闭"):
            return False, None
        return None, f"{key} 需要 on/off（或 true/false、1/0）"

    if spec.ptype is str:
        if spec.choices and txt.lower() not in spec.choices:
            return None, f"{key} 只能是: {' / '.join(spec.choices)}"
        return txt.lower(), None

    # 数值型
    try:
        val = float(txt) * spec.scale
    except (TypeError, ValueError):
        return None, f"{key} 需要数字，收到: {raw}"

    if spec.ptype is int:
        val = int(round(val))

    if spec.lo is not None and val < spec.lo:
        return None, f"{key} 不能小于 {_fmt(spec.lo, spec)}"
    if spec.hi is not None and val > spec.hi:
        return None, f"{key} 不能大于 {_fmt(spec.hi, spec)}"

    return val, None


def _fmt(v, spec: ParamSpec) -> str:
    """按 scale 还原成用户输入的单位来显示"""
    shown = v / spec.scale if spec.scale else v
    if spec.ptype is int:
        return f"{int(shown)}{spec.unit}"
    if abs(shown) < 1:
        return f"{shown:.4f}{spec.unit}"
    return f"{shown:.2f}{spec.unit}"


def display(key: str, value) -> str:
    """把值格式化成易读形式"""
    spec = PARAMS.get(key)
    if spec is None:
        return str(value)
    if spec.ptype is bool:
        return "开启" if value else "关闭"
    if spec.ptype is str:
        return str(value)
    return _fmt(value, spec)


def range_hint(spec: ParamSpec) -> str:
    parts = []
    if spec.choices:
        parts.append("/".join(spec.choices))
    else:
        if spec.lo is not None:
            parts.append(f"≥{_fmt(spec.lo, spec)}")
        if spec.hi is not None:
            parts.append(f"≤{_fmt(spec.hi, spec)}")
    return " ".join(parts)
