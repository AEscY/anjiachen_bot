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
from ui.states import AppSG
from okx_client.ws_public import PublicWS
from okx_client.ws_private import PrivateWS
from okx_client.rest import OKXRest
from strategies.manager import StrategyManager
from notifier import alert

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")
WEBHOOK_PATH = "/webhook"
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
WEB_SERVER_HOST = "0.0.0.0"
WEB_SERVER_PORT = int(os.environ.get("PORT", 10000))

bot = Bot(token=TG_BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

VALID_BARS = ["1m", "3m", "5m", "15m", "30m", "1H", "2H", "4H", "6H", "12H", "1D"]


async def on_ticker(inst_id: str, price: float, raw: dict):
    _last_price[inst_id] = price
    if dlg.MANAGER:
        await dlg.MANAGER.on_ticker(inst_id, price, raw)


async def on_order_update(order: dict):
    await alert(f"订单更新: {order.get('instId')} {order.get('side')} {order.get('state')} @ {order.get('px')}")


def _allowed(user_id: int) -> bool:
    return (not TG_ALLOWED_IDS) or (user_id in TG_ALLOWED_IDS)


# ==================== 命令处理 ====================
@dp.message(CommandStart())
async def cmd_start(msg: Message, dialog_manager: DialogManager):
    if not _allowed(msg.from_user.id):
        await msg.answer("无权访问")
        return
    await dialog_manager.start(AppSG.menu, mode=StartMode.RESET_STACK)


@dp.message(Command("menu"))
async def cmd_menu(msg: Message, dialog_manager: DialogManager):
    if not _allowed(msg.from_user.id):
        await msg.answer("无权访问")
        return
    await dialog_manager.start(AppSG.menu, mode=StartMode.RESET_STACK)


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
    if not dlg.MANAGER:
        await msg.answer("未初始化")
        return
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
    if not dlg.MANAGER:
        await msg.answer("未初始化")
        return
    ok, text = await dlg.MANAGER.remove_inst(inst_id)
    if ok and dlg.P dlg.PUB_WS.unsubscribe(inst_id)
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
        bar = d.params.get("bar", "15m") if d else "-"
        lines.append(f"{iid} 网格{gs} 低吸{ds} [{bar}] {_last_price.get(iid, '-')}")
    await msg.answer("状态总览:\n" + "\n".join(lines))


@dp.message(Command("bar"))
async def cmd_bar(msg: Message):
    if not _allowed(msg.from_user.id):
        await msg.answer("无权访问")
        return
    parts = (msg.text or "").split()
    if len(parts) < 3:
        await msg.answer(
            "用法: /bar <币种> <周期>\n"
            f"可用周期: {', '.join(VALID_BARS)}\n"
            "示例: /bar BTC-USDT 15m"
        )
        return
    inst_id = parts[1].upper()
    bar = parts[2]
    if bar not in VALID_BARS:
        await msg.answer(f"周期 {bar} 不支持。可用: {', '.join(VALID_BARS)}")
        return
    dip = dlg.MANAGER.dips.get(inst_id) if dlg.MANAGER else None
    if not dip:
        await msg.answer(f"{inst_id} 不在监控中")
        return
    await dip.set_param("bar", bar)
    await msg.answer(f"{inst_id} K线周期已改为 {bar}，下次取K线生效。")


@dp.message(Command("signal_params"))
async def cmd_signal_params(msg: Message):
    if not _allowed(msg.from_user.id):
        await msg.answer("无权访问")
        return
    if not dlg.MANAGER:
        await msg.answer("未初始化")
        return
    lines = ["当前信号参数"]
    for iid in dlg.MANAGER.all_inst_ids():
        dip = dlg.MANAGER.dips.get(iid)
        if not dip:
            continue
        p = dip.params
        lines.append(
            f"\n{iid}\n"
            f"  K线周期: {p.get('bar', '15m')}\n"
            f"  RSI: 周期{p.get('rsi_period', 14)} 超卖<{p.get('rsi_oversold', 30)} 超买>{p.get('rsi_overbought', 70)}\n"
            f"  BB: 周期{p.get('bb_period', 20)} 标准差{p.get('bb_std', 2.0)}\n"
            f"  MACD: {p.get('macd_fast', 12)}/{p.get('macd_slow', 26)}/{p.get('macd_signal', 9)}\n"
            f"  EMA趋势: {p.get('ema_period', 200)}\n"
            f"  成交量倍数: {p.get('vol_multiplier', 1.5)}\n"
            f"  趋势过滤: {'开' if p.get('trend_filter', True) else '关'}\n"
            f"  成交量确认: {'开' if p.get('volume_confirm', True) else '关'}"
        )
    lines.append("\n修改周期用 /bar <币种> <周期>")
    lines.append("修改其他参数可扩展，需要时告诉我。")
    await msg.answer("\n".join(lines))


@dp.message(Command("positions"))
async def cmd_positions(msg: Message):
    if not _allowed(msg.from_user.id):
        await msg.answer("无权访问")
        return
    if not dlg.MANAGER:
        await msg.answer("未初始化")
        return

    lines = ["建仓与挂单总览"]
    has_any = False

    for iid in dlg.MANAGER.all_inst_ids():
        dip = dlg.MANAGER.dips.get(iid)
        if not dip:
            continue
        s = dip.snapshot()
        pos = s.get("position", 0)
        pending_buy = s.get("pending_buy")
        pending_sell = s.get("pending_sell")
        last_action = s.get("last_action")
        profit = s.get("total_profit", 0)
        fee = s.get("total_fee", 0)
        price = _last_price.get(iid, 0)

        if pos <= 0 and not pending_buy and not pending_sell and not last_action:
            continue

        has_any = True
        lines.append(f"\n{iid}  [{s.get('bar', '15m')}]")
        lines.append(f"  当前价: {price}")
        lines.append(f"  持仓: {pos:.6f}")
        if s.get("avg_buy_price", 0) > 0:
            lines.append(f"  平均买入价: {s['avg_buy_price']:.6f}")
        if s.get("peak_price", 0) > 0:
            lines.append(f"  持仓最高价: {s['peak_price']:.6f}")
        if pending_buy:
            lines.append(f"  ⏳ 挂单买入: {pending_buy}")
        if pending_sell:
            lines.append(f"  ⏳ 挂单卖出: {pending_sell}")
        if last_action:
            lines.append(f"  最近动作: {last_action[0]} @ {last_action[1]:.2f}")
        lines.append(f"  已实现盈亏: {profit:.4f} USDT")
        lines.append(f"  累计手续费: {fee:.4f} USDT")

    if not has_any:
        lines.append("\n暂无任何建仓、挂单或交易记录。")
        lines.append("发送 /signals 查看当前信号状态。")

    await msg.answer("\n".join(lines))


@dp.message(Command("signals"))
async def cmd_signals(msg: Message):
    if not _allowed(msg.from_user.id):
        await msg.answer("无权访问")
        return
    if not dlg.MANAGER:
        await msg.answer("未初始化")
        return

    lines = ["信号诊断"]
    for iid in dlg.MANAGER.all_inst_ids():
        dip = dlg.MANAGER.dips.get(iid)
        if not dip:
            continue
        try:
            status = await dip.get_signal_status()
        except Exception as e:
            lines.append(f"\n{iid}: 获取失败 {e}")
            continue

        if "error" in status:
            lines.append(f"\n{iid}: {status['error']}")
            continue

        rsi = status["rsi"]
        bb_lower = status["bb_lower"]
        price = status["price"]
        rsi_ok = status["rsi_ok"]
        bb_ok = status["bb_ok"]
        macd_ok = status["macd_ok"]
        bar = status.get("bar", "15m")

        lines.append(f"\n{iid}  [{bar}]")
        lines.append(f"  当前价: {price:.4f}")
        lines.append(f"  RSI(14): {rsi:.1f} {'✅超卖' if rsi_ok else '❌需<' + str(status['rsi_oversold'])}")
        lines.append(f"  BB下轨: {bb_lower:.4f}  {'✅已触及' if bb_ok else '❌未触及'}")
        lines.append(f"  MACD: {'✅金叉/转正' if macd_ok else '❌未金叉'}")
        if status["buy_ready"]:
            lines.append(f"  🎯 买入条件已满足")
        else:
            lines.append(f"  ⏳ 等待条件满足")

    lines.append("\n提示：三个条件同时满足才会触发买入。")
    lines.append("调整K线周期: /bar <币种> <周期>")
    lines.append("查看参数: /signal_params")
    await msg.answer("\n".join(lines))


@dp.message(Command("balance"))
async def cmd_balance(msg: Message):
    if not _allowed(msg.from_user.id):
        await msg.answer("无权访问")
        return
    try:
        rest = OKXRest()
        resp = await asyncio.to_thread(rest.get_balance, "USDT")
        if resp.get("code") != "0" or not resp.get("data"):
            await msg.answer("查询余额失败")
            return
        details = resp["data"][0].get("details", [])
        lines = ["账户余额"]
        for d in details:
            ccy = d.get("ccy", "")
            try:
                eq = float(d.get("eq", 0))
                avail = float(d.get("availBal", 0))
            except (ValueError, TypeError):
                continue
            if eq > 0:
                lines.append(f"{ccy}: 总额 {eq:.6f} | 可用 {avail:.6f}")
        await msg.answer("\n".join(lines) if len(lines) > 1 else "账户无资产")
    except Exception as e:
        await msg.answer(f"查询失败: {e}")


@dp.message(Command("profit"))
async def cmd_profit(msg: Message):
    if not _allowed(msg.from_user.id):
        await msg.answer("无权访问")
        return
    if not dlg.MANAGER:
        await msg.answer("未初始化")
        return
    lines = ["获利与手续费统计"]
    total_profit = 0.0
    total_fee = 0.0
    for iid in dlg.MANAGER.all_inst_ids():
        dip = dlg.MANAGER.dips.get(iid)
        if dip:
            s = dip.snapshot()
            profit = s.get("total_profit", 0)
            fee = s.get("total_fee", 0)
            total_profit += profit
            total_fee += fee
            lines.append(
                f"\n{iid}\n"
                f"  已实现盈亏: {profit:.4f} USDT\n"
                f"  手续费: {fee:.4f} USDT\n"
                f"  成交次数: {s.get('trade_count', 0)}"
            )
    net = total_profit - total_fee
    lines.append(
        f"\n合计\n"
        f"  已实现盈亏: {total_profit:.4f} USDT\n"
        f"  累计手续费: {total_fee:.4f} USDT\n"
        f"  净收益: {net:.4f} USDT"
    )
    await msg.answer("\n".join(lines))


async def setup_menu():
    await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    await bot.set_my_commands([
        BotCommand(command="start", description="启动"),
        BotCommand(command="menu", description="主菜单"),
        BotCommand(command="list", description="币种列表"),
        BotCommand(command="add", description="添加币种"),
        BotCommand(command="remove", description="删除币种"),
        BotCommand(command="status", description="状态"),
        BotCommand(command="bar", description="设置K线周期"),
        BotCommand(command="signal_params", description="信号参数"),
        BotCommand(command="positions", description="建仓与挂单"),
        BotCommand(command="signals", description="信号诊断"),
        BotCommand(command="balance", description="查询余额"),
        BotCommand(command="profit", description="获利与手续费"),
    ])


async def health(request):
    return web.Response(text="OK")


async def main():
    manager = StrategyManager()
    dlg.MANAGER = manager

    for iid in WATCHLIST:
        ok, text = await manager.add_inst(iid)
        logger.info(text)

    for dialog in get_dialogs():
        dp.include_router(dialog)
    setup_dialogs(dp)

    await setup_menu()

    try:
        await manager.restore_all()
    except Exception as e:
        await alert(f"状态恢复失败: {e}")

    pub_ws = PublicWS(on_ticker)
    priv_ws = PrivateWS(on_order_update)
    asyncio.create_task(pub_ws.connect(manager.all_inst_ids()))
    asyncio.create_task(priv_ws.connect())
    dlg.PUB_WS = pub_ws

    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)

    webhook_handler = SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        secret_token=WEBHOOK_SECRET or None,
    )
    webhook_handler.register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)

    if WEBHOOK_URL:
        webhook_full_url = f"{WEBHOOK_URL.rstrip('/')}{WEBHOOK_PATH}"
        await bot.set_webhook(
            url=webhook_full_url,
            secret_token=WEBHOOK_SECRET or None,
            drop_pending_updates=True,
        )
        logger.info(f"Webhook 已设置: {webhook_full_url}")
    else:
        logger.error("WEBHOOK_URL 未设置！")
        await alert("WEBHOOK_URL 未配置，机器人将无法接收消息")

    commit = os.environ.get("RENDER_GIT_COMMIT", "unknown")[:8]
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    await alert(
        f"部署完成\n"
        f"Commit: {commit}\n"
        f"时间: {now}\n"
        f"币种: {', '.join(manager.all_inst_ids())}"
    )

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, WEB_SERVER_HOST, WEB_SERVER_PORT)
    await site.start()
    logger.info(f"Web 服务器已启动: http://{WEB_SERVER_HOST}:{WEB_SERVER_PORT}")

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