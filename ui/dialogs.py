# ui/dialogs.py
import logging
from aiogram import Router
from aiogram.types import CallbackQuery
from aiogram_dialog import (
    Dialog, DialogManager, StartMode, Window,
)
from aiogram_dialog.widgets.kbd import Button, Back, Column
from aiogram_dialog.widgets.text import Const, Format

from ui.states import MainSG, GridSG, DipSG
from strategies.grid import GridStrategy
from strategies.dip_sell import DipSellStrategy

logger = logging.getLogger(__name__)

# ============ 全局策略实例（由 main.py 注入） ============
GRID: "GridStrategy | None" = None
DIP: "DipSellStrategy | None" = None

# ============ 行情缓存（由 main.py 的 on_ticker 更新） ============
_last_price: dict = {}


# ==================== 主菜单回调 ====================
async def on_switch_to_grid(cb: CallbackQuery, button, manager: DialogManager):
    await manager.start(GridSG.panel)


async def on_switch_to_dip(cb: CallbackQuery, button, manager: DialogManager):
    await manager.start(DipSG.panel)


# ==================== 网格模式回调 ====================
async def on_start_grid(cb: CallbackQuery, button, manager: DialogManager):
    if not GRID:
        await cb.answer("网格策略未初始化", show_alert=True)
        return
    if GRID.running:
        await cb.answer("网格已在运行中", show_alert=True)
        return
    try:
        await GRID.start()
        await cb.answer("✅ 网格已启动")
    except Exception as e:
        logger.error(f"启动网格失败: {e}")
        await cb.answer(f"启动失败: {e}", show_alert=True)
    await manager.update()


async def on_stop_grid(cb: CallbackQuery, button, manager: DialogManager):
    if not GRID:
        await cb.answer("网格策略未初始化", show_alert=True)
        return
    if not GRID.running:
        await cb.answer("网格未在运行", show_alert=True)
        return
    try:
        await GRID.stop()
        await cb.answer("🛑 网格已停止")
    except Exception as e:
        logger.error(f"停止网格失败: {e}")
        await cb.answer(f"停止失败: {e}", show_alert=True)
    await manager.update()


async def on_edit_grid_range(cb: CallbackQuery, button, manager: DialogManager):
    await cb.answer("✏️ 修改价格区间功能开发中", show_alert=True)


async def on_edit_grid_num(cb: CallbackQuery, button, manager: DialogManager):
    await cb.answer("✏️ 修改网格数量功能开发中", show_alert=True)


# ==================== 低吸高卖回调 ====================
async def on_start_dip(cb: CallbackQuery, button, manager: DialogManager):
    if not DIP:
        await cb.answer("策略未初始化", show_alert=True)
        return
    if DIP.running:
        await cb.answer("监控已在运行中", show_alert=True)
        return
    await DIP.start()
    await cb.answer("✅ 监控已启动")
    await manager.update()


async def on_stop_dip(cb: CallbackQuery, button, manager: DialogManager):
    if not DIP:
        await cb.answer("策略未初始化", show_alert=True)
        return
    if not DIP.running:
        await cb.answer("监控未在运行", show_alert=True)
        return
    await DIP.stop()
    await cb.answer("⏸ 监控已暂停")
    await manager.update()


async def on_edit_dip_buy(cb: CallbackQuery, button, manager: DialogManager):
    await cb.answer("✏️ 修改买入线功能开发中", show_alert=True)


async def on_edit_dip_sell(cb: CallbackQuery, button, manager: DialogManager):
    await cb.answer("✏️ 修改卖出线功能开发中", show_alert=True)


# ==================== Getter 函数 ====================
async def grid_getter(dialog_manager: DialogManager, **kwargs):
    if not GRID:
        return {
            "inst_id": "-", "status": "未初始化",
            "minPx": "-", "maxPx": "-", "gridNum": "-",
            "lastPx": "-", "algo_id": "-",
        }
    s = GRID.snapshot()
    price = _last_price.get(s["inst_id"], "-")
    return {
        "inst_id": s["inst_id"],
        "status": "🟢 运行中" if s["running"] else "🔴 已停止",
        "minPx": s["params"].get("minPx", "-"),
        "maxPx": s["params"].get("maxPx", "-"),
        "gridNum": s["params"].get("gridNum", "-"),
        "lastPx": price,
        "algo_id": s.get("algo_id") or "-",
    }


async def dip_getter(dialog_manager: DialogManager, **kwargs):
    if not DIP:
        return {
            "inst_id": "-", "status": "未初始化",
            "base_px": "-", "buy_px": "-", "sell_px": "-",
            "lastPx": "-", "position": "-",
        }
    s = DIP.snapshot()
    price = _last_price.get(s["inst_id"], "-")
    return {
        "inst_id": s["inst_id"],
        "status": "🟢 监控中" if s["running"] else "⏸ 已暂停",
        "base_px": s.get("base_px", "-"),
        "buy_px": round(s.get("buy_px", 0), 2),
        "sell_px": round(s.get("sell_px", 0), 2),
        "lastPx": price,
        "position": f"{s.get('position', 0):.6f}",
    }


# ==================== Window 定义 ====================
main_menu_window = Window(
    Const(
        "📊 <b>OKX 交易控制台</b>\n\n"
        "请选择策略模式："
    ),
    Column(
        Button(Const("📈 网格模式"), id="to_grid", on_click=on_switch_to_grid),
        Button(Const("📉 低吸高卖"), id="to_dip", on_click=on_switch_to_dip),
    ),
    state=MainSG.menu,
)

grid_panel_window = Window(
    Format(
        "📊 <b>网格模式</b>\n"
        "──────────────────\n"
        "交易对: {inst_id}\n"
        "状态: {status}\n"
        "价格区间: {minPx} — {maxPx}\n"
        "网格数: {gridNum}\n"
        "当前价: {lastPx}\n"
        "Algo ID: {algo_id}"
    ),
    Column(
        Button(Const("▶️ 启动网格"), id="grid_start", on_click=on_start_grid),
        Button(Const("🛑 停止并撤销"), id="grid_stop", on_click=on_stop_grid),
        Button(Const("✏️ 修改区间"), id="grid_edit_range", on_click=on_edit_grid_range),
        Button(Const("🔢 修改网格数"), id="grid_edit_num", on_click=on_edit_grid_num),
        Back(Const("🔙 返回主菜单")),
    ),
    state=GridSG.panel,
    getter=grid_getter,
)

dip_panel_window = Window(
    Format(
        "📉 <b>低吸高卖</b>\n"
        "──────────────────\n"
        "交易对: {inst_id}\n"
        "状态: {status}\n"
        "基准价: {base_px}\n"
        "买入线: ≤ {buy_px}\n"
        "卖出线: ≥ {sell_px}\n"
        "当前价: {lastPx}\n"
        "持仓: {position}"
    ),
    Column(
        Button(Const("▶️ 启动监控"), id="dip_start", on_click=on_start_dip),
        Button(Const("⏸ 暂停"), id="dip_stop", on_click=on_stop_dip),
        Button(Const("✏️ 修改买入线"), id="dip_edit_buy", on_click=on_edit_dip_buy),
        Button(Const("✏️ 修改卖出线"), id="dip_edit_sell", on_click=on_edit_dip_sell),
        Back(Const("🔙 返回主菜单")),
    ),
    state=DipSG.panel,
    getter=dip_getter,
)


# ==================== Dialog 对象 ====================
main_dialog = Dialog(main_menu_window)
grid_dialog = Dialog(grid_panel_window)
dip_dialog = Dialog(dip_panel_window)


# ==================== 供 main.py 调用 ====================
def get_dialogs() -> list:
    """返回所有 Dialog 对象，由 main.py 直接注册到 Dispatcher"""
    return [main_dialog, grid_dialog, dip_dialog]