import logging
import asyncio
from aiogram.types import CallbackQuery, Message
from aiogram_dialog import Dialog, DialogManager, Window
from aiogram_dialog.widgets.input import MessageInput
from aiogram_dialog.widgets.kbd import Button, Back, Column, Select, ScrollingGroup
from aiogram_dialog.widgets.text import Const, Format

from ui.states import MainSG, CoinSG
from strategies.manager import StrategyManager
from okx_client.rest import OKXRest

logger = logging.getLogger(__name__)

MANAGER: "StrategyManager | None" = None
PUB_WS = None
_last_price: dict = {}


# ==================== 解析工具 ====================
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


def _parse_range(text: str):
    parts = text.replace(",", " ").split()
    if len(parts) != 2:
        return None, None, "格式错误，请输入：最低价 最高价，例如 55000 60000"
    try:
        return float(parts[0]), float(parts[1]), None
    except ValueError:
        return None, None, "价格必须为有效数字"


async def _handle_expired(cb: CallbackQuery, manager: DialogManager):
    """通用会话过期处理：提示并返回主菜单"""
    await cb.answer("页面已过期，请重新选择币种", show_alert=True)
    await manager.start(MainSG.menu)


# ==================== 主菜单 ====================
async def on_coin_selected(cb: CallbackQuery, widget, manager: DialogManager, item_id: str):
    manager.dialog_data["inst_id"] = item_id
    await manager.start(CoinSG.panel)


async def on_add_coin_click(cb: CallbackQuery, button, manager: DialogManager):
    await manager.switch_to(MainSG.add_coin)


async def on_add_coin_input(msg: Message, widget, manager: DialogManager):
    if not MANAGER:
        return
    text = (msg.text or "").strip()
    # 关键修复：如果用户输入的是命令（以 / 开头），不拦截，交给命令处理器
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
    await manager.start(MainSG.menu)


async def main_getter(dialog_manager: DialogManager, **kwargs):
    if not MANAGER:
        return {"coins": [], "coins_count": 0}
    coins = [{"id": iid, "name": iid, "price": _last_price.get(iid, "-")} for iid in MANAGER.all_inst_ids()]
    return {"coins": coins, "coins_count": len(coins)}


main_menu_window = Window(
    Format("OKX 多币种控制台\n\n当前监控 {coins_count} 个币种："),
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


async def on_enter_grid(cb: CallbackQuery, button, manager: DialogManager):
    inst_id = manager.dialog_data.get("inst_id")
    if not inst_id:
        await _handle_expired(cb, manager)
        return
    await manager.switch_to(CoinSG.grid)


async def on_enter_dip(cb: CallbackQuery, button, manager: DialogManager):
    inst_id = manager.dialog_data.get("inst_id")
    if not inst_id:
        await _handle_expired(cb, manager)
        return
    await manager.switch_to(CoinSG.dip)


async def on_delete_coin(cb: CallbackQuery, button, manager: DialogManager):
    inst_id = manager.dialog_data.get("inst_id")

    if not inst_id:
        if MANAGER:
            current_coins = MANAGER.all_inst_ids()
            if len(current_coins) == 1:
                inst_id = current_coins[0]
                logger.info(f"会话过期，自动锁定唯一币种 {inst_id} 执行删除")
            else:
                await cb.answer("页面已过期，请重新点击菜单选择要删除的币种", show_alert=True)
                await manager.start(MainSG.menu)
                return
        else:
            await cb.answer("策略管理器未初始化", show_alert=True)
            return

    ok, text = await MANAGER.remove_inst(inst_id)
    if ok and PUB_WS:
        await PUB_WS.unsubscribe(inst_id)
    await cb.answer(text, show_alert=True)
    await manager.start(MainSG.menu)


async def on_query_balance(cb: CallbackQuery, button, manager: DialogManager):
    try:
        rest = OKXRest()
        resp = await asyncio.to_thread(rest.get_balance, "USDT")
        if resp.get("code") != "0" or not resp.get("data"):
            await cb.answer("查询失败", show_alert=True)
            return
        details = resp["data"][0].get("details", [])
        lines = []
        for d in details:
            try:
                eq = float(d.get("eq", 0))
                avail = float(d.get("availBal", 0))
            except (ValueError, TypeError):
                continue
            if eq > 0:
                lines.append(f"{d.get('ccy')}: {eq:.4f} (可用 {avail:.4f})")
        text = "\n".join(lines) if lines else "账户无资产"
        await cb.answer(text, show_alert=True)
    except Exception as e:
        await cb.answer(f"查询失败: {e}", show_alert=True)


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
        Button(Const("查询余额"), id="query_balance", on_click=on_query_balance),
        Button(Const("删除此币种"), id="del_coin", on_click=on_delete_coin),
        Button(Const("返回主菜单"), id="back_main", on_click=lambda c, b, m: m.start(MainSG.menu)),
    ),
    state=CoinSG.panel,
    getter=coin_getter,
)


# ==================== 网格面板 ====================
async def grid_getter(dialog_manager: DialogManager, **kwargs):
    inst_id = dialog_manager.dialog_data.get("inst_id", "-")
    grid = MANAGER.grids.get(inst_id) if MANAGER else None
    if not grid:
        return {"inst_id": inst_id, "status": "未初始化", "minPx": "-", "maxPx": "-",
                "gridNum": "-", "lastPx": "-", "algo_id": "-"}
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
        await _handle_expired(cb, manager)
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
    if not grid:
        await _handle_expired(cb, manager)
        return
    if not grid.running:
        await cb.answer("未在运行", show_alert=True)
        return
    try:
        await grid.stop()
        await cb.answer("已停止")
    except Exception as e:
        await cb.answer(f"停止失败: {e}", show_alert=True)
    await manager.update()


async def on_edit_grid_range(cb: CallbackQuery, button, manager: DialogManager):
    inst_id = manager.dialog_data.get("inst_id")
    if not inst_id:
        await _handle_expired(cb, manager)
        return
    await manager.switch_to(CoinSG.grid_edit_range)


async def on_edit_grid_num(cb: CallbackQuery, button, manager: DialogManager):
    inst_id = manager.dialog_data.get("inst_id")
    if not inst_id:
        await _handle_expired(cb, manager)
        return
    await manager.switch_to(CoinSG.grid_edit_num)


async def on_grid_range_input(msg: Message, widget, manager: DialogManager):
    min_px, max_px, err = _parse_range(msg.text or "")
    if err:
        await msg.answer(err)
        return
    if min_px >= max_px:
        await msg.answer("最低价必须小于最高价")
        return
    inst_id = manager.dialog_data.get("inst_id")
    grid = MANAGER.grids.get(inst_id) if MANAGER else None
    if grid:
        grid.params["minPx"] = min_px
        grid.params["maxPx"] = max_px
        await msg.answer(f"区间已修改为 {min_px} - {max_px}")
    await manager.switch_to(CoinSG.grid)


async def on_grid_num_input(msg: Message, widget, manager: DialogManager):
    num, err = _parse_int(msg.text or "")
    if err:
        await msg.answer(err)
        return
    if not (2 <= num <= 200):
        await msg.answer("网格数应在 2-200 之间")
        return
    inst_id = manager.dialog_data.get("inst_id")
    grid = MANAGER.grids.get(inst_id) if MANAGER else None
    if grid:
        grid.params["gridNum"] = num
        await msg.answer(f"网格数已修改为 {num}")
    await manager.switch_to(CoinSG.grid)


grid_panel_window = Window(
    Format(
        "{inst_id} 网格\n"
        "状态: {status}\n"
        "价格区间: {minPx} - {maxPx}\n"
        "网格数: {gridNum}\n"
        "当前价: {lastPx}\n"
        "Algo ID: {algo_id}"
    ),
    Column(
        Button(Const("启动网格"), id="grid_start", on_click=on_start_grid),
        Button(Const("停止并撤销"), id="grid_stop", on_click=on_stop_grid),
        Button(Const("修改区间"), id="grid_edit_range", on_click=on_edit_grid_range),
        Button(Const("修改网格数"), id="grid_edit_num", on_click=on_edit_grid_num),
        Back(Const("返回币种面板")),
    ),
    state=CoinSG.grid,
    getter=grid_getter,
)

grid_edit_range_window = Window(
    Const("修改价格区间\n\n请输入最低价和最高价，用空格或逗号分隔。\n例如：55000 60000"),
    MessageInput(on_grid_range_input),
    Button(Const("取消"), id="cancel", on_click=lambda c, b, m: m.switch_to(CoinSG.grid)),
    state=CoinSG.grid_edit_range,
)

grid_edit_num_window = Window(
    Const("修改网格数量\n\n请输入整数，建议 10-100："),
    MessageInput(on_grid_num_input),
    Button(Const("取消"), id="cancel", on_click=lambda c, b, m: m.switch_to(CoinSG.grid)),
    state=CoinSG.grid_edit_num,
)


