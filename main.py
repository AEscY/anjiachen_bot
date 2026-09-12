# main.py
import asyncio
import logging
from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart
from aiogram.types import Message
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram_dialog import setup_dialogs, DialogManager, StartMode

from config import (
    TG_BOT_TOKEN, TG_ALLOWED_IDS, DEFAULT_INST_ID
)
from ui.dialogs import setup_dialogs as setup_ui_dialogs, _last_price
import ui.dialogs as dlg
from ui.states import MainSG
from state import StateStore
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
store = StateStore()
dialog_manager = None  # 由 setup_dialogs 赋值


async def on_ticker(price: float, raw: dict):
    """行情推送回调"""
    _last_price[DEFAULT_INST_ID] = price
    if dlg.GRID:
        await dlg.GRID.on_ticker(price, raw)
    if dlg.DIP:
        await dlg.DIP.on_ticker(price, raw)


async def on_order_update(order: dict):
    """订单更新回调（可在此处更新策略持仓）"""
    await alert(f"📦 订单更新\n{order.get('instId')} {order.get('side')} "
                f"{order.get('state')} @ {order.get('px')}")


async def restore_from_okx():
    """从 OKX API 恢复策略状态"""
    rest = OKXRest()

    # 1. 恢复网格策略
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

    # 2. 恢复低吸高卖策略（从现货余额解析持仓）
    try:
        bal_resp = rest.get_balance("USDT")
        if bal_resp.get("code") == "0" and bal_resp.get("data"):
            details = bal_resp["data"][0].get("details", [])
            base_ccy = DEFAULT_INST_ID.split("-")[0]  # 例如 BTC
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


async def persist_loop():
    """定期备份状态到 GitHub（30 分钟一次）"""
    while True:
        await asyncio.sleep(1800)
        try:
            await store.save({
                "grid": dlg.GRID.snapshot() if dlg.GRID else {},
                "dip": dlg.DIP.snapshot() if dlg.DIP else {},
            })
        except Exception as e:
            logger.error(f"持久化失败: {e}")


@dp.message(CommandStart())
async def start_cmd(msg: Message, dialog_manager: DialogManager):
    # 如果配置了 TG_ALLOWED_IDS，则进行鉴权；未配置时允许所有用户（方便调试）
    if TG_ALLOWED_IDS and msg.from_user.id not in TG_ALLOWED_IDS:
        await msg.answer("⛔ 无权访问")
        return
    await dialog_manager.start(MainSG.menu, mode=StartMode.RESET_STACK)


async def main():
    global dialog_manager

    # 初始化策略实例
    dlg.GRID = GridStrategy(DEFAULT_INST_ID)
    dlg.DIP = DipSellStrategy(DEFAULT_INST_ID)

    # 注册 UI dialogs 并获取 dialog_manager
    dp.include_router(setup_ui_dialogs())
    dialog_manager = setup_dialogs(dp)

    # 优先从 OKX 恢复状态，失败则回退到 state.json
    try:
        await restore_from_okx()
    except Exception as e:
        await alert(f"⚠️ OKX 状态恢复失败，尝试本地备份: {e}")
        saved = await store.load()
        if saved.get("grid"):
            dlg.GRID.params.update(saved["grid"])
        if saved.get("dip"):
            dlg.DIP.params.update(saved["dip"])

    # 启动 WebSocket（公共行情 + 私有订单）
    pub = PublicWS(on_ticker)
    priv = PrivateWS(on_order_update)
    asyncio.create_task(pub.connect(DEFAULT_INST_ID))
    asyncio.create_task(priv.connect())

    # 启动定期持久化备份
    asyncio.create_task(persist_loop())

    await alert("✅ OKX Trader 已启动")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())