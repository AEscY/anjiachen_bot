import logging
import asyncio
from aiogram.types import CallbackQuery, Message
from aiogram_dialog import Dialog, DialogManager, Window
from aiogram_dialog.widgets.input import MessageInput
from aiogram_dialog.widgets.kbd import Button, Back, Column, Select, ScrollingGroup
from aiogram_dialog.widgets.text import Const, Format
from magic_filter import F

from ui.states import AppSG
from strategies.manager import StrategyManager
from strategies.dip_sell import DEFAULT_PARAMS

logger = logging.getLogger(__name__)

MANAGER: "StrategyManager | None" = None
PUB_WS = None
_last_price: dict = {}

VALID_BARS = ["1m", "3m", "5m", "15m", "30m", "1H", "2H", "4H", "6H", "12H", "1D"]


PARAM_GROUPS = {
    "signal": {
        "title": "信号参数",
        "params": {
            "bar":                  {"type": "bar",   "range": None,         "desc": "K线周期"},
            "use_adaptive":         {"type": "bool",  "range": None,         "desc": "自适应模式"},
            "use_dynamic_grid":     {"type": "bool",  "range": None,         "desc": "动态网格"},
            "use_trailing_entry":   {"type": "bool",  "range": None,         "desc": "追踪建仓"},
            "trailing_entry_pct":   {"type": "pct",   "range": (0.001, 0.05), "desc": "追踪确认%"},
            "use_trailing_stop":    {"type": "bool",  "range": None,         "desc": "追踪止损"},
            "trend_filter":         {"type": "bool",  "range": None,         "desc": "趋势过滤"},
            "volume_confirm":       {"type": "bool",  "range": None,         "desc": "成交量确认"},
            "rsi_period":           {"type": "int",   "range": (2, 100),     "desc": "RSI周期"},
            "rsi_oversold":         {"type": "float", "range": (5, 50),      "desc": "RSI超卖"},
            "rsi_overbought":       {"type": "float", "range": (50, 95),     "desc": "RSI超买"},
            "bb_period":            {"type": "int",   "range": (5, 100),     "desc": "布林带周期"},
            "bb_std":               {"type": "float", "range": (0.5, 5),     "desc": "布林带标准差"},
            "macd_fast":            {"type": "int",   "range": (2, 50),      "desc": "MACD快线"},
            "macd_slow":            {"type": "int",   "range": (5, 100),     "desc": "MACD慢线"},
            "macd_signal":          {"type": "int",   "range": (2, 50),      "desc": "MACD信号线"},
            "ema_period":           {"type": "int",   "range": (20, 500),    "desc": "EMA趋势周期"},
            "vol_ma_period":        {"type": "int",   "range": (5, 100),     "desc": "成交量均线"},
            "vol_multiplier":       {"type": "float", "range": (1.0, 5.0),   "desc": "成交量倍数"},
        },
    },
    "risk": {
        "title": "风控参数",
        "params": {
            "take_profit_pct": {"type": "pct",   "range": (0.001, 0.5), "desc": "固定止盈%"},
            "stop_loss_pct":   {"type": "pct",   "range": (0.001, 0.5), "desc": "固定止损%"},
            "trailing_pct":    {"type": "pct",   "range": (0.001, 0.5), "desc": "固定回撤%"},
        },
    },
    "trade": {
        "title": "交易参数",
        "params": {
            "maxSpend": {"type": "float", "range": (1, 100000), "desc": "单次金额USDT"},
        },
    },
}

PARAM_META = {}
for group in PARAM_GROUPS.values():
    PARAM_META.update(group["params"])


def _fmt(key, value, meta):
    t = meta.get("type")
    if t == "pct":
        try:
            return f"{float(value) * 100:.2f}%"
        except (ValueError, TypeError):
            return str(value)
    if t == "bool":
        return "开" if value else "关"
    return str(value)


def _hint(meta):
    t = meta.get("type")
    rng = meta.get("range")
    if t == "bar":
        return "可选: " + "/".join(VALID_BARS)
    if t == "bool":
        return "输入 true 或 false"
    if t == "pct":
        if rng:
            return f"输入百分比数字，如 3 表示 3%，范围 {rng[0]*100:.1f}-{rng[1]*100:.1f}"
        return "输入百分比数字，如 3 表示 3%"
    if t == "int":
        if rng:
            return f"输入整数，范围 {rng[0]}-{rng[1]}"
        return "输入整数"
    if t == "float":
        if rng:
            return f"输入数字，范围 {rng[0]}-{rng[1]}"
        return "输入数字"
    return ""


async def _expired(cb: CallbackQuery, manager: DialogManager):
    await cb.answer("页面已过期，请重新选择", show_alert=True)
    await manager.start(AppSG.menu)


# ==================== 主菜单 ====================
async def on_coin_selected(cb: CallbackQuery, widget, manager: DialogManager, item_id: str):
    manager.dialog_data["inst_id"] = item_id
    await manager.switch_to(AppSG.coin_panel)


async def on_add_coin_click(cb: CallbackQuery, button, manager: DialogManager):
    await manager.switch_to(AppSG.add_coin)


async def on_add_coin_input(msg: Message, widget, manager: DialogManager):
    if not MANAGER:
        return
    text = (msg.text or "").strip()
    if not text:
        await msg.answer("输入不能为空")
        return
    inst_id = text.upper()
    ok, result = await MANAGER.add_inst(inst_id)
    if ok:
        await msg.answer(result)
        if PUB_WS:
            await PUB_WS.subscribe(inst_id)
    else:
        await msg.answer(result)
    await manager.switch_to(AppSG.menu)


async def menu_getter(dialog_manager: DialogManager, **kwargs):
    if not MANAGER:
        return {"coins": [], "coins_count": 0, "total_profit": "0.0000",
                "risk_state": "未初始化", "daily_pnl": "0.0000"}

    coins = []
    total_profit = 0.0
    for iid in MANAGER.all_inst_ids():
        d = MANAGER.dips.get(iid)
        profit = d.total_profit if d else 0.0
        total_profit += profit
        running = bool(d and d.running)
        pos = d.position if d else 0.0
        coins.append({
            "id": iid,
            "name": iid,
            "price": _last_price.get(iid, "-"),
            "status": "🟢" if running else "⏸",
            "pos": f"{pos:.4f}" if pos > 0 else "-",
            "pnl": f"{profit:+.2f}",
        })

    rm = MANAGER.risk_manager
    risk_state = "⚠️ 已触发" if rm.is_risk_triggered() else "✅ 正常"

    return {
        "coins": coins,
        "coins_count": len(coins),
        "total_profit": f"{total_profit:+.4f}",
        "risk_state": risk_state,
        "daily_pnl": f"{rm.daily_pnl:+.2f}",
    }


menu_window = Window(
    Format(
        "💼 <b>OKX 低吸高卖</b>\n"
        "━━━━━━━━━━━━━━━\n"
        "监控: {coins_count} 个 | 风控: {risk_state}\n"
        "累计盈亏: {total_profit} USDT\n"
        "今日盈亏: {daily_pnl} USDT\n"
        "━━━━━━━━━━━━━━━\n"
        "👇 点击币种查看详情"
    ),
    ScrollingGroup(
        Select(
            Format("{item[status]} {item[name]}  {item[price]}  仓:{item[pos]}  盈亏:{item[pnl]}"),
            id="coin_select",
            item_id_getter=lambda x: x["id"],
            items="coins",
            on_click=on_coin_selected,
        ),
        id="coins_scroll",
        width=1,
        height=6,
    ),
    Button(Const("➕ 添加币种"), id="add_coin", on_click=on_add_coin_click),
    state=AppSG.menu,
    getter=menu_getter,
)

add_coin_window = Window(
    Const(
        "➕ <b>添加币种</b>\n\n"
        "请输入币种名称，例如：\n"
        "<code>BTC-USDT</code>\n"
        "<code>ETH-USDT</code>\n"
        "<code>SOL-USDT</code>"
    ),
    MessageInput(on_add_coin_input, filter=~F.text.startswith("/")),
    Button(Const("🔙 取消"), id="cancel", on_click=lambda c, b, m: m.switch_to(AppSG.menu)),
    state=AppSG.add_coin,
)


# ==================== 币种面板 ====================
async def coin_getter(dialog_manager: DialogManager, **kwargs):
    inst_id = dialog_manager.dialog_data.get("inst_id", "-")
    dip = MANAGER.dips.get(inst_id) if MANAGER else None
    if not dip:
        return {"inst_id": inst_id, "status": "未初始化", "price": "-",
                "zone": "-", "pos": "0", "pnl": "0.0000", "regime": "-"}

    status = "🟢 监控中" if dip.running else "⏸ 已暂停"
    try:
        f = await dip.get_signal_forecast()
        if "error" in f:
            zone = "-"
            regime = "-"
        else:
            zone = f.get("status", "-")
            regime = f.get("regime", "-")
    except Exception:
        zone = "-"
        regime = "-"

    regime_icon = {"trending": "📈 趋势", "ranging": "📊 震荡", "transitional": "🔄 过渡"}.get(regime, "-")

    return {
        "inst_id": inst_id,
        "status": status,
        "price": _last_price.get(inst_id, "-"),
        "zone": zone,
        "pos": f"{dip.position:.6f}",
        "pnl": f"{dip.total_profit:+.4f}",
        "regime": regime_icon,
    }


async def on_toggle_dip(cb: CallbackQuery, button, manager: DialogManager):
    inst_id = manager.dialog_data.get("inst_id")
    if not inst_id or not MANAGER:
        await _expired(cb, manager)
        return
    dip = MANAGER.dips.get(inst_id)
    if not dip:
        await cb.answer("未初始化", show_alert=True)
        return
    if dip.running:
        await dip.stop()
        await cb.answer("⏸ 已暂停")
    else:
        ok, msg = await MANAGER.activate_dip_only(inst_id)
        await cb.answer(msg, show_alert=True)
    await manager.update()


async def on_signal_detail(cb: CallbackQuery, button, manager: DialogManager):
    if not manager.dialog_data.get("inst_id"):
        await _expired(cb, manager)
        return
    await manager.switch_to(AppSG.signal_detail)


async def on_position_detail(cb: CallbackQuery, button, manager: DialogManager):
    if not manager.dialog_data.get("inst_id"):
        await _expired(cb, manager)
        return
    await manager.switch_to(AppSG.position_detail)


async def on_params(cb: CallbackQuery, button, manager: DialogManager):
    if not manager.dialog_data.get("inst_id"):
        await _expired(cb, manager)
        return
    await manager.switch_to(AppSG.params)


async def on_delete(cb: CallbackQuery, button, manager: DialogManager):
    inst_id = manager.dialog_data.get("inst_id")
    if not inst_id:
        if MANAGER and len(MANAGER.all_inst_ids()) == 1:
            inst_id = MANAGER.all_inst_ids()[0]
        else:
            await cb.answer("页面已过期，请重新选择", show_alert=True)
            await manager.start(AppSG.menu)
            return
    ok, text = await MANAGER.remove_inst(inst_id)
    if ok and PUB_WS:
        await PUB_WS.unsubscribe(inst_id)
    await cb.answer(text, show_alert=True)
    await manager.start(AppSG.menu)


coin_panel_window = Window(
    Format(
        "📊 <b>{inst_id}</b>\n"
        "━━━━━━━━━━━━━━━\n"
        "状态: {status}\n"
        "市场体制: {regime}\n"
        "当前价: {price}\n"
        "买入区域: {zone}\n"
        "持仓: {pos}\n"
        "已实现盈亏: {pnl} USDT"
    ),
    Column(
        Button(Const("▶️/⏸ 启停监控"), id="toggle", on_click=on_toggle_dip),
        Button(Const("📈 信号详情"), id="sig", on_click=on_signal_detail),
        Button(Const("💰 持仓"), id="pos", on_click=on_position_detail),
        Button(Const("⚙️ 参数设置"), id="params", on_click=on_params),
        Button(Const("🗑 删除此币种"), id="del", on_click=on_delete),
        Button(Const("🔙 返回主菜单"), id="back", on_click=lambda c, b, m: m.start(AppSG.menu)),
    ),
    state=AppSG.coin_panel,
    getter=coin_getter,
)


# ==================== 信号详情 ====================
async def signal_getter(dialog_manager: DialogManager, **kwargs):
    inst_id = dialog_manager.dialog_data.get("inst_id", "-")

    empty = {
        "inst_id": inst_id,
        "bar": "-",
        "status": "❌ 数据不足",
        "grid_line": "网格: -",
        "distance_line": "距买入区: -",
        "rsi_line": "RSI: -",
        "regime_line": "市场体制: -",
        "trailing_line": "追踪建仓: 未激活",
        "blocker_text": "  无",
    }

    dip = MANAGER.dips.get(inst_id) if MANAGER else None
    if not dip:
        empty["blocker_text"] = "  策略未初始化"
        return empty

    try:
        f = await dip.get_signal_forecast()
    except Exception as e:
        empty["blocker_text"] = f"  获取失败: {e}"
        return empty

    if "error" in f:
        empty["blocker_text"] = f"  {f['error']}"
        return empty

    status = f.get("status", "-")
    price = f.get("price", 0)
    grid_lower = f.get("grid_lower", 0)
    grid_upper = f.get("grid_upper", 0)
    spacing = f.get("spacing_pct", 0)
    distance = f.get("distance_to_buy", 0)
    rsi = f.get("rsi")
    regime = f.get("regime", "unknown")
    atr = f.get("atr", 0)
    blockers = f.get("blockers", [])
    trailing_active = f.get("trailing_entry_active", False)
    trailing_low = f.get("trailing_entry_low", 0)

    grid_line = f"网格: {grid_lower:.4f} — {grid_upper:.4f} (间距{spacing:.2f}%)"
    distance_line = f"距买入区: {distance:+.2f}%" if distance > 0 else "距买入区: ✅ 已在区域内"
    rsi_line = f"RSI: {rsi:.1f}" if rsi is not None else "RSI: -"

    regime_icon = {"trending": "📈 趋势", "ranging": "📊 震荡", "transitional": "🔄 过渡"}.get(regime, "❓")
    regime_line = f"市场体制: {regime_icon} | ATR: {atr:.4f}"

    if trailing_active:
        trailing_line = f"追踪建仓: 🟡 跟踪低点 {trailing_low:.4f}，等反弹确认"
    else:
        trailing_line = "追踪建仓: 未激活"

    blocker_text = "\n".join(f"  · {b}" for b in blockers) if blockers else "  无"

    return {
        "inst_id": inst_id,
        "bar": f.get("bar", "15m"),
        "status": status,
        "grid_line": grid_line,
        "distance_line": distance_line,
        "rsi_line": rsi_line,
        "regime_line": regime_line,
        "trailing_line": trailing_line,
        "blocker_text": blocker_text,
    }


signal_window = Window(
    Format(
        "📈 <b>{inst_id} 信号详情</b> [{bar}]\n"
        "━━━━━━━━━━━━━━━\n"
        "状态: {status}\n"
        "━━━━━━━━━━━━━━━\n"
        "{grid_line}\n"
        "{distance_line}\n"
        "{rsi_line}\n"
        "{regime_line}\n"
        "{trailing_line}\n"
        "━━━━━━━━━━━━━━━\n"
        "⚠️ 拦截原因:\n{blocker_text}"
    ),
    Column(
        Back(Const("🔙 返回币种面板")),
        Button(Const("🏠 主菜单"), id="home", on_click=lambda c, b, m: m.start(AppSG.menu)),
    ),
    state=AppSG.signal_detail,
    getter=signal_getter,
)


# ==================== 持仓详情 ====================
async def position_getter(dialog_manager: DialogManager, **kwargs):
    inst_id = dialog_manager.dialog_data.get("inst_id", "-")
    dip = MANAGER.dips.get(inst_id) if MANAGER else None
    if not dip:
        return {
            "inst_id": inst_id, "pos": "0", "avg": "-", "peak": "-", "price": "-",
            "realized": "0", "fee": "0", "pnl_line": "浮动盈亏: -",
            "batch_line": "分批止盈: 未触发", "trailing_line": "追踪止损: 未激活",
        }

    s = dip.snapshot()
    pos = s.get("position", 0)
    avg = s.get("avg_buy_price", 0)
    peak = s.get("peak_price", 0)

    price = _last_price.get(inst_id, 0)
    if pos > 0 and avg > 0:
        pnl_pct = (price - avg) / avg * 100 if price else 0
        pnl_line = f"浮动盈亏: {pnl_pct:+.2f}%"
    else:
        pnl_line = "浮动盈亏: -"

    batch_count = s.get("batch_tp_triggered", 0)
    batch_line = f"分批止盈已触发: {batch_count}/3 档" if batch_count > 0 else "分批止盈: 未触发"

    if hasattr(dip, "trailing_stop") and getattr(dip.trailing_stop, "trailing_active", False):
        trailing_line = "追踪止损: 🟢 已激活"
    else:
        trailing_line = "追踪止损: 未激活"

    return {
        "inst_id": inst_id,
        "pos": f"{pos:.6f}",
        "avg": f"{avg:.6f}" if avg > 0 else "-",
        "peak": f"{peak:.6f}" if peak > 0 else "-",
        "price": price,
        "realized": f"{s.get('total_profit', 0):+.4f}",
        "fee": f"{s.get('total_fee', 0):.4f}",
        "pnl_line": pnl_line,
        "batch_line": batch_line,
        "trailing_line": trailing_line,
    }


position_window = Window(
    Format(
        "💰 <b>{inst_id} 持仓</b>\n"
        "━━━━━━━━━━━━━━━\n"
        "当前价: {price}\n"
        "持仓: {pos}\n"
        "均价: {avg}\n"
        "最高价: {peak}\n"
        "{pnl_line}\n"
        "{batch_line}\n"
        "{trailing_line}\n"
        "━━━━━━━━━━━━━━━\n"
        "已实现盈亏: {realized} USDT\n"
        "累计手续费: {fee} USDT"
    ),
    Column(
        Back(Const("🔙 返回币种面板")),
        Button(Const("🏠 主菜单"), id="home", on_click=lambda c, b, m: m.start(AppSG.menu)),
    ),
    state=AppSG.position_detail,
    getter=position_getter,
)


# ==================== 参数设置 ====================
async def params_getter(dialog_manager: DialogManager, **kwargs):
    inst_id = dialog_manager.dialog_data.get("inst_id", "-")
    dip = MANAGER.dips.get(inst_id) if MANAGER else None
    if not dip:
        return {"inst_id": inst_id, "params": [], "mode_text": ""}

    is_adaptive = dip.params.get("use_adaptive", True)
    if is_adaptive:
        mode_text = "🔵 自适应模式\n动态网格 + 追踪建仓 + 追踪止损"
    else:
        mode_text = "🟠 固定参数模式"

    items = []
    for group_key, group in PARAM_GROUPS.items():
        for key, meta in group["params"].items():
            value = dip.params.get(key, DEFAULT_PARAMS.get(key))
            items.append({
                "id": key,
                "label": f"[{group['title']}] {meta['desc']}",
                "value": _fmt(key, value, meta),
            })
    return {"inst_id": inst_id, "params": items, "mode_text": mode_text}


async def on_param_selected(cb: CallbackQuery, widget, manager: DialogManager, item_id: str):
    manager.dialog_data["param_key"] = item_id
    await manager.switch_to(AppSG.edit_param)


async def on_reset(cb: CallbackQuery, button, manager: DialogManager):
    inst_id = manager.dialog_data.get("inst_id")
    dip = MANAGER.dips.get(inst_id) if MANAGER else None
    if not dip:
        await cb.answer("未初始化", show_alert=True)
        return
    await dip.reset_params()
    await cb.answer("✅ 已恢复默认参数", show_alert=True)
    await manager.update()


params_window = Window(
    Format(
        "⚙️ <b>{inst_id} 参数设置</b>\n\n"
        "{mode_text}\n\n"
        "点击下方参数修改："
    ),
    ScrollingGroup(
        Select(
            Format("{item[label]}\n  当前: {item[value]}"),
            id="param_select",
            item_id_getter=lambda x: x["id"],
            items="params",
            on_click=on_param_selected,
        ),
        id="params_scroll",
        width=1,
        height=8,
    ),
    Column(
        Button(Const("🔄 恢复默认值"), id="reset", on_click=on_reset),
        Back(Const("🔙 返回币种面板")),
    ),
    state=AppSG.params,
    getter=params_getter,
)


# ==================== 编辑单个参数 ====================
async def edit_getter(dialog_manager: DialogManager, **kwargs):
    inst_id = dialog_manager.dialog_data.get("inst_id", "-")
    key = dialog_manager.dialog_data.get("param_key", "-")
    meta = PARAM_META.get(key, {})
    dip = MANAGER.dips.get(inst_id) if MANAGER else None
    value = dip.params.get(key, DEFAULT_PARAMS.get(key)) if dip else "-"
    return {
        "desc": meta.get("desc", key),
        "value": _fmt(key, value, meta) if dip else "-",
        "hint": _hint(meta),
    }


async def on_param_input(msg: Message, widget, manager: DialogManager):
    if not MANAGER:
        return
    inst_id = manager.dialog_data.get("inst_id")
    key = manager.dialog_data.get("param_key")
    dip = MANAGER.dips.get(inst_id)
    meta = PARAM_META.get(key)

    if not dip or not meta:
        await msg.answer("无效操作")
        await manager.switch_to(AppSG.params)
        return

    raw = (msg.text or "").strip()

    ptype = meta["type"]
    try:
        if ptype == "bar":
            if raw not in VALID_BARS:
                await msg.answer(f"不支持的周期 {raw}。可用: " + "/".join(VALID_BARS))
                return
            value = raw
        elif ptype == "bool":
            value = raw.lower() in ("1", "true", "on", "yes", "开")
        elif ptype == "int":
            value = int(raw)
        elif ptype == "float":
            value = float(raw)
        elif ptype == "pct":
            value = float(raw) / 100.0
        else:
            await msg.answer(f"未知类型: {ptype}")
            return
    except ValueError:
        await msg.answer(f"格式不正确: {raw}")
        return

    rng = meta.get("range")
    if rng and not (rng[0] <= value <= rng[1]):
        await msg.answer(f"超出范围 {rng[0]}-{rng[1]}")
        return

    await dip.set_param(key, value)
    await msg.answer(f"✅ {meta['desc']} 已设为 {_fmt(key, value, meta)}")
    await manager.switch_to(AppSG.params)


edit_window = Window(
    Format(
        "✏️ <b>修改 {desc}</b>\n\n"
        "当前值: {value}\n\n"
        "{hint}"
    ),
    MessageInput(on_param_input, filter=~F.text.startswith("/")),
    Button(Const("🔙 取消"), id="cancel", on_click=lambda c, b, m: m.switch_to(AppSG.params)),
    state=AppSG.edit_param,
    getter=edit_getter,
)


# ==================== Dialog 组装 ====================
main_dialog = Dialog(
    menu_window,
    add_coin_window,
    coin_panel_window,
    signal_window,
    position_window,
    params_window,
    edit_window,
)


def get_dialogs() -> list:
    return [main_dialog]