"""
bot.py - 完整版（修复：告警泛滥 / 单币重复开仓 / 百分比异常 / 多币种并发）
"""
import asyncio
import aiohttp
import os
import json
import aiosqlite
import time
from datetime import datetime, timezone, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ForceReply
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, MessageHandler, filters
from config import settings, logger
from indicators import TechnicalEngine
from ws_manager import WSDataManager
from storage import (
    init_db, load_config, save_config, load_trades, save_trade,
    save_trade_detail, get_recent_performance, get_today_trades,
    export_db_to_json, save_runtime_state, load_runtime_state
)

CST = timezone(timedelta(hours=8))


class RealDataEngine:
    def __init__(self, exchange_rest, ws_manager):
        self.exchange = exchange_rest
        self.ws = ws_manager
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
        if value < 25:
            return {'is_safe': False, 'score': value / 100, 'status': f"🚨 极度恐惧 ({value})"}
        elif value > 75:
            return {'is_safe': False, 'score': value / 100, 'status': f"⚠️ 极度贪婪 ({value})"}
        return {'is_safe': True, 'score': value / 100, 'status': f"🟢 {fg['classification']} ({value})"}


class OrderbookEngine:
    async def validate(self, orderbook):
        if orderbook is None:
            return False, "盘口数据缺失"
        bids = orderbook.get('bids', [])
        asks = orderbook.get('asks', [])
        if not bids or not asks:
            return False, "盘口数据缺失"
        spread = ((asks[0][0] - bids[0][0]) / bids[0][0]) * 100
        if spread > 0.2:
            return False, f"价差过大 ({spread:.3f}%)"
        return True, f"盘口健康 (价差: {spread:.3f}%)"


class SignalEngine:
    @staticmethod
    def score(tech, funding_rate, fear_greed):
        score = 50
        rsi = tech['rsi']
        if rsi < 30:
            score += 25
        elif rsi < 40:
            score += 15
        elif rsi < 50:
            score += 5
        elif rsi > 70:
            score -= 20
        elif rsi > 60:
            score -= 10
        bb_lower = tech['bb_lower']
        price = tech['bb_middle']
        bb_position = (price - bb_lower) / (tech['bb_upper'] - bb_lower) if tech['bb_upper'] != bb_lower else 0.5
        if bb_position < 0.2:
            score += 20
        elif bb_position < 0.35:
            score += 10
        elif bb_position > 0.8:
            score -= 15
        if funding_rate is not None:
            if funding_rate < -0.0005:
                score += 10
            elif funding_rate < 0:
                score += 5
            elif funding_rate > 0.001:
                score -= 10
        if fear_greed is not None:
            if fear_greed < 25:
                score += 10
            elif fear_greed > 75:
                score -= 5
        return max(0, min(100, score))

    @staticmethod
    def interpret(score):
        if score >= 80:
            return "🔥🔥🔥 强烈买入"
        elif score >= 65:
            return "🔥🔥 建议买入"
        elif score >= 50:
            return "🔥 可以关注"
        elif score >= 35:
            return "📉 暂时观望"
        elif score >= 20:
            return "📉📉 不建议买入"
        return "🚨 强烈回避"


class QuantBot:
    def __init__(self, exchange):
        self.exchange = exchange
        self.ws = WSDataManager(exchange)
        self.tech = TechnicalEngine(exchange)
        self.real_data = RealDataEngine(exchange, self.ws)
        self.orderbook_engine = OrderbookEngine()
        self.signal_engine = SignalEngine()
        self.lock = asyncio.Lock()

        self.is_running = True
        self.orderbook_filter = True
        self.waterfall_breaker = True
        self.symbols = [settings.SYMBOL, "BTC/USDT", "SOL/USDT"]
        self.tp_pct = 0.08
        self.sl_pct = 0.05
        self.trailing_sl_pct = 0.02
        self.trailing_tp_pct = 0.01
        self.single_order_usdt = 100
        self.timeframe = "15m"
        self.reserve_bottom = 50
        self.max_daily_trades = 0
        self.auto_trade_enabled = False
        self.auto_min_score = 75
        self.max_per_coin_usdt = 0
        self.max_daily_loss_pct = 0.05
        self.max_total_allocated_pct = 1.0
        self.max_drawdown_pct = 0.15
        self.api_error_count = 0
        self.max_api_errors = 5
        self.api_error_pause_time = 0
        self.max_positions_per_coin = 18
        self.position_counts = {}
        self.coin_configs = {}
        self.grid_configs = {}
        self.btc_risk_paused = False
        self._last_btc_check_time = 0
        self._rsi_history = {}
        self._volume_history = {}
        self._last_grid_entry = {}

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
        self.trades = []
        self._trailing_active = {}
        self._trailing_high = {}
        self.entry_details = {}
        self.consecutive_failures = 0
        self.last_failure_time = 0
        self.peak_total_value = 0
        self.learning_enabled = True
        self.last_learning_check = 0
        self.ai_optimize_count = 0

        self._cached_balances = {}
        self._cached_usdt_free = 0.0
        self._balance_cache_time = 0
        self._balance_cache_ttl = 30

        self._btc_safe_flag = True
        self._drawdown_safe_flag = True

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
                CommandHandler("autotrade", self.cmd_autotrade),
                CommandHandler("autoscore", self.cmd_autoscore),
                CommandHandler("holdings", self.cmd_holdings),
                CommandHandler("setmaxcoin", self.cmd_set_max_coin),
                CommandHandler("setmaxloss", self.cmd_set_max_loss),
                CommandHandler("setmaxpos", self.cmd_set_max_pos),
                CommandHandler("setmaxalloc", self.cmd_set_max_alloc),
                CommandHandler("setcoin", self.cmd_set_coin),
                CommandHandler("resetcoin", self.cmd_reset_coin),
                CommandHandler("coininfo", self.cmd_coin_info),
                CommandHandler("setgrid", self.cmd_set_grid),
                CommandHandler("resetgrid", self.cmd_reset_grid),
                CommandHandler("learn", self.cmd_learn),
                CommandHandler("stats", self.cmd_stats),
                CommandHandler("backup", self.cmd_backup),
            ]
            for h in handlers:
                self.tg_app.add_handler(h)
            self.tg_app.add_handler(CallbackQueryHandler(self.handle_button_click))
            self.tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_text_input))

    def _get_coin_param(self, sym, key, default):
        if sym in self.coin_configs and key in self.coin_configs[sym]:
            return self.coin_configs[sym][key]
        return default

    def _get_usdt_free(self, bal):
        try:
            usdt = bal.get('USDT', {})
            if isinstance(usdt, dict):
                free = usdt.get('free', 0)
            elif isinstance(usdt, (int, float)):
                free = usdt
            else:
                free = 0
            return float(free)
        except:
            return 0

    def _extract_balances(self, bal):
        result = {}
        system_keys = {'info', 'free', 'used', 'total', 'datetime', 'timestamp'}
        for key, val in bal.items():
            if key in system_keys:
                continue
            if isinstance(val, dict):
                result[key] = val.get('free', 0)
        return result

    async def _refresh_balance_cache(self, force=False):
        now = time.time()
        if force or (now - self._balance_cache_time > self._balance_cache_ttl):
            bal = await self.exchange.fetch_balance()
            self._cached_balances = self._extract_balances(bal)
            self._cached_usdt_free = self._get_usdt_free(bal)
            self._balance_cache_time = now
        return self._cached_usdt_free

    async def load_and_init(self):
        await init_db()
        cfg = await load_config()
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
        self.auto_trade_enabled = cfg.get('auto_trade_enabled', False)
        self.auto_min_score = cfg.get('auto_min_score', 75)
        self.max_per_coin_usdt = cfg.get('max_per_coin_usdt', 0)
        self.max_daily_loss_pct = cfg.get('max_daily_loss_pct', 0.05)
        self.max_total_allocated_pct = 1.0
        self.max_drawdown_pct = cfg.get('max_drawdown_pct', 0.15)
        self.max_positions_per_coin = cfg.get('max_positions_per_coin', 18)
        coin_cfg_raw = cfg.get('coin_configs', '{}')
        if isinstance(coin_cfg_raw, str):
            try:
                self.coin_configs = json.loads(coin_cfg_raw)
            except:
                self.coin_configs = {}
        elif isinstance(coin_cfg_raw, dict):
            self.coin_configs = coin_cfg_raw
        grid_cfg_raw = cfg.get('grid_configs', '{}')
        if isinstance(grid_cfg_raw, str):
            try:
                self.grid_configs = json.loads(grid_cfg_raw)
            except:
                self.grid_configs = {}
        elif isinstance(grid_cfg_raw, dict):
            self.grid_configs = grid_cfg_raw
        self.trades = await load_trades()

        state = await load_runtime_state()
        if state:
            self.position_counts = state.get('position_counts', {})
            self.entries = state.get('entries', {})
            self.peak_total_value = state.get('peak_total_value', 0)
            self.daily_trades = state.get('daily_trades', 0)
            self._trailing_active = state.get('trailing_active', {})
            self._trailing_high = state.get('trailing_high', {})
            logger.info("✅ 运行时状态（含移动止损）已恢复")

    async def _save_runtime_state(self):
        state = {
            'position_counts': self.position_counts,
            'entries': self.entries,
            'peak_total_value': self.peak_total_value,
            'daily_trades': self.daily_trades,
            'trailing_active': self._trailing_active,
            'trailing_high': self._trailing_high,
        }
        await save_runtime_state(state)

    async def _save_config(self):
        cfg = {
            'tp_pct': self.tp_pct,
            'sl_pct': self.sl_pct,
            'trailing_sl_pct': self.trailing_sl_pct,
            'trailing_tp_pct': self.trailing_tp_pct,
            'single_order_usdt': self.single_order_usdt,
            'timeframe': self.timeframe,
            'reserve_bottom': self.reserve_bottom,
            'symbols': self.symbols,
            'orderbook_filter': self.orderbook_filter,
            'waterfall_breaker': self.waterfall_breaker,
            'max_daily_trades': self.max_daily_trades,
            'auto_trade_enabled': self.auto_trade_enabled,
            'auto_min_score': self.auto_min_score,
            'max_per_coin_usdt': self.max_per_coin_usdt,
            'max_daily_loss_pct': self.max_daily_loss_pct,
            'max_total_allocated_pct': self.max_total_allocated_pct,
            'max_drawdown_pct': self.max_drawdown_pct,
            'max_positions_per_coin': self.max_positions_per_coin,
            'coin_configs': json.dumps(self.coin_configs),
            'grid_configs': json.dumps(self.grid_configs)
        }
        await save_config(cfg)

    async def _alert(self, message: str, level: str = "warning"):
        emoji = {"info": "ℹ️", "warning": "⚠️", "critical": "🚨"}
        if settings.TG_CHAT_ID and self.tg_app and self.tg_app.bot:
            try:
                await self.tg_app.bot.send_message(
                    chat_id=settings.TG_CHAT_ID,
                    text=f"{emoji.get(level, '⚠️')} **系统告警**\n{message}",
                    parse_mode="Markdown"
                )
            except:
                pass

    async def _ai_optimize_params(self):
        self.ai_optimize_count += 1
        if self.ai_optimize_count < 50:
            return
        self.ai_optimize_count = 0
        perf = await get_recent_performance(50)
        if not perf or perf['total'] < 30:
            return
        win_rate = perf['win_rate']
        avg_win = perf['avg_win_pct']
        if win_rate > 0.5 and avg_win > 0:
            new_tp = round(avg_win * 0.8, 3)
            new_sl = round(new_tp / 2, 3)
            if new_tp != self.tp_pct:
                self.tp_pct = new_tp
                self.sl_pct = new_sl
                await self._save_config()
                await self._alert(f"🤖 AI 动态优化完成\n止盈: {self.tp_pct * 100:.1f}%\n止损: {self.sl_pct * 100:.1f}%")

    async def _check_btc_risk(self):
        now = asyncio.get_event_loop().time()
        if now - self._last_btc_check_time < 60:
            return not self.btc_risk_paused
        self._last_btc_check_time = now
        try:
            btc_sym = "BTC/USDT"
            if btc_sym not in self.symbols:
                return True
            ticker = self.ws.get_ticker(btc_sym)
            if ticker is None:
                ticker = await self.exchange.fetch_ticker(btc_sym)
            if ticker is None:
                return True
            change_24h = ticker.get('percentage', ticker.get('change', 0))
            if change_24h is None:
                return True
            if change_24h < -5:
                if not self.btc_risk_paused:
                    await self._alert(f"🚨 BTC 24h 跌幅 {change_24h:.1f}%，暂停山寨币交易", "critical")
                    self.btc_risk_paused = True
                return False
            if change_24h > -3 and self.btc_risk_paused:
                await self._alert(f"✅ BTC 跌幅收窄至 {change_24h:.1f}%，恢复山寨币交易", "info")
                self.btc_risk_paused = False
            return True
        except Exception as e:
            logger.error(f"BTC风险检查失败: {e}")
            return True

    def _check_rsi_divergence(self, symbol, current_tech):
        try:
            if symbol not in self._rsi_history:
                self._rsi_history[symbol] = []
            history = self._rsi_history[symbol]
            history.append({
                'price': current_tech['bb_middle'],
                'rsi': current_tech['rsi'],
                'time': asyncio.get_event_loop().time()
            })
            if len(history) > 20:
                history.pop(0)
            if len(history) < 5:
                return False
            recent_prices = [h['price'] for h in history[-5:]]
            recent_rsi = [h['rsi'] for h in history[-5:]]
            price_new_low = recent_prices[-1] < min(recent_prices[:-1])
            rsi_not_new_low = recent_rsi[-1] > min(recent_rsi[:-1])
            return price_new_low and rsi_not_new_low
        except Exception:
            return False

    def _get_dynamic_amount(self, sym):
        base_amount = self._get_coin_param(sym, 'single_order_usdt', self.single_order_usdt)
        fg = self.real_data._fear_greed_cache.get('value', 50)
        if fg < 25:
            factor = 0.5
        elif fg < 45:
            factor = 0.8
        elif fg > 75:
            factor = 1.0
        else:
            factor = 1.0
        return max(1, int(base_amount * factor))

    async def _round_amount_by_precision(self, symbol: str, amount: float) -> float:
        try:
            if self.exchange and self.exchange.exchange:
                market = self.exchange.exchange.market(symbol)
                if market and 'limits' in market and 'amount' in market['limits']:
                    min_amt = market['limits']['amount'].get('min', 0)
                    max_amt = market['limits']['amount'].get('max', float('inf'))
                    if min_amt and amount < min_amt:
                        logger.warning(f"⚠️ {symbol} 数量 {amount:.8f} 低于最小限制 {min_amt}，自动调至 {min_amt}")
                        amount = min_amt
                    if max_amt != float('inf') and amount > max_amt:
                        logger.warning(f"⚠️ {symbol} 数量 {amount:.8f} 超过最大限制 {max_amt}，自动调至 {max_amt}")
                        amount = max_amt
                if market and 'precision' in market and 'amount' in market['precision']:
                    precision = market['precision']['amount']
                    if precision > 0:
                        amount = float(int(amount / precision) * precision)
                    elif precision == 0:
                        amount = int(amount)
            return max(0.000001, amount)
        except Exception as e:
            logger.warning(f"精度截断失败 {symbol}: {e}")
            return amount

    async def _sync_positions(self):
        try:
            bal = await self.exchange.fetch_balance()
            real_positions = {}
            for sym in self.symbols:
                coin = sym.split('/')[0]
                free = bal.get(coin, {}).get('free', 0) if isinstance(bal.get(coin), dict) else 0
                if free > 0.000001:
                    real_positions[sym] = free
            new_position_counts = {}
            for sym in self.symbols:
                if sym in real_positions:
                    new_position_counts[sym] = max(1, self.position_counts.get(sym, 1))
                else:
                    new_position_counts[sym] = 0
                    if sym in self.entries:
                        del self.entries[sym]
                    if sym in self.entry_details:
                        del self.entry_details[sym]
            self.position_counts = new_position_counts
            for sym, amount in real_positions.items():
                if sym not in self.entries or self.entries[sym] == 0:
                    ticker = self.ws.get_ticker(sym)
                    if ticker is None:
                        ticker = await self.exchange.fetch_ticker(sym)
                    if ticker:
                        self.entries[sym] = ticker['last']
                        logger.info(f"📝 补录 {sym} 入场价: {ticker['last']:.2f}")
            await self._save_runtime_state()
            logger.info(f"✅ 持仓同步完成: {real_positions}")
            return True
        except Exception as e:
            logger.error(f"持仓同步失败: {e}")
            return False

    async def _adjust_tp_sl_by_volatility(self, symbol):
        try:
            tech = await self.tech.calc(symbol, self.timeframe, 20)
            volatility = tech['atr'] / tech['bb_middle']
            factor = max(0.5, min(2.0, 1.0 + (volatility - 0.01) * 50))
            tp = self._get_coin_param(symbol, 'tp_pct', self.tp_pct)
            sl = self._get_coin_param(symbol, 'sl_pct', self.sl_pct)
            return tp * factor, sl * factor
        except Exception:
            return self._get_coin_param(symbol, 'tp_pct', self.tp_pct), self._get_coin_param(symbol, 'sl_pct', self.sl_pct)

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
            [InlineKeyboardButton("🔄 同步持仓", callback_data="sync_pos"),
             InlineKeyboardButton("🔄 刷新", callback_data="refresh_panel")]
        ])

    def _build_option_keyboard(self, options, prefix, setting_key):
        kb = []
        row = []
        for label, val in options:
            row.append(InlineKeyboardButton(label, callback_data=f"{prefix}:{val}"))
            if len(row) == 2:
                kb.append(row)
                row = []
        if row:
            kb.append(row)
        kb.append([InlineKeyboardButton("✍️ 自填", callback_data=f"prompt_manual:{setting_key}")])
        kb.append([InlineKeyboardButton("🔙 返回", callback_data="refresh_panel")])
        return InlineKeyboardMarkup(kb)

    # ==================== 命令函数 ====================

    def _auth(self, update: Update):
        if not self.allowed:
            return True
        return update.effective_user.id in self.allowed

    def _parse_pct(self, val):
        return val / 100.0

    async def cmd_menu(self, update, context):
        if not self._auth(update):
            await update.message.reply_text("⛔ 未授权")
            return
        await update.effective_message.reply_text(f"⚙️ 控制台 {self.env_tag}", reply_markup=self._build_main_keyboard())

    async def cmd_holdings(self, update, context):
        if not self._auth(update):
            return
        bal = await self.exchange.fetch_balance()
        lines = ["📋 **当前持币**\n"]
        has_any = False
        for sym in self.symbols:
            coin = sym.split('/')[0]
            free = bal.get(coin, {}).get('free', 0) if isinstance(bal.get(coin), dict) else float(bal.get(coin, 0))
            if free > 0.0001:
                has_any = True
                ticker = self.ws.get_ticker(sym)
                if ticker is None:
                    ticker = await self.exchange.fetch_ticker(sym)
                if ticker:
                    p = ticker['last']
                    val = free * p
                    count = self.position_counts.get(sym, 0)
                    pnl = ""
                    if sym in self.entries and self.entries[sym] > 0:
                        pnl_pct = ((p - self.entries[sym]) / self.entries[sym]) * 100
                        pnl = f" | {'🟢' if pnl_pct >= 0 else '🔴'} {pnl_pct:+.2f}%"
                    lines.append(f"• {sym}: {free:.4f} 现价{p:.2f} 价值{val:.2f} 仓位{count}/{self.max_positions_per_coin}{pnl}")
        if not has_any:
            lines.append("暂无持仓")
        await update.effective_message.reply_text("\n".join(lines), parse_mode="Markdown")

    async def cmd_autotrade(self, update, context):
        if not self._auth(update):
            return
        try:
            mode = context.args[0].lower()
            if mode == "on":
                self.auto_trade_enabled = True
                await self._save_config()
                await update.effective_message.reply_text("🤖 自动交易已开启")
            elif mode == "off":
                self.auto_trade_enabled = False
                await self._save_config()
                await update.effective_message.reply_text("🤖 自动交易已关闭")
            else:
                await update.effective_message.reply_text("用法: /autotrade on|off")
        except:
            pass

    async def cmd_autoscore(self, update, context):
        if not self._auth(update):
            return
        try:
            score = int(context.args[0])
            if 50 <= score <= 95:
                self.auto_min_score = score
                await self._save_config()
                await update.effective_message.reply_text(f"✅ 阈值: {score}分")
            else:
                await update.effective_message.reply_text("阈值需在50-95之间")
        except:
            pass

    async def cmd_set_max_coin(self, update, context):
        if not self._auth(update):
            return
        try:
            self.max_per_coin_usdt = float(context.args[0])
            await self._save_config()
            await update.effective_message.reply_text(f"✅ 单币最大持仓: {self.max_per_coin_usdt}U")
        except:
            await update.effective_message.reply_text("❌ 格式: /setmaxcoin 200")

    async def cmd_set_max_loss(self, update, context):
        if not self._auth(update):
            return
        try:
            pct = float(context.args[0]) / 100.0
            self.max_daily_loss_pct = pct
            await self._save_config()
            await update.effective_message.reply_text(f"✅ 日亏损熔断: {pct * 100:.1f}%")
        except:
            await update.effective_message.reply_text("❌ /setmaxloss 5")

    async def cmd_set_max_pos(self, update, context):
        if not self._auth(update):
            return
        try:
            num = int(context.args[0])
            if num < 1:
                raise ValueError
            self.max_positions_per_coin = num
            await self._save_config()
            await update.effective_message.reply_text(f"✅ 每币最大仓位: {num}")
        except:
            await update.effective_message.reply_text("❌ /setmaxpos 18")

    async def cmd_set_max_alloc(self, update, context):
        if not self._auth(update):
            return
        try:
            pct = float(context.args[0]) / 100.0
            self.max_total_allocated_pct = max(0.1, min(1.0, pct))
            await self._save_config()
            await update.effective_message.reply_text(f"✅ 总仓位上限: {self.max_total_allocated_pct * 100:.0f}%")
        except:
            await update.effective_message.reply_text("❌ /setmaxalloc 80")

    async def cmd_learn(self, update, context):
        if not self._auth(update):
            return
        try:
            mode = context.args[0].lower()
            if mode == "on":
                self.learning_enabled = True
                await update.effective_message.reply_text("🧠 自适应学习已开启")
            elif mode == "off":
                self.learning_enabled = False
                await update.effective_message.reply_text("🧠 自适应学习已关闭")
            else:
                await update.effective_message.reply_text("用法: /learn on|off")
        except:
            pass

    async def cmd_stats(self, update, context):
        if not self._auth(update):
            return
        bal = await self.exchange.fetch_balance()
        usdt_free = self._get_usdt_free(bal)
        total_value = usdt_free
        positions = []
        for sym in self.symbols:
            ticker = self.ws.get_ticker(sym)
            if ticker is None:
                ticker = await self.exchange.fetch_ticker(sym)
            if ticker is None:
                continue
            p = ticker['last']
            coin = sym.split('/')[0]
            free = bal.get(coin, {}).get('free', 0) if isinstance(bal.get(coin), dict) else 0
            val = free * p
            total_value += val
            count = self.position_counts.get(sym, 0)
            positions.append(f"{sym}: {free:.4f} 价值{val:.2f}U 仓位{count}/{self.max_positions_per_coin}")
        today = await get_today_trades()
        lines = [
            f"📊 **仪表盘** {self.env_tag}",
            f"💰 总资产: {total_value:.2f}U | 可用: {usdt_free:.2f}U",
            f"📈 持仓:",
            *positions,
            f"━━━━━━━━━━━━━━━━━"
        ]
        if today:
            lines.append(f"今日交易: {today['total']}笔 胜率{today['win_rate']:.0%} 总盈亏{today['total_pnl_sum']:+.2f}%")
        else:
            lines.append("今日暂无平仓记录")
        lines.append(f"自适应学习: {'🟢' if self.learning_enabled else '🔴'} | 阈值: {self.auto_min_score} | 仓位: {self.single_order_usdt}U")
        lines.append(f"回撤熔断: {self.max_drawdown_pct * 100:.0f}%")
        await update.effective_message.reply_text("\n".join(lines), parse_mode="Markdown")

    async def cmd_backup(self, update, context):
        if not self._auth(update):
            return
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
        if not self._auth(update):
            return
        try:
            sym = context.args[0].upper()
            price = float(context.args[1])
            self.entries[sym] = price
            await update.effective_message.reply_text(f"📝 {sym} 入场价: {price:.2f}")
        except:
            await update.effective_message.reply_text("❌ `/entry ETH/USDT 3120`")

    async def cmd_set_trades(self, update, context):
        if not self._auth(update):
            return
        try:
            self.max_daily_trades = int(context.args[0])
            await self._save_config()
            await update.effective_message.reply_text(f"✅ 单日最大交易: {self.max_daily_trades}")
        except:
            pass

    async def cmd_reset_trades(self, update, context):
        if not self._auth(update):
            return
        self.daily_trades = 0
        await update.effective_message.reply_text("✅ 计数已重置")

    async def cmd_preset(self, update, context):
        if not self._auth(update):
            return
        try:
            mode = context.args[0].lower()
            presets = {
                "conservative": {"tp": 3, "sl": 2, "tsl": 1, "tmpt": 1, "tf": "1h", "amt": 1, "reserve": 2},
                "balanced": {"tp": 1.5, "sl": 1, "tsl": 0.5, "tmpt": 0.5, "tf": "15m", "amt": 1, "reserve": 1},
                "aggressive": {"tp": 0.8, "sl": 0.5, "tsl": 0.3, "tmpt": 0.3, "tf": "5m", "amt": 1, "reserve": 0.5},
                "ETH滚雪球": {"tp": 0.8, "sl": 0.5, "tsl": 0.5, "tmpt": 0.3, "tf": "1m", "amt": 10, "reserve": 5, "score": 60},
                "BTC滚雪球": {"tp": 0.6, "sl": 0.4, "tsl": 0.4, "tmpt": 0.2, "tf": "1m", "amt": 10, "reserve": 5, "score": 60},
                "SOL滚雪球": {"tp": 1.0, "sl": 0.5, "tsl": 0.5, "tmpt": 0.3, "tf": "1m", "amt": 1, "reserve": 1, "score": 60},
                "DOGE滚雪球": {"tp": 1.2, "sl": 0.6, "tsl": 0.6, "tmpt": 0.4, "tf": "1m", "amt": 1, "reserve": 1, "score": 60},
                "ADA滚雪球": {"tp": 1.2, "sl": 0.6, "tsl": 0.6, "tmpt": 0.4, "tf": "1m", "amt": 0.5, "reserve": 0.5, "score": 60},
                "XRP滚雪球": {"tp": 1.0, "sl": 0.5, "tsl": 0.5, "tmpt": 0.3, "tf": "1m", "amt": 0.5, "reserve": 0.5, "score": 60},
                "MATIC滚雪球": {"tp": 1.2, "sl": 0.6, "tsl": 0.6, "tmpt": 0.4, "tf": "1m", "amt": 0.5, "reserve": 0.5, "score": 60},
            }
            if mode not in presets:
                await update.effective_message.reply_text("可选: conservative/balanced/aggressive/滚雪球系列")
                return
            p = presets[mode]
            self.tp_pct = p["tp"] / 100
            self.sl_pct = p["sl"] / 100
            self.trailing_sl_pct = p["tsl"] / 100
            self.trailing_tp_pct = p["tmpt"] / 100
            self.timeframe = p["tf"]
            self.single_order_usdt = p["amt"]
            self.reserve_bottom = p["reserve"]
            if "score" in p:
                self.auto_min_score = p["score"]
            await self._save_config()
            names = {
                "conservative": "保守",
                "balanced": "平衡",
                "aggressive": "激进",
                "ETH滚雪球": "ETH滚雪球",
                "BTC滚雪球": "BTC滚雪球",
                "SOL滚雪球": "SOL滚雪球",
                "DOGE滚雪球": "DOGE滚雪球",
                "ADA滚雪球": "ADA滚雪球",
                "XRP滚雪球": "XRP滚雪球",
                "MATIC滚雪球": "MATIC滚雪球"
            }
            await update.effective_message.reply_text(
                f"⚡ {names[mode]}方案已生效\n止盈{self.tp_pct * 100:.1f}% 止损{self.sl_pct * 100:.1f}%"
            )
        except:
            pass

    async def cmd_history(self, update, context):
        if not self._auth(update):
            return
        if not self.trades:
            await update.effective_message.reply_text("📜 暂无记录")
            return
        lines = ["📜 **最近交易**\n"]
        for t in self.trades[:10]:
            net_pnl = t.get('net_pnl', 0)
            net_pnl_pct = t.get('net_pnl_pct', 0)
            if net_pnl != 0:
                lines.append(f"{'🟢' if net_pnl_pct > 0 else '🔴'} {t['time']} {t['symbol']} 净利{net_pnl_pct:+.2f}% ({net_pnl:+.4f}U)")
            else:
                lines.append(f"{'🟢' if t['pnl_pct'] > 0 else '🔴'} {t['time']} {t['symbol']} {t['pnl_pct']:+.2f}%")
        await update.effective_message.reply_text("\n".join(lines), parse_mode="Markdown")

    async def cmd_status(self, update, context):
        if not self._auth(update):
            return
        lines = ["📊 **持仓**\n"]
        bal = await self.exchange.fetch_balance()
        for sym in self.symbols:
            ticker = self.ws.get_ticker(sym)
            if ticker is None:
                ticker = await self.exchange.fetch_ticker(sym)
            if ticker is None:
                continue
            p = ticker['last']
            coin = sym.split('/')[0]
            free = bal.get(coin, {}).get('free', 0) if isinstance(bal.get(coin), dict) else float(bal.get(coin, 0))
            val = free * p
            count = self.position_counts.get(sym, 0)
            pnl = ""
            if sym in self.entries and self.entries[sym] > 0 and free > 0:
                pnl_pct = ((p - self.entries[sym]) / self.entries[sym]) * 100
                pnl = f" | {'🟢' if pnl_pct >= 0 else '🔴'} {pnl_pct:+.2f}%"
            lines.append(f"{sym}: {free:.4f} 现价{p:.2f} 价值{val:.2f} 仓位{count}/{self.max_positions_per_coin}{pnl}")
        lines.append(f"💵 USDT: {self._get_usdt_free(bal):.2f}")
        await update.effective_message.reply_text("\n".join(lines))

    async def cmd_check(self, update, context):
        if not self._auth(update):
            return
        lines = ["📈 **信号 + 开仓条件**\n"]
        fg_data = await self.real_data.get_fear_greed_index()
        fg = fg_data["value"] if fg_data else None
        bal = await self.exchange.fetch_balance()
        usdt_free = self._get_usdt_free(bal)
        for sym in self.symbols:
            try:
                ticker = self.ws.get_ticker(sym)
                if ticker is None:
                    continue
                p = ticker['last']
                tech = await self.tech.calc(sym, self.timeframe, 50)
                funding = await self.exchange.fetch_funding_rate(sym)
                sc = self.signal_engine.score(tech, funding, fg)
                txt = self.signal_engine.interpret(sc)
                coin = sym.split('/')[0]
                free = bal.get(coin, {}).get('free', 0) if isinstance(bal.get(coin), dict) else float(bal.get(coin, 0))
                coin_value = free * p
                count = self.position_counts.get(sym, 0)
                coin_score = self._get_coin_param(sym, 'auto_min_score', self.auto_min_score)
                cond_signal = sc >= coin_score
                cond_price = p <= tech['bb_lower'] * 1.02
                cond_book = True
                if self.orderbook_filter:
                    ob = self.ws.get_orderbook(sym)
                    if ob is None:
                        ob = await self.exchange.fetch_orderbook(sym)
                    cond_book, _ = await self.orderbook_engine.validate(ob)
                cond_pos = count < self.max_positions_per_coin
                if self.max_per_coin_usdt > 0 and coin_value >= self.max_per_coin_usdt:
                    cond_pos = False
                coin_amount = self._get_coin_param(sym, 'single_order_usdt', self.single_order_usdt)
                cond_balance = usdt_free >= coin_amount + self.reserve_bottom
                cond_daily = True if self.max_daily_trades <= 0 or self.daily_trades < self.max_daily_trades else False
                cond_str = (f"{'✅' if cond_signal else '❌'}信({coin_score}) {'✅' if cond_price else '❌'}价 "
                            f"{'✅' if cond_book else '❌'}盘 {'✅' if cond_pos else '❌'}仓({count}/{self.max_positions_per_coin}) "
                            f"{'✅' if cond_balance else '❌'}钱 {'✅' if cond_daily else '❌'}天")
                all_met = all([cond_signal, cond_price, cond_book, cond_pos, cond_balance, cond_daily])
                status = "🎯 可开仓" if all_met else "⏳ 等待"
                lines.append(f"{sym}: {p:.2f} | 信号: {txt} ({sc})\n   条件: {cond_str} → {status}")
            except Exception:
                continue
        await update.effective_message.reply_text("\n".join(lines))

    async def cmd_symbols(self, update, context):
        if not self._auth(update):
            return
        s_list = "\n".join([f"• `{s}`" for s in self.symbols])
        await update.effective_message.reply_text(f"📋 **监控列表**:\n{s_list}", parse_mode="Markdown")

    async def cmd_panic(self, update, context):
        if not self._auth(update):
            return
        await self.panic_sell_all()
        await update.effective_message.reply_text("🚨 全平")

    async def cmd_analysis(self, update, context):
        await self.render_gap_analysis(update.effective_message)

    async def cmd_brain(self, update, context):
        await self.render_brain_status(update.effective_message)

    async def cmd_help(self, update, context):
        await update.effective_message.reply_text(
            f"🤖 **命令列表**\n"
            f"/stats 仪表盘 /backup 备份\n"
            f"/menu 控制台 /status 持仓 /check 信号\n"
            f"/settp 5 /setsl 2 /setamount 1\n"
            f"/setcoin DOGE tp 1  独立设币种参数\n"
            f"/resetcoin SOL  重置币种参数\n"
            f"/coininfo  查看币种参数和盈亏\n"
            f"/setgrid SOL 3 1 0.5  固定间距网格\n"
            f"/resetgrid SOL  移除固定网格\n"
            f"/preset SOL滚雪球  一键高频方案\n"
            f"/setmaxpos 18 仓位上限 /setmaxalloc 100 总仓位上限\n"
            f"/autotrade on /learn on\n"
            f"/preset balanced /panic 全平\n"
            f"保本线: >{self.breakeven_pct * 100:.2f}%"
        )

    async def cmd_set_tp(self, update, context):
        if not self._auth(update):
            return
        try:
            val = self._parse_pct(float(context.args[0]))
            if val < self.breakeven_pct:
                await update.effective_message.reply_text(f"❌ 低于保本线 {self.breakeven_pct * 100:.2f}%")
                return
            if self.sl_pct > 0 and val / self.sl_pct < 1.2:
                await update.effective_message.reply_text("❌ 盈亏比不足")
                return
            self.tp_pct = val
            await self._save_config()
            await update.effective_message.reply_text(f"✅ 止盈: {self.tp_pct * 100:.2f}%")
        except:
            pass

    async def cmd_set_sl(self, update, context):
        if not self._auth(update):
            return
        try:
            val = self._parse_pct(float(context.args[0]))
            if self.tp_pct > 0 and self.tp_pct / val < 1.2:
                await update.effective_message.reply_text("❌ 盈亏比不足")
                return
            self.sl_pct = val
            await self._save_config()
            await update.effective_message.reply_text("✅")
        except:
            pass

    async def cmd_set_tsl(self, update, context):
        if not self._auth(update):
            return
        try:
            self.trailing_sl_pct = self._parse_pct(float(context.args[0]))
            await self._save_config()
            await update.effective_message.reply_text("✅")
        except:
            pass

    async def cmd_set_trailing_tp(self, update, context):
        if not self._auth(update):
            return
        try:
            val = self._parse_pct(float(context.args[0]))
            self.trailing_tp_pct = val
            await self._save_config()
            await update.effective_message.reply_text(f"✅ 移动止盈: {self.trailing_tp_pct * 100:.2f}%")
        except:
            pass

    async def cmd_set_amount(self, update, context):
        if not self._auth(update):
            return
        try:
            self.single_order_usdt = float(context.args[0])
            await self._save_config()
            await update.effective_message.reply_text("✅")
        except:
            pass

    async def cmd_set_tf(self, update, context):
        if not self._auth(update):
            return
        try:
            self.timeframe = context.args[0].lower()
            await self._save_config()
            await update.effective_message.reply_text("✅")
        except:
            pass

    async def cmd_set_reserve(self, update, context):
        if not self._auth(update):
            return
        try:
            self.reserve_bottom = float(context.args[0])
            await self._save_config()
            await update.effective_message.reply_text(f"✅ 底线: {self.reserve_bottom}U")
        except:
            pass

    async def cmd_add_symbol(self, update, context):
        if not self._auth(update):
            return
        try:
            sym = context.args[0].upper()
            self.symbols.append(sym)
            await self._save_config()
            await update.effective_message.reply_text("✅")
        except:
            pass

    async def cmd_del_symbol(self, update, context):
        if not self._auth(update):
            return
        try:
            sym = context.args[0].upper()
            self.symbols.remove(sym)
            await self._save_config()
            await update.effective_message.reply_text("✅")
        except:
            pass

    # ==================== 固定网格命令 ====================

    async def cmd_set_grid(self, update, context):
        if not self._auth(update):
            return
        try:
            sym = context.args[0].upper()
            drop_pct = float(context.args[1]) / 100.0
            base_amount = float(context.args[2])
            increment = float(context.args[3]) if len(context.args) > 3 else 1.0
            self.grid_configs[sym] = {"drop_pct": drop_pct, "base_amount": base_amount, "increment": increment}
            await self._save_config()
            await update.effective_message.reply_text(
                f"✅ {sym} 固定网格: 每跌{drop_pct*100:.1f}%买一次, 起始{base_amount}U, 递增{increment}U"
            )
        except:
            await update.effective_message.reply_text("❌ 格式: /setgrid SOL 3 1 0.5")

    async def cmd_reset_grid(self, update, context):
        if not self._auth(update):
            return
        try:
            sym = context.args[0].upper()
            if sym in self.grid_configs:
                del self.grid_configs[sym]
                await self._save_config()
                await update.effective_message.reply_text(f"✅ {sym} 固定网格已移除")
            else:
                await update.effective_message.reply_text(f"⚠️ {sym} 没有固定网格")
        except:
            await update.effective_message.reply_text("❌ 格式: /resetgrid SOL")

    # ==================== 币种独立参数命令 ====================

    async def cmd_set_coin(self, update, context):
        if not self._auth(update):
            return
        try:
            sym = context.args[0].upper()
            key = context.args[1].lower()
            val_str = context.args[2]
            key_map = {
                'tp': 'tp_pct',
                'sl': 'sl_pct',
                'tsl': 'trailing_sl_pct',
                'tmpt': 'trailing_tp_pct',
                'amount': 'single_order_usdt',
                'score': 'auto_min_score'
            }
            if key not in key_map:
                await update.effective_message.reply_text(f"❌ 参数: tp/sl/tsl/tmpt/amount/score")
                return
            attr = key_map[key]
            if attr in ('tp_pct', 'sl_pct', 'trailing_sl_pct', 'trailing_tp_pct'):
                val = float(val_str) / 100.0
            elif attr == 'single_order_usdt':
                val = float(val_str)
            elif attr == 'auto_min_score':
                val = int(val_str)
            else:
                val = float(val_str)
            if sym not in self.coin_configs:
                self.coin_configs[sym] = {}
            self.coin_configs[sym][attr] = val
            await self._save_config()
            name_map = {
                'tp_pct': '止盈',
                'sl_pct': '止损',
                'trailing_sl_pct': '移动止损',
                'trailing_tp_pct': '移动止盈',
                'single_order_usdt': '单笔额度',
                'auto_min_score': '信号阈值'
            }
            display = val * 100 if attr in ('tp_pct', 'sl_pct', 'trailing_sl_pct', 'trailing_tp_pct') else val
            unit = '%' if attr in ('tp_pct', 'sl_pct', 'trailing_sl_pct', 'trailing_tp_pct') else 'U' if attr == 'single_order_usdt' else '分'
            await update.effective_message.reply_text(f"✅ {sym} {name_map[attr]}: {display:.1f}{unit}")
        except:
            await update.effective_message.reply_text("❌ 格式: /setcoin DOGE tp 1")

    async def cmd_reset_coin(self, update, context):
        if not self._auth(update):
            return
        try:
            sym = context.args[0].upper()
            if sym in self.coin_configs:
                del self.coin_configs[sym]
                await self._save_config()
                await update.effective_message.reply_text(f"✅ {sym} 独立参数已重置")
            else:
                await update.effective_message.reply_text(f"⚠️ {sym} 没有独立参数")
        except:
            await update.effective_message.reply_text("❌ 格式: /resetcoin SOL")

    async def cmd_coin_info(self, update, context):
        if not self._auth(update):
            return
        target = context.args[0].upper() if context.args else None
        if target and target not in self.symbols:
            await update.effective_message.reply_text(f"⚠️ {target} 不在监控列表中")
            return
        bal = await self.exchange.fetch_balance()
        lines = [f"📊 **币种参数与盈亏** {self.env_tag}\n"]
        symbols_to_show = [target] if target else self.symbols
        for sym in symbols_to_show:
            coin = sym.split('/')[0]
            free = bal.get(coin, {}).get('free', 0) if isinstance(bal.get(coin), dict) else float(bal.get(coin, 0))
            ticker = self.ws.get_ticker(sym)
            if ticker is None:
                ticker = await self.exchange.fetch_ticker(sym)
            p = ticker['last'] if ticker else 0
            val = free * p
            count = self.position_counts.get(sym, 0)
            pnl_str = ""
            if sym in self.entries and self.entries[sym] > 0 and free > 0:
                pnl_pct = ((p - self.entries[sym]) / self.entries[sym]) * 100
                pnl_str = f" | {'🟢' if pnl_pct >= 0 else '🔴'} {pnl_pct:+.2f}%"
            tp = self._get_coin_param(sym, 'tp_pct', self.tp_pct)
            sl = self._get_coin_param(sym, 'sl_pct', self.sl_pct)
            tsl = self._get_coin_param(sym, 'trailing_sl_pct', self.trailing_sl_pct)
            tmpt = self._get_coin_param(sym, 'trailing_tp_pct', self.trailing_tp_pct)
            amount = self._get_coin_param(sym, 'single_order_usdt', self.single_order_usdt)
            score = self._get_coin_param(sym, 'auto_min_score', self.auto_min_score)
            total_net_pnl = 0.0
            try:
                async with aiosqlite.connect("bot.db", timeout=30.0) as db:
                    async with db.execute("SELECT SUM(net_pnl) FROM trade_details WHERE side='sell' AND symbol=? AND net_pnl IS NOT NULL", (sym,)) as cursor:
                        row = await cursor.fetchone()
                        if row and row[0]:
                            total_net_pnl = row[0]
            except:
                pass
            extra = "🔸独立" if sym in self.coin_configs else "🌐全局"
            lines.append(
                f"{extra} **{sym}**\n"
                f"  止盈{tp * 100:.1f}% 止损{sl * 100:.1f}% 移盈{tmpt * 100:.1f}% 移损{tsl * 100:.1f}%\n"
                f"  单笔{amount}U 阈值{score}分 仓位{count}/{self.max_positions_per_coin}\n"
                f"  持仓{free:.4f} 现价{p:.2f} 价值{val:.2f}U{pnl_str}\n"
                f"  累计净盈亏: {total_net_pnl:+.4f}U"
            )
        lines.append("💡 /setcoin 修改独立参数 | /resetcoin 重置为全局")
        await update.effective_message.reply_text("\n".join(lines), parse_mode="Markdown")

    async def render_brain_status(self, msg_obj):
        try:
            macro = await self.real_data.check_macro_risk()
            lines = [f"🧠 **AI 超级大脑** {self.env_tag}", f"1️⃣ 宏观: {macro['status']}"]
            for idx, sym in enumerate(self.symbols):
                try:
                    if idx > 0:
                        await asyncio.sleep(1.5)
                    ticker = self.ws.get_ticker(sym)
                    if ticker is None:
                        ticker = await self.exchange.fetch_ticker(sym)
                    if ticker is None:
                        lines.append(f"{idx + 2}️⃣ {sym}: 现价获取失败")
                        continue
                    p = ticker['last']
                    tech = await self.tech.calc(sym, self.timeframe, 50)
                    lines.append(f"{idx + 2}️⃣ {sym}: {p:.2f} 布林{tech['bb_upper']:.1f}/{tech['bb_lower']:.1f} RSI{tech['rsi']:.0f}")
                except Exception:
                    lines.append(f"{idx + 2}️⃣ {sym}: 数据获取失败")
            await msg_obj.reply_text("\n".join(lines))
        except Exception as e:
            logger.error(f"brain err: {e}")

    async def render_gap_analysis(self, msg_obj):
        try:
            lines = ["📈 **差距分析**\n"]
            for sym in self.symbols:
                ticker = self.ws.get_ticker(sym)
                if ticker is None:
                    ticker = await self.exchange.fetch_ticker(sym)
                if ticker is None:
                    continue
                p = ticker['last']
                try:
                    tech = await self.tech.calc(sym, self.timeframe, 50)
                    target = min(tech['bb_lower'], p * 0.99)
                    gap = ((p - target) / p) * 100
                    lines.append(f"{sym}: {p:.2f} → {target:.2f} ({gap:+.2f}%)")
                except:
                    lines.append(f"{sym}: 指标计算失败")
            await msg_obj.reply_text("\n".join(lines))
        except Exception as e:
            logger.error(f"analysis err: {e}")

    async def handle_text_input(self, update, context):
        pending = context.user_data.get('pending_setting')
        if not pending:
            return
        try:
            user_text = update.message.text.strip()
            if pending in ("settf", "addsymbol", "delsymbol"):
                if pending == "settf":
                    self.timeframe = user_text.lower()
                elif pending == "addsymbol":
                    sym = user_text.upper()
                    if sym not in self.symbols:
                        self.symbols.append(sym)
                    else:
                        await update.message.reply_text("⚠️ 已存在")
                elif pending == "delsymbol":
                    sym = user_text.upper()
                    if sym in self.symbols:
                        self.symbols.remove(sym)
                    else:
                        await update.message.reply_text("⚠️ 不存在")
            else:
                val = float(user_text)
                if pending == "settp":
                    pct = self._parse_pct(val)
                    self.tp_pct = pct
                elif pending == "setsl":
                    self.sl_pct = self._parse_pct(val)
                elif pending == "settsl":
                    self.trailing_sl_pct = self._parse_pct(val)
                elif pending == "settmpt":
                    self.trailing_tp_pct = self._parse_pct(val)
                elif pending == "setamount":
                    self.single_order_usdt = val
                elif pending == "setreserve":
                    self.reserve_bottom = val
                elif pending == "settrades":
                    self.max_daily_trades = int(val)
                elif pending == "autoscore":
                    self.auto_min_score = int(val)
                elif pending == "setmaxcoin":
                    self.max_per_coin_usdt = val
                elif pending == "setmaxloss":
                    self.max_daily_loss_pct = val / 100.0
                elif pending == "setmaxpos":
                    self.max_positions_per_coin = int(val)
                elif pending == "setmaxalloc":
                    self.max_total_allocated_pct = val / 100.0
            await self._save_config()
            context.user_data['pending_setting'] = None
            await update.message.reply_text("✅")
        except ValueError:
            await update.message.reply_text("❌ 格式有误")
            context.user_data['pending_setting'] = None

    async def handle_button_click(self, update, context):
        query = update.callback_query
        data = query.data
        try:
            if data == "refresh_panel":
                await self.cmd_menu(update, context)
            elif data == "toggle_filter":
                self.orderbook_filter = not self.orderbook_filter
                await self._save_config()
                await query.answer(f"盘口过滤已{'开启' if self.orderbook_filter else '关闭'}")
                try:
                    await query.edit_message_reply_markup(reply_markup=self._build_main_keyboard())
                except:
                    pass
            elif data == "toggle_breaker":
                self.waterfall_breaker = not self.waterfall_breaker
                await self._save_config()
                await query.answer(f"瀑布熔断已{'开启' if self.waterfall_breaker else '关闭'}")
                try:
                    await query.edit_message_reply_markup(reply_markup=self._build_main_keyboard())
                except:
                    pass
            elif data == "toggle_auto":
                self.auto_trade_enabled = not self.auto_trade_enabled
                await self._save_config()
                await query.answer(f"自动交易已{'开启' if self.auto_trade_enabled else '关闭'}")
                try:
                    await query.edit_message_reply_markup(reply_markup=self._build_main_keyboard())
                except:
                    pass
            elif data == "bot_start":
                self.is_running = True
                await query.answer("已开启")
            elif data == "bot_stop":
                self.is_running = False
                await query.answer("已关机")
            elif data == "brain_status":
                await self.render_brain_status(query.message)
                await query.answer()
            elif data == "gap_analysis":
                await self.render_gap_analysis(query.message)
                await query.answer()
            elif data == "dashboard":
                auto_state = "开启" if self.auto_trade_enabled else "关闭"
                msg = (
                    f"📊 看板\n止盈{self.tp_pct * 100:.2f}% 止损{self.sl_pct * 100:.2f}%\n"
                    f"移损{self.trailing_sl_pct * 100:.2f}% 移盈{self.trailing_tp_pct * 100:.2f}%\n"
                    f"额度{self.single_order_usdt}U 周期{self.timeframe} 底线{self.reserve_bottom}U\n"
                    f"自动交易: {auto_state} 阈值: {self.auto_min_score}分\n"
                    f"仓位上限: {self.max_positions_per_coin}个\n"
                    f"日熔断: {self.max_daily_loss_pct * 100:.1f}%\n"
                    f"今日交易: {self.daily_trades}/{self.max_daily_trades if self.max_daily_trades > 0 else '∞'}"
                )
                await query.message.reply_text(msg)
                await query.answer()
            elif data == "balance":
                bal = await self.exchange.fetch_balance()
                await query.message.reply_text(f"💳 USDT: {self._get_usdt_free(bal):.2f}")
                await query.answer()
            elif data == "history":
                await self.cmd_history(update, context)
            elif data == "holdings":
                await self.cmd_holdings(update, context)
            elif data == "list_symbols":
                await self.cmd_symbols(update, context)
            elif data == "stats_panel":
                await self.cmd_stats(update, context)
            elif data == "backup_panel":
                await self.cmd_backup(update, context)
            elif data == "sync_pos":
                await self._sync_positions()
                await query.message.reply_text("🔄 持仓已同步校准")
                await query.answer("✅ 同步完成", show_alert=True)
            elif data == "menu_preset":
                opts = [
                    ("🛡️保守", "conservative"),
                    ("⚖️平衡", "balanced"),
                    ("⚡激进", "aggressive"),
                    ("🔥ETH", "ETH滚雪球"),
                    ("🔥BTC", "BTC滚雪球"),
                    ("🔥SOL", "SOL滚雪球"),
                    ("🔥DOGE", "DOGE滚雪球"),
                    ("🔥ADA", "ADA滚雪球"),
                    ("🔥XRP", "XRP滚雪球"),
                    ("🔥MATIC", "MATIC滚雪球")
                ]
                kb = [[InlineKeyboardButton(label, callback_data=f"preset:{val}") for label, val in opts[i:i + 2]] for i in range(0, len(opts), 2)]
                kb.append([InlineKeyboardButton("🔙返回", callback_data="refresh_panel")])
                await query.edit_message_text("⚡ 选择方案:", reply_markup=InlineKeyboardMarkup(kb))
                await query.answer()
            elif data.startswith("preset:"):
                mode = data.split(":")[1]
                p = {
                    "conservative": {"tp": 3, "sl": 2, "tsl": 1, "tmpt": 1, "tf": "1h", "amt": 1, "reserve": 2},
                    "balanced": {"tp": 1.5, "sl": 1, "tsl": 0.5, "tmpt": 0.5, "tf": "15m", "amt": 1, "reserve": 1},
                    "aggressive": {"tp": 0.8, "sl": 0.5, "tsl": 0.3, "tmpt": 0.3, "tf": "5m", "amt": 1, "reserve": 0.5},
                    "ETH滚雪球": {"tp": 0.8, "sl": 0.5, "tsl": 0.5, "tmpt": 0.3, "tf": "1m", "amt": 10, "reserve": 5, "score": 60},
                    "BTC滚雪球": {"tp": 0.6, "sl": 0.4, "tsl": 0.4, "tmpt": 0.2, "tf": "1m", "amt": 10, "reserve": 5, "score": 60},
                    "SOL滚雪球": {"tp": 1.0, "sl": 0.5, "tsl": 0.5, "tmpt": 0.3, "tf": "1m", "amt": 1, "reserve": 1, "score": 60},
                    "DOGE滚雪球": {"tp": 1.2, "sl": 0.6, "tsl": 0.6, "tmpt": 0.4, "tf": "1m", "amt": 1, "reserve": 1, "score": 60},
                    "ADA滚雪球": {"tp": 1.2, "sl": 0.6, "tsl": 0.6, "tmpt": 0.4, "tf": "1m", "amt": 0.5, "reserve": 0.5, "score": 60},
                    "XRP滚雪球": {"tp": 1.0, "sl": 0.5, "tsl": 0.5, "tmpt": 0.3, "tf": "1m", "amt": 0.5, "reserve": 0.5, "score": 60},
                    "MATIC滚雪球": {"tp": 1.2, "sl": 0.6, "tsl": 0.6, "tmpt": 0.4, "tf": "1m", "amt": 0.5, "reserve": 0.5, "score": 60}
                }[mode]
                self.tp_pct = p["tp"] / 100
                self.sl_pct = p["sl"] / 100
                self.trailing_sl_pct = p["tsl"] / 100
                self.trailing_tp_pct = p["tmpt"] / 100
                self.timeframe = p["tf"]
                self.single_order_usdt = p["amt"]
                self.reserve_bottom = p["reserve"]
                if "score" in p:
                    self.auto_min_score = p["score"]
                await self._save_config()
                await query.answer("✅ 已生效", show_alert=True)
                await self._refresh_panel(query)
            elif data == "menu_set_autoscore":
                opts = [("70分", "70"), ("75分", "75"), ("80分", "80"), ("85分", "85")]
                await query.edit_message_text("🎯 阈值", reply_markup=self._build_option_keyboard(opts, "cfg_autoscore", "autoscore"))
                await query.answer()
            elif data == "menu_set_trades":
                opts = [("3次", "3"), ("5次", "5"), ("10次", "10"), ("无限", "0")]
                await query.edit_message_text("🔢 上限", reply_markup=self._build_option_keyboard(opts, "cfg_trades", "settrades"))
                await query.answer()
            elif data == "menu_set_tp":
                opts = [("3%", "0.03"), ("5%", "0.05"), ("8%", "0.08")]
                await query.edit_message_text("🎯", reply_markup=self._build_option_keyboard(opts, "cfg_tp", "settp"))
                await query.answer()
            elif data == "menu_set_sl":
                opts = [("1%", "0.01"), ("2%", "0.02"), ("3%", "0.03")]
                await query.edit_message_text("🛡️", reply_markup=self._build_option_keyboard(opts, "cfg_sl", "setsl"))
                await query.answer()
            elif data == "menu_set_tsl":
                opts = [("0.5%", "0.005"), ("1%", "0.01"), ("1.5%", "0.015")]
                await query.edit_message_text("📉", reply_markup=self._build_option_keyboard(opts, "cfg_tsl", "settsl"))
                await query.answer()
            elif data == "menu_set_tmpt":
                opts = [("0.5%", "0.005"), ("1%", "0.01"), ("1.5%", "0.015")]
                await query.edit_message_text("🏹", reply_markup=self._build_option_keyboard(opts, "cfg_tmpt", "settmpt"))
                await query.answer()
            elif data == "menu_set_amount":
                opts = [("1U", "1"), ("2U", "2"), ("5U", "5")]
                await query.edit_message_text("💵", reply_markup=self._build_option_keyboard(opts, "cfg_amt", "setamount"))
                await query.answer()
            elif data == "menu_set_tf":
                opts = [("1m", "1m"), ("5m", "5m"), ("15m", "15m"), ("1h", "1h")]
                await query.edit_message_text("⏱", reply_markup=self._build_option_keyboard(opts, "cfg_tf", "settf"))
                await query.answer()
            elif data == "menu_set_reserve":
                opts = [("0.5U", "0.5"), ("1U", "1"), ("2U", "2"), ("5U", "5")]
                await query.edit_message_text("🔒", reply_markup=self._build_option_keyboard(opts, "cfg_res", "setreserve"))
                await query.answer()
            elif data == "menu_add_symbol":
                opts = [("BTC/USDT", "BTC/USDT"), ("SOL/USDT", "SOL/USDT"), ("DOGE/USDT", "DOGE/USDT"), ("ADA/USDT", "ADA/USDT"), ("XRP/USDT", "XRP/USDT"), ("MATIC/USDT", "MATIC/USDT")]
                await query.edit_message_text("➕", reply_markup=self._build_option_keyboard(opts, "cfg_add", "addsymbol"))
                await query.answer()
            elif data == "menu_del_symbol":
                opts = [(s, s) for s in self.symbols]
                await query.edit_message_text("➖", reply_markup=self._build_option_keyboard(opts, "cfg_del", "delsymbol"))
                await query.answer()
            elif data.startswith("cfg_"):
                prefix = data.split(":")[0] if ":" in data else ""
                val_str = data.split(":")[1] if ":" in data else ""
                if prefix == "cfg_tp":
                    val_f = float(val_str)
                    if val_f < self.breakeven_pct:
                        await query.answer(f"❌ 低于保本线", show_alert=True)
                        return
                    if self.sl_pct > 0 and val_f / self.sl_pct < 1.2:
                        await query.answer("❌ 盈亏比不足", show_alert=True)
                        return
                    self.tp_pct = val_f
                elif prefix == "cfg_sl":
                    val_f = float(val_str)
                    if self.tp_pct > 0 and self.tp_pct / val_f < 1.2:
                        await query.answer("❌ 盈亏比不足", show_alert=True)
                        return
                    self.sl_pct = val_f
                elif prefix == "cfg_tsl":
                    self.trailing_sl_pct = float(val_str)
                elif prefix == "cfg_tmpt":
                    self.trailing_tp_pct = float(val_str)
                elif prefix == "cfg_amt":
                    self.single_order_usdt = float(val_str)
                elif prefix == "cfg_tf":
                    self.timeframe = val_str
                elif prefix == "cfg_res":
                    self.reserve_bottom = float(val_str)
                elif prefix == "cfg_autoscore":
                    self.auto_min_score = int(val_str)
                elif prefix == "cfg_trades":
                    self.max_daily_trades = int(val_str)
                elif prefix == "cfg_add":
                    if val_str not in self.symbols:
                        self.symbols.append(val_str)
                    else:
                        await query.answer("已存在", show_alert=True)
                        return
                elif prefix == "cfg_del":
                    if val_str in self.symbols:
                        self.symbols.remove(val_str)
                    else:
                        await query.answer("不存在", show_alert=True)
                        return
                await self._save_config()
                await query.answer("✅", show_alert=True)
                await self._refresh_panel(query)
            elif data.startswith("prompt_manual:"):
                key = data.split(":")[1]
                context.user_data['pending_setting'] = key
                prompts = {
                    "settp": "✍️ 止盈率（例：6.5%）：",
                    "setsl": "✍️ 硬止损率（例：2.5%）：",
                    "settsl": "✍️ 移动止损回调（例：1.5%）：",
                    "settmpt": "✍️ 移动止盈回调（例：1%）：",
                    "setamount": "✍️ 单笔 USDT（例：150）：",
                    "settf": "✍️ K线周期（例：15m）：",
                    "setreserve": "✍️ 安全底线（例：100）：",
                    "addsymbol": "✍️ 币种（例：DOGE/USDT）：",
                    "delsymbol": "✍️ 要删除的币种：",
                    "autoscore": "✍️ 信号阈值（50-95）：",
                    "settrades": "✍️ 单日最大交易次数：",
                    "setmaxcoin": "✍️ 单币最大持仓U：",
                    "setmaxloss": "✍️ 日熔断百分比（例：5）：",
                    "setmaxpos": "✍️ 每币最大仓位数量：",
                    "setmaxalloc": "✍️ 总仓位上限%（例：80）：",
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
            try:
                await query.answer("操作失败，请重试", show_alert=True)
            except:
                pass

    async def _refresh_panel(self, query):
        try:
            await query.edit_message_text(f"⚙️ 控制台 {self.env_tag}", reply_markup=self._build_main_keyboard())
        except:
            pass

    # ==================== 核心监控任务 ====================

    async def panic_sell_all(self):
        for sym in self.symbols:
            await self.exchange.cancel_all_orders(sym)
            bal = await self.exchange.fetch_balance()
            coin = sym.split('/')[0]
            amount = bal.get(coin, {}).get('free', 0) if isinstance(bal.get(coin), dict) else 0
            if isinstance(amount, (int, float)) and amount > 0:
                rounded_amount = await self._round_amount_by_precision(sym, amount)
                if rounded_amount > 0:
                    await self.exchange.create_market_sell_order(sym, rounded_amount)
            self.position_counts[sym] = 0
        await self._save_runtime_state()

    async def _risk_monitor_task(self):
        while True:
            try:
                self._btc_safe_flag = await self._check_btc_risk()
                bal = await self.exchange.fetch_balance()
                usdt_free = self._get_usdt_free(bal)
                total_value = usdt_free
                for sym in self.symbols:
                    ticker = self.ws.get_ticker(sym)
                    if ticker:
                        coin = sym.split('/')[0]
                        free = bal.get(coin, {}).get('free', 0) if isinstance(bal.get(coin), dict) else 0
                        total_value += free * ticker['last']
                if total_value > self.peak_total_value:
                    self.peak_total_value = total_value
                if self.peak_total_value > 0:
                    drawdown = (self.peak_total_value - total_value) / self.peak_total_value
                    self._drawdown_safe_flag = drawdown <= self.max_drawdown_pct
                else:
                    self._drawdown_safe_flag = True
            except Exception as e:
                logger.error(f"风控监控异常: {e}")
            await asyncio.sleep(60)

    async def _auto_trade_monitor(self):
        await asyncio.sleep(10)
        while True:
            try:
                if not self.is_running or not self.auto_trade_enabled:
                    await asyncio.sleep(10)
                    continue

                today = datetime.now(CST).day
                if today != self.last_reset_day:
                    self.daily_trades = 0
                    self.last_reset_day = today
                if self.max_daily_trades > 0 and self.daily_trades >= self.max_daily_trades:
                    await asyncio.sleep(10)
                    continue

                usdt_free = await self._refresh_balance_cache()

                if not self._drawdown_safe_flag:
                    await self._alert(f"⛔ 回撤熔断触发", "critical")
                    await asyncio.sleep(60)
                    continue

                if self.api_error_count >= self.max_api_errors:
                    if asyncio.get_event_loop().time() - self.api_error_pause_time < 1800:
                        await asyncio.sleep(60)
                        continue
                    else:
                        self.api_error_count = 0

                today_stats = await get_today_trades()
                if today_stats and today_stats['total'] >= 3:
                    if today_stats['win_rate'] < 0.2 and abs(today_stats['avg_loss_pct']) > self.max_daily_loss_pct:
                        await self._alert("⛔ 日亏损熔断", "critical")
                        await asyncio.sleep(300)
                        continue

                # ========== 连续失败熔断修复 ==========
                if self.consecutive_failures >= 3:
                    await self._alert(f"⚠️ 连续开仓失败 {self.consecutive_failures} 次，暂停60秒", "warning")
                    self.consecutive_failures = 0
                    self.last_failure_time = asyncio.get_event_loop().time()
                    await asyncio.sleep(60)
                    continue

                fg_data = await self.real_data.get_fear_greed_index()
                fg = fg_data["value"] if fg_data else None

                candidates = []
                for sym in self.symbols:
                    try:
                        if sym != "BTC/USDT" and not self._btc_safe_flag:
                            continue

                        ticker = self.ws.get_ticker(sym)
                        if ticker is None:
                            continue
                        p = ticker['last']
                        coin = sym.split('/')[0]
                        free = self._cached_balances.get(coin, 0)
                        coin_value = free * p
                        count = self.position_counts.get(sym, 0)

                        if count >= self.max_positions_per_coin:
                            continue
                        if self.max_per_coin_usdt > 0 and coin_value >= self.max_per_coin_usdt:
                            continue

                        grid = self.grid_configs.get(sym)
                        if grid:
                            last_trigger = self._last_grid_entry.get(sym, p)
                            drop_from_last = (last_trigger - p) / last_trigger if last_trigger else 0
                            if drop_from_last >= grid["drop_pct"]:
                                count = self.position_counts.get(sym, 0)
                                coin_amount = grid["base_amount"] * (1 + count * grid["increment"])
                                self._last_grid_entry[sym] = p
                                candidates.append((100, sym, p, None, self.tp_pct, self.sl_pct, 2.0, coin_amount))
                                logger.info(f"📊 固定网格触发 {sym} 下跌{drop_from_last * 100:.2f}%，金额{coin_amount:.2f}U")
                            continue

                        tech_vol = await self.tech.calc(sym, self.timeframe, 20)
                        vol_factor = max(1.5, min(2.5, 2.0 * (tech_vol['atr'] / tech_vol['bb_middle'] * 100)))
                        tech = await self.tech.calc(sym, self.timeframe, 50, bb_multiplier=vol_factor)

                        volatility = tech['atr'] / tech['bb_middle']
                        bb_threshold = 1.01 + min(0.04, volatility * 150)
                        if p > tech['bb_lower'] * bb_threshold:
                            continue

                        funding = await self.exchange.fetch_funding_rate(sym)
                        sc = self.signal_engine.score(tech, funding, fg)
                        coin_score = self._get_coin_param(sym, 'auto_min_score', self.auto_min_score)
                        if sc < coin_score:
                            continue

                        if self._check_rsi_divergence(sym, tech):
                            sc += 10

                        if self.orderbook_filter:
                            ob = self.ws.get_orderbook(sym)
                            if ob is None:
                                continue
                            ob_valid, _ = await self.orderbook_engine.validate(ob)
                            if not ob_valid:
                                continue

                        coin_amount = self._get_dynamic_amount(sym)
                        dyn_tp, dyn_sl = await self._adjust_tp_sl_by_volatility(sym)
                        candidates.append((sc, sym, p, funding, dyn_tp, dyn_sl, vol_factor, coin_amount))
                    except Exception as e:
                        logger.error(f"候选生成异常 {sym}: {e}")
                        continue

                candidates.sort(key=lambda x: x[0], reverse=True)

                # ========== 多币种并发开仓（每个币种每轮只开一次） ==========
                opened_coins = set()

                for item in candidates:
                    if len(item) == 8:
                        sc, sym, p, funding, dyn_tp, dyn_sl, vol_factor, coin_amount = item
                    else:
                        sc, sym, p, funding, dyn_tp, dyn_sl, vol_factor = item
                        coin_amount = self._get_dynamic_amount(sym)

                    if sym in opened_coins:
                        continue

                    if usdt_free < coin_amount + self.reserve_bottom:
                        break

                    coin = sym.split('/')[0]
                    old_usdt_free = usdt_free
                    old_balance = self._cached_balances.get(coin, 0)

                    raw_amount = coin_amount / p
                    rounded_amount = await self._round_amount_by_precision(sym, raw_amount)
                    if rounded_amount <= 0:
                        logger.warning(f"⚠️ {sym} 下单数量 {rounded_amount:.8f} 无效，跳过")
                        continue

                    order = await self.exchange.create_market_buy_order(sym, rounded_amount)
                    if order:
                        self.daily_trades += 1
                        await asyncio.sleep(2)

                        await self._refresh_balance_cache(force=True)
                        new_balance = self._cached_balances.get(coin, 0)
                        new_usdt_free = self._cached_usdt_free

                        real_cost = old_usdt_free - new_usdt_free
                        if real_cost <= 0:
                            real_cost = coin_amount

                        if new_balance > old_balance:
                            self.entries[sym] = p
                            self._trailing_high[sym] = p
                            self._trailing_active[sym] = False
                            self.position_counts[sym] = self.position_counts.get(sym, 0) + 1
                            self.entry_details[sym] = {
                                'signal_score': sc,
                                'fear_greed': fg,
                                'funding_rate': funding,
                                'dyn_tp': dyn_tp,
                                'dyn_sl': dyn_sl,
                                'real_cost': real_cost
                            }
                            await save_trade_detail({
                                "time": datetime.now(CST).strftime("%m-%d %H:%M"),
                                "symbol": sym,
                                "side": "buy",
                                "price": p,
                                "amount": rounded_amount,
                                "signal_score": sc,
                                "fear_greed": fg or 0,
                                "funding_rate": funding or 0,
                                "pnl_pct": 0,
                                "real_cost": round(real_cost, 4)
                            })
                            await self._save_runtime_state()
                            self.consecutive_failures = 0
                            usdt_free = new_usdt_free
                            opened_coins.add(sym)

                            if settings.TG_CHAT_ID:
                                try:
                                    await self.tg_app.bot.send_message(
                                        chat_id=settings.TG_CHAT_ID,
                                        text=f"🤖 开仓 {sym} {coin_amount}U @ {p:.2f} 仓位{self.position_counts[sym]}/{self.max_positions_per_coin}"
                                    )
                                except:
                                    pass
                        else:
                            self.consecutive_failures += 1
                            self.last_failure_time = asyncio.get_event_loop().time()
                    else:
                        self.consecutive_failures += 1
                        self.last_failure_time = asyncio.get_event_loop().time()
                    await asyncio.sleep(1)

                await asyncio.sleep(10)
            except Exception as e:
                logger.error(f"自动交易错误: {e}")
                self.api_error_count += 1
                self.api_error_pause_time = asyncio.get_event_loop().time()
                await asyncio.sleep(10)

    async def _trailing_monitor(self):
        await asyncio.sleep(5)
        while True:
            try:
                if not self.is_running:
                    await asyncio.sleep(5)
                    continue

                await self._refresh_balance_cache()
                bal = self._cached_balances

                for sym in self.symbols:
                    try:
                        if self.position_counts.get(sym, 0) <= 0:
                            continue

                        ticker = self.ws.get_ticker(sym)
                        if ticker is None:
                            continue
                        p = ticker['last']

                        coin = sym.split('/')[0]
                        amount = bal.get(coin, 0)
                        if amount <= 0:
                            self.position_counts[sym] = 0
                            await self._save_runtime_state()
                            continue

                        entry_price = self.entries.get(sym, p)
                        detail = self.entry_details.get(sym, {})
                        use_tp = detail.get('dyn_tp', self._get_coin_param(sym, 'tp_pct', self.tp_pct))
                        use_sl = detail.get('dyn_sl', self._get_coin_param(sym, 'sl_pct', self.sl_pct))
                        use_tsl = self._get_coin_param(sym, 'trailing_sl_pct', self.trailing_sl_pct)
                        use_tmpt = self._get_coin_param(sym, 'trailing_tp_pct', self.trailing_tp_pct)
                        real_cost = detail.get('real_cost', self._get_coin_param(sym, 'single_order_usdt', self.single_order_usdt))

                        # ========== 硬止损 ==========
                        if p <= entry_price * (1 - use_sl):
                            logger.info(f"🛡️ 硬止损 {sym} @ {p:.2f}")
                            rounded_amount = await self._round_amount_by_precision(sym, amount)
                            if rounded_amount > 0:
                                await self.exchange.create_market_sell_order(sym, rounded_amount)
                            await asyncio.sleep(0.5)
                            await self._refresh_balance_cache(force=True)
                            new_usdt = self._cached_usdt_free
                            old_usdt = self._get_usdt_free(bal)
                            net_pnl = new_usdt - old_usdt
                            pnl_pct = ((p - entry_price) / entry_price) * 100
                            if real_cost < 0.01:
                                net_pnl_pct = pnl_pct
                            else:
                                net_pnl_pct = (net_pnl / real_cost) * 100
                            net_pnl_pct = min(1000, max(-1000, net_pnl_pct))
                            trade = {
                                "time": datetime.now(CST).strftime("%m-%d %H:%M"),
                                "symbol": sym,
                                "entry": entry_price,
                                "exit": p,
                                "pnl_pct": round(pnl_pct, 2),
                                "net_pnl": round(net_pnl, 4),
                                "net_pnl_pct": round(net_pnl_pct, 2)
                            }
                            await save_trade(trade)
                            self.trades.insert(0, trade)
                            await save_trade_detail({
                                "time": datetime.now(CST).strftime("%m-%d %H:%M"),
                                "symbol": sym,
                                "side": "sell",
                                "price": p,
                                "amount": amount,
                                "pnl_pct": round(pnl_pct, 2),
                                "signal_score": detail.get('signal_score', 0),
                                "fear_greed": detail.get('fear_greed', 0),
                                "funding_rate": detail.get('funding_rate', 0),
                                "real_revenue": round(net_pnl, 4),
                                "net_pnl_pct": round(net_pnl_pct, 2)
                            })
                            self._trailing_active[sym] = False
                            self._trailing_high[sym] = 0
                            if sym in self.entries:
                                del self.entries[sym]
                            if sym in self.entry_details:
                                del self.entry_details[sym]
                            self.position_counts[sym] = 0
                            await self._save_runtime_state()
                            await self._ai_optimize_params()
                            if settings.TG_CHAT_ID:
                                try:
                                    await self.tg_app.bot.send_message(
                                        chat_id=settings.TG_CHAT_ID,
                                        text=f"🛡️ 硬止损 {sym} @ {p:.2f} 净利{net_pnl_pct:+.2f}% ({net_pnl:+.4f}U)"
                                    )
                                except:
                                    pass
                            continue

                        # ========== 移动止盈激活 ==========
                        if not self._trailing_active.get(sym, False):
                            if p >= entry_price * (1 + use_tp):
                                self._trailing_active[sym] = True
                                self._trailing_high[sym] = p
                        else:
                            if p > self._trailing_high.get(sym, 0):
                                self._trailing_high[sym] = p
                            high = self._trailing_high[sym]

                            # ========== 移动止损 ==========
                            if use_tsl > 0 and p <= high * (1 - use_tsl):
                                logger.info(f"📉 移动止损触发 {sym} @ {p:.2f}")
                                rounded_amount = await self._round_amount_by_precision(sym, amount)
                                if rounded_amount > 0:
                                    await self.exchange.create_market_sell_order(sym, rounded_amount)
                                await asyncio.sleep(0.5)
                                await self._refresh_balance_cache(force=True)
                                new_usdt = self._cached_usdt_free
                                old_usdt = self._get_usdt_free(bal)
                                net_pnl = new_usdt - old_usdt
                                pnl_pct = ((p - entry_price) / entry_price) * 100
                                if real_cost < 0.01:
                                    net_pnl_pct = pnl_pct
                                else:
                                    net_pnl_pct = (net_pnl / real_cost) * 100
                                net_pnl_pct = min(1000, max(-1000, net_pnl_pct))
                                trade = {
                                    "time": datetime.now(CST).strftime("%m-%d %H:%M"),
                                    "symbol": sym,
                                    "entry": entry_price,
                                    "exit": p,
                                    "pnl_pct": round(pnl_pct, 2),
                                    "net_pnl": round(net_pnl, 4),
                                    "net_pnl_pct": round(net_pnl_pct, 2)
                                }
                                await save_trade(trade)
                                self.trades.insert(0, trade)
                                await save_trade_detail({
                                    "time": datetime.now(CST).strftime("%m-%d %H:%M"),
                                    "symbol": sym,
                                    "side": "sell",
                                    "price": p,
                                    "amount": amount,
                                    "pnl_pct": round(pnl_pct, 2),
                                    "signal_score": detail.get('signal_score', 0),
                                    "fear_greed": detail.get('fear_greed', 0),
                                    "funding_rate": detail.get('funding_rate', 0),
                                    "real_revenue": round(net_pnl, 4),
                                    "net_pnl_pct": round(net_pnl_pct, 2)
                                })
                                self._trailing_active[sym] = False
                                self._trailing_high[sym] = 0
                                if sym in self.entries:
                                    del self.entries[sym]
                                if sym in self.entry_details:
                                    del self.entry_details[sym]
                                self.position_counts[sym] = 0
                                await self._save_runtime_state()
                                await self._ai_optimize_params()
                                if settings.TG_CHAT_ID:
                                    try:
                                        await self.tg_app.bot.send_message(
                                            chat_id=settings.TG_CHAT_ID,
                                            text=f"📉 移动止损 {sym} @ {p:.2f} 净利{net_pnl_pct:+.2f}% ({net_pnl:+.4f}U)"
                                        )
                                    except:
                                        pass
                                continue

                            # ========== 移动止盈 ==========
                            if use_tmpt > 0 and p <= high * (1 - use_tmpt):
                                rounded_amount = await self._round_amount_by_precision(sym, amount)
                                if rounded_amount > 0:
                                    await self.exchange.create_market_sell_order(sym, rounded_amount)
                                await asyncio.sleep(0.5)
                                await self._refresh_balance_cache(force=True)
                                new_usdt = self._cached_usdt_free
                                old_usdt = self._get_usdt_free(bal)
                                net_pnl = new_usdt - old_usdt
                                pnl_pct = ((p - entry_price) / entry_price) * 100
                                if real_cost < 0.01:
                                    net_pnl_pct = pnl_pct
                                else:
                                    net_pnl_pct = (net_pnl / real_cost) * 100
                                net_pnl_pct = min(1000, max(-1000, net_pnl_pct))
                                trade = {
                                    "time": datetime.now(CST).strftime("%m-%d %H:%M"),
                                    "symbol": sym,
                                    "entry": entry_price,
                                    "exit": p,
                                    "pnl_pct": round(pnl_pct, 2),
                                    "net_pnl": round(net_pnl, 4),
                                    "net_pnl_pct": round(net_pnl_pct, 2)
                                }
                                await save_trade(trade)
                                self.trades.insert(0, trade)
                                await save_trade_detail({
                                    "time": datetime.now(CST).strftime("%m-%d %H:%M"),
                                    "symbol": sym,
                                    "side": "sell",
                                    "price": p,
                                    "amount": amount,
                                    "pnl_pct": round(pnl_pct, 2),
                                    "signal_score": detail.get('signal_score', 0),
                                    "fear_greed": detail.get('fear_greed', 0),
                                    "funding_rate": detail.get('funding_rate', 0),
                                    "real_revenue": round(net_pnl, 4),
                                    "net_pnl_pct": round(net_pnl_pct, 2)
                                })
                                self._trailing_active[sym] = False
                                self._trailing_high[sym] = 0
                                if sym in self.entries:
                                    del self.entries[sym]
                                if sym in self.entry_details:
                                    del self.entry_details[sym]
                                self.position_counts[sym] = 0
                                await self._save_runtime_state()
                                await self._ai_optimize_params()
                                if settings.TG_CHAT_ID:
                                    try:
                                        await self.tg_app.bot.send_message(
                                            chat_id=settings.TG_CHAT_ID,
                                            text=f"🏹 移动止盈 {sym} @ {p:.2f} 净利{net_pnl_pct:+.2f}% ({net_pnl:+.4f}U)"
                                        )
                                    except:
                                        pass
                    except Exception as e:
                        logger.error(f"追踪异常 {sym}: {e}")
                await asyncio.sleep(5)
            except Exception as e:
                logger.error(f"追踪任务异常: {e}")
                await asyncio.sleep(5)

    async def run(self):
        await self.load_and_init()
        if not self.tg_app:
            return

        ws_ok = await self.ws.connect()
        if ws_ok:
            asyncio.create_task(self.ws.watch_tickers(self.symbols))
            asyncio.create_task(self.ws.watch_orderbooks(self.symbols))

        await self.tg_app.bot.delete_webhook(drop_pending_updates=True)
        asyncio.create_task(self._auto_trade_monitor())
        asyncio.create_task(self._trailing_monitor())
        asyncio.create_task(self._risk_monitor_task())

        while True:
            try:
                await self.tg_app.initialize()
                await self.tg_app.start()
                await self.tg_app.updater.start_polling(drop_pending_updates=True)
                logger.info("✅ Bot 完整版启动")
                if settings.TG_CHAT_ID:
                    try:
                        await self.tg_app.bot.send_message(
                            chat_id=settings.TG_CHAT_ID,
                            text="🤖 量化机器人已上线 (固定网格+动态仓位版)"
                        )
                    except:
                        pass
                while True:
                    await asyncio.sleep(30)
            except Exception as e:
                logger.error(f"Bot 断开，5秒后重连: {e}")
                await asyncio.sleep(5)