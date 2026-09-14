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


def _parse_int(text: str):
    try:
        return int(text.strip()), None
    except ValueError:
        return None, "输入必须为整数"


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
    return {
        "inst_id": inst_id,
        "grid_status": "运行中" if (grid and grid.running) else "已停止",
        "dip_status": "监控中" if (dip and dip.running) else "已暂停",
        "lastPx": _last_price.get(inst_id, "-"),
    }


async def on_start_all(cb: CallbackQuery, button, manager: DialogManager):
    inst_id = manager.dialog_data.get("inst_id")
    if not inst_id or not MANAGER:
        await _handle_expired(cb, manager)
        return

    grid = MANAGER.grids.get(inst_id)
    dip = MANAGER.dips.get(inst_id)
    messages = []

    if grid and not grid.running:
        try:
            await grid.start()
            messages.append("网格已启动")
        except Exception as e:
            messages.append(f"网格启动失败: {e}")

    if dip and not dip.running:
        try:
            await dip.start()
            messages.append("低吸高卖已启动")
        except Exception as e:
            messages.append(f"低吸高卖启动失败: {e}")

    text = " / ".join(messages) if messages else "已在运行"
    await cb.answer(text, show_alert=True)
    await manager.update()


async def on_stop_all(cb: CallbackQuery, button, manager: DialogManager):
    inst_id = manager.dialog_data.get("inst_id")
    if not inst_id or not MANAGER:
        await _handle_expired(cb, manager)
        return

    grid = MANAGER.grids.get(inst_id)
    dip = MANAGER.dips.get(inst_id)

    if grid and grid.running:
        try:
            await grid.stop()
        except Exception as e:
            logger.error(f"停止网格失败: {e}")
    if dip and dip.running:
        try:
            await dip.stop()
        except Exception as e:
            logger.error(f"停止低吸高卖失败: {e}")

    await cb.answer("已停止全部策略", show_alert=True)
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
        "网格: {grid_status}\n"
        "低吸高卖: {dip_status}\n"
        "\n全自动模式：机器自己判断买卖点，无需手动改参数。"
    ),
    Column(
        Button(Const("启动全自动"), id="start_all", on_click=on_start_all),
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
                "gridNum": "-", "lastPx": "-", "algo_id": "-", "rebuilds": "0"}
    s = grid.snapshot()
    return {
        "inst_id": inst_id,
        "status": "运行中" if s["running"] else "已停止",
        "minPx": s["params"].get("minPx", "-"),
        "maxPx": s["params"].get("maxPx", "-"),
        "gridNum": s["params"].get("gridNum", "-"),
        "lastPx": _last_price.get(inst_id, "-"),
        "algo_id": s.get("algo_id") or "-",
        "rebuilds": s.get("rebuild_count", 0),
    }


grid_panel_window = Window(
    Format(
        "{inst_id} 全自动网格\n"
        "状态: {status}\n"
        "自动区间: {minPx} - {maxPx}\n"
        "网格数: {gridNum}\n"
        "当前价: {lastPx}\n"
        "自动重建次数: {rebuilds}\n"
        "Algo ID: {algo_id}\n"
        "\n区间由 ATR 自动计算，价格接近边界时自动重建。"
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
                "avg_buy_price": "-", "position": "-", "profit": "0", "fee": "0",
                "sl": "5", "tp": "3", "last_action": "-"}
    s = dip.snapshot()
    avg = s.get("avg_buy_price", 0)
    la = s.get("last_action")
    la_text = f"{la[0]} @ {la[1]:.2f}" if la else "-"
    return {
        "inst_id": inst_id,
        "status": "监控中" if s["running"] else "已暂停",
        "lastPx": _last_price.get(inst_id, "-"),
        "avg_buy_price": f"{avg:.6f}" if avg > 0 else "-",
        "position": f"{s.get('position', 0):.6f}",
        "profit": f"{s.get('total_profit', 0):.4f}",
        "fee": f"{s.get('total_fee', 0):.4f}",
        "sl": f"{dip.params.get('stop_loss_pct', 0.05) * 100:.1f}",
        "tp": f"{dip.params.get('take_profit_pct', 0.03) * 100:.1f}",
        "last_action": la_text,
    }


async def on_start_dip(cb: CallbackQuery, button, manager: DialogManager):
    inst_id = manager.dialog_data.get("inst_id")
    dip = MANAGER.dips.get(inst_id) if MANAGER else None
    if not dip:
        await _handle_expired(cb, manager)
        return
    await dip.start()
    await cb.answer("已启动")
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
        "止盈: +{tp}%\n"
        "止损: -{sl}%\n"
        "最近动作: {last_action}\n"
        "已实现盈亏: {profit} USDT\n"
        "累计手续费: {fee} USDT\n"
        "\n买卖完全由信号驱动，无需手动设价格。"
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
    grid_panel_window,
    dip_panel_window,
)


def get_dialogs() -> list:
    return [main_dialog]