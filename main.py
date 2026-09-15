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
from strategies.dip_sell import DEFAULT_PARAMS
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

PARAM_TYPES = {
    "bar": "bar",
    "rsi_period": "int",
    "rsi_oversold": "float",
    "rsi_overbought": "float",
    "bb_period": "int",
    "bb_std": "float",
    "macd_fast": "int",
    "macd_slow": "int",
    "macd_signal": "int",
    "ema_period": "int",
    "vol_ma_period": "int",
    "vol_multiplier": "float",
    "trend_filter": "bool",
    "volume_confirm": "bool",
    "use_adaptive": "bool",
    "limit_offset_pct": "pct",
    "maxSpend": "float",
    "take_profit_pct": "pct",
    "stop_loss_pct": "pct",
    "trailing_pct": "pct",
    "use_trailing": "bool",
}


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
        g = dlg.MANAGER.grids.get(iid)
        d = dlg.MANAGER.dips.get(iid)
        gs = "运行" if (g and g.running) else "停止"
        ds = "运行" if (d and d.running) else "暂停"
        mode = "自适应" if (d and d.params.get("use_adaptive")) else "固定"
        lines.append(f"{iid} 网格{gs} 低吸{ds}[{mode}] {_last_price.get(iid, '-')}")
    await msg.answer("状态总览:\n" + "\n".join(lines))


@dp.message(Command("adaptive"))
async def cmd_adaptive(msg: Message):
    if not _allowed(msg.from_user.id):
        await msg.answer("无权访问")
        return
    if not dlg.MANAGER:
        await msg.answer("未初始化")
        return

    lines = ["自适应参数（基于ATR和趋势）"]
    for iid in dlg.MANAGER.all_inst_ids():
        dip = dlg.MANAGER.dips.get(iid)
        if not dip:
            continue
        a = dip.adaptive
        mode = "自适应" if dip.params.get("use_adaptive", True) else "固定参数"
        lines.append(
            f"\n{iid}  [{mode}]\n"
            f"  当前波动率: {a.volatility * 100:.2f}%\n"
            f"  ATR(14): {a.atr:.4f}\n"
            f"  趋势强度: {a.trend_strength * 100:+.2f}%\n"
            f"  --- 动态参数 ---\n"
            f"  RSI超卖: <{a.rsi_oversold:.1f} | RSI超买: >{a.rsi_overbought:.1f}\n"
            f"  布林带标准差: {a.bb_std:.2f}\n"
            f"  止损: -{a.stop_loss_pct * 100:.2f}% | 止盈: +{a.take_profit_pct * 100:.2f}%\n"
            f"  移动回撤: {a.trailing_pct * 100:.2f}%\n"
            f"  买入分数阈值: {a.buy_threshold:.0f}"
        )
    lines.append("\n切换模式: /set <币种> use_adaptive true/false")
    await msg.answer("\n".join(lines))


@dp.message(Command("bar"))
async def cmd_bar(msg: Message):
    if not _allowed(msg.from_user.id):
        await msg.answer("无权访问")
        return
    parts = (msg.text or "").split()
    if len(parts) < 3:
        await msg.answer("用法: /bar <币种> <周期>\n可用: " + "/".join(VALID_BARS))
        return
    inst_id = parts[1].upper()
    bar = parts[2]
    if bar not in VALID_BARS:
        await msg.answer(f"周期 {bar} 不支持")
        return
    dip = dlg.MANAGER.dips.get(inst_id) if dlg.MANAGER else None
    if not dip:
        await msg.answer(f"{inst_id} 不在监控中")
        return
    await dip.set_param("bar", bar)
    await msg.answer(f"{inst_id} K线周期已改为 {bar}")


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
        lines = ["用法: /set <币种> <参数名> <值>", "示例: /set BTC-USDT use_adaptive false", "", "可用参数:"]
        for k, t in PARAM_TYPES.items():
            default = DEFAULT_PARAMS.get(k, "-")
            lines.append(f"  {k}: {t} (默认 {default})")
        await msg.answer("\n".join(lines))
        return

    inst_id = parts[1].upper()
    key = parts[2]
    raw_val = parts[3]

    if key not in PARAM_TYPES:
        await msg.answer(f"未知参数: {key}")
        return

    dip = dlg.MANAGER.dips.get(inst_id)
    if not dip:
        await msg.answer(f"{inst_id} 不在监控中")
        return

    ptype = PARAM_TYPES[key]
    try:
        if ptype == "bar":
            if raw_val not in VALID_BARS:
                await msg.answer(f"周期不支持")
                return
            value = raw_val
        elif ptype == "bool":
            value = raw_val.lower() in ("1", "true", "on", "yes", "开")
        elif ptype == "int":
            value = int(raw_val)
        elif ptype == "float":
            value = float(raw_val)
        elif ptype == "pct":
            value = float(raw_val) / 100.0
    except ValueError:
        await msg.answer(f"格式不正确")
        return

    await dip.set_param(key, value)

    if ptype == "pct":
        shown = f"{value * 100:.2f}%"
    elif ptype == "bool":
        shown = "true" if value else "false"
    else:
        shown = str(value)
    await msg.answer(f"{inst_id} 的 {key} 已设置为 {shown}")


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
    await msg.answer(f"{inst_id} 参数已恢复默认（含自适应模式）")


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

        bar = status.get("bar", "15m")
        if status.get("use_adaptive"):
            lines.append(
                f"\n{iid}  [{bar}] 自适应\n"
                f"  当前价: {status['price']:.4f}\n"
                f"  波动率: {status['volatility']*100:.2f}%\n"
                f"  RSI: {status['rsi']:.1f} (超卖阈值 <{status['rsi_oversold']:.1f})\n"
                f"  BB下轨: {status['bb_lower']:.4f}\n"
                f"  MACD: {'✅金叉/转正' if status['macd_ok'] else '❌'}\n"
                f"  买入分数: {status['buy_score']:.0f} / 阈值 {status['buy_threshold']:.0f}\n"
                f"  {'🎯 已达阈值' if status['buy_ready'] else '⏳ 等待中'}"
            )
        else:
            lines.append(
                f"\n{iid}  [{bar}] 固定参数\n"
                f"  当前价: {status['price']:.4f}\n"
                f"  RSI: {status['rsi']:.1f}\n"
                f"  BB下轨: {status['bb_lower']:.4f}\n"
                f"  MACD: {'✅' if status['macd_ok'] else '❌'}"
            )
    lines.append("\n自适应参数: /adaptive")
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
        la = s.get("last_action")
        if pos <= 0 and not pb and not ps and not la:
            continue
        has_any = True
        lines.append(f"\n{iid}  [{s.get('bar', '15m')}]")
        lines.append(f"  当前价: {_last_price.get(iid, 0)}")
        lines.append(f"  持仓: {pos:.6f}")
        if s.get("avg_buy_price", 0) > 0:
            lines.append(f"  平均买入价: {s['avg_buy_price']:.6f}")
        if pb:
            lines.append(f"  ⏳ 挂单买入: {pb}")
        if ps:
            lines.append(f"  ⏳ 挂单卖出: {ps}")
        if la:
            lines.append(f"  最近动作: {la[0]} @ {la[1]:.2f}")
        lines.append(f"  已实现盈亏: {s.get('total_profit', 0):.4f} USDT")
        lines.append(f"  累计手续费: {s.get('total_fee', 0):.4f} USDT")

    if not has_any:
        lines.append("\n暂无建仓或挂单。")
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
        BotCommand(command="adaptive", description="自适应参数"),
        BotCommand(command="set", description="设置参数"),
        BotCommand(command="reset", description="恢复默认"),
        BotCommand(command="positions", description="建仓与挂单"),
        BotCommand(command="signals", description="信号诊断"),
        BotCommand(command="balance", description="查询余额"),
        BotCommand(command="profit", description="获利与手续费"),
    ])


async def health(request):
    return web.Response(text="OK")


async def background_init(manager):
    try:
        for iid in WATCHLIST:
            ok, text = await manager.add_inst(iid)
            logger.info(text)
    except Exception as e:
        logger.error(f"添加初始币种失败: {e}")

    try:
        await manager.restore_all()
    except Exception as e:
        logger.error(f"状态恢复失败: {e}")

    pub_ws = PublicWS(on_ticker)
    priv_ws = PrivateWS(on_order_update)
    asyncio.create_task(pub_ws.connect(manager.all_inst_ids()))
    asyncio.create_task(priv_ws.connect())
    dlg.PUB_WS = pub_ws

    commit = os.environ.get("RENDER_GIT_COMMIT", "unknown")[:8]
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        await alert(
            f"部署完成\nCommit: {commit}\n时间: {now}\n"
            f"币种: {', '.join(manager.all_inst_ids())}"
        )
    except Exception:
        pass


async def main():
    manager = StrategyManager()
    dlg.MANAGER = manager

    for dialog in get_dialogs():
        dp.include_router(dialog)
    setup_dialogs(dp)

    try:
        await setup_menu()
    except Exception as e:
        logger.error(f"设置菜单失败: {e}")

    # 先启动 aiohttp，让端口立即监听
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

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, WEB_SERVER_HOST, WEB_SERVER_PORT)
    await site.start()
    logger.info(f"Web 服务器已启动: {WEB_SERVER_HOST}:{WEB_SERVER_PORT}")

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

    asyncio.create_task(background_init(manager))

    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        pass
    finally:
        await bot.session.close()
        await runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass