# main.py
import asyncio, logging
from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart
from aiogram_dialog import setup_dialogs
from aiogram.fsm.storage.memory import MemoryStorage

from config import TG_BOT_TOKEN, TG_ALLOWED_IDS, DEFAULT_INST_ID
from ui.dialogs import setup_dialogs as setup_ui_dialogs
from ui import dialogs
from state import StateStore
from okx_client.ws_public import PublicWS
from okx_client.ws_private import PrivateWS
from strategies.grid import GridStrategy
from strategies.dip_sell import DipSellStrategy
from notifier import alert
import ui.dialogs as dlg

logging.basicConfig(level=logging.INFO)

bot = Bot(TG_BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
store = StateStore()

# 全局行情缓存，供 UI getter 读取
from ui.dialogs import _last_price

async def on_ticker(price: float, raw: dict):
    _last_price[DEFAULT_INST_ID] = price
    if dlg.GRID:
        await dlg.GRID.on_ticker(price, raw)
    if dlg.DIP:
        await dlg.DIP.on_ticker(price, raw)

async def on_order_update(order: dict):
    await alert(f"📦 订单更新\n{order.get('instId')} {order.get('side')} "
                f"{order.get('state')} @ {order.get('px')}")

@dp.message(CommandStart())
async def start_cmd(msg: Message):
    if msg.from_user.id not in TG_ALLOWED_IDS:
        await msg.answer("⛔ 无权访问")
        return
    # 启动主菜单 Dialog
    from aiogram_dialog import StartMode
    from ui.states import MainSG
    await dialog_manager.start(MainSG.menu, mode=StartMode.RESET_STACK)

async def main():
    # 初始化策略
    dlg.GRID = GridStrategy(DEFAULT_INST_ID)
    dlg.DIP  = DipSellStrategy(DEFAULT_INST_ID)

    # 注册 Dialogs
    dp.include_router(setup_ui_dialogs())
   ()
 dialog_manager = setup_dialogs(dp)

    # 恢复状态
    saved = await store.load    if saved.get("grid"):
        dlg.GRID.params.update(saved["grid"])
    if saved.get("dip"):
        dlg.DIP.params.update(saved["dip"])

    # 启动 WebSocket
    pub = PublicWS(on_ticker)
    priv = PrivateWS(on_order_update)
    asyncio.create_task(pub.connect(DEFAULT_INST_ID))
    asyncio.create_task(priv.connect())

    # 定期持久化
    async def persist_loop():
        while True:
            await asyncio.sleep(300)
            await store.save({
                "grid": dlg.GRID.snapshot() if dlg.GRID else {},
                "dip": dlg.DIP.snapshot() if dlg.DIP else {},
            })

    asyncio.create_task(persist_loop())
    await alert("✅ OKX Trader 已启动")

    await dp.start_polling(bot)

if __name__ == "__main__":
    from aiogram.types import Message
    asyncio.run(main())