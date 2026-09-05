"""
Telegram 控制命令。

与核心交易逻辑分离：bot.py 只管买卖，这里只管人机交互。
所有命令通过模块级 BOT 引用同一个 TradingBot 实例。
"""
import asyncio
import logging
import time

from telegram.ext import Application, CommandHandler, ContextTypes

import config as C
import params as PM
from grid import effective_sell, lot_pnl_pct
from params import LABELS
from bot import TradingBot

logger = logging.getLogger(__name__)

BOT = None


BOT: TradingBot | None = None


def _auth(update) -> bool:
    return update.effective_chat.id == C.TG_CHAT_ID


async def cmd_start(update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _auth(update):
        return
    await update.message.reply_text(
        "网格交易机器人（精简版）\n\n"
        "/status  状态与风控\n"
        "/grid    各币种挂单与持仓\n"
        "/set     修改参数（/set spacing 0.02）\n"
        "/params  当前参数\n"
        "/add /del  增删币种\n"
        "/recenter 重设中枢\n"
        "/on /off  启停\n"
        "/resume  解除冷却\n"
        "/panic   全部市价清仓\n"
        "/backup  推状态备份")


async def cmd_status(update, ctx):
    if not _auth(update):
        return
    b = BOT
    free, coins = b.ex.balances()
    prices = {s: b.ex.price(s) for s in b.state["coins"]}
    equity = free + sum(q * prices.get(f"{c}/USDT", 0.0) for c, q in coins.items())

    lines = [f"📊 状态 {'(模拟盘)' if C.SANDBOX else '(实盘)'}",
             f"运行  {'是' if b.running else '否'}",
             "模式  网格",
             ""]
    lines.append(b.risk.summary(equity))
    lines.append("")
    lines.append(f"可用现金  {free:.2f}U")
    lines.append(f"每档金额  {b.per_grid(free):.2f}U")
    lines.append(f"币种      {', '.join(b.state['coins']) or '无'}")
    await update.message.reply_text("\n".join(lines))


async def cmd_grid(update, ctx):
    if not _auth(update):
        return
    b = BOT
    if not b.state["coins"]:
        await update.message.reply_text("未配置币种。/add SOL/USDT")
        return

    lines = [f"🕸 网格（层数 {int(b.p.get('levels'))} "
             f"间距 {b.p.get('spacing')*100:.2f}%）", ""]
    for sym, st in b.state["coins"].items():
        price = b.ex.price(sym)
        lines.append(f"── {sym} ──")
        lines.append(f"  中枢 {st['center']:.4f}   现价 {price:.4f}")
        if st["center"] > 0:
            lines.append(f"  区间 {st['center']*(1-b.p.get('stop_loss')):.4f} "
                         f"~ {st['center']:.4f}")
        if not st["lots"]:
            lines.append("  持仓 无")
        for lvl, lot in st["lots"].items():
            age_h = (time.time() - float(lot.get("buy_time", time.time()))) / 3600
            pnl = lot_pnl_pct(lot, price)
            sp = effective_sell(
                lot, price, b.p.get("spacing"),
                follow=b.p.get("follow_hours") > 0,
                follow_hours=b.p.get("follow_hours"),
                follow_max_loss=b.p.get("follow_max_loss"))
            moved = " ⬇已下移" if sp < float(lot["sell_price"]) - 1e-9 else ""
            lines.append(f"  档{lvl} {lot['qty']} @ {lot['buy_price']:.4f}"
                         f"  现 {pnl*100:+.2f}%")
            lines.append(f"      卖 {sp:.4f}{moved}  持 {age_h:.1f}h")
        if st["orders"]:
            lines.append("  挂单:")
            for key, rec in st["orders"].items():
                lines.append(f"      {key} {rec['qty']} @ {rec['price']:.4f}")
        lines.append("")
    await update.message.reply_text("\n".join(lines))


async def cmd_params(update, ctx):
    if not _auth(update):
        return
    b = BOT
    lines = ["⚙️ 参数", ""]
    for k, v in b.p.dump().items():
        mark = "*" if v != PM.DEFAULTS[k] else " "
        lines.append(f"{mark} {k:<16} {v}")
    lines.append("")
    lines.append("* = 与默认值不同")
    lines.append("修改: /set <参数名> <值>")
    await update.message.reply_text("\n".join(lines))


async def cmd_set(update, ctx):
    if not _auth(update):
        return
    b = BOT
    args = (update.message.text or "").split()
    if len(args) != 3:
        await update.message.reply_text("用法: /set <参数名> <值>\n"
                                        "例如: /set spacing 0.02")
        return
    key, raw = args[1], args[2]
    try:
        b.p.set(key, raw)
    except KeyError:
        await update.message.reply_text(f"未知参数: {key}\n/params 查看全部")
        return
    except (TypeError, ValueError):
        await update.message.reply_text(f"值不合法: {raw}")
        return
    b.save()
    await update.message.reply_text(
        f"✅ {LABELS.get(key, key)} = {b.p.get(key)}")


async def cmd_add(update, ctx):
    if not _auth(update):
        return
    b = BOT
    args = (update.message.text or "").split()
    if len(args) != 2:
        await update.message.reply_text("用法: /add SOL/USDT")
        return
    sym = args[1].upper()
    if sym in b.state["coins"]:
        await update.message.reply_text(f"{sym} 已在列表")
        return
    try:
        price = b.ex.price(sym)
    except Exception as e:
        await update.message.reply_text(f"无法获取行情: {e}")
        return
    if price <= 0:
        await update.message.reply_text(f"交易对不存在或无行情: {sym}")
        return
    b.state["coins"][sym] = b._new_coin(price)
    b.save()
    await update.message.reply_text(f"✅ 已添加 {sym}，中枢 {price:.4f}")


async def cmd_del(update, ctx):
    if not _auth(update):
        return
    b = BOT
    args = (update.message.text or "").split()
    if len(args) != 2:
        await update.message.reply_text("用法: /del SOL/USDT")
        return
    sym = args[1].upper()
    st = b.state["coins"].pop(sym, None)
    if st is None:
        await update.message.reply_text(f"{sym} 不在列表")
        return
    # 必须连同挂单一并撤销，否则撤掉的币还在挂单
    for rec in st.get("orders", {}).values():
        b.ex.cancel(rec["id"], sym)
    b.save()
    warn = ""
    if st.get("lots"):
        warn = f"\n⚠️ 该币仍有持仓 {len(st['lots'])} 档，已不纳入网格，"
        "需手动处理"
    await update.message.reply_text(f"✅ 已移除 {sym}{warn}")


async def cmd_recenter(update, ctx):
    if not _auth(update):
        return
    b = BOT
    args = (update.message.text or "").split()
    if len(args) < 2:
        await update.message.reply_text("用法: /recenter SOL/USDT [价格]")
        return
    sym = args[1].upper()
    st = b.state["coins"].get(sym)
    if st is None:
        await update.message.reply_text(f"{sym} 不在列表")
        return
    price = float(args[2]) if len(args) > 2 else b.ex.price(sym)
    if price <= 0:
        await update.message.reply_text("价格无效")
        return
    st["center"] = price
    b.save()
    await update.message.reply_text(f"✅ {sym} 中枢设为 {price:.4f}")


async def cmd_on(update, ctx):
    if not _auth(update):
        return
    BOT.running = True
    BOT.save()
    await update.message.reply_text("✅ 已启动")


async def cmd_off(update, ctx):
    if not _auth(update):
        return
    BOT.running = False
    BOT.save()
    await update.message.reply_text("⏸ 已停止（挂单保留，不新开仓）")


async def cmd_resume(update, ctx):
    if not _auth(update):
        return
    b = BOT
    free, coins = b.ex.balances()
    prices = {s: b.ex.price(s) for s in b.state["coins"]}
    equity = free + sum(q * prices.get(f"{c}/USDT", 0.0) for c, q in coins.items())
    b.risk.resume(equity)
    b.save()
    await update.message.reply_text("✅ 冷却已解除")


async def cmd_panic(update, ctx):
    if not _auth(update):
        return
    b = BOT
    lines = ["🚨 全平", ""]
    ok = 0
    fail = []
    for sym, st in list(b.state["coins"].items()):
        for rec in st.get("orders", {}).values():
            b.ex.cancel(rec["id"], sym)
        st["orders"] = {}
        for lvl in list(st["lots"]):
            lot = st["lots"].pop(lvl)
            qty = b.ex.round_qty(sym, lot["qty"])
            if qty <= 0:
                continue
            if b.ex.market_sell(sym, qty):
                ok += 1
                lines.append(f"✅ {sym} 卖出 {qty}")
            else:
                fail.append(sym)
                lines.append(f"❌ {sym} 卖出失败")
    b.save()
    if fail:
        lines.append("")
        lines.append(f"⚠️ {len(fail)} 个币种未成功，请到交易所确认")
    else:
        lines.append(f"\n完成，共 {ok} 笔")
    await update.message.reply_text("\n".join(lines))


async def cmd_backup(update, ctx):
    if not _auth(update):
        return
    await BOT.backup()


async def on_error(update, ctx):
    logger.exception(f"Telegram 处理异常: {ctx.error}")


def build_app(bot: TradingBot) -> Application:
    global BOT
    BOT = bot
    app = Application.builder().token(C.TG_TOKEN).build()
    for name, fn in (("start", cmd_start), ("help", cmd_start),
                     ("status", cmd_status), ("grid", cmd_grid),
                     ("params", cmd_params), ("set", cmd_set),
                     ("add", cmd_add), ("del", cmd_del),
                     ("recenter", cmd_recenter),
                     ("on", cmd_on), ("off", cmd_off),
                     ("resume", cmd_resume), ("panic", cmd_panic),
                     ("backup", cmd_backup)):
        app.add_handler(CommandHandler(name, fn))
    app.add_error_handler(on_error)

    async def _post_init(a):
        asyncio.create_task(bot.loop())

    app.post_init = _post_init
    return app
