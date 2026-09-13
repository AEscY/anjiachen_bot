# main.py
import asyncio
import logging
import os
from datetime import datetime

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram_dialog import setup_dialogs, DialogManager, StartMode

from config import TG_BOT_TOKEN, TG_ALLOWED_IDS, DEFAULT_INST_ID
from ui.dialogs import get_dialogs, _last_price
import ui.dialogs as dlg
from ui.states import MainSG
from okx_client.ws_public import PublicWS
from okx_client.ws_private import PrivateWS
from okx_client.rest import OKXRest
from strategies.grid import GridStrategy
from strategies.dip_sell import DipSellStrategy
from notifier import alert

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=TG_BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())


# ==================== 行情 / 订单回调 ====================
async def on_ticker(price: float, raw: dict):
    _last_price[DEFAULT_INST_ID] = price
    if dlg.GRID:
        await dlg.GRID.on_ticker(price, raw)
    if dlg.DIP:
        await dlg.DIP.on_ticker(price, raw)


async def on_order_update(order: dict):
    await alert(f"📦 订单更新\n{order.get('instId')} {order.get('side')} "
                f"{order.get('state')} @ {order.get('px')}")


# ==================== 从 OKX 恢复状态 ====================
async def restore_from_okx():
    rest = OKXRest()

    try:
        grids_resp = rest.get_pending_grids(DEFAULT_INST_ID)
        if grids_resp.get("code") == "0" and grids_resp.get("data"):
            latest = grids_resp["data"][0]
            dlg.GRID.algo_id = latest["algoId"]
            dlg.GRID.params.update({
                "minPx": float(latest.get("minPx", 0)),
                "maxPx": float(latest.get("maxPx", 0)),
                "gridNum": int(latest.get("gridNum", 0)),
            })
            dlg.GRID.running = True
            await alert(f"✅ 已从 OKX 恢复网格策略: {latest['algoId']}")
    except Exception as e:
        await alert(f"⚠️ 网格状态恢复失败: {e}")

    try:
        bal_resp = rest.get_balance("USDT")
        if bal_resp.get("code") == "0" and bal_resp.get("data"):
            details = bal_resp["data"][0].get("details", [])
            base_ccy = DEFAULT_INST_ID.split("-")[0]
            for d in details:
                if d.get("ccy") == base_ccy:
                    pos = float(d.get("eq", 0))
                    if pos > 0:
                        dlg.DIP.position = pos
                        dlg.DIP.cost = float(d.get("eqUsd", 0))
                        dlg.DIP.running = True
                        await alert(f"✅ 已恢复 {base_ccy} 持仓: {pos}")
    except Exception as e:
        await alert(f"⚠️ 持仓恢复失败: {e}")


# ==================== 命令处理 ====================
def _allowed(user_id: int) -> bool:
    return (not TG_ALLOWED_IDS) or (user_id in TG_ALLOWED_IDS)


@dp.message(CommandStart())
async def cmd_start(msg: Message, dialog_manager: DialogManager):
    if not _allowed(msg.from_user.id):
        await msg.answer("⛔ 无权访问")
        return
    await dialog_manager.start(MainSG.menu, mode=StartMode.RESET_STACK)


@dp.message(Command("menu"))
async def cmd_menu(msg: Message, dialog_manager: DialogManager):
    if not _allowed(msg.from_user.id):
        await msg.answer("⛔ 无权访问")
        return
    await dialog_manager.start(MainSG.menu, mode=StartMode.RESET_STACK)


@dp.message(Command("status"))
async def cmd_status(msg: Message):
    if not _allowed(msg.from_user.id):
        await msg.answer("⛔ 无权访问")
        return
    grid = dlg.GRID.snapshot() if dlg.GRID else {}
    dip = dlg.DIP.snapshot() if dlg.DIP else {}
    text = (
        f"📊 <b>当前状态</b>\n"
        f"网格: {'🟢 运行中' if grid.get('running') else '🔴 已停止'}\n"
        f"网格 AlgoID: {grid.get('algo_id') or '-'}\n"
        f"低吸高卖: {'🟢 监控中' if dip.get('running') else '⏸ 已暂停'}\n"
        f"持仓: {dip.get('position', 0):.6f}"
    )
    await msg.answer(text, parse_mode="HTML")


# ==================== Render 健康检查 ====================
async def health(request):
    return web.Response(text="OK")


async def start_health_server():
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 10000)
    await site.start()
    logger.info("健康检查服务已启动，端口 10000")


# ==================== 启动入口 ====================
async def main():
    # 初始化策略
    dlg.GRID = GridStrategy(DEFAULT_INST_ID)
    dlg.DIP = DipSellStrategy(DEFAULT_INST_ID)

    # 注册所有 Dialog（Dialog 本身就是 Router）
    for dialog in get_dialogs():
        dp.include_router(dialog)

    # 初始化 aiogram_dialog 中间件与核心 handler（不需要赋值）
    setup_dialogs(dp)

    # 从 OKX 恢复状态
    try:
        await restore_from_okx()
    except Exception as e:
        await alert(f"⚠️ OKX 状态恢复失败: {e}")

    # 启动 WebSocket
    pub = PublicWS(on_ticker)
    priv = PrivateWS(on_order_update)
    asyncio.create_task(pub.connect(DEFAULT_INST_ID))
    asyncio.create_task(priv.connect())

    # 启动健康检查服务（应对 Render 端口扫描）
    asyncio.create_task(start_health_server())

    # 部署完成提示
    commit = os.environ.get("RENDER_GIT_COMMIT", "unknown")[:8]
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    await alert(
        f"🚀 <b>部署完成</b>\n"
        f"Commit: <code>{commit}</code>\n"
        f"时间: {now}\n"
        f"机器人已启动，监听中…"
    )

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())