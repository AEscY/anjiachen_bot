import logging
from aiogram.types import CallbackQuery, Message
from aiogram_dialog import Dialog, DialogManager, Window
from aiogram_dialog.widgets.input import MessageInput
from aiogram_dialog.widgets.kbd import Button, Back, Column, Select, ScrollingGroup
from aiogram_dialog.widgets.text import Const, Format

from ui.states import MainSG, CoinSG
from strategies.manager import StrategyManager

logger = logging.getLogger(__name__)

MANAGER: "StrategyManager | None" = None
PUB_WS = None
_last_price: dict = {}

async def on_coin_selected(cb: CallbackQuery, widget, manager: DialogManager, item_id: str):
    manager.dialog_data["inst_id"] = item_id
    await manager.start(CoinSG.panel)

async def on_add_coin_click(cb: CallbackQuery, button, manager: DialogManager):
    await manager.switch_to(MainSG.add_coin)

async def on_add_coin_input(msg: Message, widget, manager: DialogManager):
    if not MANAGER:
        return
    inst_id = (msg.text or "").strip().upper()
    if not inst_id:
        await msg.answer("输入不能为空")
        return
    ok, text = await MANAGER.add_inst(inst_id)
    if ok:
        await msg.answer(f"成功: {text}")
        if PUB_WS:
            await PUB_WS.subscribe(inst_id)
    else:
        await msg.answer(f"失败: {text}")
    await manager.start(MainSG.menu)

async def main_getter(dialog_manager: DialogManager, **kwargs):
    if not MANAGER:
        return {"coins": []}
    coins = []
    for iid in MANAGER.all_inst_ids():
        price = _last_price.get(iid, "-")
        coins.append({"id": iid, "name": iid, "price": price})
    return {"coins": coins}

main_menu_window = Window(
    Format("OKX 多币种交易控制台\n\n当前监控 {coins} 个币种："),
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
    state=MainSG.menu,
    getter=main_getter,
)

add_coin_window = Window(
    Const("添加币种\n\n请输入币种名称，例如 BTC-USDT："),
    MessageInput(on_add_coin_input),
    Button(Const("取消"), id="cancel", on_click=lambda c, b, m: m.start(MainSG.menu)),
    state=MainSG.add_coin,
)

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

async def on_enter_grid(cb: CallbackQuery, button, manager: DialogManager):
    await manager.switch_to(CoinSG.grid)

async def on_enter_dip(cb: CallbackQuery, button, manager: DialogManager):
    await manager.switch_to(CoinSG.dip)

async def on_delete_coin(cb: CallbackQuery, button, manager: DialogManager):
    inst_id = manager.dialog_data.get("inst_id")
    if not inst_id or not MANAGER:
        await cb.answer("无效操作", show_alert=True)
        return
    ok, text = await MANAGER.remove_inst(inst_id)
    if ok and PUB_WS:
        await PUB_WS.unsubscribe(inst_id)
    await cb.answer(text, show_alert=True)
    await manager.start(MainSG.menu)

coin_panel_window = Window(
    Format(
        "{inst_id}\n"
        "当前价: {lastPx}\n"
        "网格: {grid_status}\n"
        "低吸高卖: {dip_status}"
    ),
    Column(
        Button(Const("网格模式"), id="to_grid", on_click=on_enter_grid),
        Button(Const("低吸高卖"), id="to_dip", on_click=on_enter_dip),
        Button(Const("删除此币种"), id="del_coin", on_click=on_delete_coin),
        Button(Const("返回主菜单"), id="back_main", on_click=lambda c, b, m: m.start(MainSG.menu)),
    ),
    state=CoinSG.panel,
    getter=coin_getter,
)

async def grid_getter(dialog_manager: DialogManager, **kwargs):
    inst_id = dialog_manager.dialog_data.get("inst_id", "-")
    grid = MANAGER.grids.get(inst_id) if MANAGER else None
    if not grid:
        return {"inst_id": inst_id, "status": "未初始化", "minPx": "-", "maxPx": "-", "gridNum": "-", "lastPx": "-", "algo_id": "-"}
    s = grid.snapshot()
    return {
        "inst_id": inst_id,
        "status": "运行中" if s["running"] else "已停止",
        "minPx": s["params"].get("minPx", "-"),
        "maxPx": s["params"].get("maxPx", "-"),
        "gridNum": s["params"].get("gridNum", "-"),
        "lastPx": _last_price.get(inst_id, "-"),
        "algo_id": s.get("algo_id") or "-",
    }

async def on_start_grid(cb: CallbackQuery, button, manager: DialogManager):
    inst_id = manager.dialog_data.get("inst_id")
    grid = MANAGER.grids.get(inst_id) if MANAGER else None
    if not grid:
        await cb.answer("未初始化", show_alert=True)
        return
    if grid.running:
        await cb.answer("已在运行", show_alert=True)
        return
    try:
        await grid.start()
        await cb.answer("网格已启动")
    except Exception as e:
        await cb.answer(f"启动失败: {e}", show_alert=True)
    await manager.update()

async def on_stop_grid(cb: CallbackQuery, button, manager: DialogManager):
    inst_id = manager.dialog_data.get("inst_id")
    grid = MANAGER.grids.get(inst_id) if MANAGER else None
    if not grid or not grid.running:
        await cb.answer("未在运行", show_alert=True)
        return
    try:
        await grid.stop()
        await cb.answer("已停止")
    except Exception as e:
        await cb.answer(f"停止失败: {e}", show_alert=True)
    await manager.update()

grid_panel_window = Window(
    Format(
        "{inst_id} 网格\n"
        "状态: {status}\n"
        "价格区间: {minPx} — {maxPx}\n"
        "网格数: {gridNum}\n"
        "当前价: {lastPx}\n"
        "Algo ID: {algo_id}"
    ),
    Column(
        Button(Const("启动网格"), id="grid_start", on_click=on_start_grid),
        Button(Const("停止并撤销"), id="grid_stop", on_click=on_stop_grid),
        Back(Const("返回币种面板")),
    ),
    state=CoinSG.grid,
    getter=grid_getter,
)

async def dip_getter(dialog_manager: DialogManager, **kwargs):
    inst_id = dialog_manager.dialog_data.get("inst_id", "-")
    dip = MANAGER.dips.get(inst_id) if MANAGER else None
    if not dip:
        return {"inst_id": inst_id, "status": "未初始化", "base_px": "-", "buy_px": "-", "sell_px": "-", "lastPx": "-", "position": "-"}
    s = dip.snapshot()
    return {
        "inst_id": inst_id,
        "status": "监控中" if s["running"] else "已暂停",
        "base_px": s.get("base_px", "-"),
        "buy_px": round(s.get("buy_px", 0), 6),
        "sell_px": round(s.get("sell_px", 0), 6),
        "lastPx": _last_price.get(inst_id, "-"),
        "position": f"{s.get('position', 0):.6f}",
    }

async def on_start_dip(cb: CallbackQuery, button, manager: DialogManager):
    inst_id = manager.dialog_data.get("inst_id")
    dip = MANAGER.dips.get(inst_id) if MANAGER else None
    if not dip:
        await cb.answer("未初始化", show_alert=True)
        return
    await dip.start()
    await cb.answer("已启动")
    await manager.update()

async def on_stop_dip(cb: CallbackQuery, button, manager: DialogManager):
    inst_id = manager.dialog_data.get("inst_id")
    dip = MANAGER.dips.get(inst_id) if MANAGER else None
    if not dip:
        await cb.answer("未初始化", show_alert=True)
        return
    await dip.stop()
    await cb.answer("已暂停")
    await manager.update()

dip_panel_window = Window(
    Format(
        "{inst_id} 低吸高卖\n"
        "状态: {status}\n"
        "基准价: {base_px}\n"
        "买入线: ≤ {buy_px}\n"
        "卖出线: ≥ {sell_px}\n"
        "当前价: {lastPx}\n"
        "持仓: {position}"
    ),
    Column(
        Button(Const("启动监控"), id="dip_start", on_click=on_start_dip),
        Button(Const("暂停"), id="dip_stop", on_click=on_stop_dip),
        Back(Const("返回币种面板")),
    ),
    state=CoinSG.dip,
    getter=dip_getter,
)

main_dialog = Dialog(main_menu_window, add_coin_window)
coin_dialog = Dialog(coin_panel_window, grid_panel_window, dip_panel_window)

def get_dialogs() -> list:
    return [main_dialog, coin_dialog]