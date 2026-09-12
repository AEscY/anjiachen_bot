# ui/windows.py
from aiogram_dialog import Window
from aiogram_dialog.widgets.kbd import Button, SwitchTo, Back, Column
from aiogram_dialog.widgets.text import Const, Format
from ui.states import MainSG, GridSG, DipSG
from ui.dialogs import (
    on_start_grid, on_stop_grid, on_grid_detail,
    on_start_dip, on_stop_dip, on_edit_buy, on_edit_sell,
)

def main_menu_window():
    return Window(
        Const("📊 <b>OKX 交易控制台</b>\n\n选择策略模式:"),
        Column(
            Button(Const("📈 网格模式"), id="to_grid", on_click=on_switch_to_grid),
            Button(Const("📉 低吸高卖"), id="to_dip", on_click=on_switch_to_dip),
        ),
        state=MainSG.menu,
    )

def grid_panel_window():
    return Window(
        Format(
            "📊 <b>网格模式</b>\n"
            "──────────────────\n"
            "交易对: {inst_id}\n"
            "状态: {status}\n"
            "价格区间: {minPx} — {maxPx}\n"
            "网格数: {gridNum}\n"
            "当前价: {lastPx}"
        ),
        Column(
            Button(Const("▶️ 启动网格"), id="grid_start", on_click=on_start_grid),
            Button(Const("🛑 停止并撤销"), id="grid_stop", on_click=on_stop_grid),
            SwitchTo(Const("✏️ 修改区间"), id="grid_edit_range", state=GridSG.edit_range),
            SwitchTo(Const("🔢 修改网格数"), id="grid_edit_num", state=GridSG.edit_grid_num),
            Back(Const("🔙 返回主菜单")),
        ),
        state=GridSG.panel,
        getter=_grid_getter,
    )

def dip_panel_window():
    return Window(
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
            SwitchTo(Const("✏️ 修改买入线"), id="dip_edit_buy", state=DipSG.edit_buy_pct),
            SwitchTo(Const("✏️ 修改卖出线"), id="dip_edit_sell", state=DipSG.edit_sell_pct),
            Back(Const("🔙 返回主菜单")),
        ),
        state=DipSG.panel,
        getter=_dip_getter,
    )