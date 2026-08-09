"""
bot.py - 完全体量化机器人（硬止损 + 加仓修复 + 优先级排序 + 全真实数据）
"""
import asyncio, random, aiohttp
from datetime import datetime, timezone, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ForceReply
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, MessageHandler, ContextTypes, filters
from config import settings, logger
from indicators import TechnicalEngine
from storage import init_db, load_config, save_config, load_trades, save_trade

CST = timezone(timedelta(hours=8))

# ... (RealDataEngine, OrderbookEngine, SignalEngine 类保持不变，此处省略以节约篇幅，实际文件包含全部)

class QuantBot:
    def __init__(self, exchange):
        # ... (初始化代码保持不变)
        pass

    # ... (数据库、键盘、命令处理等函数保持不变，此处省略)

    # ========== 自动交易（修复：加仓逻辑 + 优先级排序 + 数据缺失跳过） ==========
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

                fg = (await self.real_data.get_fear_greed_index())["value"]
                bal = await self.exchange.fetch_balance()
                usdt_free = self._get_usdt_free(bal)
                if usdt_free < self.single_order_usdt + self.reserve_bottom:
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

                        # 修复后的加仓逻辑
                        if self.max_per_coin_usdt > 0:
                            if coin_value >= self.max_per_coin_usdt: continue
                        # 如果限额为0，也允许开第一仓
                        elif free > 0.0001:
                            continue

                        tech = await self.tech.calc(sym, self.timeframe, 50)
                        funding = await self.exchange.fetch_funding_rate(sym)
                        sc = self.signal_engine.score(tech, funding, fg)
                        if sc < self.auto_min_score: continue
                        if p > tech['bb_lower'] * 1.02: continue
                        if self.orderbook_filter:
                            ob = await self.exchange.fetch_orderbook(sym)
                            if ob is None:
                                continue
                            ob_valid, _ = await self.orderbook_engine.validate(ob)
                            if not ob_valid: continue

                        candidates.append((sc, sym, p))
                    except Exception:
                        continue

                # 按信号评分从高到低排序
                candidates.sort(key=lambda x: x[0], reverse=True)

                for sc, sym, p in candidates:
                    coin = sym.split('/')[0]
                    free = bal.get(coin, {}).get('free', 0) if isinstance(bal.get(coin), dict) else 0

                    order = await self.exchange.create_market_buy_order(sym, self.single_order_usdt / p)
                    if order:
                        self.daily_trades += 1
                        await asyncio.sleep(3)
                        new_bal = await self.exchange.fetch_balance()
                        new_free = new_bal.get(coin, {}).get('free', 0) if isinstance(new_bal.get(coin), dict) else 0
                        if new_free > free:
                            self.entries[sym] = p
                            self._trailing_high[sym] = p
                            self._trailing_active[sym] = False
                            if settings.TG_CHAT_ID:
                                try: await self.tg_app.bot.send_message(chat_id=settings.TG_CHAT_ID, text=f"🤖 自动开仓 {sym} {self.single_order_usdt}U @ {p:.2f} 信号{sc}分")
                                except: pass
                        else:
                            if settings.TG_CHAT_ID:
                                try: await self.tg_app.bot.send_message(chat_id=settings.TG_CHAT_ID, text=f"⚠️ 开仓失败 {sym}")
                                except: pass
                        await asyncio.sleep(5)
                        break  # 买一次后跳出，等下次扫描再买，控制节奏

                await asyncio.sleep(30)
            except Exception as e:
                logger.error(f"自动交易错误: {e}")
                await asyncio.sleep(30)

    # ========== 移动止盈 + 硬止损 ==========
    async def _trailing_monitor(self):
        await asyncio.sleep(5)
        while True:
            try:
                if not self.is_running:
                    await asyncio.sleep(5); continue

                for sym in self.symbols:
                    try:
                        bal = await self.exchange.fetch_balance()
                        coin = sym.split('/')[0]
                        amount = bal.get(coin, {}).get('free', 0) if isinstance(bal.get(coin), dict) else 0

                        if amount <= 0:
                            self._trailing_active[sym] = False
                            self._trailing_high[sym] = 0
                            if sym in self.entries: del self.entries[sym]
                            continue

                        ticker = await self.exchange.fetch_ticker(sym)
                        if ticker is None: continue
                        p = ticker['last']

                        entry_price = self.entries.get(sym, p)

                        # ---- 硬止损逻辑 ----
                        if p <= entry_price * (1 - self.sl_pct):
                            logger.info(f"🛡️ 硬止损触发 {sym} @ {p:.2f}")
                            await self.exchange.create_market_sell_order(sym, amount)
                            pnl_pct = ((p - entry_price) / entry_price) * 100
                            trade = {"time": datetime.now(CST).strftime("%m-%d %H:%M"), "symbol": sym, "entry": entry_price, "exit": p, "pnl_pct": round(pnl_pct, 2)}
                            await save_trade(trade); self.trades.insert(0, trade)
                            self._trailing_active[sym] = False; self._trailing_high[sym] = 0
                            if sym in self.entries: del self.entries[sym]
                            if settings.TG_CHAT_ID:
                                try: await self.tg_app.bot.send_message(chat_id=settings.TG_CHAT_ID, text=f"🛡️ 硬止损平仓 {sym} @ {p:.2f} 亏损{pnl_pct:+.2f}%")
                                except: pass
                            continue

                        # ---- 移动止盈逻辑 ----
                        if not self._trailing_active.get(sym, False):
                            if p >= entry_price * (1 + self.tp_pct):
                                self._trailing_active[sym] = True
                                self._trailing_high[sym] = p
                        else:
                            if p > self._trailing_high.get(sym, 0):
                                self._trailing_high[sym] = p
                            high = self._trailing_high[sym]
                            if p <= high * (1 - self.trailing_tp_pct):
                                await self.exchange.create_market_sell_order(sym, amount)
                                pnl_pct = ((p - entry_price) / entry_price) * 100
                                trade = {"time": datetime.now(CST).strftime("%m-%d %H:%M"), "symbol": sym, "entry": entry_price, "exit": p, "pnl_pct": round(pnl_pct, 2)}
                                await save_trade(trade); self.trades.insert(0, trade)
                                self._trailing_active[sym] = False; self._trailing_high[sym] = 0
                                if sym in self.entries: del self.entries[sym]
                                if settings.TG_CHAT_ID:
                                    try: await self.tg_app.bot.send_message(chat_id=settings.TG_CHAT_ID, text=f"🏹 移动止盈平仓 {sym} @ {p:.2f} 盈亏{pnl_pct:+.2f}%")
                                    except: pass
                    except Exception as e:
                        logger.error(f"追踪异常 {sym}: {e}")
                await asyncio.sleep(1)
            except Exception as e:
                logger.error(f"追踪任务异常: {e}")
                await asyncio.sleep(5)

    # ... (其他函数保持之前的完整版)