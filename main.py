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
from core.risk_manager import RiskManager
from core.event_bus import bus
from core.events import MarketEvent, RiskEvent
from web.dashboard import Dashboard
from notifier import alert

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")
WEBHOOK_PATH = "/webhook"
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
WEB_SERVER_HOST = "0.0.0.0"
WEB_SERVER_PORT = int(os.environ.get("PORT", 10000))
DASHBOARD_PORT = int(os.environ.get("DASHBOARD_PORT", 8080))

bot = Bot(token=TG_BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

VALID_BARS = ["1m", "3m", "5m", "15m", "30m", "1H", "2H", "4H", "6H", "12H", "1D"]


# ==================== 事件处理 ====================
async def on_market_event(event: MarketEvent):
    _last_price[event.inst_id] = event.price
    if dlg.MANAGER:
        await dlg.MANAGER.on_ticker(event.inst_id, event.price, event.raw)


async def on_risk_event(event: RiskEvent):
    await alert(f"⚠️ 风控触发: {event.detail}")
    if dlg.MANAGER:
        for iid in dlg.MANAGER.all_inst_ids():
            await dlg.MANAGER.stop_all_strategies(iid)


# ==================== 命令处理 ====================
def _allowed(user_id: int) -> bool:
    return (not TG_ALLOWED_IDS) or (user_id in TG_ALLOWED_IDS)


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
        d = dlg.MANAGER.dips.get(iid)
        ds = "监控" if (d and d.running) else "暂停"
        lines.append(f"{iid} 低吸{ds} {_last_price.get(iid, '-')}")
    await msg.answer("状态总览:\n" + "\n".join(lines))


@dp.message(Command("risk"))
async def cmd_risk(msg: Message):
    if not _allowed(msg.from_user.id):
        await msg.answer("无权访问")
        return
    if not dlg.MANAGER:
        await msg.answer("未初始化")
        return
    rm = dlg.MANAGER.risk_manager
    lines = [
        "组合风控",
        f"当前资金: {rm.current_capital:.2f} USDT",
        f"峰值资金: {rm.peak_capital:.2f} USDT",
        f"今日盈亏: {rm.daily_pnl:.2f} USDT",
        f"最大回撤限制: {rm.max_drawdown*100:.0f}%",
        f"每日亏损限制: {rm.daily_loss_limit} USDT",
        f"风控状态: {'已触发' if rm.is_risk_triggered() else '正常'}",
    ]
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
        pb = s.get("pending_buy")
        ps = s.get("pending_sell")
        if pos <= 0 and not pb and not ps:
            continue
        has_any = True
        lines.append(f"\n{iid}")
        lines.append(f"  当前价: {_last_price.get(iid, 0)}")
        lines.append(f"  持仓: {pos:.6f}")
        if s.get("avg_buy_price", 0) > 0:
            lines.append(f"  平均买入价: {s['avg_buy_price']:.6f}")
        if pb:
            lines.append(f"  ⏳ 挂单买入: {pb}")
        if ps:
            lines.append(f"  ⏳ 挂单卖出: {ps}")
        lines.append(f"  已实现盈亏: {s.get('total_profit', 0):.4f} USDT")
    if not has_any:
        lines.append("\n暂无建仓或挂单。")
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
            f = await dip.get_signal_forecast()
        except Exception as e:
            lines.append(f"\n{iid}: 获取失败 {e}")
            continue

        if "error" in f:
            lines.append(f"\n{iid}: {f['error']}")
            continue

        bar = f["bar"]
        gap = f["gap"]
        current_score = f["current_score"]
        threshold = f["threshold"]

        if not f["running"]:
            status_icon = "⛔ 未启动"
        elif gap == 0 and not f["blockers"]:
            status_icon = "✅ 等待成交"
        elif gap == 0:
            status_icon = "🟡 已满足但有拦截"
        elif gap <= 10:
            status_icon = "🔥 接近"
        elif gap <= 25:
            status_icon = "⚡ 中等"
        else:
            status_icon = "⏳ 等待"

        lines.append(f"\n{iid}  [{bar}]  {status_icon}")

        pct = min(100, current_score / threshold * 100) if threshold > 0 else 0
        bar_filled = int(pct / 10)
        bar_str = "█" * bar_filled + "░" * (10 - bar_filled)
        lines.append(f"  评分: [{bar_str}] {current_score:.0f}/{threshold:.0f}")

        rsi = f["rsi"]
        if rsi is not None:
            rsi_gap = f["rsi_gap"]
            rsi_status = f"差 {rsi_gap:.1f}" if rsi_gap > 0 else "✅"
            lines.append(f"  RSI: {rsi:.1f} (阈值<{f['rsi_threshold']:.1f}) {rsi_status}")

        bb_lower = f["bb_lower"]
        if bb_lower is not None and f["bb_gap_pct"] is not None:
            bb_status = f"差 {f['bb_gap_pct']:.2f}%" if f["bb_gap_pct"] > 0 else "✅"
            lines.append(f"  布林带下轨: {bb_lower:.4f} {bb_status}")

        lines.append(f"  MACD: {'✅ 金叉' if f['macd_ok'] else '❌ 未金叉'}")

        if f["vol_ratio"] is not None:
            lines.append(f"  成交量倍数: {f['vol_ratio']:.2f}x")

        blockers = f.get("blockers", [])
        if blockers:
            lines.append("  ⚠️ 拦截原因:")
            for b in blockers:
                lines.append(f"    · {b}")

        est = f.get("estimated_minutes")
        if est is not None and est > 0:
            if est < 60:
                lines.append(f"  ⏱ 预估: 约 {est} 分钟")
            elif est < 1440:
                lines.append(f"  ⏱ 预估: 约 {est/60:.1f} 小时")
            else:
                lines.append(f"  ⏱ 预估: 约 {est/1440:.1f} 天")
        elif gap == 0:
            lines.append(f"  ⏱ 评分已达标")

    lines.append("\n提示: 如显示拦截原因，按原因排查即可。")
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
            lines.append(f"\n{iid}\n  已实现盈亏: {profit:.4f} USDT\n  手续费: {fee:.4f} USDT")
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
        BotCommand(command="add", description="添加币种"),
        BotCommand(command="remove", description="删除币种"),
        BotCommand(command="status", description="状态"),
        BotCommand(command="signals", description="信号与买入预测"),
        BotCommand(command="risk", description="风控状态"),
        BotCommand(command="positions", description="建仓与挂单"),
        BotCommand(command="balance", description="查询余额"),
        BotCommand(command="profit", description="获利与手续费"),
    ])


