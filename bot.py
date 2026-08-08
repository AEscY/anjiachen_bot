"""
bot.py - 超前思维优化版（盈亏追踪 + 交易信号 + 风险控制 + 预设方案 + 交易日志）
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

# =================================================================
# 交易历史记录
# =================================================================
def load_trades():
    try:
        with open(TRADES_FILE, 'r') as f:
            return json.load(f)
    except:
        return []

def save_trades(trades):
    try:
        with open(TRADES_FILE, 'w') as f:
            json.dump(trades[-50:], f)  # 只保留最近50条
    except Exception as e:
        logger.error(f"保存交易记录失败: {e}")

# =================================================================
# 真实数据引擎
# =================================================================
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

# =================================================================
# 交易信号引擎（综合评分）
# =================================================================
class SignalEngine:
    @staticmethod
    def score(tech, funding_rate, fear_greed):
        """0-100 买入评分，越高越值得买"""
        score = 50
        # RSI 加分：超卖加分，超买减分
        rsi = tech['rsi']
        if rsi < 30: score += 25
        elif rsi < 40: score += 15
        elif rsi < 50: score += 5
        elif rsi > 70: score -= 20
        elif rsi > 60: score -= 10
        # 布林带位置加分：接近下轨加分
        bb_lower = tech['bb_lower']; price = tech['bb_middle']
        bb_position = (price - bb_lower) / (tech['bb_upper'] - bb_lower) if tech['bb_upper'] != bb_lower else 0.5
        if bb_position < 0.2: score += 20
        elif bb_position < 0.35: score += 10
        elif bb_position > 0.8: score -= 15
        # 资金费率加分：负费率加分（多头有优势）
        if funding_rate < -0.0005: score += 10
        elif funding_rate < 0: score += 5
        elif funding_rate > 0.001: score -= 10
        # 恐惧指数加分：极度恐惧时是抄底机会
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

# =================================================================
# 主机器人
# =================================================================
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

        # 盈亏追踪
        self.entries = {}          # symbol -> float 入场均价
        # 风险控制
        self.daily_trades = 0
        self.last_reset_day = datetime.now(CST).day
        # 交易日志
        self.trades = load_trades()
        # 移动止盈状态
        self._trailing_active = {}
        self._trailing_high = {}

        self.tg_app = None
        if settings.TG_BOT_TOKEN:
            self.tg_app = ApplicationBuilder().token(settings.TG_BOT_TOKEN).build()
            self.tg_app.add_handler(CommandHandler("start", self.cmd_menu))
            self.tg_app.add_handler(CommandHandler("menu", self.cmd_menu))
            self.tg_app.add_handler(CommandHandler("status", self.cmd_status))
            self.tg_app.add_handler(CommandHandler("check", self.cmd_check))
            self.tg_app.add_handler(CommandHandler("symbols", self.cmd_symbols))
            self.tg_app.add_handler(CommandHandler("analysis", self.cmd_analysis))
            self.tg_app.add_handler(CommandHandler("brain", self.cmd_brain))
            self.tg_app.add_handler(CommandHandler("help", self.cmd_help))
            self.tg_app.add_handler(CommandHandler("settp", self.cmd_set_tp))
            self.tg_app.add_handler(CommandHandler("setsl", self.cmd_set_sl))
            self.tg_app.add_handler(CommandHandler("settsl", self.cmd_set_tsl))
            self.tg_app.add_handler(CommandHandler("settmpt", self.cmd_set_trailing_tp))
            self.tg_app.add_handler(CommandHandler("setamount", self.cmd_set_amount))
            self.tg_app.add_handler(CommandHandler("settf", self.cmd_set_tf))
            self.tg_app.add_handler(CommandHandler("setreserve", self.cmd_set_reserve))
            self.tg_app.add_handler(CommandHandler("addsymbol", self.cmd_add_symbol))
            self.tg_app.add_handler(CommandHandler("delsymbol", self.cmd_del_symbol))
            self.tg_app.add_handler(CommandHandler("panic", self.cmd_panic))
            # 新增命令
            self.tg_app.add_handler(CommandHandler("entry", self.cmd_entry))
            self.tg_app.add_handler(CommandHandler("settrades", self.cmd_set_trades))
            self.tg_app.add_handler(CommandHandler("resettrades", self.cmd_reset_trades))
            self.tg_app.add_handler(CommandHandler("preset", self.cmd_preset))
            self.tg_app.add_handler(CommandHandler("history", self.cmd_history))
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

    def _check_daily_limit(self):
        """检查每日交易限制"""
        today = datetime.now(CST).day
        if today != self.last_reset_day:
            self.daily_trades = 0
            self.last_reset_day = today
        if self.max_daily_trades > 0 and self.daily_trades >= self.max_daily_trades:
            return False
        return True

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

    def _build_main_keyboard(self):
        f_status = "已开启" if self.orderbook_filter else "已关闭"
        b_status = "已开启" if self.waterfall_breaker else "已关闭"
        limit_info = f"今日交易: {self.daily_trades}/{self.max_daily_trades}" if self.max_daily_trades > 0 else "无限制"
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("🚨 紧急全平", callback_data="panic_confirm")],
            [InlineKeyboardButton(f"🏮 盘口: [{f_status}]", callback_data="toggle_filter"),
             InlineKeyboardButton(f"🚨 熔断: [{b_status}]", callback_data="toggle_breaker")],
            [InlineKeyboardButton("⚡ 开启", callback_data="bot_start"),
             InlineKeyboardButton("🔴 关机", callback_data="bot_stop")],
            [InlineKeyboardButton("📊 看板", callback_data="dashboard"),
             InlineKeyboardButton("💳 余额", callback_data="balance")],
            [InlineKeyboardButton("🎯 止盈", callback_data="menu_set_tp"),
             InlineKeyboardButton("🛡️ 止损", callback_data="menu_set_sl")],
            [InlineKeyboardButton("📉 移损", callback_data="menu_set_tsl"),
             InlineKeyboardButton("🏹 移盈", callback_data="menu_set_tmpt")],
            [InlineKeyboardButton("💵 额度", callback_data="menu_set_amount"),
             InlineKeyboardButton("⏱ 周期", callback_data="menu_set_tf")],
            [InlineKeyboardButton("🧠 大脑", callback_data="brain_status"),
             InlineKeyboardButton("📈 分析", callback_data="gap_analysis")],
            [InlineKeyboardButton("⚡ 预设", callback_data="menu_preset"),
             InlineKeyboardButton("📜 历史", callback_data="history")],
            [InlineKeyboardButton("🔄 刷新", callback_data="refresh_panel")]
        ])

    def _build_option_keyboard(self, options, prefix, setting_key):
        kb = []; row = []
        for label, val in options:
            row.append(InlineKeyboardButton(label, callback_data=f"{prefix}:{val}"))
            if len(row) == 2: kb.append(row); row = []
        if row: kb.append(row)
        kb.append([InlineKeyboardButton("✍️ 自填", callback_data=f"prompt_manual:{setting_key}")])
        kb.append([InlineKeyboardButton("🔙 返回", callback_data="refresh_panel")])
        return InlineKeyboardMarkup(kb)

    # =================================================================
    # 新增命令
    # =================================================================
    async def cmd_entry(self, update, context):
        """记录入场价：/entry ETH/USDT 3120"""
        if not self._auth(update): return
        try:
            sym = context.args[0].upper()
            price = float(context.args[1])
            self.entries[sym] = price
            await update.effective_message.reply_text(f"📝 {sym} 入场价已记录: {price:.2f} USDT")
        except: await update.effective_message.reply_text("❌ 格式：`/entry ETH/USDT 3120`", parse_mode="Markdown")

    async def cmd_set_trades(self, update, context):
        """设置单日最大交易次数：/settrades 5"""
        if not self._auth(update): return
        try:
            self.max_daily_trades = int(context.args[0])
            async with self.lock: self._save()
            await update.effective_message.reply_text(f"✅ 单日最大交易: {self.max_daily_trades} 次")
        except: await update.effective_message.reply_text("❌ 格式：`/settrades 5`")

    async def cmd_reset_trades(self, update, context):
        """重置每日交易计数"""
        if not self._auth(update): return
        self.daily_trades = 0
        await update.effective_message.reply_text("✅ 交易计数已重置")

    async def cmd_preset(self, update, context):
        """一键预设方案"""
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
            self.tp_pct = p["tp"] / 100; self.sl_pct = p["sl"] / 100
            self.trailing_sl_pct = p["tsl"] / 100; self.trailing_tp_pct = p["tmpt"] / 100
            self.timeframe = p["tf"]; self.single_order_usdt = p["amt"]
            async with self.lock: self._save()
            names = {"conservative": "保守", "balanced": "平衡", "aggressive": "激进"}
            await update.effective_message.reply_text(
                f"⚡ **{names[mode]}方案已生效**\n"
                f"止盈: {self.tp_pct*100:.1f}% | 止损: {self.sl_pct*100:.1f}%\n"
                f"移动止损: {self.trailing_sl_pct*100:.2f}% | 移动止盈: {self.trailing_tp_pct*100:.2f}%\n"
                f"K线: {self.timeframe} | 单笔: {self.single_order_usdt}U", parse_mode="Markdown")
        except: await update.effective_message.reply_text("❌ 格式：`/preset balanced`")

    async def cmd_history(self, update, context):
        """查看交易历史"""
        if not self._auth(update): return
        if not self.trades:
            await update.effective_message.reply_text("📜 暂无交易记录")
            return
        lines = ["📜 **最近交易记录**\n━━━━━━━━━━━━━━━━━━"]
        for t in self.trades[-10:]:
            emoji = "🟢" if t['pnl_pct'] > 0 else "🔴"
            lines.append(f"{emoji} {t['time']} {t['symbol']}\n  入场: {t['entry']:.2f} → 出场: {t['exit']:.2f} | {t['pnl_pct']:+.2f}%")
        await update.effective_message.reply_text("\n".join(lines), parse_mode="Markdown")

    # =================================================================
    # 优化后的 /status（带盈亏）
    # =================================================================
    async def cmd_status(self, update, context):
        if not self._auth(update): return
        await update.effective_message.reply_chat_action("typing")
        lines = [f"📊 **持仓面板** {self.env_tag}\n━━━━━━━━━━━━━━━━━━"]
        bal = await self.exchange.fetch_balance()
        total_value = 0.0; total_pnl = 0.0
        for sym in self.symbols:
            ticker = await self.exchange.fetch_ticker(sym)
            price = ticker.get('last', 0)
            coin = sym.split('/')[0]
            free = 0
            if isinstance(bal.get(coin), dict): free = float(bal[coin].get('free', 0))
            elif coin in bal and isinstance(bal[coin], (int, float)): free = float(bal[coin])
            value = free * price
            total_value += value
            # 盈亏计算
            pnl_info = ""
            if sym in self.entries and self.entries[sym] > 0 and free > 0:
                entry_price = self.entries[sym]
                pnl_pct = ((price - entry_price) / entry_price) * 100
                pnl_amount = free * (price - entry_price)
                total_pnl += pnl_amount
                emoji = "🟢" if pnl_pct >= 0 else "🔴"
                pnl_info = f"  |  {emoji} {pnl_pct:+.2f}% ({pnl_amount:+.3f}U)"
            lines.append(f"🔹 {sym}: {free:.4f} | 现价 {price:.2f} | 价值 {value:.2f}U{pnl_info}")
        usdt_free = bal.get('USDT', {}).get('free', 0) if isinstance(bal.get('USDT'), dict) else float(bal.get('USDT', 0))
        total_value += usdt_free
        lines.append(f"━━━━━━━━━━━━━━━━━━\n💵 USDT: {usdt_free:.2f} | 💰 总估值: {total_value:.2f}U")
        if total_pnl != 0:
            emoji = "🟢" if total_pnl >= 0 else "🔴"
            lines.append(f"📈 浮动盈亏: {emoji} {total_pnl:+.3f}U")
        lines.append(f"🔒 底线: {self.reserve_bottom}U | 📋 今日交易: {self.daily_trades}/{self.max_daily_trades if self.max_daily_trades>0 else '∞'}")
        await update.effective_message.reply_text("\n".join(lines), parse_mode="Markdown")

    # =================================================================
    # 优化后的 /check（带交易信号评分）
    # =================================================================
    async def cmd_check(self, update, context):
        if not self._auth(update): return
        await update.effective_message.reply_chat_action("typing")
        lines = [f"📈 **实时指标+交易信号** {self.env_tag}\n━━━━━━━━━━━━━━━━━━"]
        fg_data = await self.real_data.get_fear_greed_index()
        fg_value = fg_data["value"]
        for sym in self.symbols:
            ticker = await self.exchange.fetch_ticker(sym)
            p = ticker['last']
            ohlcv = await self.exchange.fetch_ohlcv(sym, self.timeframe, 50)
            tech = self.tech.calc(ohlcv, p)
            funding = await self.exchange.fetch_funding_rate(sym)
            # 综合评分
            sig_score = self.signal_engine.score(tech, funding, fg_value)
            sig_text = self.signal_engine.interpret(sig_score)
            target = min(tech['bb_lower'], p * 0.99)
            gap = ((p - target) / p) * 100
            bal = await self.exchange.fetch_balance()
            coin = sym.split('/')[0]
            holding = bal.get(coin, {}).get('free', 0) if isinstance(bal.get(coin), dict) else float(bal.get(coin, 0))
            hold_info = f"持仓 {holding:.4f}" if holding > 0 else "空仓"
            lines.append(
                f"🔹 **{sym}**: {p:.2f} | {hold_info}\n"
                f"   信号: {sig_text} (评分 {sig_score})\n"
                f"   布林下轨: {tech['bb_lower']:.2f} | RSI: {tech['rsi']:.1f} | ATR: {tech['atr']:.2f}\n"
                f"   距买点: {gap:+.2f}% | 保本卖价: {p*(1+self.breakeven_pct):.2f}"
            )
        lines.append(f"💡 恐惧贪婪指数: {fg_value} | 评分>65可考虑买入")
        await update.effective_message.reply_text("\n".join(lines), parse_mode="Markdown")

    # =================================================================
    # 其余命令（保持原有，略）
    # =================================================================
    async def cmd_menu(self, update, context):
        if not self._auth(update): await update.message.reply_text("⛔ 未授权"); return
        status = "🟢 运行中" if self.is_running else "🔴 停止"
        now = datetime.now(CST).strftime("%H:%M:%S")
        msg = f"⚙️ **量化控制台 {self.env_tag}**\n状态: {status}\n⏱ {now}"
        await update.effective_message.reply_text(msg, reply_markup=self._build_main_keyboard(), parse_mode="Markdown")

    async def cmd_symbols(self, update, context):
        if not self._auth(update): return
        s_list = "\n".join([f"• `{s}`" for s in self.symbols])
        await update.effective_message.reply_text(f"📋 **监控列表**:\n{s_list}", parse_mode="Markdown")

    async def cmd_panic(self, update, context):
        if not self._auth(update): await update.message.reply_text("⛔ 未授权"); return
        await self.panic_sell_all()
        await update.effective_message.reply_text("🚨 全平已执行")

    async def cmd_analysis(self, update, context):
        if not self._auth(update): return
        await self.render_gap_analysis(update.effective_message)

    async def cmd_brain(self, update, context):
        if not self._auth(update): return
        await self.render_brain_status(update.effective_message)

    async def cmd_help(self, update, context):
        msg = ("💡 **命令清单**\n━━━━━━━━━━━━━━━━━━\n"
               "/menu /status /check /brain /analysis\n"
               "/settp 5 /setsl 2 /settsl 0.5 /settmpt 1\n"
               "/setamount 1 /settf 15m /setreserve 1\n"
               "/entry ETH/USDT 3120 → 记录入场价\n"
               "/settrades 5 → 单日最大交易\n"
               "/preset balanced → 一键预设\n"
               "/history → 交易记录\n"
               f"保本线: >{self.breakeven_pct*100:.2f}%")
        await update.effective_message.reply_text(msg, parse_mode="Markdown")

    async def cmd_set_tp(self, update, context):
        if not self._auth(update): return
        try:
            val = self._parse_pct(float(context.args[0]))
            if val < self.breakeven_pct:
                await update.effective_message.reply_text(
                    f"❌ **{val*100:.2f}%** 低于保本线 **{self.breakeven_pct*100:.2f}%**", parse_mode="Markdown")
                return
            self.tp_pct = val; async with self.lock: self._save()
            await update.effective_message.reply_text(f"✅ 止盈: {self.tp_pct*100:.2f}%")
        except: await update.effective_message.reply_text("❌ 格式：`/settp 5`")

    async def cmd_set_sl(self, update, context):
        if not self._auth(update): return
        try:
            self.sl_pct = self._parse_pct(float(context.args[0]))
            async with self.lock: self._save()
            await update.effective_message.reply_text(f"✅ 止损: {self.sl_pct*100:.1f}%")
        except: await update.effective_message.reply_text("❌ 格式：`/setsl 2`")

    async def cmd_set_tsl(self, update, context):
        if not self._auth(update): return
        try:
            self.trailing_sl_pct = self._parse_pct(float(context.args[0]))
            async with self.lock: self._save()
            await update.effective_message.reply_text(f"✅ 移动止损: {self.trailing_sl_pct*100:.1f}%")
        except: await update.effective_message.reply_text("❌ 格式：`/settsl 0.5`")

    async def cmd_set_trailing_tp(self, update, context):
        if not self._auth(update): return
        try:
            val = self._parse_pct(float(context.args[0]))
            if val <= 0:
                await update.effective_message.reply_text("❌ 必须 > 0%"); return
            self.trailing_tp_pct = val; async with self.lock: self._save()
            await update.effective_message.reply_text(f"✅ 移动止盈: {self.trailing_tp_pct*100:.2f}%")
        except: await update.effective_message.reply_text("❌ 格式：`/settmpt 1`")

    async def cmd_set_amount(self, update, context):
        if not self._auth(update): return
        try:
            self.single_order_usdt = float(context.args[0])
            async with self.lock: self._save()
            await update.effective_message.reply_text(f"✅ 单笔: {self.single_order_usdt}U")
        except: await update.effective_message.reply_text("❌ 格式：`/setamount 1`")

    async def cmd_set_tf(self, update, context):
        if not self._auth(update): return
        try:
            self.timeframe = context.args[0].lower()
            async with self.lock: self._save()
            await update.effective_message.reply_text(f"✅ 周期: {self.timeframe}")
        except: await update.effective_message.reply_text("❌ 格式：`/settf 15m`")

    async def cmd_set_reserve(self, update, context):
        if not self._auth(update): return
        try:
            self.reserve_bottom = float(context.args[0])
            async with self.lock: self._save()
            await update.effective_message.reply_text(f"✅ 底线: {self.reserve_bottom}U")
        except: await update.effective_message.reply_text("❌ 格式：`/setreserve 1`")

    async def cmd_add_symbol(self, update, context):
        if not self._auth(update): return
        try:
            sym = context.args[0].upper()
            if sym not in self.symbols:
                self.symbols.append(sym)
                async with self.lock: self._save()
                await update.effective_message.reply_text(f"➕ {sym}")
            else: await update.effective_message.reply_text(f"⚠️ {sym} 已存在")
        except: await update.effective_message.reply_text("❌ 格式：`/addsymbol SOL/USDT`")

    async def cmd_del_symbol(self, update, context):
        if not self._auth(update): return
        try:
            sym = context.args[0].upper()
            if sym in self.symbols:
                self.symbols.remove(sym)
                async with self.lock: self._save()
                await update.effective_message.reply_text(f"➖ {sym}")
            else: await update.effective_message.reply_text(f"⚠️ {sym} 不存在")
        except: await update.effective_message.reply_text("❌ 格式：`/delsymbol SOL/USDT`")

    # =================================================================
    # 渲染（保持原有）
    # =================================================================
    async def render_brain_status(self, msg_obj):
        try:
            macro = await self.real_data.check_macro_risk()
            sym = self.symbols[0]
            ticker = await self.exchange.fetch_ticker(sym); price = ticker['last']
            liq = await self.real_data.get_liquidation_risk(sym)
            ob = await self.exchange.fetch_orderbook(sym)
            ob_valid, ob_msg = await self.orderbook_engine.validate(ob)
            ohlcv = await self.exchange.fetch_ohlcv(sym, self.timeframe, 50)
            tech = self.tech.calc(ohlcv, price)
            msg = (f"🧠 **大脑诊断** {self.env_tag}\n"
                   f"━━━━━━━━━━━━━━━━━━\n"
                   f"1️⃣ 宏观: {macro['status']}\n"
                   f"2️⃣ 费率: {liq['funding_rate']*100:+.4f}% | 多空比: {liq['long_short_ratio']:.2f}\n"
                   f"3️⃣ 盘口: {'✅ ' + ob_msg if ob_valid else '⚠️ ' + ob_msg}\n"
                   f"4️⃣ {sym}: 布林 {tech['bb_upper']:.1f}/{tech['bb_lower']:.1f} RSI {tech['rsi']:.0f} ATR {tech['atr']:.2f}")
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 刷新", callback_data="brain_status")],
                [InlineKeyboardButton("🔙 返回", callback_data="refresh_panel")]
            ])
            try: await msg_obj.edit_text(msg, reply_markup=kb, parse_mode="Markdown")
            except: await msg_obj.reply_text(msg, reply_markup=kb, parse_mode="Markdown")
        except Exception as e: logger.error(f"大脑异常: {e}")

    async def render_gap_analysis(self, msg_obj):
        try:
            lines = ["📈 **差距分析**\n━━━━━━━━━━━━━━━━━━"]
            for sym in self.symbols:
                ticker = await self.exchange.fetch_ticker(sym); p = ticker['last']
                ohlcv = await self.exchange.fetch_ohlcv(sym, self.timeframe, 50)
                tech = self.tech.calc(ohlcv, p)
                target = min(tech['bb_lower'], p*0.99); gap = ((p-target)/p)*100
                icon = "🔥" if gap < 0.5 else "📉"
                lines.append(f"🔹 {sym}: {p:.2f} → {target:.2f} ({gap:+.2f}%) {icon}")
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 刷新", callback_data="gap_analysis")],
                [InlineKeyboardButton("🔙 返回", callback_data="refresh_panel")]
            ])
            try: await msg_obj.edit_text("\n".join(lines), reply_markup=kb, parse_mode="Markdown")
            except: await msg_obj.reply_text("\n".join(lines), reply_markup=kb, parse_mode="Markdown")
        except Exception as e: logger.error(f"分析异常: {e}")

    # =================================================================
    # 按钮回调（简化但保留核心）
    # =================================================================
    async def handle_button_click(self, update, context):
        query = update.callback_query; data = query.data
        try:
            if data == "refresh_panel": await self.cmd_menu(update, context)
            elif data == "toggle_filter":
                self.orderbook_filter = not self.orderbook_filter
                await query.answer(f"盘口过滤{'开启' if self.orderbook_filter else '关闭'}")
                try: await query.edit_message_reply_markup(reply_markup=self._build_main_keyboard())
                except: pass
            elif data == "toggle_breaker":
                self.waterfall_breaker = not self.waterfall_breaker
                await query.answer(f"熔断{'开启' if self.waterfall_breaker else '关闭'}")
                try: await query.edit_message_reply_markup(reply_markup=self._build_main_keyboard())
                except: pass
            elif data == "bot_start": self.is_running = True; await query.answer("已开启")
            elif data == "bot_stop": self.is_running = False; await query.answer("已关机")
            elif data == "brain_status": await self.render_brain_status(query.message)
            elif data == "gap_analysis": await self.render_gap_analysis(query.message)
            elif data == "dashboard":
                msg = (f"📊 **看板**\n"
                       f"止盈: {self.tp_pct*100:.2f}% | 止损: {self.sl_pct*100:.2f}%\n"
                       f"移损: {self.trailing_sl_pct*100:.2f}% | 移盈: {self.trailing_tp_pct*100:.2f}%\n"
                       f"额度: {self.single_order_usdt}U | 周期: {self.timeframe} | 底线: {self.reserve_bottom}U\n"
                       f"费率: {self.taker_fee*100:.2f}% | 保本: >{self.breakeven_pct*100:.2f}%\n"
                       f"今日交易: {self.daily_trades}/{self.max_daily_trades if self.max_daily_trades>0 else '∞'}")
                await query.message.reply_text(msg, parse_mode="Markdown")
            elif data == "balance":
                bal = await self.exchange.fetch_balance()
                usdt = bal.get('USDT',{}).get('free',0)
                await query.message.reply_text(f"💳 USDT: {usdt:.2f}")
            elif data == "history": await self.cmd_history(update, context)
            elif data == "menu_preset":
                opts = [("🛡️ 保守", "conservative"), ("⚖️ 平衡", "balanced"), ("⚡ 激进", "aggressive")]
                kb = [[InlineKeyboardButton(label, callback_data=f"preset:{val}") for label, val in opts]]
                kb.append([InlineKeyboardButton("🔙 返回", callback_data="refresh_panel")])
                await query.edit_message_text("⚡ 选择预设方案:", reply_markup=InlineKeyboardMarkup(kb))
            elif data.startswith("preset:"):
                mode = data.split(":")[1]
                presets = {
                    "conservative": {"tp": 3, "sl": 2, "tsl": 1, "tmpt": 1, "tf": "1h", "amt": 1},
                    "balanced": {"tp": 1.5, "sl": 1, "tsl": 0.5, "tmpt": 0.5, "tf": "15m", "amt": 1},
                    "aggressive": {"tp": 0.8, "sl": 0.5, "tsl": 0.3, "tmpt": 0.3, "tf": "5m", "amt": 1},
                }
                p = presets[mode]
                self.tp_pct = p["tp"]/100; self.sl_pct = p["sl"]/100
                self.trailing_sl_pct = p["tsl"]/100; self.trailing_tp_pct = p["tmpt"]/100
                self.timeframe = p["tf"]; self.single_order_usdt = p["amt"]
                async with self.lock: self._save()
                names = {"conservative": "保守", "balanced": "平衡", "aggressive": "激进"}
                await query.answer(f"{names[mode]}方案已生效", show_alert=True)
                await self._refresh_panel(query)
            elif data == "panic_confirm":
                await query.answer("🚨 请发送 /panic 确认", show_alert=True)
            # 二层菜单（保留）
            elif data == "menu_set_tp":
                opts = [("3%","0.03"),("5%","0.05"),("8%","0.08")]
                await query.edit_message_text("🎯 止盈率:", reply_markup=self._build_option_keyboard(opts,"cfg_tp","settp"))
            elif data == "menu_set_sl":
                opts = [("1%","0.01"),("2%","0.02"),("3%","0.03")]
                await query.edit_message_text("🛡️ 止损:", reply_markup=self._build_option_keyboard(opts,"cfg_sl","setsl"))
            elif data == "menu_set_tsl":
                opts = [("0.5%","0.005"),("1%","0.01"),("1.5%","0.015")]
                await query.edit_message_text("📉 移损:", reply_markup=self._build_option_keyboard(opts,"cfg_tsl","settsl"))
            elif data == "menu_set_tmpt":
                opts = [("0.5%","0.005"),("1%","0.01"),("1.5%","0.015")]
                await query.edit_message_text("🏹 移盈:", reply_markup=self._build_option_keyboard(opts,"cfg_tmpt","settmpt"))
            elif data == "menu_set_amount":
                opts = [("1U","1"),("2U","2"),("5U","5")]
                await query.edit_message_text("💵 额度:", reply_markup=self._build_option_keyboard(opts,"cfg_amt","setamount"))
            elif data == "menu_set_tf":
                opts = [("1m","1m"),("5m","5m"),("15m","15m"),("1h","1h")]
                await query.edit_message_text("⏱ 周期:", reply_markup=self._build_option_keyboard(opts,"cfg_tf","settf"))
            # 快捷应用
            elif data.startswith("cfg_"):
                key_map = {"cfg_tp":("tp_pct",None), "cfg_sl":("sl_pct",None),
                          "cfg_tsl":("trailing_sl_pct",None), "cfg_tmpt":("trailing_tp_pct",None),
                          "cfg_amt":("single_order_usdt",None), "cfg_tf":("timeframe",None)}
                prefix = "_".join(data.split("_")[:2])
                if prefix in key_map:
                    attr, _ = key_map[prefix]
                    val = data.split(":")[1]
                    if attr == "timeframe": setattr(self, attr, val)
                    else:
                        val_f = float(val)
                        if attr == "tp_pct" and val_f < self.breakeven_pct:
                            await query.answer(f"❌ 低于保本线", show_alert=True); return
                        setattr(self, attr, val_f)
                    async with self.lock: self._save()
                    await query.answer("✅ 已修改", show_alert=True)
                    await self._refresh_panel(query)
            await query.answer()
        except Exception as e: logger.error(f"按钮异常: {e}")

    async def handle_text_input(self, update, context):
        pending = context.user_data.get('pending_setting')
        if not pending: return
        user_text = update.message.text.strip()
        try:
            if pending == "settp":
                val = self._parse_pct(float(user_text))
                if val < self.breakeven_pct:
                    await update.message.reply_text(f"❌ 低于保本线 {self.breakeven_pct*100:.2f}%")
                    context.user_data['pending_setting'] = None; return
                self.tp_pct = val
            elif pending == "setsl": self.sl_pct = self._parse_pct(float(user_text))
            elif pending == "settsl": self.trailing_sl_pct = self._parse_pct(float(user_text))
            elif pending == "settmpt":
                val = self._parse_pct(float(user_text))
                if val <= 0: await update.message.reply_text("❌ 必须>0"); return
                self.trailing_tp_pct = val
            elif pending == "setamount": self.single_order_usdt = float(user_text)
            elif pending == "settf": self.timeframe = user_text.lower()
            async with self.lock: self._save()
            context.user_data['pending_setting'] = None
            await update.message.reply_text(f"✅ 已修改")
        except ValueError:
            await update.message.reply_text("❌ 格式有误")
            context.user_data['pending_setting'] = None

    async def _refresh_panel(self, query):
        status = "🟢 运行中" if self.is_running else "🔴 停止"
        msg = f"⚙️ **控制台 {self.env_tag}**\n状态: {status}"
        try: await query.edit_message_text(msg, reply_markup=self._build_main_keyboard(), parse_mode="Markdown")
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
                        ticker = await self.exchange.fetch_ticker(sym)
                        p = ticker['last']
                        if not self._trailing_active.get(sym, False):
                            if sym not in self._trailing_high: self._trailing_high[sym] = p; continue
                            if p >= self._trailing_high[sym] * (1 + self.tp_pct):
                                self._trailing_active[sym] = True; self._trailing_high[sym] = p
                        else:
                            if p > self._trailing_high.get(sym, 0): self._trailing_high[sym] = p
                            high = self._trailing_high[sym]
                            if p <= high * (1 - self.trailing_tp_pct):
                                await self.exchange.create_market_sell_order(sym, amount)
                                # 记录交易日志
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
            await self.tg_app.initialize(); await self.tg_app.start()
            await self.register_bot_commands()
            await self.tg_app.updater.start_polling(drop_pending_updates=True)
            logger.info("✅ Bot 超前思维版启动")
            asyncio.create_task(self._trailing_monitor())
            while True: await asyncio.sleep(30)
