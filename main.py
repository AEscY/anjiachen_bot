import asyncio
import logging
import os
from datetime import datetime

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, MenuButtonCommands, BotCommand
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram_dialog import setup_dialogs, DialogManager, StartMode
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

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

# ==================== Webhook 配置 ====================
# Render 环境变量中务必设置 WEBHOOK_URL，例如 https://anjiachen-bot.onrender.com
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")
WEBHOOK_PATH = "/webhook"
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")  # 可选，但建议设置

# Render 要求服务监听 0.0.0.0，端口用 Render 分配的 PORT 环境变量
WEB_SERVER_HOST = "0.0.0.0"
WEB_SERVER_PORT = int(os.environ.get("PORT", 10000))

bot = Bot(token=TG_BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())


# ==================== 行情 / 订单回调 ====================
async def on_ticker(inst_id: str, price: float, raw: dict):
    _last_price[inst_id] = price
    if dlg.MANAGER:
        await dlg.MANAGER.on_ticker(inst_id, price, raw)


async def on_order_update(order: dict):
    await alert(f"订单更新: {order.get('instId')} {order.get('side')} {order.get('state')} @ {order.get('px')}")


# ==================== 命令处理 ====================
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


# ==================== 菜单按钮 / 命令 ====================
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


# ==================== 健康检查 ====================
async def health(request):
    return web.Response(text="OK")


# ==================== 启动入口 ====================
async def main():
    # 1. 初始化 Manager
    manager = StrategyManager()
    dlg.MANAGER = manager

    for iid in WATCHLIST:
        ok, text = await manager.add_inst(iid)
        logger.info(text)

    # 2. 注册 dialogs
    for dialog in get_dialogs():
        dp.include_router(dialog)
    setup_dialogs(dp)

    # 3. 菜单
    await setup_menu()

    # 4. 恢复状态
    try:
        await manager.restore_all()
    except Exception as e:
        await alert(f"状态恢复失败: {e}")

    # 5. WebSocket
    pub_ws = PublicWS(on_ticker)
    priv_ws = PrivateWS(on_order_update)
    asyncio.create_task(pub_ws.connect(manager.all_inst_ids()))
    asyncio.create_task(priv_ws.connect())
    dlg.PUB_WS = pub_ws

    # 6. 创建 aiohttp 应用
    app = web.Application()

    # 注册健康检查端点（Render 需要）
    app.router.add_get("/", health)
    app.router.add_get("/health", health)

    # 注册 Webhook 处理器
    webhook_handler = SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        secret_token=WEBHOOK_SECRET or None,
    )
    webhook_handler.register(app, path=WEBHOOK_PATH)

    # 设置 dispatcher 生命周期钩子
    setup_application(app, dp, bot=bot)

    # 7. 设置 Webhook URL
    if WEBHOOK_URL:
        webhook_full_url = f"{WEBHOOK_URL.rstrip('/')}{WEBHOOK_PATH}"
        await bot.set_webhook(
            url=webhook_full_url,
            secret_token=WEBHOOK_SECRET or None,
            drop_pending_updates=True,
        )
        logger.info(f"Webhook 已设置: {webhook_full_url}")
    else:
        logger.error("WEBHOOK_URL 未设置，Telegram 将无法推送更新！")
        await alert("⚠️ WEBHOOK_URL 未配置，机器人将无法接收消息")

    # 8. 部署提示
    commit = os.environ.get("RENDER_GIT_COMMIT", "unknown")[:8]
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    await alert(
        f"部署完成 (Webhook 模式)\n"
        f"Commit: {commit}\n"
        f"时间: {now}\n"
        f"币种: {', '.join(manager.all_inst_ids())}"
    )

    # 9. 启动 Web 服务器（不再使用 dp.start_polling）
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, WEB_SERVER_HOST, WEB_SERVER_PORT)
    await site.start()
    logger.info(f"Web 服务器已启动: http://{WEB_SERVER_HOST}:{WEB_SERVER_PORT}")

    # 10. 保持运行
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        pass
    finally:
        await pub_ws.close()
        await priv_ws.close()
        await bot.session.close()
        await runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass