import asyncio
import logging
import os
from datetime import datetime

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, MenuButtonCommands, BotCommand
from aiogram.fsm.context import FSMContext
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
from core.state_store import StateStore
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


# ==================== 鉴权 ====================
def _allowed(user_id: int) -> bool:
    return (not TG_ALLOWED_IDS) or (user_id in TG_ALLOWED_IDS)


# ==================== 所有命令（优先注册） ====================
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
    lines = []
    for iid in dlg.MANAGER.all_inst_ids():
        d = dlg.MANAGER.dips.get(iid)
        status = "🟢" if (d and d.running) else "⏸"
        price = _last_price.get(iid, "-")
        pos = d.position if d else 0
        lines.append(f"{status} {iid} {price} 仓:{pos:.4f}")
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
    await msg.answer(("✅ " if ok else "❌ ") + text)


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
    await msg.answer(("✅ " if ok else "❌ ") + text)


@dp.message(Command("status"))
async def cmd_status(msg: Message):
    if not _allowed(msg.from_user.id):
        await msg.answer("无权访问")
        return
    if not dlg.MANAGER:
        await msg.answer("未初始化")
        return
    rm = dlg.MANAGER.risk_manager
    lines = [
        "📊 状态概览",
        f"风控: {'⚠️ 已触发' if rm.is_risk_triggered() else '✅ 正常'}",
        f"今日盈亏: {rm.daily_pnl:+.2f} USDT",
        "",
    ]
    for iid in dlg.MANAGER.all_inst_ids():
        d = dlg.MANAGER.dips.get(iid)
        status = "🟢" if (d and d.running) else "⏸"
        pos = d.position if d else 0
        profit = d.total_profit if d else 0
        lines.append(f"{status} {iid}  {_last_price.get(iid, '-')}  仓:{pos:.4f}  盈亏:{profit:+.4f}")
    await msg.answer("\n".join(lines))


@dp.message(Command("signals"))
async def cmd_signals(msg: Message):
    if not _allowed(msg.from_user.id):
        await msg.answer("无权访问")
        return
    if not dlg.MANAGER:
        await msg.answer("未初始化")
        return

    lines = ["📈 信号诊断"]
    for iid in dlg.MANAGER.all_inst_ids():
        dip = dlg.MANAGER.dips.get(iid)
        if not dip:
            continue
        try:
            f = await dip.get_signal_forecast()
        except Exception as e:
            lines.append(f"\n{iid}: 失败 {e}")
            continue
        if "error" in f:
            lines.append(f"\n{iid}: {f['error']}")
            continue

        bar = f["bar"]
        price = f.get("price", 0)
        rsi = f.get("rsi", 0)
        bb_lower = f.get("bb_lower", 0)
        macd_ok = f.get("macd_ok", False)
        regime = f.get("regime", "unknown")
        adx = f.get("adx", 0)
        buy_ready = f.get("buy_ready", False)

        regime_icon = {"trending": "📈趋势", "ranging": "📊震荡", "transitional": "🔄过渡"}.get(regime, "❓")

        if buy_ready:
            icon = "🎯"
        else:
            icon = "⏳"

        lines.append(f"\n{icon} {iid} [{bar}] {regime_icon}")
        lines.append(f"  价格: {price}")
        if rsi:
            lines.append(f"  RSI: {rsi:.1f}")
        if bb_lower and price:
            gap = (price - bb_lower) / bb_lower * 100
            lines.append(f"  距BB下轨: {gap:+.2f}%")
        lines.append(f"  MACD: {'✅' if macd_ok else '❌'}")
        if adx:
            lines.append(f"  ADX: {adx:.1f}")

        blockers = f.get("blockers", [])
        if blockers:
            for b in blockers:
                lines.append(f"  ⚠️ {b}")

    await msg.answer("\n".join(lines))


@dp.message(Command("positions"))
async def cmd_positions(msg: Message):
    if not _allowed(msg.from_user.id):
        await msg.answer("无权访问")
        return
    if not dlg.MANAGER:
        await msg.answer("未初始化")
        return
    lines = ["💰 持仓与挂单"]
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
            lines.append(f"  均价: {s['avg_buy_price']:.6f}")
        if pb:
            lines.append(f"  ⏳ 挂单买入: {pb}")
        if ps:
            lines.append(f"  ⏳ 挂单卖出: {ps}")
        lines.append(f"  已实现盈亏: {s.get('total_profit', 0):+.4f} USDT")
        batch = s.get("batch_tp_triggered", 0)
        if batch > 0:
            lines.append(f"  分批止盈: 已触发 {batch}/3 档")
    if not has_any:
        lines.append("\n暂无持仓或挂单。")
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
            await msg.answer("查询失败")
            return
        details = resp["data"][0].get("details", [])
        lines = ["💵 账户余额"]
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
    lines = ["💹 盈亏统计"]
    total_p = 0.0
    total_f = 0.0
    for iid in dlg.MANAGER.all_inst_ids():
        dip = dlg.MANAGER.dips.get(iid)
        if not dip:
            continue
        s = dip.snapshot()
        p = s.get("total_profit", 0)
        f = s.get("total_fee", 0)
        total_p += p
        total_f += f
        lines.append(f"\n{iid}\n  已实现: {p:+.4f} USDT\n  手续费: {f:.4f} USDT")
    lines.append(f"\n━━━━━━━━━━━━━━━")
    lines.append(f"合计已实现: {total_p:+.4f}")
    lines.append(f"合计手续费: {total_f:.4f}")
    lines.append(f"净收益: {total_p - total_f:+.4f} USDT")
    await msg.answer("\n".join(lines))


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
        "🛡 组合风控",
        f"当前资金: {rm.current_capital:.2f} USDT",
        f"峰值资金: {rm.peak_capital:.2f} USDT",
        f"今日盈亏: {rm.daily_pnl:+.2f} USDT",
        f"最大回撤限制: {rm.max_drawdown*100:.0f}%",
        f"每日亏损限制: {rm.daily_loss_limit} USDT",
        f"状态: {'⚠️ 已触发' if rm.is_risk_triggered() else '✅ 正常'}",
    ]
    await msg.answer("\n".join(lines))


@dp.message(Command("set"))
async def cmd_set(msg: Message):
    if not _allowed(msg.from_user.id):
        await msg.answer("无权访问")
        return
    if not dlg.MANAGER:
        await msg.answer("未初始化")
        return

    parts = (msg.text or "").split()
    if len(parts) < 4:
        await msg.answer(
            "用法: /set <币种> <参数名> <值>\n"
            "示例: /set BTC-USDT rsi_oversold 35\n"
            "可用参数: bar, rsi_period, rsi_oversold, bb_period, bb_std, "
            "macd_fast, macd_slow, macd_signal, ema_period, vol_multiplier, "
            "maxSpend, take_profit_pct, stop_loss_pct, trailing_pct, "
            "use_adaptive, trend_filter, use_trailing, limit_offset_pct"
        )
        return

    inst_id = parts[1].upper()
    key = parts[2]
    raw_val = parts[3]

    dip = dlg.MANAGER.dips.get(inst_id)
    if not dip:
        await msg.answer(f"{inst_id} 不在监控中")
        return

    # 参数类型映射
    pct_keys = {"take_profit_pct", "stop_loss_pct", "trailing_pct", "limit_offset_pct"}
    int_keys = {"rsi_period", "bb_period", "macd_fast", "macd_slow", "macd_signal", "ema_period", "vol_ma_period"}
    float_keys = {"rsi_oversold", "rsi_overbought", "bb_std", "vol_multiplier", "maxSpend"}
    bool_keys = {"use_adaptive", "trend_filter", "use_trailing", "volume_confirm"}

    try:
        if key == "bar":
            if raw_val not in VALID_BARS:
                await msg.answer(f"不支持的周期。可用: {', '.join(VALID_BARS)}")
                return
            value = raw_val
        elif key in bool_keys:
            value = raw_val.lower() in ("1", "true", "on", "yes", "开")
        elif key in int_keys:
            value = int(raw_val)
        elif key in pct_keys:
            value = float(raw_val) / 100.0
        elif key in float_keys:
            value = float(raw_val)
        else:
            await msg.answer(f"未知参数: {key}")
            return
    except ValueError:
        await msg.answer(f"格式不正确: {raw_val}")
        return

    await dip.set_param(key, value)

    if key in pct_keys:
        shown = f"{value * 100:.2f}%"
    elif key in bool_keys:
        shown = "true" if value else "false"
    else:
        shown = str(value)
    await msg.answer(f"✅ {inst_id} 的 {key} 已设为 {shown}")


@dp.message(Command("reset"))
async def cmd_reset(msg: Message):
    if not _allowed(msg.from_user.id):
        await msg.answer("无权访问")
        return
    if not dlg.MANAGER:
        await msg.answer("未初始化")
        return
    parts = (msg.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await msg.answer("用法: /reset <币种>")
        return
    inst_id = parts[1].strip().upper()
    dip = dlg.MANAGER.dips.get(inst_id)
    if not dip:
        await msg.answer(f"{inst_id} 不在监控中")
        return
    await dip.reset_params()
    await msg.answer(f"✅ {inst_id} 参数已恢复默认")


@dp.message(Command("signal_params"))
async def cmd_signal_params(msg: Message):
    if not _allowed(msg.from_user.id):
        await msg.answer("无权访问")
        return
    if not dlg.MANAGER:
        await msg.answer("未初始化")
        return
    lines = ["⚙️ 当前信号参数"]
    for iid in dlg.MANAGER.all_inst_ids():
        dip = dlg.MANAGER.dips.get(iid)
        if not dip:
            continue
        p = dip.params
        s = dip.snapshot()
        lines.append(
            f"\n{iid}\n"
            f"  K线周期: {p.get('bar', '15m')}\n"
            f"  自适应: {'开' if p.get('use_adaptive', True) else '关'}\n"
            f"  市场体制: {s.get('regime', '-')}\n"
            f"  ADX: {s.get('adx', 0):.1f}\n"
            f"  RSI: {p.get('rsi_period', 14)}周期, 超卖<{p.get('rsi_oversold', 30)}, 超买>{p.get('rsi_overbought', 70)}\n"
            f"  BB: {p.get('bb_period', 20)}周期, {p.get('bb_std', 2.0)}标准差\n"
            f"  MACD: {p.get('macd_fast', 12)}/{p.get('macd_slow', 26)}/{p.get('macd_signal', 9)}\n"
            f"  EMA: {p.get('ema_period', 200)}\n"
            f"  单次金额: {p.get('maxSpend', 100)} USDT\n"
            f"  止盈: {p.get('take_profit_pct', 0.03)*100:.2f}% | 止损: {p.get('stop_loss_pct', 0.05)*100:.2f}%\n"
            f"  移动止盈: {'开' if p.get('use_trailing', True) else '关'} {p.get('trailing_pct', 0.02)*100:.2f}%"
        )
    await msg.answer("\n".join(lines))


# ==================== 菜单按钮 ====================
async def setup_menu():
    await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    await bot.set_my_commands([
        BotCommand(command="menu", description="主菜单"),
        BotCommand(command="list", description="币种列表"),
        BotCommand(command="status", description="状态概览"),
        BotCommand(command="signals", description="信号诊断"),
        BotCommand(command="positions", description="持仓与挂单"),
        BotCommand(command="balance", description="查询余额"),
        BotCommand(command="profit", description="盈亏统计"),
        BotCommand(command="risk", description="风控状态"),
        BotCommand(command="signal_params", description="信号参数"),
        BotCommand(command="add", description="添加币种"),
        BotCommand(command="remove", description="删除币种"),
    ])


# ==================== 健康检查 ====================
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

    await manager.start_periodic_save(60)

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
            f"✅ 部署完成\n"
            f"Commit: {commit}\n"
            f"时间: {now}\n"
            f"币种: {', '.join(manager.all_inst_ids())}"
        )
    except Exception:
        pass


# ==================== 入口 ====================
async def main():
    state_store = StateStore()
    await state_store.init()

    risk_manager = RiskManager()

    risk_saved = await state_store.load_risk()
    if risk_saved:
        try:
            risk_manager.initial_capital = risk_saved.get("initial_capital", 0.0)
            risk_manager.peak_capital = risk_saved.get("peak_capital", 0.0)
            risk_manager.current_capital = risk_saved.get("current_capital", 0.0)
            risk_manager.daily_pnl = risk_saved.get("daily_pnl", 0.0)
            logger.info(f"风控状态已从 Neon 恢复: 峰值={risk_manager.peak_capital:.2f}")
        except Exception as e:
            logger.error(f"风控状态恢复失败: {e}")

    manager = StrategyManager(risk_manager=risk_manager, state_store=state_store)
    dlg.MANAGER = manager

    dashboard = Dashboard(port=DASHBOARD_PORT)

    bus.subscribe(MarketEvent, on_market_event)
    bus.subscribe(RiskEvent, on_risk_event)

    # ================= 关键：先注册 Dialog，再启动 =================
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
    logger.info(f"Web 服务器: {WEB_SERVER_HOST}:{WEB_SERVER_PORT}")

    dashboard_runner = await dashboard.start()

    if WEBHOOK_URL:
        try:
            webhook_full_url = f"{WEBHOOK_URL.rstrip('/')}{WEBHOOK_PATH}"
            await bot.set_webhook(
                url=webhook_full_url,
                secret_token=WEBHOOK_SECRET or None,
                drop_pending_updates=True,
            )
            logger.info(f"Webhook: {webhook_full_url}")
        except Exception as e:
            logger.error(f"设置 Webhook 失败: {e}")

    asyncio.create_task(background_init(manager, dashboard))

    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        pass
    finally:
        logger.info("正在保存状态到 Neon 并关闭...")
        try:
            await manager.state_store.save_all(manager)
            await manager.state_store.close()
            logger.info("状态已保存")
        except Exception as e:
            logger.error(f"保存状态失败: {e}")

        await bus.stop()
        await bot.session.close()
        await runner.cleanup()
        await dashboard_runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass