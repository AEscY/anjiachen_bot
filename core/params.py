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
    ParamSpec("grid_max_order_usdt", 0.0, "单档最大下单金额上限（0=不限，防配置错误挂出巨额单）", float, 0.0, 1000000),

)

# ---------- 网格：风控 ----------
_reg(
    ParamSpec("grid_stop_loss_pct", 0.15, "击穿区间下限后的止损线", float, 0.02, 0.50,
              scale=0.01, aliases=("setgridstop",), unit="%", group="网格·风控"),
    ParamSpec("grid_hard_stop_loss_pct", 0.0, "网格硬止损：浮亏达此值无条件清仓，无视区间下界（0=关闭）", float, 0.0, 0.9),

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
    # 移动止损激活线：盈利达到此值后，移动止损才开始工作。
    #
    # 原实现是 `profit_pct > 0`，即只要盈利哪怕 0.01% 就启动。
    # 实测后果（开仓价 100，市价往返手续费 0.2%）：
    #   最高涨 0.55% → 回撤 0.5% 卖出 → 毛利 +0.05% → 净利 -0.15%  ❌
    #   最高涨 0.60% →                          净利 -0.10%  ❌
    #   最高涨 0.70% →                          净利 -0.00%  ❌
    #   最高涨 0.75% →                          净利 +0.05%  ⚠️
    # 也就是说：币价随便波动 0.7% 就会被洗出去，扣完手续费是亏的。
    # 而 0.7% 对主流币来说只是几分钟的正常波动。
    ParamSpec("trailing_sl_arm_pct", 0.01, "移动止损激活线（最高点盈利达此值才启动）",
              float, 0.0, 0.50, scale=0.01, aliases=("settslarm",),
              unit="%", group="单次模式"),
    # 止盈止损的安全边界。原实现硬编码为 0.06 / 0.002 / 0.04 / 1.2，
    # 用户 /settp 8 设了 8% 却只生效 6%，且毫无提示 —— 配置形同虚设。
    # 现改为可调参数，并在触发夹取时明确告警。
    ParamSpec("tp_max_pct", 0.06, "止盈上限（超过则夹到此值）", float, 0.01, 1.0,
              scale=0.01, aliases=("settpmax",), unit="%", group="单次模式"),
    ParamSpec("sl_min_pct", 0.002, "止损下限（低于则抬到此值）", float, 0.0005, 0.20,
              scale=0.01, aliases=("setslmin",), unit="%", group="单次模式"),
    ParamSpec("sl_max_pct", 0.04, "止损上限（超过则夹到此值）", float, 0.005, 0.50,
              scale=0.01, aliases=("setslmax",), unit="%", group="单次模式"),
    ParamSpec("tp_sl_min_ratio", 1.2, "止盈/止损最小比值", float, 1.0, 5.0,
              aliases=("settpslratio",), unit="倍", group="单次模式"),
    ParamSpec("auto_min_score", 65, "自动开仓评分阈值（单次模式）", int, 50, 95,
              aliases=("autoscore",), unit="分", group="单次模式"),
    ParamSpec("single_order_pct", 0.02, "单笔占总权益比例（单次模式）", float, 0.001, 0.20,
              scale=0.01, aliases=("setorderpct",), unit="%", group="单次模式"),
    # 单笔固定额度：单次模式以此为基数按资金规模缩放（见 _calculate_dynamic_amount）。
    # 原实现只在 __init__ 里硬编码为 1.0，既没有 /setamount 命令也无法调整，
    # 而 Telegram 菜单里一直列着这个按钮 —— 点了没反应。
    # 权益上限：机器人按 min(实际权益, 本值) 计算所有仓位。
    # 0 表示不限制。
    # 两个用途：
    #   1) 模拟盘模拟目标资金规模 —— OKX 模拟盘给 10 万 U 虚拟资金，
    #      而实盘只有 9U。仓位算法按权益比例缩放，
    #      不限制的话单笔会差一万倍，模拟盘根本测不出
    #      小额会撞上的各种下限（最小交易额、最小交易量）。
    #   2) 实盘资金隔离 —— 大账户只拿一部分试水。
    ParamSpec("equity_cap_usdt", 0.0, "权益上限（0=不限，按此值计算仓位）",
              float, 0.0, 10_000_000.0, aliases=("setcap",),
              unit="U", group="单次模式"),
    ParamSpec("single_order_usdt", 1.0, "单笔基础额度（单次模式，按资金缩放）",
              float, 0.1, 1000, aliases=("setamount",), unit="U", group="单次模式"),
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

# ---------- 价格守卫 ----------
# 补齐缺口：原代码只有"行情陈旧"检测（时间戳），
# 无法识别【数据是新鲜的错误值】这种情况 ——
# 交易所 API 抽风、瞬时插针都会推送错误的当前价，
# 照常下单就会以离谱的价格成交。
_reg(
    ParamSpec("price_guard_enabled", True, "启用价格突变保护（异常价格不下单）", bool,
              aliases=("setpriceguard",), group="价格守卫"),
    ParamSpec("price_guard_max_dev", 0.08, "价格突变阈值：偏离近期中位数超过此值即拦截",
              float, 0.01, 0.50, scale=0.01,
              aliases=("setpricedev",), unit="%", group="价格守卫"),
    ParamSpec("price_guard_window", 20, "价格中位数窗口大小（样本数）", int, 5, 200,
              aliases=("setpricewin",), unit="条", group="价格守卫"),
    ParamSpec("price_guard_halt_sec", 300, "触发突变后暂停该币时长", int, 60, 3600,
              aliases=("setpricehalt",), unit="秒", group="价格守卫"),
    ParamSpec("slippage_guard_enabled", True, "启用滑点检测（成交后比对预期价）", bool,
              aliases=("setslipguard",), group="价格守卫"),
    ParamSpec("slippage_max_pct", 0.01, "滑点告警阈值：成交均价偏离预期超过此值告警",
              float, 0.001, 0.10, scale=0.01,
              aliases=("setslipmax",), unit="%", group="价格守卫"),
)

# ---------- 退役线 ----------
# 补齐缺口：原有风控全是局部/周期性的（单笔止损、日内上限、
# 连亏冷却、回撤熔断），缺一条【全局累计】底线。
# 没有它可能出现：每天亏一点，每天都"没触发风控"，
# 但一个月累计亏损已经很可观。
_reg(
    ParamSpec("retire_enabled", False, "启用策略退役线（累计亏损达线则彻底停止）", bool,
              aliases=("setretire",), group="退役线"),
    ParamSpec("retire_max_loss_usdt", 20.0, "退役线：启用以来最大累计亏损（U）",
              float, 0.0, 100000.0, aliases=("setretireloss",),
              unit="U", group="退役线"),
    ParamSpec("retire_max_loss_pct", 0.20, "退役线：累计亏损占权益上限",
              float, 0.01, 1.0, scale=0.01,
              aliases=("setretirepct",), unit="%", group="退役线"),
)

# ---------- 日报 ----------
# 补齐缺口：原有告警全是事件驱动的，出事才说话。
# 于是"没消息"有两种可能（正常 / 死了）且无法区分。
# 日报提供心跳证明 —— 收到即说明它活着。
_reg(
    ParamSpec("daily_report_enabled", True, "启用每日报告（也是心跳证明）", bool,
              aliases=("setdaily",), group="日报"),
    ParamSpec("daily_report_hour", 9, "每日报告发送时间（24小时制，UTC+8）",
              int, 0, 23, aliases=("setdailyhour",),
              unit="点", group="日报"),
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
