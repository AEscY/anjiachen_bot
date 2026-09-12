# ui/dialogs.py
from aiogram.types import CallbackQuery, Message
from aiogram_dialog import DialogManager, StartMode
from aiogram_dialog.widgets.kbd import Button
from ui.states import MainSG, GridSG, DipSG
from strategies.grid import GridStrategy
from strategies.dip_sell import DipSellStrategy

# 全局策略实例（由 main.py 注入）
GRID: GridStrategy | None = None
DIP: DipSellStrategy | None = None

async def on_switch_to_grid(cb: CallbackQuery, btn: Button, mgr: DialogManager):
    await mgr.start(GridSG.panel, mode=StartMode.RESET_STACK)

async def on_switch_to_dip(cb: CallbackQuery, btn: Button, mgr: DialogManager):
    await mgr.start(DipSG.panel, mode=StartMode.RESET_STACK)

async def on_start_grid(cb: CallbackQuery, btn: Button, mgr: DialogManager):
    if GRID and not GRID.running:
        await GRID.start()
    await mgr.update()

async def on_stop_grid(cb: CallbackQuery, btn: Button, mgr: DialogManager):
    if GRID and GRID.running:
        await GRID.stop()
    await mgr.update()

async def on_start_dip(cb: CallbackQuery, btn: Button, mgr: DialogManager):
    if DIP:
        await DIP.start()
    await mgr.update()

async def on_stop_dip(cb: CallbackQuery, btn: Button, mgr: DialogManager):
    if DIP:
        await DIP.stop()
    await mgr.update()

# --- getters ---

async def _grid_getter(dialog_manager: DialogManager, **kwargs):
    if not GRID:
        return {"inst_id": "-", "status": "未初始化", "minPx": "-", "maxPx": "-",
                "gridNum": "-", "lastPx": "-"}
    s = GRID.snapshot()
    return {
        "inst_id": s["inst_id"],
        "status": "🟢 运行中" if s["running"] else "🔴 已停止",
        "minPx": s["params"]["minPx"], "maxPx": s["params"]["maxPx"],
        "gridNum": s["params"]["gridNum"],
        "lastPx": _last_price.get(s["inst_id"], "-"),
    }

async def _dip_getter(dialog_manager: DialogManager, **kwargs):
    if not DIP:
        return {"inst_id": "-", "status": "未初始化", "base_px": "-",
                "buy_px": "-", "sell_px": "-", "lastPx": "-", "position": "-"}
    s = DIP.snapshot()
    return {
        "inst_id": s["inst_id"],
        "status": "🟢 监控中" if s["running"] else "⏸ 已暂停",
        "base_px": s["base_px"], "buy_px": round(s["buy_px"], 2),
        "sell_px": round(s["sell_px"], 2),
        "lastPx": _last_price.get(s["inst_id"], "-"),
        "position": f"{s['position']:.6f}",
    }

_last_price = {}   # 由 main.py 的 ticker 回调更新