async def health(request):
    return web.Response(text="OK")


# ==================== 后台初始化 ====================
async def background_init(manager, dashboard):
    for iid in WATCHLIST:
        ok, text = await manager.add_inst(iid)
        logger.info(text)

    try:
        await manager.restore_all()
    except Exception as e:
        logger.error(f"状态恢复失败: {e}")

    pub_ws = PublicWS(lambda iid, p, r: bus.publish(MarketEvent(iid, p, r)))
    priv_ws = PrivateWS(lambda o: None)
    asyncio.create_task(pub_ws.connect(manager.all_inst_ids()))
    asyncio.create_task(priv_ws.connect())
    dlg.PUB_WS = pub_ws

    dashboard.set_manager(manager, manager.risk_manager)

    commit = os.environ.get("RENDER_GIT_COMMIT", "unknown")[:8]
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        await alert(
            f"部署完成\n"
            f"Commit: {commit}\n"
            f"时间: {now}\n"
            f"币种: {', '.join(manager.all_inst_ids())}"
        )
    except Exception:
        pass


async def main():
    risk_manager = RiskManager()
    manager = StrategyManager(risk_manager=risk_manager)
    dlg.MANAGER = manager
    dashboard = Dashboard(port=DASHBOARD_PORT)

    bus.subscribe(MarketEvent, on_market_event)
    bus.subscribe(RiskEvent, on_risk_event)

    for dialog in get_dialogs():
        dp.include_router(dialog)
    setup_dialogs(dp)

    try:
        await setup_menu()
    except Exception as e:
        logger.error(f"设置菜单失败: {e}")

    asyncio.create_task(bus.start())

    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    webhook_handler = SimpleRequestHandler(
        dispatcher=dp, bot=bot, secret_token=WEBHOOK_SECRET or None
    )
    webhook_handler.register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, WEB_SERVER_HOST, WEB_SERVER_PORT)
    await site.start()
    logger.info(f"Web 服务器已启动: {WEB_SERVER_HOST}:{WEB_SERVER_PORT}")

    dashboard_runner = await dashboard.start()

    if WEBHOOK_URL:
        try:
            webhook_full_url = f"{WEBHOOK_URL.rstrip('/')}{WEBHOOK_PATH}"
            await bot.set_webhook(
                url=webhook_full_url,
                secret_token=WEBHOOK_SECRET or None,
                drop_pending_updates=True,
            )
            logger.info(f"Webhook 已设置: {webhook_full_url}")
        except Exception as e:
            logger.error(f"设置 Webhook 失败: {e}")

    asyncio.create_task(background_init(manager, dashboard))

    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        pass
    finally:
        await bus.stop()
        await bot.session.close()
        await runner.cleanup()
        await dashboard_runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass