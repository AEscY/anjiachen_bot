import logging
import asyncio
from aiogram.types import CallbackQuery, Message
from aiogram_dialog import Dialog, DialogManager, Window
from aiogram_dialog.widgets.input import MessageInput
from aiogram_dialog.widgets.kbd import Button, Back, Column, Select, ScrollingGroup
from aiogram_dialog.widgets.text import Const, Format

from ui.states import AppSG
from strategies.manager import StrategyManager

logger = logging.getLogger(__name__)

MANAGER: "StrategyManager | None" = None
PUB_WS = None
_last_price: dict = {}


def _parse_float(text: str):
    try:
        return float(text.strip()), None
    except ValueError:
        return None, "输入必须为有效数字"


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
        "低吸高卖: {dip_status}\n"
        "\n注意: 两个模式互斥，启用一个会自动停止另一个。"
    ),
    Column(
        Button(Const("启用网格 (自动停低吸)"), id="toggle_grid", on_click=on_toggle_grid),
        Button(Const("启用低吸 (自动停网格)"), id="toggle_dip", on_click=on_toggle_dip),
        Button(Const("停止全部"), id="stop_all", on_click=on_stop_all),
        Button(Const("网格详情"), id="to_grid", on_click=on_enter_grid),
        Button(Const("低吸高卖详情"), id="to_dip", on_click=on_enter_dip),
        Button(Const("删除此币种"), id="del_coin", on_click=on_delete_coin),
        Button(Const("返回主菜单"), id="back_main", on_click=lambda c, b, m: m.start(AppSG.menu)),
    ),
    state=AppSG.coin_panel,
    getter=coin_getter,
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


async def on_toggle_trailing(cb: CallbackQuery, button, manager: DialogManager):
    inst_id = manager.dialog_data.get("inst_id")
    dip = MANAGER.dips.get(inst_id) if MANAGER else None
    if not dip:
        await _handle_expired(cb, manager)
        return
    dip.params["use_trailing"] = not dip.params.get("use_trailing", True)
    state = "开启" if dip.params["use_trailing"] else "关闭"
    await cb.answer(f"移动止盈已{state}", show_alert=True)
    await manager.update()


async def on_edit_tp(cb: CallbackQuery, button, manager: DialogManager):
    inst_id = manager.dialog_data.get("inst_id")
    if not inst_id:
        await _handle_expired(cb, manager)
        return
    await manager.switch_to(AppSG.dip_edit_tp)


async def on_edit_sl(cb: CallbackQuery, button, manager: DialogManager):
    inst_id = manager.dialog_data.get("inst_id")
    if not inst_id:
        await _handle_expired(cb, manager)
        return
    await manager.switch_to(AppSG.dip_edit_sl)


async def on_edit_trailing(cb: CallbackQuery, button, manager: DialogManager):
    inst_id = manager.dialog_data.get("inst_id")
    if not inst_id:
        await _handle_expired(cb, manager)
        return
    await manager.switch_to(AppSG.dip_edit_trailing)


async def on_edit_spend(cb: CallbackQuery, button, manager: DialogManager):
    inst_id = manager.dialog_data.get("inst_id")
    if not inst_id:
        await _handle_expired(cb, manager)
        return
    await manager.switch_to(AppSG.dip_edit_spend)


async def on_tp_input(msg: Message, widget, manager: DialogManager):
    val, err = _parse_float(msg.text or "")
    if err or val is None or val <= 0 or val > 50:
        await msg.answer("请输入 0-50 之间的数字，单位 %")
        return
    inst_id = manager.dialog_data.get("inst_id")
    dip = MANAGER.dips.get(inst_id) if MANAGER else None
    if dip:
        dip.params["take_profit_pct"] = val / 100.0
        await msg.answer(f"止盈已修改为 {val}%")
    await manager.switch_to(AppSG.dip)


async def on_sl_input(msg: Message, widget, manager: DialogManager):
    val, err = _parse_float(msg.text or "")
    if err or val is None or val <= 0 or val > 50:
        await msg.answer("请输入 0-50 之间的数字，单位 %")
        return
    inst_id = manager.dialog_data.get("inst_id")
    dip = MANAGER.dips.get(inst_id) if MANAGER else None
    if dip:
        dip.params["stop_loss_pct"] = val / 100.0
        await msg.answer(f"止损已修改为 {val}%")
    await manager.switch_to(AppSG.dip)


async def on_trailing_input(msg: Message, widget, manager: DialogManager):
    val, err = _parse_float(msg.text or "")
    if err or val is None or val <= 0 or val > 20:
        await msg.answer("请输入 0-20 之间的数字，单位 %")
        return
    inst_id = manager.dialog_data.get("inst_id")
    dip = MANAGER.dips.get(inst_id) if MANAGER else None
    if dip:
        dip.params["trailing_pct"] = val / 100.0
        await msg.answer(f"移动止盈回撤已修改为 {val}%")
    await manager.switch_to(AppSG.dip)


async def on_spend_input(msg: Message, widget, manager: DialogManager):
    val, err = _parse_float(msg.text or "")
    if err or val is None or val <= 0:
        await msg.answer("请输入大于 0 的数字，单位 USDT")
        return
    inst_id = manager.dialog_data.get("inst_id")
    dip = MANAGER.dips.get(inst_id) if MANAGER else None
    if dip:
        dip.params["maxSpend"] = val
        await msg.answer(f"单次买入金额已修改为 {val} USDT")
    await manager.switch_to(AppSG.dip)


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
        Button(Const("切换移动止盈"), id="toggle_trailing", on_click=on_toggle_trailing),
        Button(Const("修改止盈%"), id="edit_tp", on_click=on_edit_tp),
        Button(Const("修改止损%"), id="edit_sl", on_click=on_edit_sl),
        Button(Const("修改移动回撤%"), id="edit_trailing", on_click=on_edit_trailing),
        Button(Const("修改单次金额"), id="edit_spend", on_click=on_edit_spend),
        Back(Const("返回币种面板")),
    ),
    state=AppSG.dip,
    getter=dip_getter,
)

dip_edit_tp_window = Window(
    Const("修改止盈\n\n请输入百分比数字，例如 3 表示 3%\n范围 0-50："),
    MessageInput(on_tp_input),
    Button(Const("取消"), id="cancel", on_click=lambda c, b, m: m.switch_to(AppSG.dip)),
    state=AppSG.dip_edit_tp,
)

dip_edit_sl_window = Window(
    Const("修改止损\n\n请输入百分比数字，例如 5 表示 5%\n范围 0-50："),
    MessageInput(on_sl_input),
    Button(Const("取消"), id="cancel", on_click=lambda c, b, m: m.switch_to(AppSG.dip)),
    state=AppSG.dip_edit_sl,
)

dip_edit_trailing_window = Window(
    Const("修改移动止盈回撤\n\n请输入百分比数字，例如 2 表示 2%\n范围 0-20："),
    MessageInput(on_trailing_input),
    Button(Const("取消"), id="cancel", on_click=lambda c, b, m: m.switch_to(AppSG.dip)),
    state=AppSG.dip_edit_trailing,
)

dip_edit_spend_window = Window(
    Const("修改单次买入金额\n\n请输入 USDT 金额，例如 100："),
    MessageInput(on_spend_input),
    Button(Const("取消"), id="cancel", on_click=lambda c, b, m: m.switch_to(AppSG.dip)),
    state=AppSG.dip_edit_spend,
)


# ==================== Dialog 组装 ====================
main_dialog = Dialog(
    main_menu_window,
    add_coin_window,
    coin_panel_window,
    grid_panel_window,
    dip_panel_window,
    dip_edit_tp_window,
    dip_edit_sl_window,
    dip_edit_trailing_window,
    dip_edit_spend_window,
)


def get_dialogs() -> list:
    return [main_dialog]