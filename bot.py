"""
bot.py - 最终完整版（动态网格、多重熔断、学习系统、仪表盘、完整按钮）
"""
import asyncio, random, aiohttp, base64, os
from datetime import datetime, timezone, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ForceReply
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, MessageHandler, ContextTypes, filters
from config import settings, logger
from indicators import TechnicalEngine
from storage import (init_db, load_config, save_config, load_trades, save_trade,
                     save_trade_detail, get_recent_performance, get_today_trades,
                     export_db_to_json)

CST = timezone(timedelta(hours=8))


# ---------- 辅助函数 ----------
def _safe_get(obj, key, default=None):
    if isinstance(obj, dict):
        return obj.get(key, default)
    return default


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
                async with session.get("https://api.alternative.me/fng/?limit=1",
                                       timeout=aiohttp.ClientTimeout(total=5)) as resp:
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
        if now - self._fear_greed_cache["timestamp"] > 1800:
            return None
        return self._fear_greed_cache

    async def check_macro_risk(self):
        fg = await self.get_fear_greed_index()
        if fg is None:
            return {'is_safe': True, 'score': 0.5, 'status': "⚠️ 数据缺失"}
        value = fg["value"]
        if value < 25: return {'is_safe': False, 'score': value/100, 'status': f"🚨 极度恐惧 ({value})"}
        elif value > 75: return {'is_safe': False, 'score': value/100, 'status': f"⚠️ 极度贪婪 ({value})"}
        return {'is_safe': True, 'score': value/100, 'status': f"🟢 {fg['classification']} ({value})"}

    async def get_liquidation_risk(self, symbol):
        funding_rate = await self.exchange.fetch_funding_rate(symbol)
        long_short_ratio = await self.exchange.fetch_long_short_ratio(symbol)
        if long_short_ratio is None:
            long_short_ratio = 1.0
        ticker = await self.exchange.fetch_ticker(symbol)
        if ticker is None:
            return None
        p = ticker['last']
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
        if orderbook is None: return False, "盘口数据缺失"
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
        if funding_rate is not None:
            if funding_rate < -0.0005: score += 10
            elif funding_rate < 0: score += 5
            elif funding_rate > 0.001: score -= 10
        if fear_greed is not None:
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
        self.tech = TechnicalEngine(exchange)
        self.real_data = RealDataEngine(exchange)
        self.orderbook_engine = OrderbookEngine()
        self.signal_engine = SignalEngine()
        self.lock = asyncio.Lock()

        self.is_running = True
        self.orderbook_filter = True
        self.waterfall_breaker = True
        self.symbols = [settings.SYMBOL, "BTC/USDT", "SOL/USDT"]
        self.tp_pct = 0.08; self.sl_pct = 0.05; self.trailing_sl_pct = 0.02; self.trailing_tp_pct = 0.01
        self.single_order_usdt = 100; self.timeframe = "15m"; self.reserve_bottom = 50
        self.max_daily_trades = 0; self.auto_trade_enabled = False; self.auto_min_score = 75
        self.max_per_coin_usdt = 0; self.max_daily_loss_pct = 0.05
        self.max_total_allocated_pct = 0.8
        self.max_drawdown_pct = 0.15
        self.api_error_count = 0; self.max_api_errors = 5; self.api_error_pause_time = 0

        self.taker_fee = settings.TAKER_FEE; self.maker_fee = settings.MAKER_FEE
        self.min_profit_margin = settings.MIN_PROFIT_MARGIN
        self.breakeven_pct = (self.taker_fee * 2) + self.min_profit_margin

        raw = settings.ALLOWED_USERS
        self.allowed = {int(x.strip()) for x in raw.split(",") if x.strip().isdigit()} if raw else set()
        self.env_tag = "🧪 (模拟盘)" if settings.IS_SANDBOX else "🔴 (实盘)"

        self.entries = {}; self.daily_trades = 0; self.last_reset_day = datetime.now(CST).day
        self.trades = []; self._trailing_active = {}; self._trailing_high = {}
        self.entry_details = {}
        self.consecutive_failures = 0; self.last_failure_time = 0
        self.peak_total_value = 0

        self.learning_enabled = True; self.last_learning_check = 0

        # GitHub 自动备份配置
        self.github_token = os.getenv("GITHUB_TOKEN", "")
        self.github_repo = os.getenv("GITHUB_REPO", "AEscY/anjiachen_bot")
        self.github_backup_path = "bot.db"

        self.tg_app = None
        if settings.TG_BOT_TOKEN:
            self.tg_app = ApplicationBuilder().token(settings.TG_BOT_TOKEN).build()
            handlers = [
                CommandHandler("start", self.cmd_menu), CommandHandler("menu", self.cmd_menu),
                CommandHandler("status", self.cmd_status), CommandHandler("check", self.cmd_check),
                CommandHandler("symbols", self.cmd_symbols), CommandHandler("analysis", self.cmd_analysis),
                CommandHandler("brain", self.cmd_brain), CommandHandler("help", self.cmd_help),
                CommandHandler("settp", self.cmd_set_tp), CommandHandler("setsl", self.cmd_set_sl),
                CommandHandler("settsl", self.cmd_set_tsl), CommandHandler("settmpt", self.cmd_set_trailing_tp),
                CommandHandler("setamount", self.cmd_set_amount), CommandHandler("settf", self.cmd_set_tf),
                CommandHandler("setreserve", self.cmd_set_reserve),
                CommandHandler("addsymbol", self.cmd_add_symbol), CommandHandler("delsymbol", self.cmd_del_symbol),
                CommandHandler("panic", self.cmd_panic), CommandHandler("entry", self.cmd_entry),
                CommandHandler("settrades", self.cmd_set_trades), CommandHandler("resettrades", self.cmd_reset_trades),
                CommandHandler("preset", self.cmd_preset), CommandHandler("history", self.cmd_history),
                CommandHandler("autotrade", self.cmd_autotrade), CommandHandler("autoscore", self.cmd_autoscore),
                CommandHandler("holdings", self.cmd_holdings), CommandHandler("setmaxcoin", self.cmd_set_max_coin),
                CommandHandler("setmaxloss", self.cmd_set_max_loss),
                CommandHandler("learn", self.cmd_learn),
                CommandHandler("stats", self.cmd_stats),
                CommandHandler("backup", self.cmd_backup),
            ]
            for h in handlers: self.tg_app.add_handler(h)
            self.tg_app.add_handler(CallbackQueryHandler(self.handle_button_click))
            self.tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_text_input))

    # ---------- 余额辅助 ----------
    def _get_usdt_free(self, bal):
        try:
            usdt = bal.get('USDT', {})
            if isinstance(usdt, dict): free = usdt.get('free', 0)
            elif isinstance(usdt, (int, float)): free = usdt
            else: free = 0
            return float(free)
        except: return 0

    # ---------- 数据库加载 ----------
    async def load_and_init(self):
        await init_db()
        cfg = await load_config()
        self.orderbook_filter = cfg.get('orderbook_filter', True)
        self.waterfall_breaker = cfg.get('waterfall_breaker', True)
        self.symbols = cfg.get('symbols', []) or [settings.SYMBOL, "BTC/USDT", "SOL/USDT"]
        self.tp_pct = cfg.get('tp_pct', 0.08); self.sl_pct = cfg.get('sl_pct', 0.05)
        self.trailing_sl_pct = cfg.get('trailing_sl_pct', 0.02); self.trailing_tp_pct = cfg.get('trailing_tp_pct', 0.01)
        self.single_order_usdt = cfg.get('single_order_usdt', 100); self.timeframe = cfg.get('timeframe', '15m')
        self.reserve_bottom = cfg.get('reserve_bottom', 50)
        self.max_daily_trades = cfg.get('max_daily_trades', 0)
        self.auto_trade_enabled = cfg.get('auto_trade_enabled', False)
        self.auto_min_score = cfg.get('auto_min_score', 75)
        self.max_per_coin_usdt = cfg.get('max_per_coin_usdt', 0)
        self.max_daily_loss_pct = cfg.get('max_daily_loss_pct', 0.05)
        self.max_total_allocated_pct = cfg.get('max_total_allocated_pct', 0.8)
        self.max_drawdown_pct = cfg.get('max_drawdown_pct', 0.15)
        self.trades = await load_trades()

    async def _save_config(self):
        cfg = {
            'tp_pct': self.tp_pct, 'sl_pct': self.sl_pct, 'trailing_sl_pct': self.trailing_sl_pct,
            'trailing_tp_pct': self.trailing_tp_pct, 'single_order_usdt': self.single_order_usdt,
            'timeframe': self.timeframe, 'reserve_bottom': self.reserve_bottom, 'symbols': self.symbols,
            'orderbook_filter': self.orderbook_filter, 'waterfall_breaker': self.waterfall_breaker,
            'max_daily_trades': self.max_daily_trades, 'auto_trade_enabled': self.auto_trade_enabled,
            'auto_min_score': self.auto_min_score, 'max_per_coin_usdt': self.max_per_coin_usdt,
            'max_daily_loss_pct': self.max_daily_loss_pct, 'max_total_allocated_pct': self.max_total_allocated_pct,
            'max_drawdown_pct': self.max_drawdown_pct
        }
        await save_config(cfg)

    def _auth(self, update: Update):
        if not self.allowed: return True
        return update.effective_user.id in self.allowed

    def _parse_pct(self, val): return val / 100.0

    # ---------- 动态止盈止损调整 ----------
    async def _adjust_tp_sl_by_volatility(self, symbol):
        try:
            tech = await self.tech.calc(symbol, self.timeframe, 20)
            volatility = tech['atr'] / tech['bb_middle']
            factor = max(0.5, min(2.0, 1.0 + (volatility - 0.01) * 50))
            return self.tp_pct * factor, self.sl_pct * factor
        except Exception:
            return self.tp_pct, self.sl_pct

    # ---------- 键盘 ----------
    def _build_main_keyboard(self):
        f_status = "已开启" if self.orderbook_filter else "已关闭"
        b_status = "已开启" if self.waterfall_breaker else "已关闭"
        auto_status = "🟢" if self.auto_trade_enabled else "🔴"
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("🚨 紧急全平", callback_data="panic_confirm")],
            [InlineKeyboardButton(f"🏮 盘口: [{f_status}]", callback_data="toggle_filter"),
             InlineKeyboardButton(f"🚨 熔断: [{b_status}]", callback_data="toggle_breaker")],
            [InlineKeyboardButton("⚡ 开启", callback_data="bot_start"),
             InlineKeyboardButton("🔴 关机", callback_data="bot_stop")],
            [InlineKeyboardButton(f"🤖 自动交易: {auto_status}", callback_data="toggle_auto"),
             InlineKeyboardButton("🎯 阈值", callback_data="menu_set_autoscore")],
            [InlineKeyboardButton("📊 看板", callback_data="dashboard"),
             InlineKeyboardButton("💳 余额", callback_data="balance")],
            [InlineKeyboardButton("📋 持币", callback_data="holdings"),
             InlineKeyboardButton("📋 监控", callback_data="list_symbols")],
            [InlineKeyboardButton("🎯 止盈", callback_data="menu_set_tp"),
             InlineKeyboardButton("🛡️ 止损", callback_data="menu_set_sl")],
            [InlineKeyboardButton("📉 移损", callback_data="menu_set_tsl"),
             InlineKeyboardButton("🏹 移盈", callback_data="menu_set_tmpt")],
            [InlineKeyboardButton("💵 额度", callback_data="menu_set_amount"),
             InlineKeyboardButton("⏱ 周期", callback_data="menu_set_tf")],
            [InlineKeyboardButton("🔒 底线", callback_data="menu_set_reserve"),
             InlineKeyboardButton("🔢 上限", callback_data="menu_set_trades")],
            [InlineKeyboardButton("➕ 币种", callback_data="menu_add_symbol"),
             InlineKeyboardButton("➖ 币种", callback_data="menu_del_symbol")],
            [InlineKeyboardButton("🧠 大脑", callback_data="brain_status"),
             InlineKeyboardButton("📈 分析", callback_data="gap_analysis")],
            [InlineKeyboardButton("⚡ 预设", callback_data="menu_preset"),
             InlineKeyboardButton("📜 历史", callback_data="history")],
            [InlineKeyboardButton("📈 仪表盘", callback_data="stats_panel"),
             InlineKeyboardButton("💾 备份", callback_data="backup_panel")],
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

    # ==================== 命令实现 ====================
    async def cmd_holdings(self, update, context):
        if not self._auth(update): return
        bal = await self.exchange.fetch_balance()
        lines = ["📋 **当前持币**\n"]
        has_any = False
        for sym in self.symbols:
            coin = sym.split('/')[0]
            free = bal.get(coin, {}).get('free', 0) if isinstance(bal.get(coin), dict) else float(bal.get(coin, 0))
            if free > 0.0001:
                has_any = True
                ticker = await self.exchange.fetch_ticker(sym)
                if ticker:
                    p = ticker['last']; val = free * p
                    pnl = ""
                    if sym in self.entries and self.entries[sym] > 0:
                        pnl_pct = ((p - self.entries[sym]) / self.entries[sym]) * 100
                        pnl = f" | {'🟢' if pnl_pct>=0 else '🔴'} {pnl_pct:+.2f}%"
                    lines.append(f"• {sym}: {free:.4f} 现价{p:.2f} 价值{val:.2f}{pnl}")
        if not has_any: lines.append("暂无持仓")
        await update.effective_message.reply_text("\n".join(lines), parse_mode="Markdown")

    async def cmd_autotrade(self, update, context):
        if not self._auth(update): return
        try:
            mode = context.args[0].lower()
            if mode == "on": self.auto_trade_enabled = True; await self._save_config(); await update.effective_message.reply_text("🤖 自动交易已开启")
            elif mode == "off": self.auto_trade_enabled = False; await self._save_config(); await update.effective_message.reply_text("🤖 自动交易已关闭")
            else: await update.effective_message.reply_text("用法: /autotrade on|off")
        except: pass

    async def cmd_autoscore(self, update, context):
        if not self._auth(update): return
        try:
            score = int(context.args[0])
            if 50 <= score <= 95: self.auto_min_score = score; await self._save_config(); await update.effective_message.reply_text(f"✅ 阈值: {score}分")
            else: await update.effective_message.reply_text("阈值需在50-95之间")
        except: pass

    async def cmd_set_max_coin(self, update, context):
        if not self._auth(update): return
        try: self.max_per_coin_usdt = float(context.args[0]); await self._save_config(); await update.effective_message.reply_text(f"✅ 单币最大持仓: {self.max_per_coin_usdt}U")
        except: await update.effective_message.reply_text("❌ 格式: /setmaxcoin 200")

    async def cmd_set_max_loss(self, update, context):
        if not self._auth(update): return
        try:
            pct = float(context.args[0]) / 100.0
            self.max_daily_loss_pct = pct; await self._save_config()
            await update.effective_message.reply_text(f"✅ 日亏损熔断: {pct*100:.1f}%")
        except: await update.effective_message.reply_text("❌ /setmaxloss 5")

    async def cmd_learn(self, update, context):
        if not self._auth(update): return
        try:
            mode = context.args[0].lower()
            if mode == "on": self.learning_enabled = True; await update.effective_message.reply_text("🧠 自适应学习已开启")
            elif mode == "off": self.learning_enabled = False; await update.effective_message.reply_text("🧠 自适应学习已关闭")
            else: await update.effective_message.reply_text("用法: /learn on|off")
        except: pass

    async def cmd_stats(self, update, context):
        if not self._auth(update): return
        bal = await self.exchange.fetch_balance()
        usdt_free = self._get_usdt_free(bal)
        total_value = usdt_free
        positions = []
        for sym in self.symbols:
            ticker = await self.exchange.fetch_ticker(sym)
            if ticker is None: continue
            p = ticker['last']; coin = sym.split('/')[0]
            free = bal.get(coin, {}).get('free', 0) if isinstance(bal.get(coin), dict) else 0
            val = free * p; total_value += val
            positions.append(f"{sym}: {free:.4f} 价值{val:.2f}U")
        today = await get_today_trades()
        lines = [f"📊 **仪表盘** {self.env_tag}",
                 f"💰 总资产: {total_value:.2f}U | 可用: {usdt_free:.2f}U",
                 f"📈 持仓:", *positions,
                 f"━━━━━━━━━━━━━━━━━"]
        if today:
            lines.append(f"今日交易: {today['total']}笔 胜率{today['win_rate']:.0%} 总盈亏{today['total_pnl_sum']:+.2f}%")
        else:
            lines.append("今日暂无平仓记录")
        lines.append(f"自适应学习: {'🟢' if self.learning_enabled else '🔴'} | 阈值: {self.auto_min_score} | 仓位: {self.single_order_usdt}U")
        lines.append(f"总仓位上限: {self.max_total_allocated_pct*100:.0f}% | 回撤熔断: {self.max_drawdown_pct*100:.0f}%")
        await update.effective_message.reply_text("\n".join(lines), parse_mode="Markdown")

    async def cmd_backup(self, update, context):
        if not self._auth(update): return
        data = await export_db_to_json()
        if data:
            await update.effective_message.reply_document(
                document=data.encode('utf-8'),
                filename=f"backup_{datetime.now(CST).strftime('%Y%m%d_%H%M%S')}.json",
                caption="📦 数据库备份"
            )
        else:
            await update.effective_message.reply_text("❌ 备份失败")

    async def cmd_entry(self, update, context):
        if not self._auth(update): return
        try: sym = context.args[0].upper(); price = float(context.args[1]); self.entries[sym] = price; await update.effective_message.reply_text(f"📝 {sym} 入场价: {price:.2f}")
        except: await update.effective_message.reply_text("❌ `/entry ETH/USDT 3120`")

    async def cmd_set_trades(self, update, context):
        if not self._auth(update): return
        try: self.max_daily_trades = int(context.args[0]); await self._save_config(); await update.effective_message.reply_text(f"✅ 单日最大交易: {self.max_daily_trades}")
        except: pass

    async def cmd_reset_trades(self, update, context):
        if not self._auth(update): return
        self.daily_trades = 0; await update.effective_message.reply_text("✅ 计数已重置")

    async def cmd_preset(self, update, context):
        if not self._auth(update): return
        try:
            mode = context.args[0].lower()
            presets = {
                "conservative": {"tp": 3, "sl": 2, "tsl": 1, "tmpt": 1, "tf": "1h", "amt": 1, "reserve": 2},
                "balanced": {"tp": 1.5, "sl": 1, "tsl": 0.5, "tmpt": 0.5, "tf": "15m", "amt": 1, "reserve": 1},
                "aggressive": {"tp": 0.8, "sl": 0.5, "tsl": 0.3, "tmpt": 0.3, "tf": "5m", "amt": 1, "reserve": 0.5},
            }
            if mode not in presets: await update.effective_message.reply_text("可选: conservative / balanced / aggressive"); return
            p = presets[mode]; self.tp_pct = p["tp"]/100; self.sl_pct = p["sl"]/100; self.trailing_sl_pct = p["tsl"]/100; self.trailing_tp_pct = p["tmpt"]/100; self.timeframe = p["tf"]; self.single_order_usdt = p["amt"]; self.reserve_bottom = p["reserve"]
            await self._save_config()
            names = {"conservative": "保守", "balanced": "平衡", "aggressive": "激进"}
            await update.effective_message.reply_text(f"⚡ {names[mode]}方案已生效\n止盈{self.tp_pct*100:.1f}% 止损{self.sl_pct*100:.1f}%")
        except: pass

    async def cmd_history(self, update, context):
        if not self._auth(update): return
        if not self.trades: await update.effective_message.reply_text("📜 暂无记录"); return
        lines = ["📜 **最近交易**\n"]
        for t in self.trades[:10]: lines.append(f"{'🟢' if t['pnl_pct']>0 else '🔴'} {t['time']} {t['symbol']} {t['pnl_pct']:+.2f}%")
        await update.effective_message.reply_text("\n".join(lines), parse_mode="Markdown")

    async def cmd_status(self, update, context):
        if not self._auth(update): return
        lines = ["📊 **持仓**\n"]
        bal = await self.exchange.fetch_balance()
        for sym in self.symbols:
            ticker = await self.exchange.fetch_ticker(sym)
            if ticker is None: continue
            p = ticker['last']; coin = sym.split('/')[0]
            free = bal.get(coin, {}).get('free', 0) if isinstance(bal.get(coin), dict) else float(bal.get(coin, 0))
            val = free * p
            pnl = ""
            if sym in self.entries and self.entries[sym] > 0 and free > 0:
                pnl_pct = ((p - self.entries[sym]) / self.entries[sym]) * 100
                pnl = f" | {'🟢' if pnl_pct>=0 else '🔴'} {pnl_pct:+.2f}%"
            lines.append(f"{sym}: {free:.4f} 现价{p:.2f} 价值{val:.2f}{pnl}")
        lines.append(f"💵 USDT: {self._get_usdt_free(bal):.2f}")
        await update.effective_message.reply_text("\n".join(lines))

    async def cmd_check(self, update, context):
        if not self._auth(update): return
        lines = ["📈 **信号 + 开仓条件**\n"]
        fg_data = await self.real_data.get_fear_greed_index()
        fg = fg_data["value"] if fg_data else None
        bal = await self.exchange.fetch_balance()
        usdt_free = self._get_usdt_free(bal)
        for sym in self.symbols:
            try:
                ticker = await self.exchange.fetch_ticker(sym)
                if ticker is None: continue
                p = ticker['last']
                tech = await self.tech.calc(sym, self.timeframe, 50)
                funding = await self.exchange.fetch_funding_rate(sym)
                sc = self.signal_engine.score(tech, funding, fg)
                txt = self.signal_engine.interpret(sc)
                coin = sym.split('/')[0]
                free = bal.get(coin, {}).get('free', 0) if isinstance(bal.get(coin), dict) else float(bal.get(coin, 0))
                coin_value = free * p
                cond_signal = sc >= self.auto_min_score
                cond_price = p <= tech['bb_lower'] * 1.02
                cond_book = True
                if self.orderbook_filter:
                    ob = await self.exchange.fetch_orderbook(sym)
                    cond_book, _ = await self.orderbook_engine.validate(ob)
                cond_pos = True
                if self.max_per_coin_usdt > 0:
                    cond_pos = coin_value < self.max_per_coin_usdt
                cond_balance = usdt_free >= self.single_order_usdt + self.reserve_bottom
                cond_daily = True if self.max_daily_trades <= 0 or self.daily_trades < self.max_daily_trades else False
                cond_str = (f"{'✅' if cond_signal else '❌'}信 {'✅' if cond_price else '❌'}价 {'✅' if cond_book else '❌'}盘 "
                            f"{'✅' if cond_pos else '❌'}仓 {'✅' if cond_balance else '❌'}钱 {'✅' if cond_daily else '❌'}天")
                all_met = all([cond_signal, cond_price, cond_book, cond_pos, cond_balance, cond_daily])
                status = "🎯 可开仓" if all_met else "⏳ 等待"
                lines.append(f"{sym}: {p:.2f} | 信号: {txt} ({sc})\n   条件: {cond_str} → {status}")
            except Exception: continue
        await update.effective_message.reply_text("\n".join(lines))

    async def cmd_symbols(self, update, context):
        if not self._auth(update): return
        s_list = "\n".join([f"• `{s}`" for s in self.symbols])
        await update.effective_message.reply_text(f"📋 **监控列表**:\n{s_list}", parse_mode="Markdown")

    async def cmd_menu(self, update, context):
        if not self._auth(update): await update.message.reply_text("⛔ 未授权"); return
        await update.effective_message.reply_text(f"⚙️ 控制台 {self.env_tag}", reply_markup=self._build_main_keyboard())

    async def cmd_panic(self, update, context):
        if not self._auth(update): return
        await self.panic_sell_all(); await update.effective_message.reply_text("🚨 全平")

    async def cmd_analysis(self, update, context): await self.render_gap_analysis(update.effective_message)
    async def cmd_brain(self, update, context): await self.render_brain_status(update.effective_message)

    async def cmd_help(self, update, context):
        await update.effective_message.reply_text(
            f"🤖 **命令列表**\n"
            f"/stats 仪表盘 /backup 备份\n"
            f"/menu 控制台 /status 持仓 /check 信号\n"
            f"/settp 5 /setsl 2 /setamount 1\n"
            f"/autotrade on /learn on\n"
            f"/preset balanced /panic 全平\n"
            f"保本线: >{self.breakeven_pct*100:.2f}%"
        )

    async def cmd_set_tp(self, update, context):
        if not self._auth(update): return
        try:
            val = self._parse_pct(float(context.args[0]))
            if val < self.breakeven_pct: await update.effective_message.reply_text(f"❌ 低于保本线 {self.breakeven_pct*100:.2f}%"); return
            if self.sl_pct > 0 and val / self.sl_pct < 1.2: await update.effective_message.reply_text("❌ 盈亏比不足"); return
            self.tp_pct = val; await self._save_config(); await update.effective_message.reply_text(f"✅ 止盈: {self.tp_pct*100:.2f}%")
        except: pass

    async def cmd_set_sl(self, update, context):
        if not self._auth(update): return
        try:
            val = self._parse_pct(float(context.args[0]))
            if self.tp_pct > 0 and self.tp_pct / val < 1.2: await update.effective_message.reply_text("❌ 盈亏比不足"); return
            self.sl_pct = val; await self._save_config(); await update.effective_message.reply_text("✅")
        except: pass

    async def cmd_set_tsl(self, update, context):
        if not self._auth(update): return
        try: self.trailing_sl_pct = self._parse_pct(float(context.args[0])); await self._save_config(); await update.effective_message.reply_text("✅")
        except: pass

    async def cmd_set_trailing_tp(self, update, context):
        if not self._auth(update): return
        try: val = self._parse_pct(float(context.args[0])); self.trailing_tp_pct = val; await self._save_config(); await update.effective_message.reply_text(f"✅ 移动止盈: {self.trailing_tp_pct*100:.2f}%")
        except: pass

    async def cmd_set_amount(self, update, context):
        if not self._auth(update): return
        try: self.single_order_usdt = float(context.args[0]); await self._save_config(); await update.effective_message.reply_text("✅")
        except: pass

    async def cmd_set_tf(self, update, context):
        if not self._auth(update): return
        try: self.timeframe = context.args[0].lower(); await self._save_config(); await update.effective_message.reply_text("✅")
        except: pass

    async def cmd_set_reserve(self, update, context):
        if not self._auth(update): return
        try: self.reserve_bottom = float(context.args[0]); await self._save_config(); await update.effective_message.reply_text(f"✅ 底线: {self.reserve_bottom}U")
        except: pass

    async def cmd_add_symbol(self, update, context):
        if not self._auth(update): return
        try: sym = context.args[0].upper(); self.symbols.append(sym); await self._save_config(); await update.effective_message.reply_text("✅")
        except: pass

    async def cmd_del_symbol(self, update, context):
        if not self._auth(update): return
        try: sym = context.args[0].upper(); self.symbols.remove(sym); await self._save_config(); await update.effective_message.reply_text("✅")
        except: pass

    # ==================== 诊断渲染（慢速请求版，避免限频） ====================
    async def render_brain_status(self, msg_obj):
        try:
            macro = await self.real_data.check_macro_risk()
            lines = [f"🧠 **AI 超级大脑** {self.env_tag}", f"1️⃣ 宏观: {macro['status']}"]
            for idx, sym in enumerate(self.symbols):
                try:
                    if idx > 0:
                        await asyncio.sleep(1.5)
                    ticker = await self.exchange.fetch_ticker(sym)
                    if ticker is None:
                        lines.append(f"{idx+2}️⃣ {sym}: 现价获取失败")
                        continue
                    p = ticker['last']
                    tech = await self.tech.calc(sym, self.timeframe, 50)
                    lines.append(f"{idx+2}️⃣ {sym}: {p:.2f} 布林{tech['bb_upper']:.1f}/{tech['bb_lower']:.1f} RSI{tech['rsi']:.0f}")
                except Exception:
                    lines.append(f"{idx+2}️⃣ {sym}: 数据获取失败")
            await msg_obj.reply_text("\n".join(lines))
        except Exception as e:
            logger.error(f"brain err: {e}")

    async def render_gap_analysis(self, msg_obj):
        try:
            lines = ["📈 **差距分析**\n"]
            for sym in self.symbols:
                ticker = await self.exchange.fetch_ticker(sym)
                if ticker is None: continue
                p = ticker['last']
                try:
                    tech = await self.tech.calc(sym, self.timeframe, 50)
                    target = min(tech['bb_lower'], p*0.99); gap = ((p-target)/p)*100
                    lines.append(f"{sym}: {p:.2f} → {target:.2f} ({gap:+.2f}%)")
                except: lines.append(f"{sym}: 指标计算失败")
            await msg_obj.reply_text("\n".join(lines))
        except Exception as e: logger.error(f"analysis err: {e}")

    # ==================== 自填模式 ====================
    async def handle_text_input(self, update, context):
        pending = context.user_data.get('pending_setting')
        if not pending: return
        try:
            user_text = update.message.text.strip()
            if pending in ("settf", "addsymbol", "delsymbol"):
                if pending == "settf": self.timeframe = user_text.lower()
                elif pending == "addsymbol": sym = user_text.upper(); self.symbols.append(sym) if sym not in self.symbols else await update.message.reply_text("⚠️ 已存在")
                elif pending == "delsymbol": sym = user_text.upper(); self.symbols.remove(sym) if sym in self.symbols else await update.message.reply_text("⚠️ 不存在")
            else:
                val = float(user_text)
                if pending == "settp": pct = self._parse_pct(val); self.tp_pct = pct
                elif pending == "setsl": self.sl_pct = self._parse_pct(val)
                elif pending == "settsl": self.trailing_sl_pct = self._parse_pct(val)
                elif pending == "settmpt": self.trailing_tp_pct = self._parse_pct(val)
                elif pending == "setamount": self.single_order_usdt = val
                elif pending == "setreserve": self.reserve_bottom = val
                elif pending == "settrades": self.max_daily_trades = int(val)
                elif pending == "autoscore": self.auto_min_score = int(val)
                elif pending == "setmaxcoin": self.max_per_coin_usdt = val
                elif pending == "setmaxloss": self.max_daily_loss_pct = val / 100.0
            await self._save_config(); context.user_data['pending_setting'] = None; await update.message.reply_text("✅")
        except ValueError: await update.message.reply_text("❌ 格式有误"); context.user_data['pending_setting'] = None

    # ==================== 按钮回调（完整版） ====================
    async def handle_button_click(self, update, context):
        query = update.callback_query
        data = query.data
        try:
            if data == "refresh_panel": await self.cmd_menu(update, context)
            elif data == "toggle_filter":
                self.orderbook_filter = not self.orderbook_filter; await self._save_config()
                await query.answer(f"盘口过滤已{'开启' if self.orderbook_filter else '关闭'}")
                try: await query.edit_message_reply_markup(reply_markup=self._build_main_keyboard())
                except: pass
            elif data == "toggle_breaker":
                self.waterfall_breaker = not self.waterfall_breaker; await self._save_config()
                await query.answer(f"瀑布熔断已{'开启' if self.waterfall_breaker else '关闭'}")
                try: await query.edit_message_reply_markup(reply_markup=self._build_main_keyboard())
                except: pass
            elif data == "toggle_auto":
                self.auto_trade_enabled = not self.auto_trade_enabled; await self._save_config()
                await query.answer(f"自动交易已{'开启' if self.auto_trade_enabled else '关闭'}")
                try: await query.edit_message_reply_markup(reply_markup=self._build_main_keyboard())
                except: pass
            elif data == "bot_start": self.is_running = True; await query.answer("已开启")
            elif data == "bot_stop": self.is_running = False; await query.answer("已关机")
            elif data == "brain_status": await self.render_brain_status(query.message); await query.answer()
            elif data == "gap_analysis": await self.render_gap_analysis(query.message); await query.answer()
            elif data == "dashboard":
                auto_state = "开启" if self.auto_trade_enabled else "关闭"
                msg = (f"📊 看板\n止盈{self.tp_pct*100:.2f}% 止损{self.sl_pct*100:.2f}%\n移损{self.trailing_sl_pct*100:.2f}% 移盈{self.trailing_tp_pct*100:.2f}%\n"
                       f"额度{self.single_order_usdt}U 周期{self.timeframe} 底线{self.reserve_bottom}U\n"
                       f"自动交易: {auto_state} 阈值: {self.auto_min_score}分\n单币限额: {self.max_per_coin_usdt}U\n"
                       f"日熔断: {self.max_daily_loss_pct*100:.1f}%\n总仓位上限: {self.max_total_allocated_pct*100:.0f}%\n"
                       f"今日交易: {self.daily_trades}/{self.max_daily_trades if self.max_daily_trades>0 else '∞'}")
                await query.message.reply_text(msg); await query.answer()
            elif data == "balance":
                bal = await self.exchange.fetch_balance()
                await query.message.reply_text(f"💳 USDT: {self._get_usdt_free(bal):.2f}"); await query.answer()
            elif data == "history": await self.cmd_history(update, context)
            elif data == "holdings": await self.cmd_holdings(update, context)
            elif data == "list_symbols": await self.cmd_symbols(update, context)
            elif data == "stats_panel": await self.cmd_stats(update, context)
            elif data == "backup_panel": await self.cmd_backup(update, context)
            elif data == "sync_pos":
                bal = await self.exchange.fetch_balance()
                await query.message.reply_text(f"🔄 持仓已刷新\n💵 USDT: {self._get_usdt_free(bal):.2f}")
                await query.answer("已同步")
            elif data == "menu_preset":
                opts = [("🛡️保守","conservative"),("⚖️平衡","balanced"),("⚡激进","aggressive")]
                kb = [[InlineKeyboardButton(label, callback_data=f"preset:{val}") for label,val in opts]]
                kb.append([InlineKeyboardButton("🔙返回", callback_data="refresh_panel")])
                await query.edit_message_text("⚡ 选择方案:", reply_markup=InlineKeyboardMarkup(kb)); await query.answer()
            elif data.startswith("preset:"):
                mode = data.split(":")[1]
                p = {"conservative":{"tp":3,"sl":2,"tsl":1,"tmpt":1,"tf":"1h","amt":1,"reserve":2},
                     "balanced":{"tp":1.5,"sl":1,"tsl":0.5,"tmpt":0.5,"tf":"15m","amt":1,"reserve":1},
                     "aggressive":{"tp":0.8,"sl":0.5,"tsl":0.3,"tmpt":0.3,"tf":"5m","amt":1,"reserve":0.5}}[mode]
                self.tp_pct=p["tp"]/100; self.sl_pct=p["sl"]/100; self.trailing_sl_pct=p["tsl"]/100; self.trailing_tp_pct=p["tmpt"]/100
                self.timeframe=p["tf"]; self.single_order_usdt=p["amt"]; self.reserve_bottom=p["reserve"]
                await self._save_config(); await query.answer("✅ 已生效", show_alert=True); await self._refresh_panel(query)
            elif data == "menu_set_autoscore":
                opts = [("70分","70"),("75分","75"),("80分","80"),("85分","85")]
                await query.edit_message_text("🎯 阈值", reply_markup=self._build_option_keyboard(opts,"cfg_autoscore","autoscore")); await query.answer()
            elif data == "menu_set_trades":
                opts = [("3次","3"),("5次","5"),("10次","10"),("无限","0")]
                await query.edit_message_text("🔢 上限", reply_markup=self._build_option_keyboard(opts,"cfg_trades","settrades")); await query.answer()
            elif data == "menu_set_tp":
                opts = [("3%","0.03"),("5%","0.05"),("8%","0.08")]
                await query.edit_message_text("🎯", reply_markup=self._build_option_keyboard(opts,"cfg_tp","settp")); await query.answer()
            elif data == "menu_set_sl":
                opts = [("1%","0.01"),("2%","0.02"),("3%","0.03")]
                await query.edit_message_text("🛡️", reply_markup=self._build_option_keyboard(opts,"cfg_sl","setsl")); await query.answer()
            elif data == "menu_set_tsl":
                opts = [("0.5%","0.005"),("1%","0.01"),("1.5%","0.015")]
                await query.edit_message_text("📉", reply_markup=self._build_option_keyboard(opts,"cfg_tsl","settsl")); await query.answer()
            elif data == "menu_set_tmpt":
                opts = [("0.5%","0.005"),("1%","0.01"),("1.5%","0.015")]
                await query.edit_message_text("🏹", reply_markup=self._build_option_keyboard(opts,"cfg_tmpt","settmpt")); await query.answer()
            elif data == "menu_set_amount":
                opts = [("1U","1"),("2U","2"),("5U","5")]
                await query.edit_message_text("💵", reply_markup=self._build_option_keyboard(opts,"cfg_amt","setamount")); await query.answer()
            elif data == "menu_set_tf":
                opts = [("1m","1m"),("5m","5m"),("15m","15m"),("1h","1h")]
                await query.edit_message_text("⏱", reply_markup=self._build_option_keyboard(opts,"cfg_tf","settf")); await query.answer()
            elif data == "menu_set_reserve":
                opts = [("0.5U","0.5"),("1U","1"),("2U","2"),("5U","5")]
                await query.edit_message_text("🔒", reply_markup=self._build_option_keyboard(opts,"cfg_res","setreserve")); await query.answer()
            elif data == "menu_add_symbol":
                opts = [("BTC/USDT","BTC/USDT"),("SOL/USDT","SOL/USDT"),("DOGE/USDT","DOGE/USDT")]
                await query.edit_message_text("➕", reply_markup=self._build_option_keyboard(opts,"cfg_add","addsymbol")); await query.answer()
            elif data == "menu_del_symbol":
                opts = [(s, s) for s in self.symbols]
                await query.edit_message_text("➖", reply_markup=self._build_option_keyboard(opts,"cfg_del","delsymbol")); await query.answer()
            elif data.startswith("cfg_"):
                prefix = data.split(":")[0] if ":" in data else ""
                val_str = data.split(":")[1] if ":" in data else ""
                if prefix == "cfg_tp":
                    val_f = float(val_str)
                    if val_f < self.breakeven_pct: await query.answer(f"❌ 低于保本线", show_alert=True); return
                    if self.sl_pct > 0 and val_f / self.sl_pct < 1.2: await query.answer("❌ 盈亏比不足", show_alert=True); return
                    self.tp_pct = val_f
                elif prefix == "cfg_sl":
                    val_f = float(val_str)
                    if self.tp_pct > 0 and self.tp_pct / val_f < 1.2: await query.answer("❌ 盈亏比不足", show_alert=True); return
                    self.sl_pct = val_f
                elif prefix == "cfg_tsl": self.trailing_sl_pct = float(val_str)
                elif prefix == "cfg_tmpt": self.trailing_tp_pct = float(val_str)
                elif prefix == "cfg_amt": self.single_order_usdt = float(val_str)
                elif prefix == "cfg_tf": self.timeframe = val_str
                elif prefix == "cfg_res": self.reserve_bottom = float(val_str)
                elif prefix == "cfg_autoscore": self.auto_min_score = int(val_str)
                elif prefix == "cfg_trades": self.max_daily_trades = int(val_str)
                elif prefix == "cfg_add":
                    if val_str not in self.symbols: self.symbols.append(val_str)
                    else: await query.answer("已存在", show_alert=True); return
                elif prefix == "cfg_del":
                    if val_str in self.symbols: self.symbols.remove(val_str)
                    else: await query.answer("不存在", show_alert=True); return
                await self._save_config(); await query.answer("✅", show_alert=True); await self._refresh_panel(query)
            elif data.startswith("prompt_manual:"):
                key = data.split(":")[1]
                context.user_data['pending_setting'] = key
                prompts = {
                    "settp": "✍️ 止盈率（例：6.5%）：", "setsl": "✍️ 硬止损率（例：2.5%）：",
                    "settsl": "✍️ 移动止损回调（例：1.5%）：", "settmpt": "✍️ 移动止盈回调（例：1%）：",
                    "setamount": "✍️ 单笔 USDT（例：150）：", "settf": "✍️ K线周期（例：15m）：",
                    "setreserve": "✍️ 安全底线（例：100）：", "addsymbol": "✍️ 币种（例：DOGE/USDT）：",
                    "delsymbol": "✍️ 要删除的币种：", "autoscore": "✍️ 信号阈值（50-95）：",
                    "settrades": "✍️ 单日最大交易次数：", "setmaxcoin": "✍️ 单币最大持仓U：",
                    "setmaxloss": "✍️ 日熔断百分比（例：5）：",
                }
                await query.message.reply_text(prompts.get(key, "✍️ 请输入数值："), reply_markup=ForceReply(selective=True), parse_mode="Markdown")
                await query.answer()
            elif data == "panic_confirm":
                await query.answer("🚨 请发送 /panic 确认", show_alert=True)
            else:
                logger.warning(f"未处理的按钮: {data}")
                await query.answer("此按钮暂未绑定功能", show_alert=True)
        except Exception as e:
            logger.error(f"按钮异常 ({data}): {e}")
            try: await query.answer("操作失败，请重试", show_alert=True)
            except: pass

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

    # ==================== 自动交易（动态网格、多重熔断） ====================
    async def _auto_trade_monitor(self):
        await asyncio.sleep(10)
        while True:
            try:
                if not self.is_running or not self.auto_trade_enabled:
                    await asyncio.sleep(30); continue
                today = datetime.now(CST).day
                if today != self.last_reset_day: self.daily_trades = 0; self.last_reset_day = today
                if self.max_daily_trades > 0 and self.daily_trades >= self.max_daily_trades:
                    await asyncio.sleep(30); continue

                # 回撤熔断检查
                bal = await self.exchange.fetch_balance()
                usdt_free = self._get_usdt_free(bal)
                total_value = usdt_free
                for sym in self.symbols:
                    ticker = await self.exchange.fetch_ticker(sym)
                    if ticker:
                        coin = sym.split('/')[0]
                        free = bal.get(coin, {}).get('free', 0) if isinstance(bal.get(coin), dict) else 0
                        total_value += free * ticker['last']
                if total_value > self.peak_total_value:
                    self.peak_total_value = total_value
                if self.peak_total_value > 0:
                    drawdown = (self.peak_total_value - total_value) / self.peak_total_value
                    if drawdown > self.max_drawdown_pct:
                        if settings.TG_CHAT_ID:
                            try: await self.tg_app.bot.send_message(chat_id=settings.TG_CHAT_ID, text=f"⛔ 回撤熔断: {drawdown:.1%}")
                            except: pass
                        await asyncio.sleep(300); continue

                # API异常熔断检查
                if self.api_error_count >= self.max_api_errors:
                    if asyncio.get_event_loop().time() - self.api_error_pause_time < 1800:
                        await asyncio.sleep(60); continue
                    else:
                        self.api_error_count = 0

                # 日亏损熔断
                today_stats = await get_today_trades()
                if today_stats and today_stats['total'] >= 3:
                    if today_stats['win_rate'] < 0.2 and abs(today_stats['avg_loss_pct']) > self.max_daily_loss_pct:
                        if settings.TG_CHAT_ID:
                            try: await self.tg_app.bot.send_message(chat_id=settings.TG_CHAT_ID, text="⛔ 日亏损熔断，停止交易")
                            except: pass
                        await asyncio.sleep(300); continue

                # 连续开仓失败保护
                if self.consecutive_failures >= 3:
                    if asyncio.get_event_loop().time() - self.last_failure_time < 1800:
                        await asyncio.sleep(60); continue
                    else:
                        self.consecutive_failures = 0

                fg_data = await self.real_data.get_fear_greed_index()
                fg = fg_data["value"] if fg_data else None

                # 总仓位上限检查
                allocated = (total_value - usdt_free) / total_value if total_value > 0 else 0
                if allocated > self.max_total_allocated_pct:
                    await asyncio.sleep(30); continue

                candidates = []
                for sym in self.symbols:
                    try:
                        ticker = await self.exchange.fetch_ticker(sym)
                        if ticker is None: continue
                        p = ticker['last']
                        coin = sym.split('/')[0]
                        free = bal.get(coin, {}).get('free', 0) if isinstance(bal.get(coin), dict) else 0
                        coin_value = free * p

                        if self.max_per_coin_usdt > 0 and coin_value >= self.max_per_coin_usdt:
                            continue

                        # 动态网格：根据ATR调整布林带倍数
                        tech_vol = await self.tech.calc(sym, self.timeframe, 20)
                        vol_factor = max(1.5, min(2.5, 2.0 * (tech_vol['atr'] / tech_vol['bb_middle'] * 100)))
                        tech = await self.tech.calc(sym, self.timeframe, 50, bb_multiplier=vol_factor)

                        funding = await self.exchange.fetch_funding_rate(sym)
                        sc = self.signal_engine.score(tech, funding, fg)
                        if sc < self.auto_min_score: continue
                        if p > tech['bb_lower'] * 1.02: continue
                        if self.orderbook_filter:
                            ob = await self.exchange.fetch_orderbook(sym)
                            if ob is None: continue
                            ob_valid, _ = await self.orderbook_engine.validate(ob)
                            if not ob_valid: continue

                        dyn_tp, dyn_sl = await self._adjust_tp_sl_by_volatility(sym)
                        candidates.append((sc, sym, p, funding, dyn_tp, dyn_sl, vol_factor))
                    except Exception:
                        continue

                candidates.sort(key=lambda x: x[0], reverse=True)

                for sc, sym, p, funding, dyn_tp, dyn_sl, vol_factor in candidates:
                    if usdt_free < self.single_order_usdt + self.reserve_bottom:
                        break
                    coin = sym.split('/')[0]
                    free = bal.get(coin, {}).get('free', 0) if isinstance(bal.get(coin), dict) else 0
                    order = await self.exchange.create_market_buy_order(sym, self.single_order_usdt / p)
                    if order:
                        self.daily_trades += 1
                        await asyncio.sleep(2)
                        new_bal = await self.exchange.fetch_balance()
                        new_free = new_bal.get(coin, {}).get('free', 0) if isinstance(new_bal.get(coin), dict) else 0
                        if new_free > free:
                            self.entries[sym] = p
                            self._trailing_high[sym] = p
                            self._trailing_active[sym] = False
                            self.entry_details[sym] = {
                                'signal_score': sc, 'fear_greed': fg, 'funding_rate': funding,
                                'dyn_tp': dyn_tp, 'dyn_sl': dyn_sl
                            }
                            await save_trade_detail({
                                "time": datetime.now(CST).strftime("%m-%d %H:%M"), "symbol": sym, "side": "buy",
                                "price": p, "amount": self.single_order_usdt / p,
                                "signal_score": sc, "fear_greed": fg or 0, "funding_rate": funding or 0, "pnl_pct": 0
                            })
                            self.consecutive_failures = 0
                            if settings.TG_CHAT_ID:
                                try: await self.tg_app.bot.send_message(chat_id=settings.TG_CHAT_ID, text=f"🤖 开仓 {sym} {self.single_order_usdt}U @ {p:.2f} 信号{sc}分 网格倍数{vol_factor:.1f}")
                                except: pass
                        else:
                            self.consecutive_failures += 1
                            self.last_failure_time = asyncio.get_event_loop().time()
                            if settings.TG_CHAT_ID:
                                try: await self.tg_app.bot.send_message(chat_id=settings.TG_CHAT_ID, text=f"⚠️ 开仓失败 {sym} (连续{self.consecutive_failures}次)")
                                except: pass
                        usdt_free -= self.single_order_usdt
                        await asyncio.sleep(2)
                await asyncio.sleep(30)
            except Exception as e:
                logger.error(f"自动交易错误: {e}")
                self.api_error_count += 1
                self.api_error_pause_time = asyncio.get_event_loop().time()
                await asyncio.sleep(30)

    # ==================== 移动止盈/止损追踪（使用动态TP/SL） ====================
    async def _trailing_monitor(self):
        await asyncio.sleep(5)
        while True:
            try:
                if not self.is_running: await asyncio.sleep(5); continue
                for sym in self.symbols:
                    try:
                        bal = await self.exchange.fetch_balance()
                        coin = sym.split('/')[0]
                        amount = bal.get(coin, {}).get('free', 0) if isinstance(bal.get(coin), dict) else 0
                        if amount <= 0:
                            self._trailing_active[sym] = False; self._trailing_high[sym] = 0
                            if sym in self.entries: del self.entries[sym]
                            if sym in self.entry_details: del self.entry_details[sym]
                            continue

                        ticker = await self.exchange.fetch_ticker(sym)
                        if ticker is None: continue
                        p = ticker['last']
                        entry_price = self.entries.get(sym, p)
                        detail = self.entry_details.get(sym, {})
                        use_tp = detail.get('dyn_tp', self.tp_pct)
                        use_sl = detail.get('dyn_sl', self.sl_pct)

                        # 硬止损
                        if p <= entry_price * (1 - use_sl):
                            logger.info(f"🛡️ 硬止损 {sym} @ {p:.2f}")
                            await self.exchange.create_market_sell_order(sym, amount)
                            pnl_pct = ((p - entry_price) / entry_price) * 100
                            trade = {"time": datetime.now(CST).strftime("%m-%d %H:%M"), "symbol": sym, "entry": entry_price, "exit": p, "pnl_pct": round(pnl_pct, 2)}
                            await save_trade(trade); self.trades.insert(0, trade)
                            await save_trade_detail({
                                "time": datetime.now(CST).strftime("%m-%d %H:%M"), "symbol": sym, "side": "sell",
                                "price": p, "amount": amount, "pnl_pct": round(pnl_pct, 2),
                                "signal_score": detail.get('signal_score', 0),
                                "fear_greed": detail.get('fear_greed', 0),
                                "funding_rate": detail.get('funding_rate', 0)
                            })
                            self._trailing_active[sym] = False; self._trailing_high[sym] = 0
                            if sym in self.entries: del self.entries[sym]
                            if sym in self.entry_details: del self.entry_details[sym]
                            await self._learn_from_trades()
                            if settings.TG_CHAT_ID:
                                try: await self.tg_app.bot.send_message(chat_id=settings.TG_CHAT_ID, text=f"🛡️ 硬止损 {sym} @ {p:.2f} 亏损{pnl_pct:+.2f}%")
                                except: pass
                            continue

                        # 移动止盈/移动止损
                        if not self._trailing_active.get(sym, False):
                            if p >= entry_price * (1 + use_tp):
                                self._trailing_active[sym] = True
                                self._trailing_high[sym] = p
                        else:
                            if p > self._trailing_high.get(sym, 0):
                                self._trailing_high[sym] = p
                            high = self._trailing_high[sym]
                            # 移动止损保护
                            if self.trailing_sl_pct > 0:
                                sl_price = high * (1 - self.trailing_sl_pct)
                                if p <= sl_price:
                                    logger.info(f"📉 移动止损触发 {sym} @ {p:.2f}")
                                    await self.exchange.create_market_sell_order(sym, amount)
                                    pnl_pct = ((p - entry_price) / entry_price) * 100
                                    trade = {"time": datetime.now(CST).strftime("%m-%d %H:%M"), "symbol": sym, "entry": entry_price, "exit": p, "pnl_pct": round(pnl_pct, 2)}
                                    await save_trade(trade); self.trades.insert(0, trade)
                                    await save_trade_detail({
                                        "time": datetime.now(CST).strftime("%m-%d %H:%M"), "symbol": sym, "side": "sell",
                                        "price": p, "amount": amount, "pnl_pct": round(pnl_pct, 2),
                                        "signal_score": detail.get('signal_score', 0),
                                        "fear_greed": detail.get('fear_greed', 0),
                                        "funding_rate": detail.get('funding_rate', 0)
                                    })
                                    self._trailing_active[sym] = False; self._trailing_high[sym] = 0
                                    if sym in self.entries: del self.entries[sym]
                                    if sym in self.entry_details: del self.entry_details[sym]
                                    await self._learn_from_trades()
                                    if settings.TG_CHAT_ID:
                                        try: await self.tg_app.bot.send_message(chat_id=settings.TG_CHAT_ID, text=f"📉 移动止损 {sym} @ {p:.2f} 盈亏{pnl_pct:+.2f}%")
                                        except: pass
                                    continue
                            # 移动止盈
                            if p <= high * (1 - self.trailing_tp_pct):
                                await self.exchange.create_market_sell_order(sym, amount)
                                pnl_pct = ((p - entry_price) / entry_price) * 100
                                trade = {"time": datetime.now(CST).strftime("%m-%d %H:%M"), "symbol": sym, "entry": entry_price, "exit": p, "pnl_pct": round(pnl_pct, 2)}
                                await save_trade(trade); self.trades.insert(0, trade)
                                await save_trade_detail({
                                    "time": datetime.now(CST).strftime("%m-%d %H:%M"), "symbol": sym, "side": "sell",
                                    "price": p, "amount": amount, "pnl_pct": round(pnl_pct, 2),
                                    "signal_score": detail.get('signal_score', 0),
                                    "fear_greed": detail.get('fear_greed', 0),
                                    "funding_rate": detail.get('funding_rate', 0)
                                })
                                self._trailing_active[sym] = False; self._trailing_high[sym] = 0
                                if sym in self.entries: del self.entries[sym]
                                if sym in self.entry_details: del self.entry_details[sym]
                                await self._learn_from_trades()
                                if settings.TG_CHAT_ID:
                                    try: await self.tg_app.bot.send_message(chat_id=settings.TG_CHAT_ID, text=f"🏹 移动止盈 {sym} @ {p:.2f} 盈亏{pnl_pct:+.2f}%")
                                    except: pass
                    except Exception as e:
                        logger.error(f"追踪异常 {sym}: {e}")
                await asyncio.sleep(1)
            except Exception as e:
                logger.error(f"追踪任务异常: {e}")
                await asyncio.sleep(5)

    # ==================== 自适应学习 ====================
    async def _learn_from_trades(self):
        if not self.learning_enabled: return
        now = asyncio.get_event_loop().time()
        if now - self.last_learning_check < 60: return
        self.last_learning_check = now
        perf = await get_recent_performance(10)
        if not perf or perf["total"] < 5: return
        win_rate = perf["win_rate"]
        old_amount = self.single_order_usdt
        changed = False
        if win_rate < 0.4:
            self.auto_min_score = min(95, self.auto_min_score + 5)
            self.single_order_usdt = max(1, int(self.single_order_usdt * 0.8))
            changed = True
        elif win_rate > 0.6 and (perf["avg_win_pct"] / abs(perf["avg_loss_pct"]) > 1.5 if perf["avg_loss_pct"] != 0 else True):
            self.auto_min_score = max(50, self.auto_min_score - 2)
            self.single_order_usdt = old_amount
            changed = True
        if changed:
            await self._save_config()
            if settings.TG_CHAT_ID:
                try: await self.tg_app.bot.send_message(chat_id=settings.TG_CHAT_ID, text=f"🧠 自适应调整：胜率{win_rate:.0%}，阈值→{self.auto_min_score}，仓位→{self.single_order_usdt}U")
                except: pass

    # ==================== 启动 ====================
    async def run(self):
        await self.load_and_init()
        if not self.tg_app: return
        await self.tg_app.bot.delete_webhook(drop_pending_updates=True)
        asyncio.create_task(self._auto_trade_monitor())
        asyncio.create_task(self._trailing_monitor())
        while True:
            try:
                await self.tg_app.initialize()
                await self.tg_app.start()
                await self.tg_app.updater.start_polling(drop_pending_updates=True)
                logger.info("✅ Bot 最终完整版启动")
                if settings.TG_CHAT_ID:
                    try: await self.tg_app.bot.send_message(chat_id=settings.TG_CHAT_ID, text="🤖 量化机器人已上线 (最终完整版)")
                    except: pass
                while True: await asyncio.sleep(30)
            except Exception as e:
                logger.error(f"Bot 断开，5秒后重连: {e}")
                await asyncio.sleep(5)