# ==================== 低吸高卖面板 ====================
async def dip_getter(dialog_manager: DialogManager, **kwargs):
    inst_id = dialog_manager.dialog_data.get("inst_id", "-")
    dip = MANAGER.dips.get(inst_id) if MANAGER else None
    if not dip:
        return {"inst_id": inst_id, "status": "未初始化", "base_px": "-",
                "buy_px": "-", "sell_px": "-", "lastPx": "-", "position": "-",
                "profit": "0", "fee": "0"}
    s = dip.snapshot()
    return {
        "inst_id": inst_id,
        "status": "监控中" if s["running"] else "已暂停",
        "base_px": s.get("base_px", "-"),
        "buy_px": round(s.get("buy_px", 0), 6),
        "sell_px": round(s.get("sell_px", 0), 6),
        "lastPx": _last_price.get(inst_id, "-"),
        "position": f"{s.get('position', 0):.6f}",
        "profit": f"{s.get('total_profit', 0):.4f}",
        "fee": f"{s.get('total_fee', 0):.4f}",
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


async def on_edit_dip_buy(cb: CallbackQuery, button, manager: DialogManager):
    inst_id = manager.dialog_data.get("inst_id")
    if not inst_id:
        await _handle_expired(cb, manager)
        return
    await manager.switch_to(CoinSG.dip_edit_buy)


async def on_edit_dip_sell(cb: CallbackQuery, button, manager: DialogManager):
    inst_id = manager.dialog_data.get("inst_id")
    if not inst_id:
        await _handle_expired(cb, manager)
        return
    await manager.switch_to(CoinSG.dip_edit_sell)


async def on_edit_dip_base(cb: CallbackQuery, button, manager: DialogManager):
    inst_id = manager.dialog_data.get("inst_id")
    if not inst_id:
        await _handle_expired(cb, manager)
        return
    await manager.switch_to(CoinSG.dip_edit_base)


async def on_dip_buy_input(msg: Message, widget, manager: DialogManager):
    px, err = _parse_float(msg.text or "")
    if err:
        await msg.answer(err)
        return
    inst_id = manager.dialog_data.get("inst_id")
    dip = MANAGER.dips.get(inst_id) if MANAGER else None
    if dip:
        dip.buy_px = px
        if dip.base_px > 0:
            dip.params["buyPct"] = round(px / dip.base_px, 6)
        await msg.answer(f"买入线已修改为 {px}")
    await manager.switch_to(CoinSG.dip)


async def on_dip_sell_input(msg: Message, widget, manager: DialogManager):
    px, err = _parse_float(msg.text or "")
    if err:
        await msg.answer(err)
        return
    inst_id = manager.dialog_data.get("inst_id")
    dip = MANAGER.dips.get(inst_id) if MANAGER else None
    if dip:
        dip.sell_px = px
        if dip.base_px > 0:
            dip.params["sellPct"] = round(px / dip.base_px, 6)
        await msg.answer(f"卖出线已修改为 {px}")
    await manager.switch_to(CoinSG.dip)


async def on_dip_base_input(msg: Message, widget, manager: DialogManager):
    px, err = _parse_float(msg.text or "")
    if err:
        await msg.answer(err)
        return
    inst_id = manager.dialog_data.get("inst_id")
    dip = MANAGER.dips.get(inst_id) if MANAGER else None
    if dip:
        dip.base_px = px
        dip.buy_px = px * dip.params["buyPct"]
        dip.sell_px = px * dip.params["sellPct"]
        await msg.answer(f"基准价已修改为 {px}")
    await manager.switch_to(CoinSG.dip)


dip_panel_window = Window(
    Format(
        "{inst_id} 低吸高卖\n"
        "状态: {status}\n"
        "基准价: {base_px}\n"
        "买入线: <= {buy_px}\n"
        "卖出线: >= {sell_px}\n"
        "当前价: {lastPx}\n"
        "持仓: {position}\n"
        "已实现盈亏: {profit} USDT\n"
        "累计手续费: {fee} USDT"
    ),
    Column(
        Button(Const("启动监控"), id="dip_start", on_click=on_start_dip),
        Button(Const("暂停"), id="dip_stop", on_click=on_stop_dip),
        Button(Const("修改买入线"), id="dip_edit_buy", on_click=on_edit_dip_buy),
        Button(Const("修改卖出线"), id="dip_edit_sell", on_click=on_edit_dip_sell),
        Button(Const("修改基准价"), id="dip_edit_base", on_click=on_edit_dip_base),
        Back(Const("返回币种面板")),
    ),
    state=CoinSG.dip,
    getter=dip_getter,
)

dip_edit_buy_window = Window(
    Const("修改买入线\n\n请输入绝对价格，例如 58000："),
    MessageInput(on_dip_buy_input),
    Button(Const("取消"), id="cancel", on_click=lambda c, b, m: m.switch_to(CoinSG.dip)),
    state=CoinSG.dip_edit_buy,
)

dip_edit_sell_window = Window(
    Const("修改卖出线\n\n请输入绝对价格，例如 62000："),
    MessageInput(on_dip_sell_input),
    Button(Const("取消"), id="cancel", on_click=lambda c, b, m: m.switch_to(CoinSG.dip)),
    state=CoinSG.dip_edit_sell,
)

dip_edit_base_window = Window(
    Const("修改基准价\n\n请输入新的基准价，例如 60000："),
    MessageInput(on_dip_base_input),
    Button(Const("取消"), id="cancel", on_click=lambda c, b, m: m.switch_to(CoinSG.dip)),
    state=CoinSG.dip_edit_base,
)


# ==================== Dialog 组装 ====================
main_dialog = Dialog(main_menu_window, add_coin_window)
coin_dialog = Dialog(
    coin_panel_window,
    grid_panel_window,
    grid_edit_range_window,
    grid_edit_num_window,
    dip_panel_window,
    dip_edit_buy_window,
    dip_edit_sell_window,
    dip_edit_base_window,
)


def get_dialogs() -> list:
    return [main_dialog, coin_dialog]