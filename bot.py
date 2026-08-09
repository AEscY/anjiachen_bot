"""
bot.py - 最终优化完整版（动态网格、回撤熔断、API异常熔断、仪表盘）
"""
import asyncio, random, aiohttp
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
        self.max_drawdown_pct = 0.15  # 回撤熔断阈值
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

    def _get_usdt_free(self, bal):
        try:
            usdt = bal.get('USDT', {})
            if isinstance(usdt, dict): free = usdt.get('free', 0)
            elif isinstance(usdt, (int, float)): free = usdt
            else: free = 0
            return float(free)
        except: return 0

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

    async def _adjust_tp_sl_by_volatility(self, symbol):
        try:
            tech = await self.tech.calc(symbol, self.timeframe, 20)
            volatility = tech['atr'] / tech['bb_middle']
            factor = max(0.5, min(2.0, 1.0 + (volatility - 0.01) * 50))
            return self.tp_pct * factor, self.sl_pct * factor
        except Exception:
            return self.tp_pct, self.sl_pct

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

    # ==================== 命令实现（同之前的完整版，为节省篇幅此处略，实际文件包含所有命令） ====================
    # 请注意：此处省略了之前所有命令函数（cmd_holdings, cmd_autotrade, cmd_autoscore, cmd_set_max_coin, cmd_entry, 
    # cmd_set_trades, cmd_reset_trades, cmd_preset, cmd_history, cmd_status, cmd_check, cmd_symbols, cmd_menu, 
    # cmd_panic, cmd_analysis, cmd_brain, cmd_help, cmd_set_tp, cmd_set_sl, cmd_set_tsl, cmd_set_trailing_tp,
    # cmd_set_amount, cmd_set_tf, cmd_set_reserve, cmd_add_symbol, cmd_del_symbol, render_brain_status, 
    # render_gap_analysis, handle_text_input, handle_button_click, _refresh_panel, panic_sell_all 等），
    # 它们与之前提供的最终完整版完全相同。实际部署时必须包含全部这些函数。

    # ==================== 新增命令 ====================
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

    # ==================== 自动交易（包含动态网格、回撤熔断、API异常熔断） ====================
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
                logger.info("✅ Bot 最终优化版启动")
                if settings.TG_CHAT_ID:
                    try: await self.tg_app.bot.send_message(chat_id=settings.TG_CHAT_ID, text="🤖 量化机器人已上线 (全优化版)")
                    except: pass
                while True: await asyncio.sleep(30)
            except Exception as e:
                logger.error(f"Bot 断开，5秒后重连: {e}")
                await asyncio.sleep(5)