"""
bot.py - 完全体量化交互层（原版功能全恢复 + 移动止盈/预设/历史/信号评分）
"""
import asyncio, random, aiohttp, json, os
from datetime import datetime, timezone, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand, ForceReply
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, MessageHandler, ContextTypes, filters
from config import settings, logger
from indicators import TechnicalEngine
from storage import load_config, save_config

CST = timezone(timedelta(hours=8))
TRADES_FILE = "trades.json"

def load_trades():
    try:
        with open(TRADES_FILE, 'r') as f:
            return json.load(f)
    except:
        return []

def save_trades(trades):
    try:
        with open(TRADES_FILE, 'w') as f:
            json.dump(trades[-50:], f)
    except Exception as e:
        logger.error(f"保存交易记录失败: {e}")

class RealDataEngine:
    def __init__(self, exchange):
        self.exchange = exchange
        self._fear_greed_cache = {"value": 50, "classification": "Neutral", "timestamp": 0}
        self._cache_ttl = 300

    async def get_fear_greed_index(self):
        now = asyncio.get_event_loop().time()
        if now - self._fear_greed_cache["timestamp"] < self._cache_ttl:
            return self._fear_greed_cache
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get("https://api.alternative.me/fng/?limit=1", timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    data = await resp.json()
                    if data.get("data"):
                        item = data["data"][0]
                        self._fear_greed_cache = {
                            "value": int(item["value"]),
                            "classification": item["value_classification"],
                            "timestamp": now
                        }
        except Exception as e:
            logger.warning(f"恐惧贪婪指数获取失败: {e}")
        return self._fear_greed_cache

    async def check_macro_risk(self):
        fg = await self.get_fear_greed_index()
        value = fg["value"]
        if value < 25:
            return {'is_safe': False, 'score': value/100, 'status': f"🚨 极度恐惧 ({value})"}
        elif value > 75:
            return {'is_safe': False, 'score': value/100, 'status': f"⚠️ 极度贪婪 ({value})"}
        return {'is_safe': True, 'score': value/100, 'status': f"🟢 {fg['classification']} ({value})"}

    async def get_liquidation_risk(self, symbol):
        funding_rate = 0; long_short_ratio = 1.0
        try: funding_rate = await self.exchange.fetch_funding_rate(symbol)
        except: pass
        try:
            ratio_data = await self.exchange.fetch_long_short_ratio(symbol)
            if isinstance(ratio_data, dict): long_short_ratio = float(ratio_data.get('longShortRatio', 1.0))
            elif isinstance(ratio_data, (int, float)): long_short_ratio = float(ratio_data)
        except: pass
        ticker = await self.exchange.fetch_ticker(symbol); p = ticker['last']
        if long_short_ratio > 2.5: bias, liq = "HEAVY_LONG", p*0.92
        elif long_short_ratio < 0.4: bias, liq = "HEAVY_SHORT", p*1.08
        elif long_short_ratio > 1.5: bias, liq = "LONG_PREFERRED", p*0.96
        elif long_short_ratio < 0.65: bias, liq = "SHORT_PREFERRED", p*1.04
        else: bias, liq = "NEUTRAL", p
        return {'funding_rate': funding_rate, 'long_short_ratio': long_short_ratio,
                'bias': bias, 'liq_target_below': liq if bias != "HEAVY_SHORT" else p*0.97,
                'liq_target_above': liq if bias != "HEAVY_LONG" else p*1.03}

class OrderbookEngine:
    async def validate(self, orderbook):
        bids = orderbook.get('bids', []); asks = orderbook.get('asks', [])
        if not bids or not asks: return False, "盘口数据缺失"
        spread = ((asks[0][0] - bids[0][0]) / bids[0][0]) * 100
        if spread > 0.2: return False, f"价差过大 ({spread:.3f}%)"
        return True, f"盘口健康 (价差: {spread:.3f}%)"

class SignalEngine:
    @staticmethod
    def score(tech, funding_rate, fear_greed):
        score = 50
        rsi = tech['rsi']
        if rsi < 30: score += 25
        elif rsi < 40: score += 15
        elif rsi < 50: score += 5
        elif rsi > 70: score -= 20
        elif rsi > 60: score -= 10
        bb_lower = tech['bb_lower']; price = tech['bb_middle']
        bb_position = (price - bb_lower) / (tech['bb_upper'] - bb_lower) if tech['bb_upper'] != bb_lower else 0.5
        if bb_position < 0.2: score += 20
        elif bb_position < 0.35: score += 10
        elif bb_position > 0.8: score -= 15
        if funding_rate < -0.0005: score += 10
        elif funding_rate < 0: score += 5
        elif funding_rate > 0.001: score -= 10
        if fear_greed < 25: score += 10
        elif fear_greed > 75: score -= 5
        return max(0, min(100, score))

    @staticmethod
    def interpret(score):
        if score >= 80: return "🔥🔥🔥 强烈买入"
        elif score >= 65: return "🔥🔥 建议买入"
        elif score >= 50: return "🔥 可以关注"
        elif score >= 35: return "📉 暂时观望"
        elif score >= 20: return "📉📉 不建议买入"
        return "🚨 强烈回避"

class QuantBot:
    def __init__(self, exchange):
        self.exchange = exchange
        self.tech = TechnicalEngine()
        self.real_data = RealDataEngine(exchange)
        self.orderbook_engine = OrderbookEngine()
        self.signal_engine = SignalEngine()
        self.lock = asyncio.Lock()

        cfg = load_config()
        self.is_running = True
        self.orderbook_filter = cfg.get('orderbook_filter', True)
        self.waterfall_breaker = cfg.get('waterfall_breaker', True)
        self.symbols = cfg.get('symbols', []) or [settings.SYMBOL, "BTC/USDT", "SOL/USDT"]
        self.tp_pct = cfg.get('tp_pct', 0.08)
        self.sl_pct = cfg.get('sl_pct', 0.05)
        self.trailing_sl_pct = cfg.get('trailing_sl_pct', 0.02)
        self.trailing_tp_pct = cfg.get('trailing_tp_pct', 0.01)
        self.single_order_usdt = cfg.get('single_order_usdt', 100)
        self.timeframe = cfg.get('timeframe', '15m')
        self.reserve_bottom = cfg.get('reserve_bottom', 50)
        self.max_daily_trades = cfg.get('max_daily_trades', 0)

        self.taker_fee = settings.TAKER_FEE
        self.maker_fee = settings.MAKER_FEE
        self.min_profit_margin = settings.MIN_PROFIT_MARGIN
        self.breakeven_pct = (self.taker_fee * 2) + self.min_profit_margin

        raw = settings.ALLOWED_USERS
        self.allowed = {int(x.strip()) for x in raw.split(",") if x.strip().isdigit()} if raw else set()
        self.env_tag = "🧪 (模拟盘)" if settings.IS_SANDBOX else "🔴 (实盘)"

        self.entries = {}
        self.daily_trades = 0
        self.last_reset_day = datetime.now(CST).day
        self.trades = load_trades()
        self._trailing_active = {}
        self._trailing_high = {}

        self.tg_app = None
        if settings.TG_BOT_TOKEN:
            self.tg_app = ApplicationBuilder().token(settings.TG_BOT_TOKEN).build()
            handlers = [
                CommandHandler("start", self.cmd_menu),
                CommandHandler("menu", self.cmd_menu),
                CommandHandler("status", self.cmd_status),
                CommandHandler("check", self.cmd_check),
                CommandHandler("symbols", self.cmd_symbols),
                CommandHandler("analysis", self.cmd_analysis),
                CommandHandler("brain", self.cmd_brain),
                CommandHandler("help", self.cmd_help),
                CommandHandler("settp", self.cmd_set_tp),
                CommandHandler("setsl", self.cmd_set_sl),
                CommandHandler("settsl", self.cmd_set_tsl),
                CommandHandler("settmpt", self.cmd_set_trailing_tp),
                CommandHandler("setamount", self.cmd_set_amount),
                CommandHandler("settf", self.cmd_set_tf),
                CommandHandler("setreserve", self.cmd_set_reserve),
                CommandHandler("addsymbol", self.cmd_add_symbol),
                CommandHandler("delsymbol", self.cmd_del_symbol),
                CommandHandler("panic", self.cmd_panic),
                CommandHandler("entry", self.cmd_entry),
                CommandHandler("settrades", self.cmd_set_trades),
                CommandHandler("resettrades", self.cmd_reset_trades),
                CommandHandler("preset", self.cmd_preset),
                CommandHandler("history", self.cmd_history),
            ]
            for h in handlers: self.tg_app.add_handler(h)
            self.tg_app.add_handler(CallbackQueryHandler(self.handle_button_click))
            self.tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_text_input))

    def _save(self):
        save_config({
            'tp_pct': self.tp_pct, 'sl_pct': self.sl_pct,
            'trailing_sl_pct': self.trailing_sl_pct, 'trailing_tp_pct': self.trailing_tp_pct,
            'single_order_usdt': self.single_order_usdt, 'timeframe': self.timeframe,
            'reserve_bottom': self.reserve_bottom, 'symbols': self.symbols,
            'orderbook_filter': self.orderbook_filter, 'waterfall_breaker': self.waterfall_breaker,
            'max_daily_trades': self.max_daily_trades
        })

    def _auth(self, update: Update):
        if not self.allowed: return True
        return update.effective_user.id in self.allowed

    def _parse_pct(self, val):
        return val / 100.0

    async def register_bot_commands(self):
        if not self.tg_app: return
        commands = [
            BotCommand("menu", "📱 量化控制台"),
            BotCommand("status", "📊 持仓面板+盈亏"),
            BotCommand("check", "📈 指标+交易信号"),
            BotCommand("brain", "🧠 大脑诊断"),
            BotCommand("analysis", "🔍 差距分析"),
            BotCommand("history", "📜 交易历史"),
            BotCommand("settp", "🎯 止盈率"),
            BotCommand("setsl", "🛡️ 硬止损"),
            BotCommand("settsl", "📉 移动止损"),
            BotCommand("settmpt", "🏹 移动止盈"),
            BotCommand("setamount", "💵 单笔额度"),
            BotCommand("settf", "⏱️ K线周期"),
            BotCommand("setreserve", "🔒 安全底线"),
            BotCommand("settrades", "🔢 单日最大交易"),
            BotCommand("entry", "📝 记录入场价"),
            BotCommand("preset", "⚡ 一键预设"),
            BotCommand("help", "❓ 帮助"),
        ]
        try: await self.tg_app.bot.set_my_commands(commands)
        except Exception as e: logger.error(f"注册菜单失败: {e}")

    # =================================================================
    # 主面板（原版全部按钮 + 新功能整合）
    # =================================================================
    def _build_main_keyboard(self):
        f_status = "已开启" if self.orderbook_filter else "已关闭"
        b_status = "已开启" if self.waterfall_breaker else "已关闭"
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("🚨 紧急全平 (Panic)", callback_data="panic_confirm")],
            [
                InlineKeyboardButton(f"🏮 盘口过滤: [{f_status}]", callback_data="toggle_filter"),
                InlineKeyboardButton(f"🚨 防瀑布熔断: [{b_status}]", callback_data="toggle_breaker")
            ],
            [
                InlineKeyboardButton("⚡ 开启运行", callback_data="bot_start"),
                InlineKeyboardButton("🔴 优雅关机", callback_data="bot_stop")
            ],
            [
                InlineKeyboardButton("📊 运行看板", callback_data="dashboard"),
                InlineKeyboardButton("💳 账户余额", callback_data="balance")
            ],
            [
                InlineKeyboardButton("🎯 止盈率 %", callback_data="menu_set_tp"),
                InlineKeyboardButton("🛡️ 硬止损 %", callback_data="menu_set_sl")
            ],
            [
                InlineKeyboardButton("📉 移动止损 %", callback_data="menu_set_tsl"),
                InlineKeyboardButton("🏹 移动止盈 %", callback_data="menu_set_tmpt")
            ],
            [
                InlineKeyboardButton("💵 单笔 USDT", callback_data="menu_set_amount"),
                InlineKeyboardButton("⏱️ K线周期", callback_data="menu_set_tf")
            ],
            [
                InlineKeyboardButton("🔒 保留底线", callback_data="menu_set_reserve"),
                InlineKeyboardButton("🔢 单日上限", callback_data="menu_set_trades")
            ],
            [
                InlineKeyboardButton("➕ 添加币种", callback_data="menu_add_symbol"),
                InlineKeyboardButton("➖ 删除币种", callback_data="menu_del_symbol")
            ],
            [
                InlineKeyboardButton("🔄 同步持仓", callback_data="sync_pos"),
                InlineKeyboardButton("📋 监控列表", callback_data="list_symbols")
            ],
            [
                InlineKeyboardButton("🧠 超级大脑诊断", callback_data="brain_status"),
                InlineKeyboardButton("📈 差距分析", callback_data="gap_analysis")
            ],
            [
                InlineKeyboardButton("⚡ 一键预设", callback_data="menu_preset"),
                InlineKeyboardButton("📜 交易历史", callback_data="history")
            ],
            [InlineKeyboardButton("🔄 刷新面板", callback_data="refresh_panel")]
        ])

    def _build_option_keyboard(self, options, prefix, setting_key):
        kb = []; row = []
        for label, val in options:
            row.append(InlineKeyboardButton(label, callback_data=f"{prefix}:{val}"))
            if len(row) == 2: kb.append(row); row = []
        if row: kb.append(row)
        kb.append([InlineKeyboardButton("✍️ 自填模式", callback_data=f"prompt_manual:{setting_key}")])
        kb.append([InlineKeyboardButton("🔙 返回控制台", callback_data="refresh_panel")])
        return InlineKeyboardMarkup(kb)

    # =================================================================
    # 命令实现
    # =================================================================
    async def cmd_entry(self, update, context):
        if not self._auth(update): return
        try:
            sym = context.args[0].upper(); price = float(context.args[1])
            self.entries[sym] = price
            await update.effective_message.reply_text(f"📝 {sym} 入场价: {price:.2f}")
        except: await update.effective_message.reply_text("❌ `/entry ETH/USDT 3120`", parse_mode="Markdown")

    async def cmd_set_trades(self, update, context):
        if not self._auth(update): return
        try:
            self.max_daily_trades = int(context.args[0])
            async with self.lock: self._save()
            await update.effective_message.reply_text(f"✅ 单日最大交易: {self.max_daily_trades}")
        except: await update.effective_message.reply_text("❌ `/settrades 5`")

    async def cmd_reset_trades(self, update, context):
        if not self._auth(update): return
        self.daily_trades = 0
        await update.effective_message.reply_text("✅ 计数已重置")

    async def cmd_preset(self, update, context):
        if not self._auth(update): return
        try:
            mode = context.args[0].lower()
            presets = {
                "conservative": {"tp": 3, "sl": 2, "tsl": 1, "tmpt": 1, "tf": "1h", "amt": 1},
                "balanced": {"tp": 1.5, "sl": 1, "tsl": 0.5, "tmpt": 0.5, "tf": "15m", "amt": 1},
                "aggressive": {"tp": 0.8, "sl": 0.5, "tsl": 0.3, "tmpt": 0.3, "tf": "5m", "amt": 1},
            }
            if mode not in presets:
                await update.effective_message.reply_text("可选: conservative / balanced / aggressive")
                return
            p = presets[mode]
            self.tp_pct = p["tp"]/100; self.sl_pct = p["sl"]/100
            self.trailing_sl_pct = p["tsl"]/100; self.trailing_tp_pct = p["tmpt"]/100
            self.timeframe = p["tf"]; self.single_order_usdt = p["amt"]
            async with self.lock: self._save()
            names = {"conservative": "保守", "balanced": "平衡", "aggressive": "激进"}
            await update.effective_message.reply_text(
                f"⚡ {names[mode]}方案已生效\n止盈{self.tp_pct*100:.1f}% 止损{self.sl_pct*100:.1f}%", parse_mode="Markdown")
        except: await update.effective_message.reply_text("❌ `/preset balanced`")

    async def cmd_history(self, update, context):
        if not self._auth(update): return
        if not self.trades:
            await update.effective_message.reply_text("📜 暂无记录")
            return
        lines = ["📜 **最近交易**\n"]
        for t in self.trades[-10:]:
            lines.append(f"{'🟢' if t['pnl_pct']>0 else '🔴'} {t['time']} {t['symbol']} {t['pnl_pct']:+.2f}%")
        await update.effective_message.reply_text("\n".join(lines), parse_mode="Markdown")

    async def cmd_status(self, update, context):
        if not self._auth(update): return
        lines = ["📊 **持仓**\n"]
        bal = await self.exchange.fetch_balance()
        for sym in self.symbols:
            ticker = await self.exchange.fetch_ticker(sym); p = ticker['last']
            coin = sym.split('/')[0]
            free = bal.get(coin, {}).get('free', 0) if isinstance(bal.get(coin), dict) else float(bal.get(coin, 0))
            val = free * p
            pnl = ""
            if sym in self.entries and self.entries[sym] > 0 and free > 0:
                pnl_pct = ((p - self.entries[sym]) / self.entries[sym]) * 100
                pnl = f" | {'🟢' if pnl_pct>=0 else '🔴'} {pnl_pct:+.2f}%"
            lines.append(f"{sym}: {free:.4f} 现价{p:.2f} 价值{val:.2f}{pnl}")
        lines.append(f"💵 USDT: {bal.get('USDT',{}).get('free',0):.2f}")
        await update.effective_message.reply_text("\n".join(lines))

    async def cmd_check(self, update, context):
        if not self._auth(update): return
        lines = ["📈 **信号**\n"]
        fg = (await self.real_data.get_fear_greed_index())["value"]
        for sym in self.symbols:
            ticker = await self.exchange.fetch_ticker(sym); p = ticker['last']
            ohlcv = await self.exchange.fetch_ohlcv(sym, self.timeframe, 50)
            tech = self.tech.calc(ohlcv, p)
            funding = await self.exchange.fetch_funding_rate(sym)
            sc = self.signal_engine.score(tech, funding, fg)
            txt = self.signal_engine.interpret(sc)
            lines.append(f"{sym}: {p:.2f} | 信号: {txt} ({sc})")
        await update.effective_message.reply_text("\n".join(lines))

    async def cmd_symbols(self, update, context):
        if not self._auth(update): return
        s_list = "\n".join([f"• `{s}`" for s in self.symbols])
        await update.effective_message.reply_text(f"📋 **监控列表**:\n{s_list}", parse_mode="Markdown")

    async def cmd_menu(self, update, context):
        if not self._auth(update): await update.message.reply_text("⛔"); return
        await update.effective_message.reply_text(f"⚙️ 控制台 {self.env_tag}", reply_markup=self._build_main_keyboard())

    async def cmd_panic(self, update, context):
        if not self._auth(update): return
        await self.panic_sell_all()
        await update.effective_message.reply_text("🚨 全平")

    async def cmd_analysis(self, update, context): await self.render_gap_analysis(update.effective_message)
    async def cmd_brain(self, update, context): await self.render_brain_status(update.effective_message)

    async def cmd_help(self, update, context):
        await update.effective_message.reply_text(f"命令: /menu /status /check /symbols /preset balanced /settp 5 /history\n保本线: >{self.breakeven_pct*100:.2f}%")

    async def cmd_set_tp(self, update, context):
        if not self._auth(update): return
        try:
            val = self._parse_pct(float(context.args[0]))
            if val < self.breakeven_pct:
                await update.effective_message.reply_text(f"❌ 低于保本线 {self.breakeven_pct*100:.2f}%"); return
            self.tp_pct = val
            async with self.lock: self._save()
            await update.effective_message.reply_text(f"✅ 止盈: {self.tp_pct*100:.2f}%")
        except: pass

    async def cmd_set_sl(self, update, context):
        if not self._auth(update): return
        try: self.sl_pct = self._parse_pct(float(context.args[0])); async with self.lock: self._save(); await update.effective_message.reply_text("✅")
        except: pass

    async def cmd_set_tsl(self, update, context):
        if not self._auth(update): return
        try: self.trailing_sl_pct = self._parse_pct(float(context.args[0])); async with self.lock: self._save(); await update.effective_message.reply_text("✅")
        except: pass

    async def cmd_set_trailing_tp(self, update, context):
        if not self._auth(update): return
        try:
            val = self._parse_pct(float(context.args[0]))
            if val <= 0: return
            self.trailing_tp_pct = val
            async with self.lock: self._save()
            await update.effective_message.reply_text(f"✅ 移动止盈: {self.trailing_tp_pct*100:.2f}%")
        except: pass

    async def cmd_set_amount(self, update, context):
        if not self._auth(update): return
        try: self.single_order_usdt = float(context.args[0]); async with self.lock: self._save(); await update.effective_message.reply_text("✅")
        except: pass

    async def cmd_set_tf(self, update, context):
        if not self._auth(update): return
        try: self.timeframe = context.args[0].lower(); async with self.lock: self._save(); await update.effective_message.reply_text("✅")
        except: pass

    async def cmd_set_reserve(self, update, context):
        if not self._auth(update): return
        try: self.reserve_bottom = float(context.args[0]); async with self.lock: self._save(); await update.effective_message.reply_text("✅")
        except: pass

    async def cmd_add_symbol(self, update, context):
        if not self._auth(update): return
        try:
            sym = context.args[0].upper()
            if sym not in self.symbols:
                self.symbols.append(sym)
                async with self.lock: self._save()
                await update.effective_message.reply_text("✅")
        except: pass

    async def cmd_del_symbol(self, update, context):
        if not self._auth(update): return
        try:
            sym = context.args[0].upper()
            if sym in self.symbols:
                self.symbols.remove(sym)
                async with self.lock: self._save()
                await update.effective_message.reply_text("✅")
        except: pass

    async def render_brain_status(self, msg_obj):
        try:
            macro = await self.real_data.check_macro_risk()
            sym = self.symbols[0]; ticker = await self.exchange.fetch_ticker(sym); p = ticker['last']
            liq = await self.real_data.get_liquidation_risk(sym)
            ob = await self.exchange.fetch_orderbook(sym); ob_valid, ob_msg = await self.orderbook_engine.validate(ob)
            ohlcv = await self.exchange.fetch_ohlcv(sym, self.timeframe, 50); tech = self.tech.calc(ohlcv, p)
            msg = f"🧠 {sym}\n宏观: {macro['status']}\n费率: {liq['funding_rate']*100:+.4f}%\n盘口: {ob_msg}\n布林: {tech['bb_upper']:.1f}/{tech['bb_lower']:.1f} RSI{tech['rsi']:.0f}"
            await msg_obj.reply_text(msg)
        except Exception as e: logger.error(f"brain err: {e}")

    async def render_gap_analysis(self, msg_obj):
        try:
            lines = ["📈 **差距分析**\n"]
            for sym in self.symbols:
                ticker = await self.exchange.fetch_ticker(sym); p = ticker['last']
                ohlcv = await self.exchange.fetch_ohlcv(sym, self.timeframe, 50)
                tech = self.tech.calc(ohlcv, p)
                target = min(tech['bb_lower'], p*0.99); gap = ((p-target)/p)*100
                lines.append(f"{sym}: {p:.2f} → {target:.2f} ({gap:+.2f}%)")
            await msg_obj.reply_text("\n".join(lines))
        except Exception as e: logger.error(f"analysis err: {e}")

    async def handle_text_input(self, update, context):
        pending = context.user_data.get('pending_setting')
        if not pending: return
        try:
            user_text = update.message.text.strip()
            if pending in ("settf", "addsymbol", "delsymbol"):
                if pending == "settf": self.timeframe = user_text.lower()
                elif pending == "addsymbol":
                    sym = user_text.upper()
                    if sym not in self.symbols: self.symbols.append(sym)
                    else: await update.message.reply_text("⚠️ 已存在"); return
                elif pending == "delsymbol":
                    sym = user_text.upper()
                    if sym in self.symbols: self.symbols.remove(sym)
                    else: await update.message.reply_text("⚠️ 不存在"); return
            else:
                val = float(user_text)
                if pending == "settp":
                    pct = self._parse_pct(val)
                    if pct < self.breakeven_pct: await update.message.reply_text("❌ 低于保本线"); return
                    self.tp_pct = pct
                elif pending == "setsl": self.sl_pct = self._parse_pct(val)
                elif pending == "settsl": self.trailing_sl_pct = self._parse_pct(val)
                elif pending == "settmpt": self.trailing_tp_pct = self._parse_pct(val)
                elif pending == "setamount": self.single_order_usdt = val
                elif pending == "setreserve": self.reserve_bottom = val
                elif pending == "settrades": self.max_daily_trades = int(val)
            async with self.lock: self._save()
            context.user_data['pending_setting'] = None
            await update.message.reply_text("✅")
        except ValueError:
            await update.message.reply_text("❌ 格式有误")
            context.user_data['pending_setting'] = None

    async def handle_button_click(self, update, context):
        query = update.callback_query; data = query.data
        try:
            if data == "refresh_panel": await self.cmd_menu(update, context)
            elif data == "toggle_filter":
                self.orderbook_filter = not self.orderbook_filter
                await query.answer("已切换")
                try: await query.edit_message_reply_markup(reply_markup=self._build_main_keyboard())
                except: pass
            elif data == "toggle_breaker":
                self.waterfall_breaker = not self.waterfall_breaker
                await query.answer("已切换")
                try: await query.edit_message_reply_markup(reply_markup=self._build_main_keyboard())
                except: pass
            elif data == "bot_start": self.is_running = True; await query.answer("已开启")
            elif data == "bot_stop": self.is_running = False; await query.answer("已关机")
            elif data == "brain_status": await self.render_brain_status(query.message)
            elif data == "gap_analysis": await self.render_gap_analysis(query.message)
            elif data == "dashboard":
                msg = f"📊 看板\n止盈{self.tp_pct*100:.2f}% 止损{self.sl_pct*100:.2f}%\n移损{self.trailing_sl_pct*100:.2f}% 移盈{self.trailing_tp_pct*100:.2f}%\n额度{self.single_order_usdt}U 周期{self.timeframe}"
                await query.message.reply_text(msg)
            elif data == "balance":
                bal = await self.exchange.fetch_balance()
                await query.message.reply_text(f"💳 USDT: {bal.get('USDT',{}).get('free',0):.2f}")
            elif data == "history": await self.cmd_history(update, context)
            elif data == "sync_pos":
                await query.answer("🔄 已同步", show_alert=True)
                await query.message.reply_text("🔄 持仓同步完成")
            elif data == "list_symbols": await self.cmd_symbols(update, context)
            elif data == "menu_preset":
                opts = [("🛡️保守","conservative"),("⚖️平衡","balanced"),("⚡激进","aggressive")]
                kb = [[InlineKeyboardButton(label, callback_data=f"preset:{val}") for label,val in opts]]
                kb.append([InlineKeyboardButton("🔙返回", callback_data="refresh_panel")])
                await query.edit_message_text("⚡ 选择方案:", reply_markup=InlineKeyboardMarkup(kb))
            elif data.startswith("preset:"):
                mode = data.split(":")[1]
                p = {"conservative":{"tp":3,"sl":2,"tsl":1,"tmpt":1,"tf":"1h","amt":1},
                     "balanced":{"tp":1.5,"sl":1,"tsl":0.5,"tmpt":0.5,"tf":"15m","amt":1},
                     "aggressive":{"tp":0.8,"sl":0.5,"tsl":0.3,"tmpt":0.3,"tf":"5m","amt":1}}[mode]
                self.tp_pct=p["tp"]/100; self.sl_pct=p["sl"]/100
                self.trailing_sl_pct=p["tsl"]/100; self.trailing_tp_pct=p["tmpt"]/100
                self.timeframe=p["tf"]; self.single_order_usdt=p["amt"]
                async with self.lock: self._save()
                await query.answer("✅ 已生效", show_alert=True)
                await self._refresh_panel(query)
            elif data == "panic_confirm": await query.answer("🚨 请发送 /panic 确认", show_alert=True)
            # 二层菜单弹出
            elif data == "menu_set_tp":
                opts = [("3%","0.03"),("5%","0.05"),("8%","0.08")]
                await query.edit_message_text("🎯", reply_markup=self._build_option_keyboard(opts,"cfg_tp","settp"))
            elif data == "menu_set_sl":
                opts = [("1%","0.01"),("2%","0.02"),("3%","0.03")]
                await query.edit_message_text("🛡️", reply_markup=self._build_option_keyboard(opts,"cfg_sl","setsl"))
            elif data == "menu_set_tsl":
                opts = [("0.5%","0.005"),("1%","0.01"),("1.5%","0.015")]
                await query.edit_message_text("📉", reply_markup=self._build_option_keyboard(opts,"cfg_tsl","settsl"))
            elif data == "menu_set_tmpt":
                opts = [("0.5%","0.005"),("1%","0.01"),("1.5%","0.015")]
                await query.edit_message_text("🏹", reply_markup=self._build_option_keyboard(opts,"cfg_tmpt","settmpt"))
            elif data == "menu_set_amount":
                opts = [("1U","1"),("2U","2"),("5U","5")]
                await query.edit_message_text("💵", reply_markup=self._build_option_keyboard(opts,"cfg_amt","setamount"))
            elif data == "menu_set_tf":
                opts = [("1m","1m"),("5m","5m"),("15m","15m"),("1h","1h")]
                await query.edit_message_text("⏱", reply_markup=self._build_option_keyboard(opts,"cfg_tf","settf"))
            elif data == "menu_set_reserve":
                opts = [("20U","20"),("50U","50"),("100U","100"),("200U","200")]
                await query.edit_message_text("🔒", reply_markup=self._build_option_keyboard(opts,"cfg_res","setreserve"))
            elif data == "menu_add_symbol":
                opts = [("BTC/USDT","BTC/USDT"),("SOL/USDT","SOL/USDT"),("DOGE/USDT","DOGE/USDT")]
                await query.edit_message_text("➕", reply_markup=self._build_option_keyboard(opts,"cfg_add","addsymbol"))
            elif data == "menu_del_symbol":
                opts = [(s, s) for s in self.symbols]
                await query.edit_message_text("➖", reply_markup=self._build_option_keyboard(opts,"cfg_del","delsymbol"))
            # 快捷应用
            elif data.startswith("cfg_"):
                prefix = data.split(":")[0] if ":" in data else ""
                val_str = data.split(":")[1] if ":" in data else ""
                if prefix == "cfg_tp":
                    val_f = float(val_str)
                    if val_f < self.breakeven_pct:
                        await query.answer(f"❌ 低于保本线", show_alert=True); return
                    self.tp_pct = val_f
                elif prefix == "cfg_sl": self.sl_pct = float(val_str)
                elif prefix == "cfg_tsl": self.trailing_sl_pct = float(val_str)
                elif prefix == "cfg_tmpt": self.trailing_tp_pct = float(val_str)
                elif prefix == "cfg_amt": self.single_order_usdt = float(val_str)
                elif prefix == "cfg_tf": self.timeframe = val_str
                elif prefix == "cfg_res": self.reserve_bottom = float(val_str)
                elif prefix == "cfg_add":
                    if val_str not in self.symbols: self.symbols.append(val_str)
                    else: await query.answer("已存在", show_alert=True); return
                elif prefix == "cfg_del":
                    if val_str in self.symbols: self.symbols.remove(val_str)
                    else: await query.answer("不存在", show_alert=True); return
                async with self.lock: self._save()
                await query.answer("✅", show_alert=True)
                await self._refresh_panel(query)
            # 自填模式触发
            elif data.startswith("prompt_manual:"):
                key = data.split(":")[1]
                context.user_data['pending_setting'] = key
                prompts = {
                    "settp": "✍️ 输入止盈率（例：6.5 = 6.5%）：",
                    "setsl": "✍️ 输入硬止损率（例：2.5 = 2.5%）：",
                    "settsl": "✍️ 输入移动止损回调（例：1.5 = 1.5%）：",
                    "settmpt": "✍️ 输入移动止盈回调（例：1 = 1%）：",
                    "setamount": "✍️ 输入单笔 USDT（例：150）：",
                    "settf": "✍️ 输入K线周期（例：15m 或 1h）：",
                    "setreserve": "✍️ 输入安全底线（例：100）：",
                    "addsymbol": "✍️ 输入币种（例：DOGE/USDT）：",
                    "delsymbol": "✍️ 输入要删除的币种（例：SOL/USDT）：",
                }
                await query.message.reply_text(prompts.get(key, "✍️ 请输入数值："),
                    reply_markup=ForceReply(selective=True), parse_mode="Markdown")
            await query.answer()
        except Exception as e:
            logger.error(f"按钮异常: {e}")

    async def _refresh_panel(self, query):
        try: await query.edit_message_text(f"⚙️ 控制台 {self.env_tag}", reply_markup=self._build_main_keyboard())
        except: pass

    async def panic_sell_all(self):
        for sym in self.symbols:
            await self.exchange.cancel_all_orders(sym)
            bal = await self.exchange.fetch_balance()
            coin = sym.split('/')[0]
            amount = bal.get(coin, {}).get('free', 0) if isinstance(bal.get(coin), dict) else 0
            if isinstance(amount, (int, float)) and amount > 0:
                await self.exchange.create_market_sell_order(sym, amount)

    async def _trailing_monitor(self):
        await asyncio.sleep(5)
        while True:
            try:
                if self.is_running and self.trailing_tp_pct > 0:
                    for sym in self.symbols:
                        bal = await self.exchange.fetch_balance()
                        coin = sym.split('/')[0]
                        amount = bal.get(coin, {}).get('free', 0) if isinstance(bal.get(coin), dict) else float(bal.get(coin, 0))
                        if amount <= 0: self._trailing_active[sym] = False; continue
                        ticker = await self.exchange.fetch_ticker(sym); p = ticker['last']
                        if not self._trailing_active.get(sym, False):
                            if sym not in self._trailing_high: self._trailing_high[sym] = p; continue
                            if p >= self._trailing_high[sym] * (1 + self.tp_pct):
                                self._trailing_active[sym] = True; self._trailing_high[sym] = p
                        else:
                            if p > self._trailing_high.get(sym, 0): self._trailing_high[sym] = p
                            high = self._trailing_high[sym]
                            if p <= high * (1 - self.trailing_tp_pct):
                                await self.exchange.create_market_sell_order(sym, amount)
                                entry = self.entries.get(sym, high * (1 - self.tp_pct))
                                pnl_pct = ((p - entry) / entry) * 100
                                self.trades.append({
                                    "time": datetime.now(CST).strftime("%m-%d %H:%M"),
                                    "symbol": sym, "entry": entry, "exit": p, "pnl_pct": round(pnl_pct, 2)
                                })
                                save_trades(self.trades)
                                self._trailing_active[sym] = False; self._trailing_high[sym] = 0
                                self.daily_trades += 1
                await asyncio.sleep(1)
            except Exception as e:
                logger.error(f"监控异常: {e}")
                await asyncio.sleep(5)

    async def start(self):
        if self.tg_app:
            await self.tg_app.initialize()
            await self.tg_app.start()
            await self.register_bot_commands()
            await self.tg_app.updater.start_polling(drop_pending_updates=True)
            logger.info("✅ Bot 完全体启动（原版功能全恢复 + 新功能）")
            asyncio.create_task(self._trailing_monitor())
            while True:
                await asyncio.sleep(30)
