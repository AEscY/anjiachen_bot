"""
UltimateBot v14.0 - 精简现货低吸高卖引擎
移除：资金费率套利、三角套利、高级策略模块、复杂信号堆砌
增强：趋势过滤器、动态仓位、数据库连接池
"""
import asyncio
import aiohttp
import os
import json
import aiosqlite
import time
import math
import numpy as np
from datetime import datetime, timezone, timedelta
from collections import deque
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, MessageHandler, filters
from config import settings, logger
from core.indicators import TechnicalEngine
from core.ws_manager import WSDataManager
from storage import (
    init_db, load_config, save_config, load_trades, save_trade,
    save_trade_detail, get_recent_performance, get_today_trades,
    export_db_to_json, save_runtime_state, load_runtime_state,
    get_total_fees, get_total_net_profit
)

CST = timezone(timedelta(hours=8))

# ==================== 实时数据引擎（简化为仅恐惧贪婪） ====================
class RealDataEngine:
    def __init__(self, exchange, ws):
        self.exchange = exchange
        self.ws = ws
        self._fear_greed_cache = {"value": 50, "classification": "Neutral", "timestamp": 0}
        self._cache_ttl = 300
        self._session = None

    async def _get_session(self):
        if self._session is None:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self):
        if self._session:
            await self._session.close()
            self._session = None

    async def get_fear_greed_index(self):
        now = time.time()
        if now - self._fear_greed_cache["timestamp"] < self._cache_ttl:
            return self._fear_greed_cache
        try:
            session = await self._get_session()
            async with session.get("https://api.alternative.me/fng/?limit=1", timeout=5) as resp:
                data = await resp.json()
                if data.get("data"):
                    item = data["data"][0]
                    self._fear_greed_cache = {
                        "value": int(item["value"]),
                        "classification": item["value_classification"],
                        "timestamp": now
                    }
        except Exception as e:
            logger.warning(f"恐惧贪婪获取失败: {e}")
        return self._fear_greed_cache

# ==================== 核心机器人 ====================
class QuantBot:
    def __init__(self, exchange):
        self.exchange = exchange
        self.ws = WSDataManager(exchange)
        self.tech = TechnicalEngine(exchange)
        self.real_data = RealDataEngine(exchange, self.ws)
        self.lock = asyncio.Lock()

        # 基础配置（从数据库加载）
        self.symbols = [settings.SYMBOL, "BTC/USDT", "SOL/USDT"]
        self.tp_pct = 0.015
        self.sl_pct = 0.01
        self.trailing_sl_pct = 0.005
        self.trailing_tp_pct = 0.003
        self.single_order_usdt = 1.0
        self.timeframe = "5m"
        self.reserve_bottom = 10
        self.max_daily_trades = 20
        self.auto_trade_enabled = False
        self.auto_min_score = 65
        self.max_per_coin_usdt = 50
        self.max_daily_loss_pct = 0.05
        self.max_total_allocated_pct = 0.8
        self.max_drawdown_pct = 0.12
        self.max_positions_per_coin = 8
        self.orderbook_filter = True
        self.waterfall_breaker = True

        # 运行时状态
        self.is_running = True
        self.trades = []
        self.entries = {}
        self.position_lots = {}          # FIFO账本
        self.position_counts = {}
        self._trailing_high = {}
        self.entry_details = {}
        self.daily_trades = 0
        self.last_reset_day = datetime.now(CST).date().isoformat()
        self._consecutive_losses = 0
        self._today_loss_pct = 0.0
        self._today_loss_usdt = 0.0
        self._daily_start_equity = 0.0
        self._is_paused = False
        self._drawdown_safe_flag = True
        self.peak_total_value = 0

        # 缓存
        self._cached_balances = {}
        self._cached_usdt_free = 0.0
        self._balance_cache_time = 0
        self._balance_cache_ttl = 15
        self._tech_cache = {}
        self._tech_cache_time = {}
        self._tech_cache_ttl = 30
        self._price_history = {}

        # AI 分析（简化）
        self.ai_insight = {"timestamp": 0, "summary": "等待分析", "recommendation": "观望", "score": 50}
        self.ai_enabled = False

        # 费率
        self.taker_fee = settings.TAKER_FEE
        self.maker_fee = settings.MAKER_FEE
        self.min_profit_margin = settings.MIN_PROFIT_MARGIN
        self.breakeven_pct = (self.taker_fee * 2) + self.min_profit_margin

        # Telegram 权限
        raw = settings.ALLOWED_USERS
        self.allowed = {int(x.strip()) for x in raw.split(",") if x.strip().isdigit()} if raw else set()
        self.env_tag = "🧪 (模拟盘)" if settings.IS_SANDBOX else "🔴 (实盘)"
        self.coin_configs = {}

        # Telegram 应用
        self.tg_app = None
        if settings.TG_BOT_TOKEN:
            self._init_telegram()

    def _init_telegram(self):
        self.tg_app = ApplicationBuilder().token(settings.TG_BOT_TOKEN).build()
        handlers = [
            CommandHandler("start", self.cmd_menu),
            CommandHandler("menu", self.cmd_menu),
            CommandHandler("status", self.cmd_status),
            CommandHandler("check", self.cmd_check),
            CommandHandler("holdings", self.cmd_holdings),
            CommandHandler("panic", self.cmd_panic),
            CommandHandler("autotrade", self.cmd_autotrade),
            CommandHandler("settp", self.cmd_set_tp),
            CommandHandler("setsl", self.cmd_set_sl),
            CommandHandler("settsl", self.cmd_set_tsl),
            CommandHandler("settmpt", self.cmd_set_trailing_tp),
            CommandHandler("setamount", self.cmd_set_amount),
            CommandHandler("settf", self.cmd_set_tf),
            CommandHandler("setreserve", self.cmd_set_reserve),
            CommandHandler("addsymbol", self.cmd_add_symbol),
            CommandHandler("delsymbol", self.cmd_del_symbol),
            CommandHandler("setmaxpos", self.cmd_set_max_pos),
            CommandHandler("setmaxalloc", self.cmd_set_max_alloc),
            CommandHandler("setmaxloss", self.cmd_set_max_loss),
            CommandHandler("setmaxcoin", self.cmd_set_max_coin),
            CommandHandler("autoscore", self.cmd_autoscore),
            CommandHandler("stats", self.cmd_stats),
            CommandHandler("backup", self.cmd_backup),
            CommandHandler("help", self.cmd_help),
        ]
        for h in handlers:
            self.tg_app.add_handler(h)
        self.tg_app.add_handler(CallbackQueryHandler(self.handle_button_click))
        self.tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_text_input))

    # ---------- 辅助函数 ----------
    def _auth(self, update: Update):
        if not self.allowed:
            return settings.IS_SANDBOX
        return update.effective_user.id in self.allowed

    def _get_coin_param(self, sym, key, default):
        return self.coin_configs.get(sym, {}).get(key, default)

    def _position_lots_for(self, sym):
        return self.position_lots.setdefault(sym, [])

    def _bot_position_amount(self, sym):
        return sum(float(l.get('amount', 0)) for l in self.position_lots.get(sym, []))

    def _bot_position_cost(self, sym):
        return sum(float(l.get('cost', 0)) for l in self.position_lots.get(sym, []))

    def _weighted_entry(self, sym):
        lots = self.position_lots.get(sym, [])
        amount = sum(float(l.get('amount', 0)) for l in lots)
        cost = sum(float(l.get('cost', 0)) for l in lots)
        return cost / amount if amount > 0 else 0.0

    def _append_position_lot(self, sym, amount, price, cost, fee=0.0, currency=''):
        self._position_lots_for(sym).append({
            'amount': float(amount), 'price': float(price), 'cost': float(cost),
            'fee': float(fee), 'fee_currency': currency, 'time': time.time()
        })
        self.entries[sym] = self._weighted_entry(sym)
        self.position_counts[sym] = len(self.position_lots[sym])
        self._trailing_high[sym] = max(self._trailing_high.get(sym, self.entries[sym]), price)

    def _consume_position_lots(self, sym, amount, exit_price, exit_revenue, sell_fee=0.0):
        remaining = float(amount)
        realized_cost = 0.0
        realized_fee_buy = 0.0
        while remaining > 1e-12 and self.position_lots.get(sym):
            lot = self.position_lots[sym][0]
            lot_amt = float(lot.get('amount', 0))
            take = min(remaining, lot_amt)
            ratio = take / lot_amt if lot_amt else 0
            realized_cost += float(lot.get('cost', 0)) * ratio
            realized_fee_buy += float(lot.get('fee', 0)) * ratio
            lot['amount'] = lot_amt - take
            lot['cost'] = float(lot.get('cost', 0)) * (1 - ratio)
            lot['fee'] = float(lot.get('fee', 0)) * (1 - ratio)
            remaining -= take
            if lot['amount'] <= 1e-12:
                self.position_lots[sym].pop(0)
        if remaining > 1e-8:
            raise ValueError(f'{sym} 卖出数量超过账本: remaining={remaining}')
        self.position_counts[sym] = len(self.position_lots.get(sym, []))
        if self.position_lots.get(sym):
            self.entries[sym] = self._weighted_entry(sym)
        else:
            self.position_lots.pop(sym, None)
            self.entries.pop(sym, None)
            self.entry_details.pop(sym, None)
            self._trailing_high[sym] = 0
        net_pnl = float(exit_revenue) - realized_cost - realized_fee_buy - float(sell_fee or 0)
        return net_pnl, realized_cost, realized_fee_buy

    async def _refresh_balance_cache(self, force=False):
        now = time.time()
        if force or (now - self._balance_cache_time > self._balance_cache_ttl):
            bal = await self.exchange.fetch_balance()
            self._cached_balances = bal
            self._cached_usdt_free = float(bal.get('USDT', {}).get('free', 0))
            self._balance_cache_time = now
        return self._cached_usdt_free

    async def _round_amount_by_precision(self, symbol, amount):
        return await self.exchange._prepare_amount(symbol, amount)

    def _calculate_dynamic_amount(self, base_amount=0.5):
        total_balance = self._cached_usdt_free
        for coin, val in self._cached_balances.items():
            if coin == 'USDT' or not isinstance(val, dict):
                continue
            ticker = self.ws.get_ticker(coin + "/USDT")
            if ticker:
                total_balance += float(val.get('free', 0)) * ticker.get('last', 0)
        if total_balance < 10:
            return max(0.1, base_amount * 0.3)
        elif total_balance < 50:
            return max(0.3, base_amount * 0.8)
        elif total_balance < 100:
            return base_amount
        else:
            return base_amount * 2

    async def _allocation_used_usdt(self):
        used = 0.0
        for sym in self.symbols:
            used += self._bot_position_cost(sym)
        return max(0.0, used)

    async def _can_allocate(self, additional_usdt):
        balance = self._cached_usdt_free
        for coin, val in self._cached_balances.items():
            if coin == 'USDT' or not isinstance(val, dict):
                continue
            ticker = self.ws.get_ticker(coin + "/USDT")
            if ticker:
                balance += float(val.get('free', 0)) * ticker.get('last', 0)
        if balance <= 0:
            return False
        used = await self._allocation_used_usdt()
        max_alloc = balance * self.max_total_allocated_pct
        return used + additional_usdt <= max_alloc + 1e-9

    async def _check_risk_limits(self):
        today = datetime.now(CST).date().isoformat()
        if today != self.last_reset_day:
            self._today_loss_pct = 0.0
            self._today_loss_usdt = 0.0
            self._daily_start_equity = 0.0
            self._consecutive_losses = 0
            self.last_reset_day = today
        if self._consecutive_losses >= 4:
            if time.time() - getattr(self, '_last_pause_time', 0) > 3600:
                self._consecutive_losses = 0
                self._is_paused = False
            else:
                return False
        if self._today_loss_pct >= self.max_daily_loss_pct:
            if not self._is_paused:
                await self._alert(f"⛔ 日亏损达 {self._today_loss_pct*100:.1f}%，暂停交易", "critical")
                self._is_paused = True
            return False
        return True

    async def _alert(self, message, level="warning"):
        emoji = {"info":"ℹ️","warning":"⚠️","critical":"🚨"}
        if settings.TG_CHAT_ID and self.tg_app:
            try:
                await self.tg_app.bot.send_message(chat_id=settings.TG_CHAT_ID, text=f"{emoji.get(level,'⚠️')} **系统告警**\n{message}", parse_mode="Markdown")
            except:
                pass

    # ---------- 加载与保存 ----------
    async def load_and_init(self):
        await init_db()
        cfg = await load_config()
        for key, val in cfg.items():
            if hasattr(self, key):
                setattr(self, key, val)
        self.symbols = cfg.get('symbols', [settings.SYMBOL, "BTC/USDT", "SOL/USDT"])
        self.coin_configs = json.loads(cfg.get('coin_configs', '{}')) if isinstance(cfg.get('coin_configs'), str) else cfg.get('coin_configs', {})
        self.trades = await load_trades()
        state = await load_runtime_state()
        if state:
            self.position_counts = state.get('position_counts', {})
            self.entries = state.get('entries', {})
            self.peak_total_value = state.get('peak_total_value', 0)
            self.daily_trades = state.get('daily_trades', 0)
            self._trailing_high = state.get('trailing_high', {})
            self.position_lots = state.get('position_lots', {})
            self.entry_details = state.get('entry_details', {})
            self._daily_start_equity = float(state.get('daily_start_equity', 0))
            self._today_loss_usdt = float(state.get('today_loss_usdt', 0))
        logger.info("✅ UltimateBot v14.0 精简版启动")

    async def _save_runtime_state(self):
        state = {
            'position_counts': self.position_counts,
            'entries': self.entries,
            'peak_total_value': self.peak_total_value,
            'daily_trades': self.daily_trades,
            'trailing_high': self._trailing_high,
            'entry_details': self.entry_details,
            'position_lots': self.position_lots,
            'daily_start_equity': self._daily_start_equity,
            'today_loss_usdt': self._today_loss_usdt,
        }
        await save_runtime_state(state)

    async def _save_config(self):
        cfg = {
            'tp_pct': self.tp_pct, 'sl_pct': self.sl_pct,
            'trailing_sl_pct': self.trailing_sl_pct, 'trailing_tp_pct': self.trailing_tp_pct,
            'single_order_usdt': self.single_order_usdt, 'timeframe': self.timeframe,
            'reserve_bottom': self.reserve_bottom, 'symbols': self.symbols,
            'orderbook_filter': self.orderbook_filter, 'waterfall_breaker': self.waterfall_breaker,
            'max_daily_trades': self.max_daily_trades,
            'auto_trade_enabled': self.auto_trade_enabled, 'auto_min_score': self.auto_min_score,
            'max_per_coin_usdt': self.max_per_coin_usdt,
            'max_daily_loss_pct': self.max_daily_loss_pct,
            'max_total_allocated_pct': self.max_total_allocated_pct,
            'max_drawdown_pct': self.max_drawdown_pct,
            'max_positions_per_coin': self.max_positions_per_coin,
            'coin_configs': json.dumps(self.coin_configs),
        }
        await save_config(cfg)

    async def _get_cached_tech(self, sym, timeframe='5m', limit=50):
        key = f"{sym}_{timeframe}_{limit}"
        now = time.time()
        if key in self._tech_cache and (now - self._tech_cache_time.get(key, 0)) < self._tech_cache_ttl:
            return self._tech_cache[key]
        tech = await self.tech.calc(sym, timeframe, limit)
        if tech:
            self._tech_cache[key] = tech
            self._tech_cache_time[key] = now
        return tech

    # ---------- 核心信号（精简5因子） ----------
    async def _should_open_position(self, sym, p, tech, funding, fg, usdt_free):
        if tech is None:
            return {'should_open': False, 'score': 50, 'details': ['技术指标缺失']}

        # 1. RSI评分
        rsi = tech.get('rsi', 50)
        rsi_score = 50 + (50 - rsi) * 0.8
        rsi_score = max(0, min(100, rsi_score))

        # 2. 布林带位置
        bb_lower = tech.get('bb_lower', 0)
        bb_upper = tech.get('bb_upper', 0)
        if bb_upper > bb_lower and p > 0:
            bb_pos = (p - bb_lower) / (bb_upper - bb_lower)
            bb_score = 100 - bb_pos * 100
        else:
            bb_score = 50
        bb_score = max(0, min(100, bb_score))

        # 3. OFI
        ob = self.ws.get_orderbook(sym)
        ofi_score = 50
        if ob:
            bids = ob.get('bids', [])
            asks = ob.get('asks', [])
            if len(bids) >= 5 and len(asks) >= 5:
                bid_vol = sum(b[1] for b in bids[:5])
                ask_vol = sum(a[1] for a in asks[:5])
                ofi = (bid_vol - ask_vol) / (bid_vol + ask_vol + 1e-6)
                ofi_score = 50 + ofi * 40

        # 4. 趋势过滤器（防止逆势买入）
        trend_strength = tech.get('trend_strength', 0)
        if settings.TREND_FILTER_ENABLED and trend_strength > settings.TREND_THRESHOLD:
            trend_penalty = 30
        elif settings.TREND_FILTER_ENABLED and trend_strength < -settings.TREND_THRESHOLD:
            trend_penalty = 15
        else:
            trend_penalty = 0

        # 5. 成交量因子
        ticker = self.ws.get_ticker(sym)
        vol = ticker.get('volume', 0) if ticker else 0
        vol_score = 50 + min(20, (vol / 1000) * 0.5)

        total_score = (rsi_score * 0.25 + bb_score * 0.25 + ofi_score * 0.20 +
                       vol_score * 0.15 + 50 * 0.15) - trend_penalty
        total_score = max(0, min(100, total_score))

        threshold = self._get_coin_param(sym, 'auto_min_score', self.auto_min_score)
        should_open = total_score >= threshold and trend_penalty < 30

        details = [f"RSI:{rsi:.0f}", f"BB:{bb_score:.0f}", f"OFI:{ofi_score:.0f}",
                   f"趋势:{trend_strength:.2%}", f"惩罚:{trend_penalty}"]

        return {
            'should_open': should_open,
            'score': total_score,
            'details': details,
            'amount': self._calculate_dynamic_amount(self._get_coin_param(sym, 'single_order_usdt', self.single_order_usdt)),
            'state': 'trending' if trend_strength > 0.02 else 'ranging'
        }

    # ---------- 自动交易主循环 ----------
    async def _auto_trade_monitor(self):
        await asyncio.sleep(10)
        while True:
            try:
                if not self.is_running or not self.auto_trade_enabled:
                    await asyncio.sleep(10)
                    continue

                today = datetime.now(CST).date().isoformat()
                if today != self.last_reset_day:
                    self.daily_trades = 0
                    self.last_reset_day = today
                if self.max_daily_trades > 0 and self.daily_trades >= self.max_daily_trades:
                    await asyncio.sleep(10)
                    continue

                usdt_free = await self._refresh_balance_cache()
                if usdt_free < self.reserve_bottom:
                    await asyncio.sleep(10)
                    continue

                if not await self._check_risk_limits():
                    await asyncio.sleep(10)
                    continue

                fg_data = await self.real_data.get_fear_greed_index()
                fg = fg_data["value"] if fg_data else None

                candidates = []
                for sym in self.symbols:
                    try:
                        if self.position_counts.get(sym, 0) >= self.max_positions_per_coin:
                            continue
                        if self._bot_position_cost(sym) >= self.max_per_coin_usdt:
                            continue

                        ticker = self.ws.get_ticker(sym)
                        if not ticker:
                            continue
                        p = ticker['last']

                        tech = await self._get_cached_tech(sym, self.timeframe, 50)
                        if not tech:
                            continue

                        decision = await self._should_open_position(sym, p, tech, None, fg, usdt_free)
                        if not decision['should_open']:
                            continue

                        if self.orderbook_filter:
                            ob = self.ws.get_orderbook(sym)
                            if ob:
                                bids = ob.get('bids', [])
                                asks = ob.get('asks', [])
                                if not bids or not asks:
                                    continue
                                spread = (asks[0][0] - bids[0][0]) / bids[0][0]
                                if spread > 0.002:
                                    continue

                        amount_usdt = decision.get('amount', self.single_order_usdt)
                        remaining = self.max_per_coin_usdt - self._bot_position_cost(sym)
                        if remaining <= 0:
                            continue
                        amount_usdt = min(amount_usdt, remaining)

                        if not await self._can_allocate(amount_usdt):
                            continue

                        candidates.append((decision['score'], sym, p, amount_usdt))
                    except Exception as e:
                        logger.error(f"候选生成异常 {sym}: {e}")

                candidates.sort(key=lambda x: x[0], reverse=True)
                opened = set()
                for score, sym, p, amount_usdt in candidates:
                    if sym in opened:
                        continue
                    if usdt_free < amount_usdt + self.reserve_bottom:
                        continue

                    raw_amount = amount_usdt / p
                    rounded = await self._round_amount_by_precision(sym, raw_amount)
                    if rounded <= 0:
                        continue

                    order = await self.exchange.create_market_buy_order(sym, rounded)
                    if order:
                        self.daily_trades += 1
                        filled = float(order.get('filled') or 0)
                        avg = float(order.get('average') or p)
                        fee = float(order.get('_fee_cost') or 0)
                        fee_currency = order.get('_fee_currency', '')
                        base = sym.split('/')[0]
                        net_amount = filled - fee if fee_currency == base else filled
                        real_cost = filled * avg + (fee if fee_currency in ('', 'USDT') else 0)
                        if net_amount <= 0 or real_cost <= 0:
                            self._is_paused = True
                            await self._alert(f"{sym} 买单异常，已暂停", "critical")
                            continue

                        self._append_position_lot(sym, net_amount, avg, filled * avg, fee, fee_currency)
                        self.entry_details[sym] = {'signal_score': score, 'fear_greed': fg}
                        await save_trade_detail({
                            'time': datetime.now(CST).strftime("%m-%d %H:%M"), 'symbol': sym, 'side': 'buy',
                            'price': avg, 'amount': filled, 'signal_score': score, 'fear_greed': fg or 0,
                            'funding_rate': 0, 'pnl_pct': 0, 'real_cost': real_cost,
                            'fee': fee, 'fee_currency': fee_currency, 'order_id': order.get('id','')
                        })
                        await self._refresh_balance_cache(force=True)
                        usdt_free = self._cached_usdt_free
                        await self._save_runtime_state()
                        opened.add(sym)
                        if settings.TG_CHAT_ID:
                            try:
                                await self.tg_app.bot.send_message(chat_id=settings.TG_CHAT_ID,
                                    text=f"🤖 开仓 {sym} {amount_usdt:.2f}U @ {avg:.4f} 仓位{self.position_counts[sym]}/{self.max_positions_per_coin} | 评分{score:.0f}")
                            except:
                                pass
                    await asyncio.sleep(1)
                await asyncio.sleep(30)
            except Exception as e:
                logger.error(f"自动交易循环错误: {e}")
                await asyncio.sleep(30)

    # ---------- 移动止盈止损 ----------
    async def _trailing_monitor(self):
        await asyncio.sleep(5)
        while True:
            try:
                if not self.is_running:
                    await asyncio.sleep(5)
                    continue

                await self._refresh_balance_cache()
                for sym in self.symbols:
                    try:
                        if self.position_counts.get(sym, 0) <= 0:
                            continue
                        ticker = self.ws.get_ticker(sym)
                        if not ticker:
                            continue
                        p = ticker['last']
                        amount = self._bot_position_amount(sym)
                        if amount <= 0:
                            self.position_counts[sym] = 0
                            await self._save_runtime_state()
                            continue

                        entry = self._weighted_entry(sym) or self.entries.get(sym, p)
                        detail = self.entry_details.get(sym, {})
                        tp = detail.get('dyn_tp', self._get_coin_param(sym, 'tp_pct', self.tp_pct))
                        sl = detail.get('dyn_sl', self._get_coin_param(sym, 'sl_pct', self.sl_pct))
                        high = self._trailing_high.get(sym, entry)

                        profit_pct = (p - entry) / entry * 100
                        if profit_pct >= tp * 100 * 0.5:
                            if p <= high * (1 - self.trailing_tp_pct):
                                reason = "trailing_tp"
                                action = 'sell'
                            else:
                                action = 'hold'
                        elif profit_pct <= -sl * 100:
                            reason = "stop_loss"
                            action = 'sell'
                        elif profit_pct >= tp * 100:
                            reason = "take_profit"
                            action = 'sell'
                        else:
                            action = 'hold'

                        if p > high:
                            self._trailing_high[sym] = p

                        if action == 'sell':
                            rounded = await self._round_amount_by_precision(sym, amount)
                            if rounded <= 0:
                                continue
                            sell_order = await self.exchange.create_market_sell_order(sym, rounded)
                            if not sell_order:
                                continue
                            sell_filled = float(sell_order.get('filled') or 0)
                            sell_avg = float(sell_order.get('average') or p)
                            sell_revenue = sell_filled * sell_avg
                            sell_fee = float(sell_order.get('_fee_cost') or 0)
                            try:
                                net_pnl, real_cost, _ = self._consume_position_lots(sym, sell_filled, sell_avg, sell_revenue, sell_fee)
                            except ValueError as e:
                                logger.error(f"账本不一致 {sym}: {e}")
                                self._is_paused = True
                                continue
                            net_pnl_pct = (net_pnl / real_cost * 100) if real_cost > 0 else 0
                            if net_pnl < 0:
                                self._consecutive_losses += 1
                                self._today_loss_pct += abs(net_pnl_pct) / 100
                            else:
                                self._consecutive_losses = 0

                            trade = {"time": datetime.now(CST).strftime("%m-%d %H:%M"), "symbol": sym,
                                     "entry": entry, "exit": p, "pnl_pct": ((p-entry)/entry*100),
                                     "net_pnl": net_pnl, "net_pnl_pct": net_pnl_pct}
                            await save_trade(trade)
                            self.trades.insert(0, trade)
                            await save_trade_detail({"time": datetime.now(CST).strftime("%m-%d %H:%M"), "symbol": sym, "side": "sell",
                                                     "price": sell_avg, "amount": sell_filled, "pnl_pct": ((p-entry)/entry*100),
                                                     "signal_score": detail.get('signal_score',0), "fear_greed": detail.get('fear_greed',0),
                                                     "real_revenue": sell_revenue, "fee": sell_fee, "order_id": sell_order.get('id',''),
                                                     "net_pnl_pct": net_pnl_pct})
                            self._trailing_high[sym] = 0
                            await self._save_runtime_state()
                            if settings.TG_CHAT_ID:
                                try:
                                    await self.tg_app.bot.send_message(chat_id=settings.TG_CHAT_ID,
                                        text=f"📉 {reason} {sym} @ {p:.2f} 净利{net_pnl_pct:+.2f}% ({net_pnl:+.4f}U)")
                                except:
                                    pass
                    except Exception as e:
                        logger.error(f"追踪异常 {sym}: {e}")
                await asyncio.sleep(5)
            except Exception as e:
                logger.error(f"追踪任务异常: {e}")
                await asyncio.sleep(5)

    # ---------- 风险监控 ----------
    async def _risk_monitor_task(self):
        await asyncio.sleep(5)
        while self.is_running:
            try:
                await self._refresh_balance_cache(force=True)
                equity = self._cached_usdt_free
                for sym in self.symbols:
                    ticker = self.ws.get_ticker(sym)
                    if ticker:
                        coin = sym.split('/')[0]
                        free = self._cached_balances.get(coin, {}).get('free', 0)
                        equity += free * ticker.get('last', 0)
                if equity > 0:
                    if self._daily_start_equity <= 0:
                        self._daily_start_equity = equity
                    self._today_loss_usdt = max(0.0, self._daily_start_equity - equity)
                    self._today_loss_pct = self._today_loss_usdt / self._daily_start_equity if self._daily_start_equity > 0 else 0.0
                    self.peak_total_value = max(self.peak_total_value or equity, equity)
                    drawdown = (self.peak_total_value - equity) / self.peak_total_value
                    self._drawdown_safe_flag = drawdown < self.max_drawdown_pct
                    if not self._drawdown_safe_flag:
                        self._is_paused = True
                        await self._alert(f"回撤 {drawdown*100:.2f}% 达到上限，暂停", "critical")
                await self._save_runtime_state()
                await asyncio.sleep(30)
            except Exception as e:
                logger.error(f"风险监控异常: {e}")
                await asyncio.sleep(30)

    # ---------- Panic Sell ----------
    async def panic_sell_all(self):
        self._is_paused = True
        for sym in self.symbols:
            try:
                await self.exchange.cancel_all_orders(sym)
                amount = self._bot_position_amount(sym)
                if amount <= 0:
                    continue
                rounded = await self._round_amount_by_precision(sym, amount)
                if rounded > 0:
                    order = await self.exchange.create_market_sell_order(sym, rounded)
                    if order:
                        filled = float(order.get('filled') or 0)
                        avg = float(order.get('average') or 0)
                        revenue = filled * avg
                        fee = float(order.get('_fee_cost') or 0)
                        try:
                            self._consume_position_lots(sym, filled, avg, revenue, fee)
                        except ValueError:
                            logger.error(f"Panic Sell 账本不一致 {sym}")
                if self._bot_position_amount(sym) <= 1e-12:
                    self.position_counts[sym] = 0
            except Exception as e:
                logger.error(f"Panic Sell {sym} 失败: {e}")
        await self._save_runtime_state()

    # ---------- Telegram 命令（精简） ----------
    async def cmd_menu(self, update, context):
        if not self._auth(update): return
        await update.effective_message.reply_text(f"⚙️ 控制台 {self.env_tag}", reply_markup=self._build_main_keyboard())

    def _build_main_keyboard(self):
        auto = "🟢" if self.auto_trade_enabled else "🔴"
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("🚨 紧急全平", callback_data="panic_confirm")],
            [InlineKeyboardButton("⚡ 开启", callback_data="bot_start"), InlineKeyboardButton("🔴 关机", callback_data="bot_stop")],
            [InlineKeyboardButton(f"🤖 自动交易 {auto}", callback_data="toggle_auto")],
            [InlineKeyboardButton("📊 状态", callback_data="status"), InlineKeyboardButton("💳 余额", callback_data="balance")],
            [InlineKeyboardButton("📋 持币", callback_data="holdings"), InlineKeyboardButton("📊 信号", callback_data="check")],
            [InlineKeyboardButton("🎯 止盈", callback_data="menu_set_tp"), InlineKeyboardButton("🛡️ 止损", callback_data="menu_set_sl")],
            [InlineKeyboardButton("📉 移损", callback_data="menu_set_tsl"), InlineKeyboardButton("🏹 移盈", callback_data="menu_set_tmpt")],
            [InlineKeyboardButton("💵 额度", callback_data="menu_set_amount"), InlineKeyboardButton("⏱ 周期", callback_data="menu_set_tf")],
            [InlineKeyboardButton("🔒 底线", callback_data="menu_set_reserve"), InlineKeyboardButton("🔢 上限", callback_data="menu_set_trades")],
            [InlineKeyboardButton("➕ 币种", callback_data="menu_add_symbol"), InlineKeyboardButton("➖ 币种", callback_data="menu_del_symbol")],
            [InlineKeyboardButton("📈 仪表盘", callback_data="stats"), InlineKeyboardButton("💾 备份", callback_data="backup")],
            [InlineKeyboardButton("🔄 刷新", callback_data="refresh")],
        ])

    async def cmd_status(self, update, context):
        if not self._auth(update): return
        bal = await self.exchange.fetch_balance()
        usdt = float(bal.get('USDT', {}).get('free', 0))
        total = usdt
        pos_lines = []
        for sym in self.symbols:
            ticker = self.ws.get_ticker(sym)
            if ticker:
                p = ticker['last']
                coin = sym.split('/')[0]
                free = float(bal.get(coin, {}).get('free', 0))
                val = free * p
                total += val
                count = self.position_counts.get(sym, 0)
                entry = self.entries.get(sym, 0)
                pnl = f" | {'🟢' if p>=entry else '🔴'} {((p-entry)/entry*100):+.2f}%" if entry else ""
                pos_lines.append(f"• {sym}: {free:.4f} 价值{val:.2f}U 仓位{count}/{self.max_positions_per_coin}{pnl}")
        lines = [
            f"📊 状态 {self.env_tag}",
            f"💰 总资产: {total:.2f}U | 可用: {usdt:.2f}U",
            f"📈 持仓:",
            *pos_lines,
            f"━━━━━━━━━━",
            f"止盈 {self.tp_pct:.1%} 止损 {self.sl_pct:.1%} 移损 {self.trailing_sl_pct:.1%} 移盈 {self.trailing_tp_pct:.1%}",
            f"今日交易 {self.daily_trades}/{self.max_daily_trades if self.max_daily_trades>0 else '∞'}",
            f"日亏损 {self._today_loss_pct*100:.1f}% / {self.max_daily_loss_pct*100:.0f}%",
            f"回撤 {self.max_drawdown_pct*100:.0f}% | 暂停 {'是' if self._is_paused else '否'}",
        ]
        await update.effective_message.reply_text("\n".join(lines))

    async def cmd_check(self, update, context):
        if not self._auth(update): return
        lines = ["📈 信号检查\n"]
        for sym in self.symbols:
            ticker = self.ws.get_ticker(sym)
            if not ticker:
                continue
            p = ticker['last']
            tech = await self._get_cached_tech(sym, self.timeframe, 50)
            if not tech:
                continue
            decision = await self._should_open_position(sym, p, tech, None, None, self._cached_usdt_free)
            status = "🎯 可开仓" if decision['should_open'] else "⏳ 等待"
            lines.append(f"{sym}: {p:.2f} | 评分{decision['score']:.0f} | {status}")
            lines.append(f"   {', '.join(decision['details'])}")
        await update.effective_message.reply_text("\n".join(lines))

    async def cmd_holdings(self, update, context):
        if not self._auth(update): return
        bal = await self.exchange.fetch_balance()
        lines = ["📋 持币"]
        for sym in self.symbols:
            coin = sym.split('/')[0]
            free = float(bal.get(coin, {}).get('free', 0))
            if free > 0.0001:
                ticker = self.ws.get_ticker(sym)
                p = ticker['last'] if ticker else 0
                val = free * p
                count = self.position_counts.get(sym, 0)
                entry = self.entries.get(sym, 0)
                pnl = f" | {'🟢' if p>=entry else '🔴'} {((p-entry)/entry*100):+.2f}%" if entry else ""
                lines.append(f"• {sym}: {free:.4f} 价值{val:.2f}U 仓位{count}/{self.max_positions_per_coin}{pnl}")
        await update.effective_message.reply_text("\n".join(lines) if len(lines)>1 else "暂无持仓")

    async def cmd_panic(self, update, context):
        if not self._auth(update): return
        await self.panic_sell_all()
        await update.effective_message.reply_text("🚨 全平完成")

    async def cmd_autotrade(self, update, context):
        if not self._auth(update): return
        try:
            mode = context.args[0].lower()
            if mode == "on":
                self.auto_trade_enabled = True
            elif mode == "off":
                self.auto_trade_enabled = False
            else:
                raise ValueError
            await self._save_config()
            await update.effective_message.reply_text(f"🤖 自动交易已{'开启' if self.auto_trade_enabled else '关闭'}")
        except:
            await update.effective_message.reply_text("用法: /autotrade on|off")

    async def cmd_set_tp(self, update, context):
        if not self._auth(update): return
        try:
            val = float(context.args[0]) / 100.0
            if val < self.breakeven_pct:
                await update.effective_message.reply_text(f"低于保本线 {self.breakeven_pct*100:.2f}%")
                return
            self.tp_pct = val
            await self._save_config()
            await update.effective_message.reply_text(f"✅ 止盈: {self.tp_pct:.1%}")
        except:
            await update.effective_message.reply_text("❌ /settp 1.2")

    async def cmd_set_sl(self, update, context):
        if not self._auth(update): return
        try:
            val = float(context.args[0]) / 100.0
            self.sl_pct = val
            await self._save_config()
            await update.effective_message.reply_text(f"✅ 止损: {self.sl_pct:.1%}")
        except:
            pass

    async def cmd_set_tsl(self, update, context):
        if not self._auth(update): return
        try:
            self.trailing_sl_pct = float(context.args[0]) / 100.0
            await self._save_config()
            await update.effective_message.reply_text(f"✅ 移动止损: {self.trailing_sl_pct:.1%}")
        except:
            pass

    async def cmd_set_trailing_tp(self, update, context):
        if not self._auth(update): return
        try:
            self.trailing_tp_pct = float(context.args[0]) / 100.0
            await self._save_config()
            await update.effective_message.reply_text(f"✅ 移动止盈: {self.trailing_tp_pct:.1%}")
        except:
            pass

    async def cmd_set_amount(self, update, context):
        if not self._auth(update): return
        try:
            self.single_order_usdt = float(context.args[0])
            await self._save_config()
            await update.effective_message.reply_text(f"✅ 单笔额度: {self.single_order_usdt}U")
        except:
            pass

    async def cmd_set_tf(self, update, context):
        if not self._auth(update): return
        try:
            self.timeframe = context.args[0].lower()
            await self._save_config()
            await update.effective_message.reply_text(f"✅ 周期: {self.timeframe}")
        except:
            pass

    async def cmd_set_reserve(self, update, context):
        if not self._auth(update): return
        try:
            self.reserve_bottom = float(context.args[0])
            await self._save_config()
            await update.effective_message.reply_text(f"✅ 底线: {self.reserve_bottom}U")
        except:
            pass

    async def cmd_add_symbol(self, update, context):
        if not self._auth(update): return
        try:
            sym = context.args[0].upper()
            if "/" not in sym: sym += "/USDT"
            if sym not in self.symbols:
                self.symbols.append(sym)
                await self._save_config()
                await update.effective_message.reply_text(f"✅ 已添加 {sym}")
            else:
                await update.effective_message.reply_text("⚠️ 已存在")
        except:
            pass

    async def cmd_del_symbol(self, update, context):
        if not self._auth(update): return
        try:
            sym = context.args[0].upper()
            if "/" not in sym: sym += "/USDT"
            if sym in self.symbols:
                self.symbols.remove(sym)
                await self._save_config()
                await update.effective_message.reply_text(f"✅ 已删除 {sym}")
            else:
                await update.effective_message.reply_text("⚠️ 不存在")
        except:
            pass

    async def cmd_set_max_pos(self, update, context):
        if not self._auth(update): return
        try:
            self.max_positions_per_coin = int(context.args[0])
            await self._save_config()
            await update.effective_message.reply_text(f"✅ 最大仓位数: {self.max_positions_per_coin}")
        except:
            pass

    async def cmd_set_max_alloc(self, update, context):
        if not self._auth(update): return
        try:
            pct = float(context.args[0]) / 100.0
            self.max_total_allocated_pct = max(0.1, min(1.0, pct))
            await self._save_config()
            await update.effective_message.reply_text(f"✅ 总仓位上限: {self.max_total_allocated_pct*100:.0f}%")
        except:
            pass

    async def cmd_set_max_loss(self, update, context):
        if not self._auth(update): return
        try:
            pct = float(context.args[0]) / 100.0
            self.max_daily_loss_pct = max(0.01, min(0.5, pct))
            await self._save_config()
            await update.effective_message.reply_text(f"✅ 日熔断: {self.max_daily_loss_pct*100:.1f}%")
        except:
            pass

    async def cmd_set_max_coin(self, update, context):
        if not self._auth(update): return
        try:
            self.max_per_coin_usdt = float(context.args[0])
            await self._save_config()
            await update.effective_message.reply_text(f"✅ 单币最大持仓: {self.max_per_coin_usdt}U")
        except:
            pass

    async def cmd_autoscore(self, update, context):
        if not self._auth(update): return
        try:
            score = int(context.args[0])
            if 50 <= score <= 95:
                self.auto_min_score = score
                await self._save_config()
                await update.effective_message.reply_text(f"✅ 阈值: {score}分")
            else:
                await update.effective_message.reply_text("阈值需50-95")
        except:
            pass

    async def cmd_stats(self, update, context):
        if not self._auth(update): return
        perf = await get_recent_performance(20)
        today = await get_today_trades()
        lines = [
            f"📊 仪表盘 {self.env_tag}",
            f"今日: {today['total'] if today else 0}笔 胜率{today['win_rate']*100:.0f}% 盈亏{today['total_pnl_sum']:+.2f}%" if today else "今日无交易",
            f"近20笔: 胜率{perf['win_rate']*100:.0f}% 平均盈利{perf['avg_win_pct']:.2f}% 平均亏损{perf['avg_loss_pct']:.2f}%" if perf else "暂无数据",
            f"总手续费: {await get_total_fees():.4f}U",
            f"总净利: {await get_total_net_profit():.4f}U",
            f"连续亏损: {self._consecutive_losses}",
            f"市场状态: {'暂停' if self._is_paused else '正常'}"
        ]
        await update.effective_message.reply_text("\n".join(lines))

    async def cmd_backup(self, update, context):
        if not self._auth(update): return
        data = await export_db_to_json()
        if data:
            await update.effective_message.reply_document(document=data.encode(), filename=f"backup_{datetime.now(CST).strftime('%Y%m%d_%H%M%S')}.json")
        else:
            await update.effective_message.reply_text("备份失败")

    async def cmd_help(self, update, context):
        if not self._auth(update): return
        await update.effective_message.reply_text(
            "📖 命令列表\n"
            "/menu 控制台\n/status 状态\n/check 信号\n/holdings 持币\n/panic 全平\n"
            "/autotrade on|off\n/settp 1.2  /setsl 0.8  /settsl 0.5  /settmpt 0.3\n"
            "/setamount 1  /settf 5m  /setreserve 10\n"
            "/addsymbol ETH  /delsymbol ETH\n"
            "/setmaxpos 8  /setmaxalloc 80  /setmaxloss 5  /setmaxcoin 100\n"
            "/autoscore 65\n/stats  /backup"
        )

    # ---------- 按钮处理 ----------
    async def handle_button_click(self, update, context):
        query = update.callback_query
        data = query.data
        if data == "panic_confirm":
            await query.answer("发送 /panic 确认", show_alert=True)
        elif data == "toggle_auto":
            self.auto_trade_enabled = not self.auto_trade_enabled
            await self._save_config()
            await query.answer(f"自动交易已{'开启' if self.auto_trade_enabled else '关闭'}")
            await self._refresh_panel(query)
        elif data == "bot_start":
            self.is_running = True
            await query.answer("已开启")
        elif data == "bot_stop":
            self.is_running = False
            await query.answer("已关机")
        elif data == "refresh":
            await self._refresh_panel(query)
        elif data == "status":
            await self.cmd_status(update, context)
        elif data == "holdings":
            await self.cmd_holdings(update, context)
        elif data == "check":
            await self.cmd_check(update, context)
        elif data == "balance":
            bal = await self.exchange.fetch_balance()
            await query.message.reply_text(f"USDT: {float(bal.get('USDT',{}).get('free',0)):.2f}")
        elif data == "stats":
            await self.cmd_stats(update, context)
        elif data == "backup":
            await self.cmd_backup(update, context)
        elif data.startswith("menu_set_"):
            key = data.replace("menu_set_", "")
            await query.message.reply_text(f"请输入 {key} 的新值，例如 /{key} 1.2")
        else:
            await query.answer("功能待实现", show_alert=True)
        await query.answer()

    async def _refresh_panel(self, query):
        try:
            await query.edit_message_text(f"⚙️ 控制台 {self.env_tag}", reply_markup=self._build_main_keyboard())
        except:
            pass

    async def handle_text_input(self, update, context):
        # 简化输入处理，只记录
        pass

    # ---------- 主运行循环 ----------
    async def run(self):
        await self.load_and_init()
        if not self.tg_app:
            logger.error("Telegram bot token 未配置")
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
                logger.info("✅ UltimateBot v14.0 启动成功")
                if settings.TG_CHAT_ID:
                    await self.tg_app.bot.send_message(chat_id=settings.TG_CHAT_ID,
                        text="🚀 **UltimateBot v14.0 精简低吸高卖引擎已启动**\n\n"
                             "⚡ 核心功能: 现货网格低吸高卖\n"
                             "🛡️ 趋势过滤已启用\n"
                             "📌 发送 /autotrade on 启动交易")
                while True:
                    await asyncio.sleep(30)
            except Exception as e:
                logger.error(f"Bot 断开: {e}")
                await asyncio.sleep(5)