"""
bot.py - Telegram 交互层（命令、按钮、主循环）
"""
import asyncio, random
from datetime import datetime, timezone, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes
from config import settings, logger
from indicators import TechnicalEngine
from storage import load_config, save_config

CST = timezone(timedelta(hours=8))

class MacroEngine:
    async def check(self):
        score = random.uniform(0.05, 0.35)
        return {'is_safe': score<0.75, 'score': score, 'status': "🟢 平稳" if score<0.75 else "🚨 风险"}

class QuantBot:
    def __init__(self, exchange):
        self.exchange = exchange
        self.tech = TechnicalEngine()
        self.macro = MacroEngine()
        self.lock = asyncio.Lock()

        cfg = load_config()
        self.is_running = True
        self.orderbook_filter = cfg.get('orderbook_filter', True)
        self.waterfall_breaker = cfg.get('waterfall_breaker', True)
        self.symbols = cfg.get('symbols', []) or [settings.SYMBOL]
        self.tp_pct = cfg.get('tp_pct', 0.08)
        self.sl_pct = cfg.get('sl_pct', 0.05)
        self.trailing_sl_pct = cfg.get('trailing_sl_pct', 0.02)
        self.single_order_usdt = cfg.get('single_order_usdt', 100)
        self.timeframe = cfg.get('timeframe', '15m')
        self.reserve_bottom = cfg.get('reserve_bottom', 50)

        raw = settings.ALLOWED_USERS
        self.allowed = {int(x.strip()) for x in raw.split(",") if x.strip().isdigit()} if raw else set()
        self.env_tag = "🧪 模拟盘" if settings.IS_SANDBOX else "🔴 实盘"
        self.tg_app = None
        if settings.TG_BOT_TOKEN:
            self.tg_app = ApplicationBuilder().token(settings.TG_BOT_TOKEN).build()
            self.tg_app.add_handler(CommandHandler("start", self.menu))
            self.tg_app.add_handler(CommandHandler("menu", self.menu))
            self.tg_app.add_handler(CommandHandler("panic", self.panic))
            self.tg_app.add_handler(CommandHandler("analysis", self.analysis))
            self.tg_app.add_handler(CommandHandler("brain", self.brain))
            self.tg_app.add_handler(CommandHandler("help", self.help_cmd))
            self.tg_app.add_handler(CommandHandler("settp", self.set_tp))
            self.tg_app.add_handler(CommandHandler("setsl", self.set_sl))
            self.tg_app.add_handler(CommandHandler("setamount", self.set_amount))
            self.tg_app.add_handler(CallbackQueryHandler(self.handle_click))

    def _save(self):
        save_config({
            'tp_pct': self.tp_pct, 'sl_pct': self.sl_pct,
            'trailing_sl_pct': self.trailing_sl_pct, 'single_order_usdt': self.single_order_usdt,
            'timeframe': self.timeframe, 'reserve_bottom': self.reserve_bottom,
            'symbols': self.symbols, 'orderbook_filter': self.orderbook_filter,
            'waterfall_breaker': self.waterfall_breaker
        })

    def _auth(self, update: Update):
        if not self.allowed: return True
        return update.effective_user.id in self.allowed

    def _keyboard(self):
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("🚨 紧急全平", callback_data="panic")],
            [InlineKeyboardButton("⚡ 开启", callback_data="start"), InlineKeyboardButton("🔴 关机", callback_data="stop")],
            [InlineKeyboardButton("📊 看板", callback_data="dashboard"), InlineKeyboardButton("💳 余额", callback_data="balance")],
            [InlineKeyboardButton("🧠 大脑", callback_data="brain"), InlineKeyboardButton("📈 分析", callback_data="gap")],
            [InlineKeyboardButton("🔄 刷新", callback_data="refresh")]
        ])

    async def menu(self, update: Update, context):
        if not self._auth(update): await update.message.reply_text("⛔ 未授权"); return
        status = "🟢 运行中" if self.is_running else "🔴 停止"
        await update.effective_message.reply_text(
            f"🤖 量化控制台 {self.env_tag}\n状态: {status}\n⏱ {datetime.now(CST).strftime('%H:%M:%S')}",
            reply_markup=self._keyboard(), parse_mode="Markdown")

    async def panic(self, update: Update, context):
        if not self._auth(update): return
        msg = update.effective_message
        await msg.reply_text("🚨 执行全平...")
        for sym in self.symbols:
            await self.exchange.cancel_all_orders(sym)
            bal = await self.exchange.fetch_balance()
            coin = sym.split('/')[0]
            amount = bal.get(coin, {}).get('free', 0) if isinstance(bal.get(coin), dict) else 0
            if isinstance(amount, (int, float)) and amount > 0:
                await self.exchange.create_market_sell_order(sym, amount)
        await msg.reply_text("✅ 全平完成")

    async def analysis(self, update: Update, context):
        if not self._auth(update): return
        lines = ["📈 **低买分析**\n"]
        for sym in self.symbols:
            t = await self.exchange.fetch_ticker(sym)
            p = t['last']
            ohlcv = await self.exchange.fetch_ohlcv(sym, self.timeframe, 50)
            ind = self.tech.calc(ohlcv, p)
            target = min(ind['bb_lower'], p*0.99)
            gap = ((p-target)/p)*100
            lines.append(f"🔹 {sym}: {p:.2f} 买点{target:.2f}({gap:.1f}%) RSI{ind['rsi']:.0f}")
        await update.effective_message.reply_text("\n".join(lines), parse_mode="Markdown")

    async def brain(self, update: Update, context):
        if not self._auth(update): return
        sym = self.symbols[0]
        t = await self.exchange.fetch_ticker(sym)
        p = t['last']
        ohlcv = await self.exchange.fetch_ohlcv(sym, self.timeframe, 50)
        ind = self.tech.calc(ohlcv, p)
        fr = await self.exchange.fetch_funding_rate(sym)
        macro = await self.macro.check()
        msg = f"🧠 **诊断 {sym}**\n宏观: {macro['status']}\n费率: {fr*100:.4f}%\n" \
              f"布林: {ind['bb_upper']:.1f}/{ind['bb_lower']:.1f}\nRSI: {ind['rsi']:.1f} ATR: {ind['atr']:.2f}"
        await update.effective_message.reply_text(msg, parse_mode="Markdown")

    async def help_cmd(self, update: Update, context):
        await update.effective_message.reply_text(
            "命令:\n/menu /brain /analysis /panic\n/settp 5 (5%止盈)\n/setsl 2 (2%止损)\n/setamount 100")

    async def set_tp(self, update, context):
        if not self._auth(update): return
        try:
            self.tp_pct = float(context.args[0]) / 100
            async with self.lock: self._save()
            await update.message.reply_text(f"止盈: {self.tp_pct*100:.1f}%")
        except: pass

    async def set_sl(self, update, context):
        if not self._auth(update): return
        try:
            self.sl_pct = float(context.args[0]) / 100
            async with self.lock: self._save()
            await update.message.reply_text(f"止损: {self.sl_pct*100:.1f}%")
        except: pass

    async def set_amount(self, update, context):
        if not self._auth(update): return
        try:
            self.single_order_usdt = float(context.args[0])
            async with self.lock: self._save()
            await update.message.reply_text(f"单笔: {self.single_order_usdt}U")
        except: pass

    async def handle_click(self, update: Update, context):
        query = update.callback_query
        data = query.data
        try:
            if data == "refresh": await self.menu(update, context)
            elif data == "start": self.is_running = True; await query.answer("已开启")
            elif data == "stop": self.is_running = False; await query.answer("已关机")
            elif data == "panic": await self.panic(update, context)
            elif data == "dashboard":
                await query.message.reply_text(f"监控: {self.symbols}\n挂单: {self.single_order_usdt}U\n"
                    f"止盈: {self.tp_pct*100:.1f}% 止损: {self.sl_pct*100:.1f}%\n周期: {self.timeframe}")
            elif data == "balance":
                bal = await self.exchange.fetch_balance()
                usdt = bal.get('USDT',{}).get('free','?')
                await query.message.reply_text(f"余额: {usdt} USDT")
            elif data == "brain": await self.brain(update, context)
            elif data == "gap": await self.analysis(update, context)
            await query.answer()
        except Exception as e:
            logger.error(f"按钮异常: {e}")

    async def start(self):
        if self.tg_app:
            await self.tg_app.initialize()
            await self.tg_app.start()
            await self.tg_app.updater.start_polling(drop_pending_updates=True)
            logger.info("✅ Bot 启动")
            while True:
                await asyncio.sleep(30)
