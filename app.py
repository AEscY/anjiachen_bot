"""
app.py - 量化网格机器人 主入口（支持 Render/Railway/Fly.io 云部署）
修复：动态 PORT / 健康检查规范 / asyncio.run 优雅关闭
增加：详细启动日志，捕获所有异常
"""
import asyncio
import os
import signal
import traceback
import sys
from core.exchange import ExchangeManager
from core.bot import QuantBot

# 强制输出启动信息
print("=" * 50)
print("Starting UltimateBot...")
print(f"Python version: {sys.version}")
print(f"Working directory: {os.getcwd()}")
print(f"Environment configuration loaded: {'yes' if os.environ else 'no'}")
print("=" * 50)
sys.stdout.flush()


async def health_check(reader, writer):
    try:
        try:
            await asyncio.wait_for(reader.read(1024), timeout=1.0)
        except (asyncio.TimeoutError, ConnectionResetError):
            pass
        response = (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/plain\r\n"
            b"Content-Length: 2\r\n"
            b"Connection: close\r\n"
            b"\r\n"
            b"OK"
        )
        writer.write(response)
        await writer.drain()
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


async def main():
    try:
        print("🔄 Starting main()...")
        port = int(os.environ.get("PORT", 10000))
        print(f"🩺 Health check port: {port}")

        # 启动健康检查服务器
        health_server = await asyncio.start_server(health_check, '0.0.0.0', port)
        print(f"✅ Health server running on port {port}")

        print("🔌 Initializing exchange...")
        exchange = ExchangeManager()
        print("✅ Exchange initialized")

        print("🤖 Initializing bot...")
        bot = QuantBot(exchange)
        print("✅ Bot initialized")

        # 注册信号处理
        shutdown_event = asyncio.Event()
        def signal_handler():
            print("🛑 Received shutdown signal")
            shutdown_event.set()

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, signal_handler)
            except NotImplementedError:
                # Windows 不支持信号处理
                pass

        print("🚀 Starting bot...")
        bot_task = asyncio.create_task(bot.run())
        health_task = asyncio.create_task(health_server.serve_forever())
        shutdown_task = asyncio.create_task(shutdown_event.wait())

        done, pending = await asyncio.wait(
            [bot_task, health_task, shutdown_task],
            return_when=asyncio.FIRST_COMPLETED
        )

        for task in pending:
            task.cancel()
        for task in done:
            if task.cancelled():
                continue
            exc = task.exception()
            if exc:
                print(f"❌ Task failed with exception: {exc}")
                traceback.print_exception(exc)

        print("🧹 Cleaning up...")
        health_server.close()
        await health_server.wait_closed()
        await exchange.close()
        print("👋 Cleanup done, exiting.")

    except Exception as e:
        print(f"❌ CRITICAL ERROR in main(): {e}")
        traceback.print_exc()
        sys.exit(1)


def run():
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("🛑 User interrupt")
    except Exception as e:
        print(f"❌ Program crashed: {e}")
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    run()
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