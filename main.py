import asyncio
import logging
import os
import signal
from datetime import datetime

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramConflictError
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, MenuButtonCommands, BotCommand
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram_dialog import setup_dialogs, DialogManager, StartMode

from config import TG_BOT_TOKEN, TG_ALLOWED_IDS, WATCHLIST
from ui.dialogs import get_dialogs, _last_price
import ui.dialogs as dlg
from ui.states import MainSG
from okx_client.ws_public import PublicWS
from okx_client.ws_private import PrivateWS
from strategies.manager import StrategyManager
from notifier import alert

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=TG_BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# 关闭信号标记
_shutdown = asyncio.Event()


async def on_ticker(inst_id: str, price: float, raw: dict):
    _last_price[inst_id] = price
    if dlg.MANAGER:
        await dlg.MANAGER.on_ticker(inst_id, price, raw)


async def on_order_update(order: dict):
    await alert(f"订单更新: {order.get('instId')} {order.get('side')} {order.get('state')} @ {order.get('px')}")


def _allowed(user_id: int) -> bool:
    return (not TG_ALLOWED_IDS) or (user_id in TG_ALLOWED_IDS)


@dp.message(CommandStart())
async def cmd_start(msg: Message, dialog_manager: DialogManager):
    if not _allowed(msg.from_user.id):
        await msg.answer("无权访问")
        return
    await dialog_manager.start(MainSG.menu, mode=StartMode.RESET_STACK)


@dp.message(Command("menu"))
async def cmd_menu(msg: Message, dialog_manager: DialogManager):
    if not _allowed(msg.from_user.id):
        await msg.answer("无权访问")
        return
    await dialog_manager.start(MainSG.menu, mode=StartMode.RESET_STACK)


@dp.message(Command("list"))
async def cmd_list(msg: Message):
    if not _allowed(msg.from_user.id):
        await msg.answer("无权访问")
        return
    if not dlg.MANAGER:
        await msg.answer("未初始化")
        return
    lines = [f"{iid} {_last_price.get(iid, '-')}" for iid in dlg.MANAGER.all_inst_ids()]
    await msg.answer("监控币种:\n" + "\n".join(lines))


@dp.message(Command("add"))
async def cmd_add(msg: Message):
    if not _allowed(msg.from_user.id):
        await msg.answer("无权访问")
        return
    parts = (msg.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await msg.answer("用法: /add BTC-USDT")
        return
    inst_id = parts[1].strip().upper()
    ok, text = await dlg.MANAGER.add_inst(inst_id)
    if ok and dlg.PUB_WS:
        await dlg.PUB_WS.subscribe(inst_id)
    await msg.answer(("成功: " if ok else "失败: ") + text)


@dp.message(Command("remove"))
async def cmd_remove(msg: Message):
    if not _allowed(msg.from_user.id):
        await msg.answer("无权访问")
        return
    parts = (msg.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await msg.answer("用法: /remove BTC-USDT")
        return
    inst_id = parts[1].strip().upper()
    ok, text = await dlg.MANAGER.remove_inst(inst_id)
    if ok and dlg.PUB_WS:
        await dlg.PUB_WS.unsubscribe(inst_id)
    await msg.answer(("成功: " if ok else "失败: ") + text)


@dp.message(Command("status"))
async def cmd_status(msg: Message):
    if not _allowed(msg.from_user.id):
        await msg.answer("无权访问")
        return
    if not dlg.MANAGER:
        await msg.answer("未初始化")
        return
    lines = []
    for iid in dlg.MANAGER.all_inst_ids():
        g = dlg.MANAGER.grids.get(iid)
        d = dlg.MANAGER.dips.get(iid)
        gs = "运行" if (g and g.running) else "停止"
        ds = "运行" if (d and d.running) else "暂停"
        lines.append(f"{iid} 网格{gs} 低吸高卖{ds} {_last_price.get(iid, '-')}")
    await msg.answer("状态总览:\n" + "\n".join(lines))


async def setup_menu():
    await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    await bot.set_my_commands([
        BotCommand(command="start", description="启动"),
        BotCommand(command="menu", description="主菜单"),
        BotCommand(command="list", description="币种列表"),
        BotCommand(command="add", description="添加币种"),
        BotCommand(command="remove", description="删除币种"),
        BotCommand(command="status", description="状态"),
    ])


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
    return runner


async def _sigterm_handler():
    """处理 Render 的 SIGTERM 信号，优雅退出"""
    loop = asyncio.get_event_loop()
    try:
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, _shutdown.set)
    except NotImplementedError:
        pass


async def main():
    # 1. 处理信号
    await _sigterm_handler()

    # 2. 先删除 webhook，避免与 polling 冲突
    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception as e:
        logger.warning(f"删除 webhook 失败: {e}")

    # 3. 初始化 Manager
    manager = StrategyManager()
    dlg.MANAGER = manager

    for iid in WATCHLIST:
        await manager.add_inst(iid)

    # 4. 注册 dialogs
    for dialog in get_dialogs():
        dp.include_router(dialog)
    setup_dialogs(dp)

    # 5. 菜单
    await setup_menu()

    # 6. 恢复状态
    try:
        await manager.restore_all()
    except Exception as e:
        await alert(f"状态恢复失败: {e}")

    # 7. WebSocket
    pub_ws = PublicWS(on_ticker)
    priv_ws = PrivateWS(on_order_update)
    asyncio.create_task(pub_ws.connect(manager.all_inst_ids()))
    asyncio.create_task(priv_ws.connect())
    dlg.PUB_WS = pub_ws

    # 8. 健康检查
    runner = await start_health_server()

    # 9. 部署提示
    commit = os.environ.get("RENDER_GIT_COMMIT", "unknown")[:8]
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    await alert(f"部署完成\nCommit: {commit}\n时间: {now}\n币种: {', '.join(manager.all_inst_ids())}")

    # 10. Polling，带 Conflict 重试
    try:
        await dp.start_polling(bot, drop_pending_updates=True)
    except TelegramConflictError as e:
        logger.error(f"轮询冲突（旧实例未退出）: {e}")
    except asyncio.CancelledError:
        pass
    finally:
        # 11. 清理
        await pub_ws.close()
        await priv_ws.close()
        await bot.session.close()
        await runner.cleanup()
        logger.info("已优雅关闭")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass