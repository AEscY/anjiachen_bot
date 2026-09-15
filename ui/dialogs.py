import logging
import asyncio
from aiogram.types import CallbackQuery, Message
from aiogram_dialog import Dialog, DialogManager, Window
from aiogram_dialog.widgets.input import MessageInput
from aiogram_dialog.widgets.kbd import Button, Back, Column, Select, ScrollingGroup
from aiogram_dialog.widgets.text import Const, Format

from ui.states import AppSG
from strategies.manager import StrategyManager
from strategies.dip_sell import PARAM_META, DEFAULT_PARAMS

logger = logging.getLogger(__name__)

MANAGER: "StrategyManager | None" = None
PUB_WS = None
_last_price: dict = {}

VALID_BARS = ["1m", "3m", "5m", "15m", "30m", "1H", "2H", "4H", "6H", "12H", "1D"]


def _format_param_value(key, value, meta):
    t = meta.get("type")
    if t == "pct":
        try:
            return f"{float(value) * 100:.2f}%"
        except (ValueError, TypeError):
            return str(value)
    if t == "bool":
        return "开" if value else "关"
    return str(value)


def _param_hint(meta):
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


async def _handle_expired(cb: CallbackQuery, manager: DialogManager):
    await cb.answer("页面已过期，请重新选择币种", show_alert=True)
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
    if text.startswith("/"):
        return
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


async def main_getter(dialog_manager: DialogManager, **kwargs):
    if not MANAGER:
        return {"coins": [], "coins_count": 0}
    coins = [{"id": iid, "name": iid, "price": _last_price.get(iid, "-")} for iid in MANAGER.all_inst_ids()]
    return {"coins": coins, "coins_count": len(coins)}


main_menu_window = Window(
    Format("OKX 全自动控制台\n\n当前监控 {coins_count} 个币种："),
    ScrollingGroup(
        Select(
            Format("{item[name]}  {item[price]}"),
            id="coin_select",
            item_id_getter=lambda x: x["id"],
            items="coins",
            on_click=on_coin_selected,
        ),
        id="coins_scroll",
        width=1,
        height=8,
    ),
    Button(Const("添加币种"), id="add_coin", on_click=on_add_coin_click),
    state=AppSG.menu,
    getter=main_getter,
)

add_coin_window = Window(
    Const("添加币种\n\n请输入币种名称，例如 BTC-USDT："),
    MessageInput(on_add_coin_input),
    Button(Const("取消"), id="cancel", on_click=lambda c, b, m: m.switch_to(AppSG.menu)),
    state=AppSG.add_coin,
)


# ==================== 币种面板 ====================
async def coin_getter(dialog_manager: DialogManager, **kwargs):
    inst_id = dialog_manager.dialog_data.get("inst_id", "-")
    grid = MANAGER.grids.get(inst_id) if MANAGER else None
    dip = MANAGER.dips.get(inst_id) if MANAGER else None

    active = "无"
    if grid and grid.running:
        active = "网格"
    elif dip and dip.running:
        active = "低吸高卖"

    return {
        "inst_id": inst_id,
        "grid_status": "运行中" if (grid and grid.running) else "已停止",
        "dip_status": "监控中" if (dip and dip.running) else "已暂停",
        "lastPx": _last_price.get(inst_id, "-"),
        "active_mode": active,
    }


async def on_toggle_grid(cb: CallbackQuery, button, manager: DialogManager):
    inst_id = manager.dialog_data.get("inst_id")
    if not inst_id or not MANAGER:
        await _handle_expired(cb, manager)
        return
    grid = MANAGER.grids.get(inst_id)
    if not grid:
        await cb.answer("未初始化", show_alert=True)
        return

    if grid.running:
        try:
            await grid.stop()
            await cb.answer("网格已停止")
        except Exception as e:
            await cb.answer(f"停止失败: {e}", show_alert=True)
    else:
        ok, msg = await MANAGER.activate_grid_only(inst_id)
        await cb.answer(msg, show_alert=True)
    await manager.update()


async def on_toggle_dip(cb: CallbackQuery, button, manager: DialogManager):
    inst_id = manager.dialog_data.get("inst_id")
    if not inst_id or not MANAGER:
        await _handle_expired(cb, manager)
        return
    dip = MANAGER.dips.get(inst_id)
    if not dip:
        await cb.answer("未初始化", show_alert=True)
        return

    if dip.running:
        await dip.stop()
        await cb.answer("低吸高卖已暂停")
    else:
        ok, msg = await MANAGER.activate_dip_only(inst_id)
        await cb.answer(msg, show_alert=True)
    await manager.update()


async def on_stop_all(cb: CallbackQuery, button, manager: DialogManager):
    inst_id = manager.dialog_data.get("inst_id")
    if not inst_id or not MANAGER:
        await _handle_expired(cb, manager)
        return
    ok, msg = await MANAGER.stop_all_strategies(inst_id)
    await cb.answer(msg, show_alert=True)
    await manager.update()


async def on_enter_grid(cb: CallbackQuery, button, manager: DialogManager):
    inst_id = manager.dialog_data.get("inst_id")
    if not inst_id:
        await _handle_expired(cb, manager)
        return
    await manager.switch_to(AppSG.grid)


async def on_enter_dip(cb: CallbackQuery, button, manager: DialogManager):
    inst_id = manager.dialog_data.get("inst_id")
    if not inst_id:
        await _handle_expired(cb, manager)
        return
    await manager.switch_to(AppSG.dip)


async def on_enter_params(cb: CallbackQuery, button, manager: DialogManager):
    inst_id = manager.dialog_data.get("inst_id")
    if not inst_id:
        await _handle_expired(cb, manager)
        return
    await manager.switch_to(AppSG.params)


async def on_delete_coin(cb: CallbackQuery, button, manager: DialogManager):
    inst_id = manager.dialog_data.get("inst_id")
    if not inst_id:
        if MANAGER:
            current_coins = MANAGER.all_inst_ids()
            if len(current_coins) == 1:
                inst_id = current_coins[0]
            else:
                await cb.answer("页面已过期，请重新选择", show_alert=True)
                await manager.start(AppSG.menu)
                return
        else:
            await cb.answer("策略管理器未初始化", show_alert=True)
            return
    ok, text = await MANAGER.remove_inst(inst_id)
    if ok and PUB_WS:
        await PUB_WS.unsubscribe(inst_id)
    await cb.answer(text, show_alert=True)
    await manager.start(AppSG.menu)


coin_panel_window = Window(
    Format(
        "{inst_id}\n"
        "当前价: {lastPx}\n"
        "当前模式: {active_mode}\n"
        "网格: {grid_status}\n"
        "低吸高卖: {dip_status}"
    ),
    Column(
        Button(Const("启用网格 (自动停低吸)"), id="toggle_grid", on_click=on_toggle_grid),
        Button(Const("启用低吸 (自动停网格)"), id="toggle_dip", on_click=on_toggle_dip),
        Button(Const("停止全部"), id="stop_all", on_click=on_stop_all),
        Button(Const("网格详情"), id="to_grid", on_click=on_enter_grid),
        Button(Const("低吸高卖详情"), id="to_dip", on_click=on_enter_dip),
        Button(Const("参数设置"), id="to_params", on_click=on_enter_params),
        Button(Const("删除此币种"), id="del_coin", on_click=on_delete_coin),
        Button(Const("返回主菜单"), id="back_main", on_click=lambda c, b, m: m.start(AppSG.menu)),
    ),
    state=AppSG.coin_panel,
    getter=coin_getter,
)


# ==================== 参数设置 ====================
async def params_getter(dialog_manager: DialogManager, **kwargs):
    inst_id = dialog_manager.dialog_data.get("inst_id", "-")
    dip = MANAGER.dips.get(inst_id) if MANAGER else None
    if not dip:
        return {"inst_id": inst_id, "params": []}

    items = []
    for key, meta in PARAM_META.items():
        value = dip.params.get(key, DEFAULT_PARAMS.get(key))
        items.append({
            "id": key,
            "label": meta["desc"],
            "value": _format_param_value(key, value, meta),
        })
    return {"inst_id": inst_id, "params": items}


async def on_param_selected(cb: CallbackQuery, widget, manager: DialogManager, item_id: str):
    manager.dialog_data["param_key"] = item_id
    await manager.switch_to(AppSG.edit_param)


async def on_reset_params_ui(cb: CallbackQuery, button, manager: DialogManager):
    inst_id = manager.dialog_data.get("inst_id")
    dip = MANAGER.dips.get(inst_id) if MANAGER else None
    if not dip:
        await cb.answer("未初始化", show_alert=True)
        return
    await dip.reset_params()
    await cb.answer("已恢复默认参数", show_alert=True)
    await manager.update()


params_window = Window(
    Format(
        "{inst_id} 参数设置\n\n"
        "点击下方任意参数进行修改："
    ),
    ScrollingGroup(
        Select(
            Format("{item[label]}: {item[value]}"),
            id="param_select",
            item_id_getter=lambda x: x["id"],
            items="params",
            on_click=on_param_selected,
        ),
        id="params_scroll",
        width=1,
        height=10,
    ),
    Column(
        Button(Const("恢复默认值"), id="reset_params", on_click=on_reset_params_ui),
        Back(Const("返回币种面板")),
    ),
    state=AppSG.params,
    getter=params_getter,
)


# ==================== 编辑单个参数 ====================
async def edit_param_getter(dialog_manager: DialogManager, **kwargs):
    inst_id = dialog_manager.dialog_data.get("inst_id", "-")
    key = dialog_manager.dialog_data.get("param_key", "-")
    meta = PARAM_META.get(key, {})
    dip = MANAGER.dips.get(inst_id) if MANAGER else None
    value = dip.params.get(key, DEFAULT_PARAMS.get(key)) if dip else "-"

    return {
        "key": key,
        "desc": meta.get("desc", key),
        "value": _format_param_value(key, value, meta) if dip else "-",
        "hint": _param_hint(meta),
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
    if raw.startswith("/"):
        return

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
            await msg.answer(f"未知参数类型: {ptype}")
            return
    except ValueError:
        await msg.answer(f"输入格式不正确: {raw}")
        return

    rng = meta.get("range")
    if rng and not (rng[0] <= value <= rng[1]):
        await msg.answer(f"值超出范围 {rng[0]}-{rng[1]}")
        return

    await dip.set_param(key, value)
    await msg.answer(f"{meta['desc']} 已设为 {_format_param_value(key, value, meta)}")
    await manager.switch_to(AppSG.params)


edit_param_window = Window(
    Format(
        "修改 {desc}\n\n"
        "当前值: {value}\n\n"
        "{hint}"
    ),
    MessageInput(on_param_input),
    Button(Const("取消"), id="cancel", on_click=lambda c, b, m: m.switch_to(AppSG.params)),
    state=AppSG.edit_param,
    getter=edit_param_getter,
)


# ==================== 网格详情 ====================
async def grid_getter(dialog_manager: DialogManager, **kwargs):
    inst_id = dialog_manager.dialog_data.get("inst_id", "-")
    grid = MANAGER.grids.get(inst_id) if MANAGER else None
    if not grid:
        return {"inst_id": inst_id, "status": "未初始化", "minPx": "-", "maxPx": "-",
                "gridNum": "-", "lastPx": "-", "algo_id": "-",
                "tp_px": "-", "sl_px": "-", "quoteSz": "-"}
    s = grid.snapshot()
    return {
        "inst_id": inst_id,
        "status": "运行中" if s["running"] else "已停止",
        "minPx": s["params"].get("minPx", "-"),
        "maxPx": s["params"].get("maxPx", "-"),
        "gridNum": s["params"].get("gridNum", "-"),
        "lastPx": _last_price.get(inst_id, "-"),
        "algo_id": s.get("algo_id") or "-",
        "tp_px": s.get("tp_px") or "-",
        "sl_px": s.get("sl_px") or "-",
        "quoteSz": s["params"].get("quoteSz", "-"),
    }


grid_panel_window = Window(
    Format(
        "{inst_id} 全自动网格\n"
        "状态: {status}\n"
        "自动区间: {minPx} - {maxPx}\n"
        "网格数: {gridNum}\n"
        "投入金额: {quoteSz} USDT\n"
        "止盈价: {tp_px}\n"
        "止损价: {sl_px}\n"
        "当前价: {lastPx}\n"
        "Algo ID: {algo_id}\n"
        "\n止盈止损由 OKX 服务端自动执行。"
    ),
    Column(
        Back(Const("返回币种面板")),
    ),
    state=AppSG.grid,
    getter=grid_getter,
)


# ==================== 低吸高卖详情 ====================
async def dip_getter(dialog_manager: DialogManager, **kwargs):
    inst_id = dialog_manager.dialog_data.get("inst_id", "-")
    dip = MANAGER.dips.get(inst_id) if MANAGER else None
    if not dip:
        return {"inst_id": inst_id, "status": "未初始化", "lastPx": "-",
                "avg_buy_price": "-", "peak_price": "-", "position": "-",
                "profit": "0", "fee": "0", "sl": "5", "tp": "3",
                "trailing_state": "开启", "trailing_pct": "2",
                "spend": "100", "last_action": "-",
                "pending_buy": "-", "pending_sell": "-"}
    s = dip.snapshot()
    avg = s.get("avg_buy_price", 0)
    peak = s.get("peak_price", 0)
    la = s.get("last_action")
    la_text = f"{la[0]} @ {la[1]:.2f}" if la else "-"
    return {
        "inst_id": inst_id,
        "status": "监控中" if s["running"] else "已暂停",
        "lastPx": _last_price.get(inst_id, "-"),
        "avg_buy_price": f"{avg:.6f}" if avg > 0 else "-",
        "peak_price": f"{peak:.6f}" if peak > 0 else "-",
        "position": f"{s.get('position', 0):.6f}",
        "profit": f"{s.get('total_profit', 0):.4f}",
        "fee": f"{s.get('total_fee', 0):.4f}",
        "sl": f"{dip.params.get('stop_loss_pct', 0.05) * 100:.1f}",
        "tp": f"{dip.params.get('take_profit_pct', 0.03) * 100:.1f}",
        "trailing_state": "开启" if dip.params.get("use_trailing", True) else "关闭",
        "trailing_pct": f"{dip.params.get('trailing_pct', 0.02) * 100:.1f}",
        "spend": f"{dip.params.get('maxSpend', 100):.0f}",
        "last_action": la_text,
        "pending_buy": s.get("pending_buy") or "-",
        "pending_sell": s.get("pending_sell") or "-",
    }


async def on_start_dip(cb: CallbackQuery, button, manager: DialogManager):
    inst_id = manager.dialog_data.get("inst_id")
    if not inst_id or not MANAGER:
        await _handle_expired(cb, manager)
        return
    ok, msg = await MANAGER.activate_dip_only(inst_id)
    await cb.answer(msg, show_alert=True)
    await manager.update()


async def on_stop_dip(cb: CallbackQuery, button, manager: DialogManager):
    inst_id = manager.dialog_data.get("inst_id")
    dip = MANAGER.dips.get(inst_id) if MANAGER else None
    if not dip:
        await _handle_expired(cb, manager)
        return
    await dip.stop()
    await cb.answer("已暂停")
    await manager.update()


dip_panel_window = Window(
    Format(
        "{inst_id} 全自动低吸高卖\n"
        "状态: {status}\n"
        "当前价: {lastPx}\n"
        "持仓: {position}\n"
        "平均买入价: {avg_buy_price}\n"
        "持仓最高价: {peak_price}\n"
        "止盈: +{tp}%\n"
        "止损: -{sl}%\n"
        "移动止盈: {trailing_state} (回撤 {trailing_pct}%)\n"
        "单次金额: {spend} USDT\n"
        "最近动作: {last_action}\n"
        "挂单买入: {pending_buy}\n"
        "挂单卖出: {pending_sell}\n"
        "已实现盈亏: {profit} USDT\n"
        "累计手续费: {fee} USDT"
    ),
    Column(
        Button(Const("启动监控"), id="dip_start", on_click=on_start_dip),
        Button(Const("暂停"), id="dip_stop", on_click=on_stop_dip),
        Back(Const("返回币种面板")),
    ),
    state=AppSG.dip,
    getter=dip_getter,
)


# ==================== Dialog 组装 ====================
main_dialog = Dialog(
    main_menu_window,
    add_coin_window,
    coin_panel_window,
    params_window,
    edit_param_window,
    grid_panel_window,
    dip_panel_window,
)


def get_dialogs() -> list:
    return [main_dialog]