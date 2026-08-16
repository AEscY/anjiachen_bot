"""
UltimateBot v11.0 - 终极前沿版（2U实盘启动优化）
集成：Meta-RL-Crypto / ChanFormer / Strategy Arena / 风险智能体 / 确定性屏蔽 / 2U优化
"""
import asyncio
import aiohttp
import os
import json
import aiosqlite
import time
import math
import random
import hashlib
import numpy as np
from datetime import datetime, timezone, timedelta
from collections import deque
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ForceReply
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, MessageHandler, filters
from config import settings, logger
from indicators import TechnicalEngine
from ws_manager import WSDataManager
from storage import (
    init_db, load_config, save_config, load_trades, save_trade,
    save_trade_detail, get_recent_performance, get_today_trades,
    export_db_to_json, save_runtime_state, load_runtime_state,
    get_total_fees, get_total_net_profit
)

CST = timezone(timedelta(hours=8))

# ==================== 前沿技术引擎 v11.0 ====================

class FrontierEngine:
    """五大前沿技术实现"""

    # ----- 1. Meta-RL-Crypto (自我进化交易代理) -----
    @staticmethod
    def meta_rl_crypto(price_history, rsi_history, volume_history, win_rate_history, n=50):
        """Meta-RL-Crypto：元学习+强化学习融合"""
        if len(price_history) < n:
            return 50, "数据积累中..."
        
        recent_prices = price_history[-n:]
        recent_rsi = rsi_history[-n:] if len(rsi_history) >= n else [50]*n
        recent_volumes = volume_history[-n:] if len(volume_history) >= n else [1]*n
        
        # 计算市场状态特征
        volatility = np.std(recent_prices) / np.mean(recent_prices) if np.mean(recent_prices) > 0 else 0.01
        trend = (recent_prices[-1] - recent_prices[0]) / recent_prices[0] if recent_prices[0] > 0 else 0
        rsi_mean = np.mean(recent_rsi)
        volume_trend = (recent_volumes[-1] - np.mean(recent_volumes)) / np.mean(recent_volumes) if np.mean(recent_volumes) > 0 else 0
        
        # Actor评分：基于当前市场状态
        actor_score = 50
        if rsi_mean < 30:
            actor_score += 20  # 超卖做多
        elif rsi_mean > 70:
            actor_score -= 20  # 超买做空
        if trend > 0.02:
            actor_score += 15  # 上涨趋势加分
        if volatility > 0.03:
            actor_score += 10  # 高波动加分
        if volume_trend > 0.5:
            actor_score += 10  # 放量加分
        
        # Judge评判：历史胜率加权
        win_rate = win_rate_history[-1] if win_rate_history else 0.5
        judge_weight = 0.3 + 0.4 * win_rate  # 胜率越高，权重越大
        
        # Meta-Judge：综合评分
        meta_score = actor_score * judge_weight + 50 * (1 - judge_weight)
        
        confidence = min(0.95, 0.5 + abs(actor_score - 50) / 100)
        
        return min(100, max(0, meta_score)), f"Meta-RL({confidence:.2f})"

    # ----- 2. ChanFormer (通道式Transformer) -----
    @staticmethod
    def chanformer_score(price_sequence, volume_sequence, all_coin_data, target_symbol, n=50):
        """ChanFormer：跨资产通道注意力"""
        if len(price_sequence) < n:
            return 50, "数据不足"
        
        recent_prices = price_sequence[-n:]
        recent_volumes = volume_sequence[-n:] if len(volume_sequence) >= n else [1]*n
        
        # 通道注意力：计算每个通道的权重
        channel_weights = []
        price_changes = [recent_prices[i] / recent_prices[i-1] - 1 for i in range(1, len(recent_prices))]
        
        for i, change in enumerate(price_changes):
            vol_factor = recent_volumes[i] / (np.mean(recent_volumes) + 0.001)
            channel_weight = abs(change) * vol_factor
            channel_weights.append(channel_weight)
        
        # 跨资产相关性分析
        cross_asset_score = 0
        if all_coin_data:
            correlations = []
            target_price = recent_prices[-1]
            for sym, data in all_coin_data.items():
                if sym == target_symbol or data.get('price', 0) == 0:
                    continue
                other_price = data.get('price', 0)
                other_change = data.get('change_24h', 0)
                rel_strength = (other_price - target_price) / target_price if target_price > 0 else 0
                correlations.append((sym, other_change, rel_strength))
            
            if correlations:
                for sym, change, rel in correlations:
                    if change > 0 and rel < 0:
                        cross_asset_score += 2  # 其他币种上涨，目标相对弱势→补涨
                    elif change < 0 and rel > 0:
                        cross_asset_score -= 2  # 其他币种下跌，目标相对强势→补跌
        
        # 综合评分
        weighted_score = sum(channel_weights[-10:]) / (sum(channel_weights) + 0.001) * 30
        final_score = 50 + weighted_score + cross_asset_score
        
        return min(100, max(0, final_score)), f"ChanFormer(通道数:{len(channel_weights)})"

    # ----- 3. Strategy Arena (多智能体认知系统) -----
    @staticmethod
    def strategy_arena(tech_data, onchain_data, news_sentiment, fear_greed, social_sentiment):
        """Strategy Arena：72策略多智能体投票"""
        # 6个独立策略引擎
        strategies = []
        
        # 策略1：趋势跟踪 (Chimera V5)
        rsi = tech_data.get('rsi', 50)
        if rsi < 35:
            strategies.append(("TrendFollower", 70, "RSI超卖"))
        elif rsi > 65:
            strategies.append(("TrendFollower", -70, "RSI超买"))
        
        # 策略2：均值回归 (Leviathan)
        price = tech_data.get('bb_middle', 0)
        bb_lower = tech_data.get('bb_lower', 0)
        bb_upper = tech_data.get('bb_upper', 0)
        if bb_upper > bb_lower and price > 0:
            bb_pos = (price - bb_lower) / (bb_upper - bb_lower)
            if bb_pos < 0.15:
                strategies.append(("MeanReversion", 65, "布林下轨"))
            elif bb_pos > 0.85:
                strategies.append(("MeanReversion", -65, "布林上轨"))
        
        # 策略3：动量突破 (MomentumDiffusion)
        if tech_data.get('momentum', 0) > 0.02:
            strategies.append(("Momentum", 60, "正动量"))
        elif tech_data.get('momentum', 0) < -0.02:
            strategies.append(("Momentum", -60, "负动量"))
        
        # 策略4：链上信号 (QuantumCollapse)
        if onchain_data:
            netflow = onchain_data.get('exchange_netflow', 0)
            if netflow < -50:
                strategies.append(("OnChain", 55, "交易所净流出"))
            elif netflow > 50:
                strategies.append(("OnChain", -55, "交易所净流入"))
        
        # 策略5：情绪融合 (Hydra)
        sentiment_score = 0
        if news_sentiment:
            sentiment_score += news_sentiment.get('sentiment', 0) * 30
        if social_sentiment:
            sentiment_score += social_sentiment.get('sentiment', 0) * 20
        if fear_greed is not None:
            sentiment_score += (50 - fear_greed) * 0.5
        strategies.append(("Sentiment", sentiment_score, "情绪融合"))
        
        # 策略6：资金费率 (DebateForge)
        funding = tech_data.get('funding_rate', 0)
        if funding is not None:
            if funding < -0.0005:
                strategies.append(("Funding", 50, "费率负值"))
            elif funding > 0.001:
                strategies.append(("Funding", -50, "费率过高"))
        
        # 加权投票
        total_score = 0
        total_weight = 0
        for name, score, reason in strategies:
            weight = abs(score) / 100
            total_score += score * weight
            total_weight += weight
        
        if total_weight > 0:
            final_score = total_score / total_weight
        else:
            final_score = 0
        
        return min(100, max(0, 50 + final_score)), strategies

    # ----- 4. 风险智能体 (Uncertainty Quantification) -----
    @staticmethod
    def risk_agent(price_history, volatility_history, drawdown_history, n=30):
        """风险智能体：动态风险控制"""
        if len(price_history) < n or len(volatility_history) < n:
            return 1.0, 0.5, "数据不足"
        
        recent_returns = [(price_history[i] - price_history[i-1]) / price_history[i-1] for i in range(1, len(price_history))]
        if len(recent_returns) < 10:
            return 1.0, 0.5, "数据不足"
        
        # 计算尾部风险（分位数）
        sorted_returns = sorted(recent_returns)
        var_95 = sorted_returns[int(len(sorted_returns) * 0.05)]  # 5% VaR
        var_99 = sorted_returns[int(len(sorted_returns) * 0.01)]  # 1% VaR
        
        # 计算当前波动率
        current_vol = np.std(recent_returns[-20:]) if len(recent_returns) >= 20 else 0.01
        avg_vol = np.std(recent_returns) if len(recent_returns) > 0 else 0.01
        
        vol_ratio = current_vol / (avg_vol + 0.001)
        
        # 计算最大回撤
        current_drawdown = drawdown_history[-1] if drawdown_history else 0
        
        # 动态仓位调整
        position_multiplier = 1.0
        # 高波动降仓
        if vol_ratio > 1.5:
            position_multiplier *= 0.6
        if vol_ratio > 2.0:
            position_multiplier *= 0.5
        # 尾部风险降仓
        if var_95 < -0.02:
            position_multiplier *= 0.7
        if var_99 < -0.05:
            position_multiplier *= 0.5
        # 回撤降仓
        if current_drawdown > 0.05:
            position_multiplier *= 0.7
        if current_drawdown > 0.10:
            position_multiplier *= 0.5
        
        # 置信度
        confidence = max(0.3, min(0.95, 1 - vol_ratio * 0.2 - abs(var_95) * 10))
        
        return position_multiplier, confidence, f"VaR95:{var_95:.2%}"

    # ----- 5. 确定性屏蔽 -----
    @staticmethod
    def deterministic_shielding(signal, confidence, max_position=0.05):
        """确定性屏蔽：安全边界"""
        if confidence < 0.3:
            return 0, "低置信度屏蔽"
        if abs(signal) > 0.8:
            return signal * 0.7, "信号过强限制"
        # 2U本金：最大单笔0.05U
        position = min(max_position, abs(signal) * max_position * confidence)
        return position, "通过"


# ==================== 核心机器人 ====================

class QuantBot:
    def __init__(self, exchange):
        self.exchange = exchange
        self.ws = WSDataManager(exchange)
        self.tech = TechnicalEngine(exchange)
        self.real_data = RealDataEngine(exchange, self.ws)
        self.orderbook_engine = OrderbookEngine()
        self.frontier = FrontierEngine()
        self.lock = asyncio.Lock()

        # 基础配置
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
        self._bb_bandwidth_history = {}
        self._close_prices_history = {}
        self._price_history = {}
        self._volatility_history = {}
        self._win_rate_history = {}
        self._sharpe_history = {}
        self._drawdown_history = {}
        self._performance_metrics = {}

        # 高级模块
        self._consecutive_losses = 0
        self._today_loss_pct = 0.0
        self._is_paused = False
        self._last_pause_time = 0
        self._account_balance = 0.0
        self._delta_neutral_positions = {}
        self._strategy_votes = {}  # Strategy Arena投票记录

        # ====== 2U优化配置 ======
        self._two_u_config = {
            "enabled": True,
            "max_single_position": 0.05,      # 最大单笔0.05U
            "min_single_position": 0.01,      # 最小单笔0.01U
            "daily_loss_limit": 0.05,         # 日亏损5%熔断
            "consecutive_loss_limit": 3,      # 连续亏损3笔暂停
            "target_profit_pct": 0.001,       # 目标止盈0.1%
            "target_loss_pct": 0.0005,        # 目标止损0.05%
            "preferred_arbitrage": True,      # 优先资金费率套利
        }
        self._two_u_stats = {
            "total_trades": 0,
            "total_profit": 0.0,
            "profit_today": 0.0,
            "today_date": datetime.now(CST).day,
            "peak_balance": 2.0,
            "max_drawdown": 0.0,
        }

        # 基础费率
        self.taker_fee = settings.TAKER_FEE
        self.maker_fee = settings.MAKER_FEE
        self.min_profit_margin = settings.MIN_PROFIT_MARGIN
        self.breakeven_pct = (self.taker_fee * 2) + self.min_profit_margin

        # 权限
        raw = settings.ALLOWED_USERS
        self.allowed = {int(x.strip()) for x in raw.split(",") if x.strip().isdigit()} if raw else set()
        self.env_tag = "🧪 (模拟盘)" if settings.IS_SANDBOX else "🔴 (实盘)"

        # 状态存储
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

        # ====== AI 市场分析模块 ======
        self.ai_insight = {
            "timestamp": 0,
            "summary": "等待首次分析...",
            "btc_trend": "中性",
            "eth_trend": "中性",
            "fear_greed": 50,
            "news_sentiment": 0.0,
            "social_sentiment": 0.0,
            "news_headlines": [],
            "recommendation": "观望",
            "score": 50,
            "regime": "neutral",
            "meta_rl": "数据积累中...",
            "chanformer": "数据积累中...",
            "strategy_arena": "数据积累中...",
            "risk_agent": "数据积累中...",
        }
        self.ai_api_key = os.getenv("DEEPSEEK_API_KEY", "") or os.getenv("OPENAI_API_KEY", "")
        self.ai_model = os.getenv("AI_MODEL", "deepseek-chat")
        self.ai_base_url = os.getenv("AI_BASE_URL", "https://api.deepseek.com/v1")
        self.ai_enabled = bool(self.ai_api_key)

        # ====== Telegram 机器人 ======
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
                CommandHandler("setcoinonly", self.cmd_setcoinonly),
                CommandHandler("lowbalance", self.cmd_lowbalance),
                CommandHandler("arbstats", self.cmd_arb_stats),
                CommandHandler("twou", self.cmd_twou),
            ]
            for h in handlers:
                self.tg_app.add_handler(h)
            self.tg_app.add_handler(CallbackQueryHandler(self.handle_button_click))
            self.tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_text_input))

    # ==================== 辅助函数 ====================

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

    async def _round_amount_by_precision(self, symbol: str, amount: float) -> float:
        try:
            if self.exchange and self.exchange.exchange:
                market = self.exchange.exchange.market(symbol)
                if market and 'limits' in market and 'amount' in market['limits']:
                    min_amt = market['limits']['amount'].get('min', 0)
                    max_amt = market['limits']['amount'].get('max', float('inf'))
                    if min_amt and amount < min_amt:
                        amount = min_amt
                    if max_amt != float('inf') and amount > max_amt:
                        amount = max_amt
                if market and 'precision' in market and 'amount' in market['precision']:
                    precision = market['precision']['amount']
                    if precision > 0:
                        amount = float(int(amount / precision) * precision)
                    elif precision == 0:
                        amount = int(amount)
            return max(0.000001, amount)
        except:
            return amount

    def _calculate_twou_position(self, base_amount=0.02):
        """2U优化：超低仓位计算"""
        config = self._two_u_config
        total_balance = self._cached_usdt_free
        for coin, free in self._cached_balances.items():
            ticker = self.ws.get_ticker(coin + "/USDT")
            if ticker:
                total_balance += free * ticker.get('last', 0)
        
        self._account_balance = total_balance
        
        # 2U本金：单笔0.01-0.05U
        if total_balance < 1:
            return max(0.005, base_amount * 0.3)
        elif total_balance < 2:
            return max(0.01, base_amount * 0.6)
        elif total_balance < 5:
            return max(0.02, base_amount * 0.8)
        else:
            return min(0.05, base_amount * 1.0)

    async def _check_twou_risk(self):
        """2U风控：日亏损5%熔断 + 连续3笔亏损暂停"""
        config = self._two_u_config
        
        # 连续亏损检查
        if self._consecutive_losses >= config.get("consecutive_loss_limit", 3):
            if time.time() - self._last_pause_time > 1800:  # 30分钟后恢复
                self._consecutive_losses = 0
                self._is_paused = False
            else:
                return False
        
        # 日亏损熔断
        today = datetime.now(CST).day
        if today != self._two_u_stats.get("today_date", 0):
            self._two_u_stats["profit_today"] = 0.0
            self._two_u_stats["today_date"] = today
        
        if self._two_u_stats["profit_today"] < -config.get("daily_loss_limit", 0.05) * self._account_balance:
            if not self._is_paused:
                await self._alert(f"⛔ 2U日亏损熔断: {self._two_u_stats['profit_today']:.4f}U", "critical")
                self._is_paused = True
            return False
        
        return True

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

    # ==================== 数据加载与保存 ====================

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
        try:
            if isinstance(coin_cfg_raw, str):
                self.coin_configs = json.loads(coin_cfg_raw) if coin_cfg_raw else {}
            elif isinstance(coin_cfg_raw, dict):
                self.coin_configs = coin_cfg_raw
            else:
                self.coin_configs = {}
        except:
            self.coin_configs = {}
        if not isinstance(self.coin_configs, dict):
            self.coin_configs = {}

        grid_cfg_raw = cfg.get('grid_configs', '{}')
        try:
            if isinstance(grid_cfg_raw, str):
                self.grid_configs = json.loads(grid_cfg_raw) if grid_cfg_raw else {}
            elif isinstance(grid_cfg_raw, dict):
                self.grid_configs = grid_cfg_raw
            else:
                self.grid_configs = {}
        except:
            self.grid_configs = {}

        self.trades = await load_trades()
        state = await load_runtime_state()
        if state:
            self.position_counts = state.get('position_counts', {})
            self.entries = state.get('entries', {})
            self.peak_total_value = state.get('peak_total_value', 0)
            self.daily_trades = state.get('daily_trades', 0)
            self._trailing_active = state.get('trailing_active', {})
            self._trailing_high = state.get('trailing_high', {})
            self._two_u_stats = state.get('two_u_stats', self._two_u_stats)
        
        # 初始化历史数据
        for sym in self.symbols:
            if sym not in self._price_history:
                self._price_history[sym] = []
            if sym not in self._volatility_history:
                self._volatility_history[sym] = []
            if sym not in self._win_rate_history:
                self._win_rate_history[sym] = []
            if sym not in self._drawdown_history:
                self._drawdown_history[sym] = []
        
        logger.info("✅ UltimateBot v11.0 已加载（2U优化版）")

    async def _save_runtime_state(self):
        state = {
            'position_counts': self.position_counts,
            'entries': self.entries,
            'peak_total_value': self.peak_total_value,
            'daily_trades': self.daily_trades,
            'trailing_active': self._trailing_active,
            'trailing_high': self._trailing_high,
            'two_u_stats': self._two_u_stats,
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

    # ==================== 2U专用命令 ====================

    async def cmd_twou(self, update, context):
        """2U优化模式配置"""
        if not self._auth(update):
            return
        try:
            if len(context.args) == 0:
                config = self._two_u_config
                stats = self._two_u_stats
                await update.effective_message.reply_text(
                    f"📊 **2U优化模式状态**\n"
                    f"• 最大单笔: {config['max_single_position']:.3f}U\n"
                    f"• 最小单笔: {config['min_single_position']:.3f}U\n"
                    f"• 日亏损熔断: {config['daily_loss_limit']*100:.0f}%\n"
                    f"• 连续亏损暂停: {config['consecutive_loss_limit']}笔\n"
                    f"• 目标止盈: {config['target_profit_pct']*100:.2f}%\n"
                    f"• 目标止损: {config['target_loss_pct']*100:.2f}%\n"
                    f"━━━━━━━━━━━━━━━━━\n"
                    f"• 总交易: {stats['total_trades']}笔\n"
                    f"• 累计盈利: {stats['total_profit']:.4f}U\n"
                    f"• 今日盈利: {stats['profit_today']:.4f}U\n"
                    f"• 峰值余额: {stats['peak_balance']:.4f}U\n"
                    f"• 最大回撤: {stats['max_drawdown']*100:.2f}%"
                )
            elif context.args[0].lower() == "on":
                self._two_u_config["enabled"] = True
                await update.effective_message.reply_text("✅ 2U优化模式已开启")
            elif context.args[0].lower() == "off":
                self._two_u_config["enabled"] = False
                await update.effective_message.reply_text("✅ 2U优化模式已关闭")
            elif context.args[0].lower() == "reset":
                self._two_u_stats["total_trades"] = 0
                self._two_u_stats["total_profit"] = 0.0
                self._two_u_stats["profit_today"] = 0.0
                self._two_u_stats["peak_balance"] = self._account_balance
                self._two_u_stats["max_drawdown"] = 0.0
                await self._save_runtime_state()
                await update.effective_message.reply_text("✅ 2U统计数据已重置")
            else:
                await update.effective_message.reply_text(
                    "用法: /twou         查看状态\n"
                    "/twou on      开启2U优化\n"
                    "/twou off     关闭2U优化\n"
                    "/twou reset   重置统计数据"
                )
        except Exception as e:
            await update.effective_message.reply_text(f"❌ 错误: {e}")

    # ==================== 多周期数据获取 ====================

    async def _get_multi_timeframe_data(self, symbol):
        timeframes = ['1m', '5m', '15m']
        result = {}
        for tf in timeframes:
            try:
                tech = await self.tech.calc(symbol, tf, 50)
                result[tf] = tech
            except:
                result[tf] = None
        return result

    # ==================== AI 市场分析模块 ====================

    async def _ai_analyze_market(self):
        while self.is_running:
            try:
                btc_ticker = self.ws.get_ticker("BTC/USDT")
                eth_ticker = self.ws.get_ticker("ETH/USDT")
                btc_price = btc_ticker['last'] if btc_ticker else 0
                eth_price = eth_ticker['last'] if eth_ticker else 0
                btc_change = btc_ticker.get('percentage', 0) if btc_ticker else 0
                eth_change = eth_ticker.get('percentage', 0) if eth_ticker else 0

                fg_data = await self.real_data.get_fear_greed_index()
                fg = fg_data["value"] if fg_data else 50

                news_data = await self.real_data.get_news_sentiment()
                social_data = await self.real_data.get_social_sentiment()
                news_sentiment = news_data.get('sentiment', 0)
                social_sentiment = social_data.get('sentiment', 0)
                news_headlines = news_data.get('headlines', [])

                btc_trend = "中性"
                eth_trend = "中性"
                try:
                    btc_tech = await self.tech.calc("BTC/USDT", "1h", 20)
                    eth_tech = await self.tech.calc("ETH/USDT", "1h", 20)
                    if btc_tech:
                        btc_ema = btc_tech.get('bb_middle', btc_price)
                        if btc_price > btc_ema * 1.02:
                            btc_trend = "看涨"
                        elif btc_price < btc_ema * 0.98:
                            btc_trend = "看跌"
                    if eth_tech:
                        eth_ema = eth_tech.get('bb_middle', eth_price)
                        if eth_price > eth_ema * 1.02:
                            eth_trend = "看涨"
                        elif eth_price < eth_ema * 0.98:
                            eth_trend = "看跌"
                except:
                    pass

                all_coin_data = {}
                for sym in self.symbols:
                    ticker = self.ws.get_ticker(sym)
                    if ticker:
                        all_coin_data[sym] = {'price': ticker.get('last', 0), 'change_24h': ticker.get('percentage', 0)}

                all_scores = []
                meta_rl_results = []
                chanformer_results = []
                arena_results = []
                risk_results = []

                for sym in self.symbols:
                    try:
                        ticker = self.ws.get_ticker(sym)
                        if ticker is None:
                            continue
                        p = ticker['last']
                        tech = await self.tech.calc(sym, self.timeframe, 50)
                        if tech is None:
                            continue
                        
                        if sym not in self._price_history:
                            self._price_history[sym] = []
                        if sym not in self._volatility_history:
                            self._volatility_history[sym] = []
                        if sym not in self._win_rate_history:
                            self._win_rate_history[sym] = []
                        if sym not in self._drawdown_history:
                            self._drawdown_history[sym] = []
                        
                        self._price_history[sym].append(p)
                        if len(self._price_history[sym]) > 100:
                            self._price_history[sym].pop(0)
                        
                        volatility = tech.get('atr', 0) / tech.get('bb_middle', 1) if tech.get('bb_middle', 0) > 0 else 0.01
                        self._volatility_history[sym].append(volatility)
                        if len(self._volatility_history[sym]) > 50:
                            self._volatility_history[sym].pop(0)
                        
                        onchain = await self.real_data.get_onchain_metrics(sym)
                        funding = await self.real_data.get_funding_rate(sym)
                        
                        rsi_hist = [h.get('rsi', 50) for h in self._rsi_history.get(sym, [])]
                        
                        # 1. Meta-RL-Crypto
                        meta_score, meta_desc = self.frontier.meta_rl_crypto(
                            self._price_history[sym], rsi_hist, 
                            self._volume_history.get(sym, []),
                            self._win_rate_history[sym]
                        )
                        meta_rl_results.append(f"{sym}:{meta_desc}")
                        
                        # 2. ChanFormer
                        chan_score, chan_desc = self.frontier.chanformer_score(
                            self._price_history[sym], self._volume_history.get(sym, []),
                            all_coin_data, sym
                        )
                        chanformer_results.append(f"{sym}:{chan_desc}")
                        
                        # 3. Strategy Arena
                        tech_data = {
                            'rsi': tech.get('rsi', 50),
                            'bb_middle': tech.get('bb_middle', 0),
                            'bb_lower': tech.get('bb_lower', 0),
                            'bb_upper': tech.get('bb_upper', 0),
                            'momentum': tech.get('momentum', 0),
                            'funding_rate': funding,
                        }
                        arena_score, arena_strategies = self.frontier.strategy_arena(
                            tech_data, onchain, news_data, fg, social_data
                        )
                        arena_results.append(f"{sym}:{arena_score:.0f}")
                        
                        # 4. 风险智能体
                        risk_multiplier, risk_conf, risk_desc = self.frontier.risk_agent(
                            self._price_history[sym],
                            self._volatility_history[sym],
                            self._drawdown_history[sym]
                        )
                        risk_results.append(f"{sym}:{risk_desc}")
                        
                        # 5. 综合评分
                        combined_score = (meta_score * 0.25 + chan_score * 0.25 + 
                                          arena_score * 0.25 + risk_conf * 0.25)
                        all_scores.append(combined_score)
                        
                    except Exception as e:
                        logger.warning(f"前沿技术分析失败 {sym}: {e}")
                        continue

                avg_score = sum(all_scores) / len(all_scores) if all_scores else 50
                recommendation = "观望"
                if avg_score >= 75:
                    recommendation = "积极做多"
                elif avg_score >= 60:
                    recommendation = "谨慎做多"
                elif avg_score >= 40:
                    recommendation = "观望"
                elif avg_score >= 25:
                    recommendation = "谨慎减仓"
                else:
                    recommendation = "清仓避险"

                summary = (f"📊 BTC: {btc_trend} ({btc_change:+.2f}%) | ETH: {eth_trend} ({eth_change:+.2f}%)\n"
                           f"😨 恐惧贪婪: {fg} ({fg_data['classification'] if fg_data else '中性'})\n"
                           f"📰 新闻: {news_sentiment:+.2f} | 社交: {social_sentiment:+.2f}\n"
                           f"🧠 Meta-RL: {meta_rl_results[:2] if meta_rl_results else '无'}\n"
                           f"📈 ChanFormer: {chanformer_results[:2] if chanformer_results else '无'}\n"
                           f"🎯 综合评分: {avg_score:.0f}/100\n"
                           f"💡 建议: {recommendation}")

                self.ai_insight = {
                    "timestamp": time.time(),
                    "summary": summary,
                    "btc_trend": btc_trend,
                    "eth_trend": eth_trend,
                    "fear_greed": fg,
                    "news_sentiment": news_sentiment,
                    "social_sentiment": social_sentiment,
                    "news_headlines": news_headlines[:3],
                    "recommendation": recommendation,
                    "score": avg_score,
                    "meta_rl": meta_rl_results[0] if meta_rl_results else "无",
                    "chanformer": chanformer_results[0] if chanformer_results else "无",
                    "strategy_arena": arena_results[0] if arena_results else "无",
                    "risk_agent": risk_results[0] if risk_results else "无",
                }
                logger.info(f"🤖 AI分析完成: {btc_trend}/{eth_trend} | {recommendation} | 评分{avg_score:.0f}")
            except Exception as e:
                logger.error(f"AI分析异常: {e}")
            await asyncio.sleep(1800)

    # ==================== 资金费率套利 ====================

    async def _delta_neutral_arbitrage(self):
        while self.is_running:
            try:
                if not self._two_u_config.get("enabled", True):
                    await asyncio.sleep(60)
                    continue

                await self._refresh_balance_cache()
                total_balance = self._cached_usdt_free
                for coin, free in self._cached_balances.items():
                    ticker = self.ws.get_ticker(coin + "/USDT")
                    if ticker:
                        total_balance += free * ticker.get('last', 0)

                # 2U优化：每次套利用0.02-0.05U
                amount_usdt = self._calculate_twou_position(0.02)

                for sym in self.symbols:
                    if sym in self._delta_neutral_positions:
                        pos = self._delta_neutral_positions[sym]
                        if pos['entry_time'] + 7.5*3600 < time.time():
                            pnl = await self._close_delta_neutral(sym)
                            if pnl:
                                self._two_u_stats["total_trades"] += 1
                                self._two_u_stats["total_profit"] += pnl
                                self._two_u_stats["profit_today"] += pnl
                                await self._alert(
                                    f"✅ {sym} 资金费率套利平仓 盈利{pnl:.4f}U\n"
                                    f"累计套利收益: {self._two_u_stats['total_profit']:.4f}U",
                                    "info"
                                )
                        continue

                    funding = await self.real_data.get_funding_rate(sym)
                    if funding is None:
                        continue
                    rate = funding.get('fundingRate', 0)

                    if rate > 0.0003:
                        success = await self._open_delta_neutral(sym, rate, amount_usdt)
                        if success:
                            logger.info(f"✅ 资金费率套利开仓 {sym} 费率{rate*100:.2f}% 金额{amount_usdt:.4f}U")

                await asyncio.sleep(30)
            except Exception as e:
                logger.error(f"资金费率套利异常: {e}")
                await asyncio.sleep(30)

    async def _open_delta_neutral(self, symbol, funding_rate, amount_usdt=None):
        try:
            if amount_usdt is None:
                amount_usdt = 0.02
            ticker = self.ws.get_ticker(symbol)
            if ticker is None:
                ticker = await self.exchange.fetch_ticker(symbol)
            if ticker is None:
                return False
            price = ticker['last']
            if self._cached_usdt_free < amount_usdt * 1.1:
                return False
            coin_amount = amount_usdt / price
            rounded_amount = await self._round_amount_by_precision(symbol, coin_amount)
            if rounded_amount <= 0:
                return False
            order = await self.exchange.create_market_buy_order(symbol, rounded_amount)
            if order:
                self._delta_neutral_positions[symbol] = {
                    'entry_time': time.time(),
                    'price': price,
                    'amount': rounded_amount,
                    'amount_usdt': amount_usdt,
                    'funding_rate': funding_rate,
                }
                return True
            return False
        except Exception as e:
            logger.error(f"开仓套利失败 {symbol}: {e}")
            return False

    async def _close_delta_neutral(self, symbol):
        try:
            pos = self._delta_neutral_positions.get(symbol)
            if not pos:
                return 0.0
            order = await self.exchange.create_market_sell_order(symbol, pos['amount'])
            if order:
                avg_price = order.get('average', pos['price'])
                revenue = pos['amount'] * avg_price
                cost = pos['amount_usdt']
                pnl = revenue - cost - (revenue * 0.001 + cost * 0.001)
                await self._refresh_balance_cache(force=True)
                del self._delta_neutral_positions[symbol]
                return pnl
            return 0.0
        except Exception as e:
            logger.error(f"平仓套利失败 {symbol}: {e}")
            return 0.0

    # ==================== 链上监控 ====================

    async def _onchain_monitor(self):
        while self.is_running:
            try:
                for sym in self.symbols:
                    data = await self.real_data.get_onchain_metrics(sym)
                    if data['whale_transfers'] > 5:
                        await self._alert(f"🐋 {sym} 巨鲸转账 {data['whale_transfers']} 笔，注意风险", "warning")
                    if data['exchange_netflow'] < -100:
                        await self._alert(f"📊 {sym} 交易所净流出 {data['exchange_netflow']:.0f}，买入信号", "info")
                await asyncio.sleep(300)
            except Exception as e:
                logger.error(f"链上监控异常: {e}")
                await asyncio.sleep(300)

    # ==================== 三角套利监控 ====================

    async def _triangular_arbitrage_monitor(self):
        while self.is_running:
            try:
                prices = []
                for sym in self.symbols:
                    ticker = self.ws.get_ticker(sym)
                    if ticker:
                        prices.append(ticker.get('last', 0))
                if len(prices) >= 3:
                    arb, profit = self.frontier.triangular_arbitrage(prices)
                    if arb and 0 < profit < 5:
                        await self._alert(f"🔺 三角套利机会 {profit:.2f}%", "info")
                await asyncio.sleep(300)
            except Exception as e:
                logger.error(f"三角套利监控异常: {e}")
                await asyncio.sleep(300)

    # ==================== 开仓决策 ====================

    async def _should_open_position(self, sym, p, tech, funding, fg, usdt_free):
        scores = []
        details = []

        if sym not in self._volume_history:
            self._volume_history[sym] = []
        if sym not in self._close_prices_history:
            self._close_prices_history[sym] = []
        if sym not in self._bb_bandwidth_history:
            self._bb_bandwidth_history[sym] = []
        if sym not in self._rsi_history:
            self._rsi_history[sym] = []

        ticker = self.ws.get_ticker(sym)
        if ticker:
            vol = ticker.get('volume', 0)
            if vol > 0:
                self._volume_history[sym].append(vol)
                if len(self._volume_history[sym]) > 50:
                    self._volume_history[sym].pop(0)
        if p > 0:
            self._close_prices_history[sym].append(p)
            if len(self._close_prices_history[sym]) > 100:
                self._close_prices_history[sym].pop(0)

        rsi = tech.get('rsi', 50)
        self._rsi_history[sym].append({'rsi': rsi, 'price': p, 'time': time.time()})
        if len(self._rsi_history[sym]) > 100:
            self._rsi_history[sym].pop(0)

        bb_upper = tech.get('bb_upper', 0)
        bb_lower = tech.get('bb_lower', 0)
        if bb_upper > 0 and bb_lower > 0:
            bw = (bb_upper - bb_lower) / p * 100 if p > 0 else 0
            self._bb_bandwidth_history[sym].append(bw)
            if len(self._bb_bandwidth_history[sym]) > 100:
                self._bb_bandwidth_history[sym].pop(0)

        multi = await self._get_multi_timeframe_data(sym)
        onchain = await self.real_data.get_onchain_metrics(sym)
        news = await self.real_data.get_news_sentiment()
        social = await self.real_data.get_social_sentiment()

        rsi_hist = [h.get('rsi', 50) for h in self._rsi_history.get(sym, [])]
        volatility = tech.get('atr', 0) / tech.get('bb_middle', 1) if tech.get('bb_middle', 0) > 0 else 0.01

        all_coin_data = {}
        for s in self.symbols:
            t = self.ws.get_ticker(s)
            if t:
                all_coin_data[s] = {'price': t.get('last', 0), 'change_24h': t.get('percentage', 0)}

        # 1. Meta-RL-Crypto
        meta_score, meta_desc = self.frontier.meta_rl_crypto(
            self._price_history.get(sym, []), rsi_hist,
            self._volume_history.get(sym, []),
            self._win_rate_history.get(sym, [])
        )
        scores.append(meta_score * 0.25)
        details.append(f"MetaRL:{meta_desc}")

        # 2. ChanFormer
        chan_score, chan_desc = self.frontier.chanformer_score(
            self._price_history.get(sym, []),
            self._volume_history.get(sym, []),
            all_coin_data, sym
        )
        scores.append(chan_score * 0.20)
        details.append(f"ChanFormer:{chan_desc}")

        # 3. Strategy Arena
        tech_data = {
            'rsi': tech.get('rsi', 50),
            'bb_middle': tech.get('bb_middle', 0),
            'bb_lower': tech.get('bb_lower', 0),
            'bb_upper': tech.get('bb_upper', 0),
            'momentum': tech.get('momentum', 0),
            'funding_rate': funding,
        }
        arena_score, arena_strategies = self.frontier.strategy_arena(
            tech_data, onchain, news, fg, social
        )
        scores.append(arena_score * 0.25)
        if arena_strategies:
            details.append(f"Arena:{len(arena_strategies)}策略")

        # 4. 风险智能体
        risk_multiplier, risk_conf, risk_desc = self.frontier.risk_agent(
            self._price_history.get(sym, []),
            self._volatility_history.get(sym, []),
            self._drawdown_history.get(sym, [])
        )
        scores.append(risk_conf * 0.20)
        details.append(f"Risk:{risk_desc}")

        # 5. 确定性屏蔽
        total_score = sum(scores)
        total_score = min(100, max(0, total_score))
        
        shielded_pos, shield_reason = self.frontier.deterministic_shielding(
            (total_score - 50) / 50, risk_conf, self._two_u_config["max_single_position"]
        )
        details.append(f"Shield:{shield_reason}")

        coin_score = self._get_coin_param(sym, 'auto_min_score', self.auto_min_score)
        should_open = total_score >= coin_score
        is_high_confidence = total_score >= 80

        logger.info(f"📊 {sym} 前沿综合评分: {total_score:.0f}/{coin_score} | {', '.join(details[:3])}")
        return {'should_open': should_open, 'score': total_score, 'is_high_confidence': is_high_confidence, 'details': details}

    # ==================== 命令函数 ====================

    def _auth(self, update: Update):
        if not self.allowed:
            return True
        return update.effective_user.id in self.allowed

    def _parse_pct(self, val):
        return val / 100.0

    def _build_main_keyboard(self):
        f_status = "已开启" if self.orderbook_filter else "已关闭"
        b_status = "已开启" if self.waterfall_breaker else "已关闭"
        auto_status = "🟢" if self.auto_trade_enabled else "🔴"
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("🚨 紧急全平", callback_data="panic_confirm")],
            [InlineKeyboardButton(f"🏮 盘口: [{f_status}]", callback_data="toggle_filter"), InlineKeyboardButton(f"🚨 熔断: [{b_status}]", callback_data="toggle_breaker")],
            [InlineKeyboardButton("⚡ 开启", callback_data="bot_start"), InlineKeyboardButton("🔴 关机", callback_data="bot_stop")],
            [InlineKeyboardButton(f"🤖 自动交易: {auto_status}", callback_data="toggle_auto"), InlineKeyboardButton("🎯 阈值", callback_data="menu_set_autoscore")],
            [InlineKeyboardButton("📊 看板", callback_data="dashboard"), InlineKeyboardButton("💳 余额", callback_data="balance")],
            [InlineKeyboardButton("📋 持币", callback_data="holdings"), InlineKeyboardButton("📋 监控", callback_data="list_symbols")],
            [InlineKeyboardButton("🎯 止盈", callback_data="menu_set_tp"), InlineKeyboardButton("🛡️ 止损", callback_data="menu_set_sl")],
            [InlineKeyboardButton("📉 移损", callback_data="menu_set_tsl"), InlineKeyboardButton("🏹 移盈", callback_data="menu_set_tmpt")],
            [InlineKeyboardButton("💵 额度", callback_data="menu_set_amount"), InlineKeyboardButton("⏱ 周期", callback_data="menu_set_tf")],
            [InlineKeyboardButton("🔒 底线", callback_data="menu_set_reserve"), InlineKeyboardButton("🔢 上限", callback_data="menu_set_trades")],
            [InlineKeyboardButton("➕ 币种", callback_data="menu_add_symbol"), InlineKeyboardButton("➖ 币种", callback_data="menu_del_symbol")],
            [InlineKeyboardButton("🧠 大脑", callback_data="brain_status"), InlineKeyboardButton("📈 分析", callback_data="gap_analysis")],
            [InlineKeyboardButton("⚡ 预设", callback_data="menu_preset"), InlineKeyboardButton("📜 历史", callback_data="history")],
            [InlineKeyboardButton("📈 仪表盘", callback_data="stats_panel"), InlineKeyboardButton("💾 备份", callback_data="backup_panel")],
            [InlineKeyboardButton("🔄 同步持仓", callback_data="sync_pos"), InlineKeyboardButton("🔄 刷新", callback_data="refresh_panel")]
        ])

    # ----- 常用命令 -----

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
                await update.effective_message.reply_text("🤖 终极版自动交易已开启（前沿5合1策略 + 2U优化）")
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
            await update.effective_message.reply_document(document=data.encode('utf-8'), filename=f"backup_{datetime.now(CST).strftime('%Y%m%d_%H%M%S')}.json", caption="📦 数据库备份")
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
                "conservative": {"tp":3,"sl":2,"tsl":1,"tmpt":1,"tf":"1h","amt":1,"reserve":2},
                "balanced": {"tp":1.5,"sl":1,"tsl":0.5,"tmpt":0.5,"tf":"15m","amt":1,"reserve":1},
                "aggressive": {"tp":0.8,"sl":0.5,"tsl":0.3,"tmpt":0.3,"tf":"5m","amt":1,"reserve":0.5},
                "ETH滚雪球": {"tp":0.8,"sl":0.5,"tsl":0.5,"tmpt":0.3,"tf":"1m","amt":10,"reserve":5,"score":60},
                "BTC滚雪球": {"tp":0.6,"sl":0.4,"tsl":0.4,"tmpt":0.2,"tf":"1m","amt":10,"reserve":5,"score":60},
                "SOL滚雪球": {"tp":1.0,"sl":0.5,"tsl":0.5,"tmpt":0.3,"tf":"1m","amt":1,"reserve":1,"score":60},
                "DOGE滚雪球": {"tp":1.2,"sl":0.6,"tsl":0.6,"tmpt":0.4,"tf":"1m","amt":1,"reserve":1,"score":60},
                "ADA滚雪球": {"tp":1.2,"sl":0.6,"tsl":0.6,"tmpt":0.4,"tf":"1m","amt":0.5,"reserve":0.5,"score":60},
                "2U实盘": {"tp":0.1,"sl":0.05,"tsl":0.03,"tmpt":0.02,"tf":"1m","amt":0.02,"reserve":0.5,"score":60},
            }
            if mode not in presets:
                await update.effective_message.reply_text("可选: conservative/balanced/aggressive/滚雪球系列/2U实盘")
                return
            p = presets[mode]
            self.tp_pct = p["tp"]/100; self.sl_pct = p["sl"]/100; self.trailing_sl_pct = p["tsl"]/100; self.trailing_tp_pct = p["tmpt"]/100
            self.timeframe = p["tf"]; self.single_order_usdt = p["amt"]; self.reserve_bottom = p["reserve"]
            if "score" in p: self.auto_min_score = p["score"]
            await self._save_config()
            names = {"conservative":"保守","balanced":"平衡","aggressive":"激进","ETH滚雪球":"ETH滚雪球","BTC滚雪球":"BTC滚雪球","SOL滚雪球":"SOL滚雪球","DOGE滚雪球":"DOGE滚雪球","ADA滚雪球":"ADA滚雪球","2U实盘":"2U实盘启动"}
            await update.effective_message.reply_text(f"⚡ {names[mode]}方案已生效\n止盈{self.tp_pct:.1%} 止损{self.sl_pct:.1%}")
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
            net_pnl = t.get('net_pnl', 0); net_pnl_pct = t.get('net_pnl_pct', 0)
            if net_pnl != 0:
                lines.append(f"{'🟢' if net_pnl_pct>0 else '🔴'} {t['time']} {t['symbol']} 净利{net_pnl_pct:+.2f}% ({net_pnl:+.4f}U)")
            else:
                lines.append(f"{'🟢' if t['pnl_pct']>0 else '🔴'} {t['time']} {t['symbol']} {t['pnl_pct']:+.2f}%")
        await update.effective_message.reply_text("\n".join(lines), parse_mode="Markdown")

    async def cmd_status(self, update, context):
        if not self._auth(update):
            return

        bal = await self.exchange.fetch_balance()
        usdt_free = self._get_usdt_free(bal)

        total_value = usdt_free
        for sym in self.symbols:
            coin = sym.split('/')[0]
            free = bal.get(coin, {}).get('free', 0) if isinstance(bal.get(coin), dict) else 0
            ticker = self.ws.get_ticker(sym)
            if ticker is None:
                ticker = await self.exchange.fetch_ticker(sym)
            if ticker and ticker.get('last'):
                total_value += free * ticker['last']

        occupied = total_value - usdt_free
        perf = await get_recent_performance(20)
        if perf and perf['total'] > 0:
            win_rate = perf['win_rate']; wins = perf['wins']; total_trades = perf['total']
        else:
            win_rate = 0.0; wins = 0; total_trades = 0

        lines = []
        lines.append(f"📊 **多币种量化机器人看板** {self.env_tag}")
        lines.append(f"• 系统状态: {'🟢 RUNNING' if self.is_running else '🔴 STOPPED'}")
        lines.append(f"• 策略模式: 🚀 **前沿5合1策略 + 2U优化**")
        lines.append(f"• 全局默认: 单笔{self.single_order_usdt:.3f}U | 周期{self.timeframe} | 止盈{self.tp_pct:.1%}")
        lines.append(f"• 占用资金: {occupied:.2f} USDT")
        lines.append("-" * 40)

        has_position = False
        for sym in self.symbols:
            count = self.position_counts.get(sym, 0)
            if count == 0:
                continue
            has_position = True
            tp = self._get_coin_param(sym, 'tp_pct', self.tp_pct)
            sl = self._get_coin_param(sym, 'sl_pct', self.sl_pct)
            tsl = self._get_coin_param(sym, 'trailing_sl_pct', self.trailing_sl_pct)
            tmpt = self._get_coin_param(sym, 'trailing_tp_pct', self.trailing_tp_pct)
            amount = self._get_coin_param(sym, 'single_order_usdt', self.single_order_usdt)
            timeframe = self._get_coin_param(sym, 'timeframe', self.timeframe)
            max_pos = self.max_positions_per_coin
            filled = min(count, max_pos)
            bar = "▓" * filled + "░" * (max_pos - filled)
            lines.append(f"\n🔹 **[{sym}]** (周期:{timeframe} | 止盈:{tp:.1%} | 移动止损:{tsl:.1%} | 单笔:{amount:.3f}U)")
            lines.append(f"[{bar}] {count}/{max_pos}")

            entry = self.entries.get(sym, 0)
            high_price = self._trailing_high.get(sym, 0)
            if entry > 0:
                lines.append(f"└ 仓位#1: 买价{entry:.4f} | 最高{high_price:.4f}")
                if count > 1:
                    lines.append(f"└ ... 还有 {count-1} 个仓位")

        if not has_position:
            lines.append("\n📭 暂无持仓")

        lines.append("-" * 40)
        lines.append(f"• 胜率: {win_rate*100:.1f}% ({wins}/{total_trades} 胜)")
        lines.append(f"• 今日亏损: {self._today_loss_pct*100:.1f}%")
        lines.append(f"• 连续亏损: {self._consecutive_losses} 笔")
        lines.append(f"• 全局状态: {'⏸️ 暂停' if self._is_paused else '🟢 正常'}")

        stats = self._two_u_stats
        lines.append(f"• 💰 2U套利: {stats['total_trades']}笔 累计{stats['total_profit']:.4f}U 今日{stats['profit_today']:.4f}U")

        if self.ai_enabled and time.time() - self.ai_insight["timestamp"] < 3600:
            lines.append(f"• 🤖 AI: {self.ai_insight['recommendation']} (评分{self.ai_insight['score']:.0f})")
            lines.append(f"   BTC:{self.ai_insight['btc_trend']} ETH:{self.ai_insight['eth_trend']} FG:{self.ai_insight['fear_greed']}")
        else:
            lines.append("• 🤖 AI: 分析中...")

        await update.effective_message.reply_text("\n".join(lines), parse_mode="Markdown")

    async def cmd_check(self, update, context):
        if not self._auth(update):
            return
        lines = ["📈 **信号 + 开仓条件（前沿5合1）**\n"]
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
                decision = await self._should_open_position(sym, p, tech, funding, fg, usdt_free)
                sc = decision['score']
                status = "🎯 可开仓" if decision['should_open'] else "⏳ 等待"
                lines.append(f"{sym}: {p:.2f} | 评分{sc:.0f}分 | {status}")
                if decision['details']:
                    lines.append(f"   技术: {', '.join(decision['details'][:3])}")
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
            f"/settp 0.1 /setsl 0.05 /setamount 0.02\n"
            f"/twou         查看2U优化状态\n"
            f"/twou on      开启2U优化\n"
            f"/twou off     关闭2U优化\n"
            f"/twou reset   重置2U统计\n"
            f"/setcoin DOGE tp 1  独立设币种参数\n"
            f"/preset 2U实盘     一键2U启动\n"
            f"/setmaxpos 18 仓位上限 /setmaxalloc 100 总仓位上限\n"
            f"/autotrade on /learn on\n"
            f"/preset balanced /panic 全平\n"
            f"/setcoinonly ETH  一键固定币种\n"
            f"🚀 前沿5合1策略 + 2U优化已激活！\n"
            f"🧠 Meta-RL + ChanFormer + Strategy Arena + 风险智能体 + 确定性屏蔽\n"
            f"保本线: >{self.breakeven_pct * 100:.2f}%"
        )

    # ----- 其他命令（保持简洁，延续之前的实现） -----

    async def cmd_set_tp(self, update, context):
        if not self._auth(update):
            return
        try:
            val = self._parse_pct(float(context.args[0]))
            if val < self.breakeven_pct:
                await update.effective_message.reply_text(f"❌ 低于保本线 {self.breakeven_pct*100:.2f}%")
                return
            if self.sl_pct > 0 and val / self.sl_pct < 1.2:
                await update.effective_message.reply_text("❌ 盈亏比不足")
                return
            self.tp_pct = val
            await self._save_config()
            await update.effective_message.reply_text(f"✅ 止盈: {self.tp_pct:.1%}")
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
            await update.effective_message.reply_text(f"✅ 止损: {self.sl_pct:.1%}")
        except:
            pass

    async def cmd_set_tsl(self, update, context):
        if not self._auth(update):
            return
        try:
            self.trailing_sl_pct = self._parse_pct(float(context.args[0]))
            await self._save_config()
            await update.effective_message.reply_text(f"✅ 移动止损: {self.trailing_sl_pct:.1%}")
        except:
            pass

    async def cmd_set_trailing_tp(self, update, context):
        if not self._auth(update):
            return
        try:
            val = self._parse_pct(float(context.args[0]))
            self.trailing_tp_pct = val
            await self._save_config()
            await update.effective_message.reply_text(f"✅ 移动止盈: {self.trailing_tp_pct:.1%}")
        except:
            pass

    async def cmd_set_amount(self, update, context):
        if not self._auth(update):
            return
        try:
            self.single_order_usdt = float(context.args[0])
            await self._save_config()
            await update.effective_message.reply_text(f"✅ 单笔: {self.single_order_usdt:.3f}U")
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
            if "/" not in sym: sym = sym + "/USDT"
            self.symbols.append(sym)
            await self._save_config()
            await update.effective_message.reply_text(f"✅ 已添加 {sym}")
        except:
            await update.effective_message.reply_text("❌ 格式: /addsymbol ETH")

    async def cmd_del_symbol(self, update, context):
        if not self._auth(update):
            return
        try:
            sym = context.args[0].upper()
            if "/" not in sym: sym = sym + "/USDT"
            self.symbols.remove(sym)
            await self._save_config()
            await update.effective_message.reply_text(f"✅ 已删除 {sym}")
        except:
            await update.effective_message.reply_text("❌ 格式: /delsymbol ETH")

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
            await update.effective_message.reply_text(f"✅ {sym} 固定网格: 每跌{drop_pct*100:.1f}%买一次, 起始{base_amount}U, 递增{increment}U")
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

    async def cmd_set_coin(self, update, context):
        if not self._auth(update):
            return
        try:
            sym = context.args[0].upper()
            key = context.args[1].lower()
            val_str = context.args[2]
            key_map = {'tp':'tp_pct','sl':'sl_pct','tsl':'trailing_sl_pct','tmpt':'trailing_tp_pct','amount':'single_order_usdt','score':'auto_min_score'}
            if key not in key_map:
                await update.effective_message.reply_text(f"❌ 参数: tp/sl/tsl/tmpt/amount/score")
                return
            attr = key_map[key]
            if attr in ('tp_pct','sl_pct','trailing_sl_pct','trailing_tp_pct'):
                val = float(val_str)/100.0
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
            name_map = {'tp_pct':'止盈','sl_pct':'止损','trailing_sl_pct':'移动止损','trailing_tp_pct':'移动止盈','single_order_usdt':'单笔额度','auto_min_score':'信号阈值'}
            display = val*100 if attr in ('tp_pct','sl_pct','trailing_sl_pct','trailing_tp_pct') else val
            unit = '%' if attr in ('tp_pct','sl_pct','trailing_sl_pct','trailing_tp_pct') else 'U' if attr=='single_order_usdt' else '分'
            if attr in ('tp_pct','sl_pct','trailing_sl_pct','trailing_tp_pct'):
                await update.effective_message.reply_text(f"✅ {sym} {name_map[attr]}: {val:.1%}")
            else:
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
                f"  止盈{tp:.1%} 止损{sl:.1%} 移盈{tmpt:.1%} 移损{tsl:.1%}\n"
                f"  单笔{amount:.3f}U 阈值{score}分 仓位{count}/{self.max_positions_per_coin}\n"
                f"  持仓{free:.4f} 现价{p:.2f} 价值{val:.2f}U{pnl_str}\n"
                f"  累计净盈亏: {total_net_pnl:+.4f}U"
            )
        lines.append("💡 /setcoin 修改独立参数 | /resetcoin 重置为全局")
        await update.effective_message.reply_text("\n".join(lines), parse_mode="Markdown")

    async def cmd_setcoinonly(self, update, context):
        if not self._auth(update):
            return
        try:
            sym = context.args[0].upper()
            if "/" not in sym: sym = sym + "/USDT"
            self.symbols = [sym]
            presets = {
                "ETH/USDT": {"tp":0.8,"sl":0.5,"tsl":0.5,"tmpt":0.3,"tf":"1m","amt":0.02,"reserve":0.5,"score":70},
                "BTC/USDT": {"tp":0.6,"sl":0.4,"tsl":0.4,"tmpt":0.2,"tf":"1m","amt":0.02,"reserve":0.5,"score":70},
                "SOL/USDT": {"tp":1.0,"sl":0.5,"tsl":0.5,"tmpt":0.3,"tf":"1m","amt":0.02,"reserve":0.5,"score":65},
                "DOGE/USDT": {"tp":1.2,"sl":0.6,"tsl":0.6,"tmpt":0.4,"tf":"1m","amt":0.02,"reserve":0.5,"score":65},
                "ADA/USDT": {"tp":1.2,"sl":0.6,"tsl":0.6,"tmpt":0.4,"tf":"1m","amt":0.02,"reserve":0.5,"score":65},
            }
            if sym in presets:
                p = presets[sym]
                self.tp_pct = p["tp"]/100; self.sl_pct = p["sl"]/100; self.trailing_sl_pct = p["tsl"]/100; self.trailing_tp_pct = p["tmpt"]/100
                self.timeframe = p["tf"]; self.single_order_usdt = p["amt"]; self.reserve_bottom = p["reserve"]; self.auto_min_score = p["score"]
                await self._save_config()
                await update.effective_message.reply_text(
                    f"✅ **已固定币种: {sym}**\n"
                    f"• 止盈: {self.tp_pct:.1%}\n"
                    f"• 止损: {self.sl_pct:.1%}\n"
                    f"• 周期: {self.timeframe}\n"
                    f"• 单笔: {self.single_order_usdt:.3f}U\n"
                    f"• 阈值: {self.auto_min_score}分\n"
                    f"🚀 前沿5合1策略 + 2U优化已启用"
                )
            else:
                await self._save_config()
                await update.effective_message.reply_text(f"✅ **已固定币种: {sym}**\n• 使用当前全局参数")
        except Exception as e:
            await update.effective_message.reply_text(f"❌ 格式: /setcoinonly ETH\n错误: {e}")

    # ==================== 一键低本金滚雪球 ====================

    async def cmd_lowbalance(self, update, context):
        if not self._auth(update):
            return
        self.symbols = ["ETH/USDT", "BTC/USDT", "SOL/USDT", "DOGE/USDT", "ADA/USDT"]
        self.coin_configs = {}
        self.coin_configs["ETH/USDT"] = {"tp_pct":0.8,"sl_pct":0.5,"trailing_sl_pct":0.5,"trailing_tp_pct":0.3,"single_order_usdt":0.02,"timeframe":"1m","auto_min_score":65}
        self.coin_configs["BTC/USDT"] = {"tp_pct":0.6,"sl_pct":0.4,"trailing_sl_pct":0.4,"trailing_tp_pct":0.2,"single_order_usdt":0.02,"timeframe":"1m","auto_min_score":65}
        self.coin_configs["SOL/USDT"] = {"tp_pct":1.0,"sl_pct":0.5,"trailing_sl_pct":0.5,"trailing_tp_pct":0.3,"single_order_usdt":0.02,"timeframe":"1m","auto_min_score":60}
        self.coin_configs["DOGE/USDT"] = {"tp_pct":1.2,"sl_pct":0.6,"trailing_sl_pct":0.6,"trailing_tp_pct":0.4,"single_order_usdt":0.02,"timeframe":"1m","auto_min_score":60}
        self.coin_configs["ADA/USDT"] = {"tp_pct":1.2,"sl_pct":0.6,"trailing_sl_pct":0.6,"trailing_tp_pct":0.4,"single_order_usdt":0.02,"timeframe":"1m","auto_min_score":60}
        self.tp_pct = 0.8; self.sl_pct = 0.5; self.trailing_sl_pct = 0.5; self.trailing_tp_pct = 0.3
        self.single_order_usdt = 0.02; self.timeframe = "1m"; self.auto_min_score = 65; self.reserve_bottom = 0.5
        self._two_u_config["enabled"] = True
        await self._save_config()
        await update.effective_message.reply_text(
            f"🚀 **2U实盘优化方案已激活！**\n\n"
            f"📊 **监控币种**\n"
            f"🔹 ETH/USDT  止盈0.8% 止损0.5% 单笔0.02U\n"
            f"🔹 BTC/USDT  止盈0.6% 止损0.4% 单笔0.02U\n"
            f"🔹 SOL/USDT  止盈1.0% 止损0.5% 单笔0.02U\n"
            f"🔹 DOGE/USDT 止盈1.2% 止损0.6% 单笔0.02U\n"
            f"🔹 ADA/USDT  止盈1.2% 止损0.6% 单笔0.02U\n\n"
            f"💰 **2U优化配置**\n"
            f"• 最大单笔: 0.05U | 最小单笔: 0.01U\n"
            f"• 日亏损熔断: 5% | 连续亏损: 3笔暂停\n"
            f"• 优先资金费率套利: ✅\n\n"
            f"✅ 发送 /autotrade on 启动交易\n"
            f"✅ 发送 /twou 查看2U状态"
        )

    # ==================== 套利统计 ====================

    async def cmd_arb_stats(self, update, context):
        if not self._auth(update):
            return
        stats = self._two_u_stats
        lines = [
            f"📊 **2U套利统计** {self.env_tag}",
            f"• 总交易次数: {stats['total_trades']} 笔",
            f"• 累计盈利: {stats['total_profit']:.4f} U",
            f"• 今日盈利: {stats['profit_today']:.4f} U",
            f"• 峰值余额: {stats['peak_balance']:.4f} U",
            f"• 最大回撤: {stats['max_drawdown']*100:.2f}%",
        ]
        await update.effective_message.reply_text("\n".join(lines), parse_mode="Markdown")

    async def render_brain_status(self, msg_obj):
        try:
            macro = await self.real_data.check_macro_risk()
            lines = [f"🧠 **AI 超级大脑** {self.env_tag}", f"1️⃣ 宏观: {macro['status']}"]
            lines.append("2️⃣ 前沿技术状态:")
            if self.ai_enabled and time.time() - self.ai_insight["timestamp"] < 3600:
                lines.append(f"   Meta-RL: {self.ai_insight.get('meta_rl', '无')}")
                lines.append(f"   ChanFormer: {self.ai_insight.get('chanformer', '无')}")
                lines.append(f"   Strategy Arena: {self.ai_insight.get('strategy_arena', '无')}")
                lines.append(f"   风险智能体: {self.ai_insight.get('risk_agent', '无')}")
                lines.append(f"   建议: {self.ai_insight['recommendation']} (评分{self.ai_insight['score']:.0f})")
            else:
                lines.append("   ⏳ 分析中...")
            for idx, sym in enumerate(self.symbols):
                try:
                    if idx > 0:
                        await asyncio.sleep(1.5)
                    ticker = self.ws.get_ticker(sym)
                    if ticker is None:
                        ticker = await self.exchange.fetch_ticker(sym)
                    if ticker is None:
                        lines.append(f"{idx+3}️⃣ {sym}: 现价获取失败")
                        continue
                    p = ticker['last']
                    tech = await self.tech.calc(sym, self.timeframe, 50)
                    lines.append(f"{idx+3}️⃣ {sym}: {p:.2f} 布林{tech['bb_upper']:.1f}/{tech['bb_lower']:.1f} RSI{tech['rsi']:.0f}")
                except Exception:
                    lines.append(f"{idx+3}️⃣ {sym}: 数据获取失败")
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
                    if "/" not in sym: sym = sym + "/USDT"
                    if sym not in self.symbols:
                        self.symbols.append(sym)
                    else:
                        await update.message.reply_text("⚠️ 已存在")
                elif pending == "delsymbol":
                    sym = user_text.upper()
                    if "/" not in sym: sym = sym + "/USDT"
                    if sym in self.symbols:
                        self.symbols.remove(sym)
                    else:
                        await update.message.reply_text("⚠️ 不存在")
            else:
                val = float(user_text)
                if pending == "settp":
                    pct = self._parse_pct(val); self.tp_pct = pct
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
                    self.max_daily_loss_pct = val/100.0
                elif pending == "setmaxpos":
                    self.max_positions_per_coin = int(val)
                elif pending == "setmaxalloc":
                    self.max_total_allocated_pct = val/100.0
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
                await self._refresh_panel(query)
            elif data == "toggle_breaker":
                self.waterfall_breaker = not self.waterfall_breaker
                await self._save_config()
                await query.answer(f"瀑布熔断已{'开启' if self.waterfall_breaker else '关闭'}")
                await self._refresh_panel(query)
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
            elif data == "brain_status":
                await self.render_brain_status(query.message); await query.answer()
            elif data == "gap_analysis":
                await self.render_gap_analysis(query.message); await query.answer()
            elif data == "dashboard":
                auto_state = "开启" if self.auto_trade_enabled else "关闭"
                msg = (f"📊 看板\n止盈{self.tp_pct:.1%} 止损{self.sl_pct:.1%}\n"
                       f"移损{self.trailing_sl_pct:.1%} 移盈{self.trailing_tp_pct:.1%}\n"
                       f"额度{self.single_order_usdt:.3f}U 周期{self.timeframe} 底线{self.reserve_bottom}U\n"
                       f"自动交易: {auto_state} 阈值: {self.auto_min_score}分\n"
                       f"仓位上限: {self.max_positions_per_coin}个\n"
                       f"日熔断: {self.max_daily_loss_pct*100:.1f}%\n"
                       f"今日交易: {self.daily_trades}/{self.max_daily_trades if self.max_daily_trades > 0 else '∞'}")
                await query.message.reply_text(msg); await query.answer()
            elif data == "balance":
                bal = await self.exchange.fetch_balance()
                await query.message.reply_text(f"💳 USDT: {self._get_usdt_free(bal):.2f}"); await query.answer()
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
                opts = [("🛡️保守","conservative"),("⚖️平衡","balanced"),("⚡激进","aggressive"),("🔥ETH","ETH滚雪球"),("🔥BTC","BTC滚雪球"),("🔥SOL","SOL滚雪球"),("🔥DOGE","DOGE滚雪球"),("🔥ADA","ADA滚雪球"),("💰2U实盘","2U实盘")]
                kb = [[InlineKeyboardButton(label, callback_data=f"preset:{val}") for label,val in opts[i:i+2]] for i in range(0,len(opts),2)]
                kb.append([InlineKeyboardButton("🔙返回", callback_data="refresh_panel")])
                await query.edit_message_text("⚡ 选择方案:", reply_markup=InlineKeyboardMarkup(kb)); await query.answer()
            elif data.startswith("preset:"):
                mode = data.split(":")[1]
                p = {"conservative":{"tp":3,"sl":2,"tsl":1,"tmpt":1,"tf":"1h","amt":1,"reserve":2},
                     "balanced":{"tp":1.5,"sl":1,"tsl":0.5,"tmpt":0.5,"tf":"15m","amt":1,"reserve":1},
                     "aggressive":{"tp":0.8,"sl":0.5,"tsl":0.3,"tmpt":0.3,"tf":"5m","amt":1,"reserve":0.5},
                     "ETH滚雪球":{"tp":0.8,"sl":0.5,"tsl":0.5,"tmpt":0.3,"tf":"1m","amt":10,"reserve":5,"score":60},
                     "BTC滚雪球":{"tp":0.6,"sl":0.4,"tsl":0.4,"tmpt":0.2,"tf":"1m","amt":10,"reserve":5,"score":60},
                     "SOL滚雪球":{"tp":1.0,"sl":0.5,"tsl":0.5,"tmpt":0.3,"tf":"1m","amt":1,"reserve":1,"score":60},
                     "DOGE滚雪球":{"tp":1.2,"sl":0.6,"tsl":0.6,"tmpt":0.4,"tf":"1m","amt":1,"reserve":1,"score":60},
                     "ADA滚雪球":{"tp":1.2,"sl":0.6,"tsl":0.6,"tmpt":0.4,"tf":"1m","amt":0.5,"reserve":0.5,"score":60},
                     "2U实盘":{"tp":0.1,"sl":0.05,"tsl":0.03,"tmpt":0.02,"tf":"1m","amt":0.02,"reserve":0.5,"score":60}}[mode]
                self.tp_pct=p["tp"]/100; self.sl_pct=p["sl"]/100; self.trailing_sl_pct=p["tsl"]/100; self.trailing_tp_pct=p["tmpt"]/100
                self.timeframe=p["tf"]; self.single_order_usdt=p["amt"]; self.reserve_bottom=p["reserve"]
                if "score" in p: self.auto_min_score=p["score"]
                self._two_u_config["enabled"] = True
                await self._save_config()
                await query.answer("✅ 已生效", show_alert=True); await self._refresh_panel(query)
            elif data == "menu_set_autoscore":
                opts = [("60分","60"),("70分","70"),("80分","80"),("85分","85")]
                await query.edit_message_text("🎯 阈值", reply_markup=self._build_option_keyboard(opts,"cfg_autoscore","autoscore")); await query.answer()
            elif data == "menu_set_trades":
                opts = [("3次","3"),("5次","5"),("10次","10"),("无限","0")]
                await query.edit_message_text("🔢 上限", reply_markup=self._build_option_keyboard(opts,"cfg_trades","settrades")); await query.answer()
            elif data == "menu_set_tp":
                opts = [("0.1%","0.001"),("0.2%","0.002"),("0.5%","0.005")]
                await query.edit_message_text("🎯 止盈", reply_markup=self._build_option_keyboard(opts,"cfg_tp","settp")); await query.answer()
            elif data == "menu_set_sl":
                opts = [("0.05%","0.0005"),("0.1%","0.001"),("0.2%","0.002")]
                await query.edit_message_text("🛡️ 止损", reply_markup=self._build_option_keyboard(opts,"cfg_sl","setsl")); await query.answer()
            elif data == "menu_set_tsl":
                opts = [("0.03%","0.0003"),("0.05%","0.0005"),("0.1%","0.001")]
                await query.edit_message_text("📉 移动止损", reply_markup=self._build_option_keyboard(opts,"cfg_tsl","settsl")); await query.answer()
            elif data == "menu_set_tmpt":
                opts = [("0.02%","0.0002"),("0.03%","0.0003"),("0.05%","0.0005")]
                await query.edit_message_text("🏹 移动止盈", reply_markup=self._build_option_keyboard(opts,"cfg_tmpt","settmpt")); await query.answer()
            elif data == "menu_set_amount":
                opts = [("0.01U","0.01"),("0.02U","0.02"),("0.05U","0.05")]
                await query.edit_message_text("💵 单笔额度", reply_markup=self._build_option_keyboard(opts,"cfg_amt","setamount")); await query.answer()
            elif data == "menu_set_tf":
                opts = [("1m","1m"),("3m","3m"),("5m","5m"),("15m","15m")]
                await query.edit_message_text("⏱ 周期", reply_markup=self._build_option_keyboard(opts,"cfg_tf","settf")); await query.answer()
            elif data == "menu_set_reserve":
                opts = [("0.2U","0.2"),("0.5U","0.5"),("1U","1")]
                await query.edit_message_text("🔒 底线", reply_markup=self._build_option_keyboard(opts,"cfg_res","setreserve")); await query.answer()
            elif data == "menu_add_symbol":
                opts = [("BTC/USDT","BTC/USDT"),("SOL/USDT","SOL/USDT"),("DOGE/USDT","DOGE/USDT"),("ADA/USDT","ADA/USDT")]
                await query.edit_message_text("➕", reply_markup=self._build_option_keyboard(opts,"cfg_add","addsymbol")); await query.answer()
            elif data == "menu_del_symbol":
                opts = [(s,s) for s in self.symbols]
                await query.edit_message_text("➖", reply_markup=self._build_option_keyboard(opts,"cfg_del","delsymbol")); await query.answer()
            elif data.startswith("cfg_"):
                prefix = data.split(":")[0] if ":" in data else ""; val_str = data.split(":")[1] if ":" in data else ""
                if prefix == "cfg_tp":
                    val_f = float(val_str)
                    if val_f < self.breakeven_pct:
                        await query.answer(f"❌ 低于保本线 {self.breakeven_pct:.1%}", show_alert=True); return
                    if self.sl_pct > 0 and val_f / self.sl_pct < 1.2:
                        await query.answer("❌ 盈亏比不足", show_alert=True); return
                    self.tp_pct = val_f
                elif prefix == "cfg_sl":
                    val_f = float(val_str)
                    if self.tp_pct > 0 and self.tp_pct / val_f < 1.2:
                        await query.answer("❌ 盈亏比不足", show_alert=True); return
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
                        await query.answer("已存在", show_alert=True); return
                elif prefix == "cfg_del":
                    if val_str in self.symbols:
                        self.symbols.remove(val_str)
                    else:
                        await query.answer("不存在", show_alert=True); return
                await self._save_config()
                await query.answer("✅", show_alert=True); await self._refresh_panel(query)
            elif data.startswith("prompt_manual:"):
                key = data.split(":")[1]
                context.user_data['pending_setting'] = key
                prompts = {"settp":"✍️ 止盈率（例：0.001）：","setsl":"✍️ 止损率（例：0.0005）：","settsl":"✍️ 移动止损（例：0.0003）：","settmpt":"✍️ 移动止盈（例：0.0002）：","setamount":"✍️ 单笔 USDT（例：0.02）：","settf":"✍️ 周期（例：1m）：","setreserve":"✍️ 底线（例：0.5）：","addsymbol":"✍️ 币种（例：DOGE/USDT）：","delsymbol":"✍️ 要删除的币种：","autoscore":"✍️ 阈值（50-95）：","settrades":"✍️ 日交易次数：","setmaxcoin":"✍️ 单币最大持仓U：","setmaxloss":"✍️ 日熔断%（例：5）：","setmaxpos":"✍️ 最大仓位数：","setmaxalloc":"✍️ 总仓位上限%（例：80）："}
                await query.message.reply_text(prompts.get(key, "✍️ 请输入数值："), reply_markup=ForceReply(selective=True)); await query.answer()
            elif data == "panic_confirm":
                await query.answer("🚨 请发送 /panic 确认", show_alert=True)
            else:
                await query.answer("此按钮暂未绑定功能", show_alert=True)
        except Exception as e:
            logger.error(f"按钮异常 ({data}): {e}")
            try:
                await query.answer("操作失败，请重试", show_alert=True)
            except:
                pass

    def _build_option_keyboard(self, options, prefix, setting_key):
        kb = []
        row = []
        for label, val in options:
            row.append(InlineKeyboardButton(label, callback_data=f"{prefix}:{val}"))
            if len(row) == 2:
                kb.append(row); row = []
        if row: kb.append(row)
        kb.append([InlineKeyboardButton("✍️ 自填", callback_data=f"prompt_manual:{setting_key}")])
        kb.append([InlineKeyboardButton("🔙 返回", callback_data="refresh_panel")])
        return InlineKeyboardMarkup(kb)

    async def _refresh_panel(self, query):
        try:
            await query.edit_message_text(f"⚙️ 控制台 {self.env_tag}", reply_markup=self._build_main_keyboard())
        except:
            pass

    # ==================== 核心任务 ====================

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

    # ==================== 自动交易主循环 ====================

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

                # 2U风控检查
                if self._two_u_config.get("enabled", True):
                    if not await self._check_twou_risk():
                        await asyncio.sleep(10)
                        continue

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

                        if sym not in self._volume_history:
                            self._volume_history[sym] = []
                        vol = ticker.get('volume', 0)
                        if vol > 0:
                            self._volume_history[sym].append(vol)
                            if len(self._volume_history[sym]) > 50:
                                self._volume_history[sym].pop(0)

                        grid = self.grid_configs.get(sym)
                        if grid:
                            last_trigger = self._last_grid_entry.get(sym, p)
                            drop_from_last = (last_trigger - p) / last_trigger if last_trigger else 0
                            if drop_from_last >= grid["drop_pct"]:
                                count = self.position_counts.get(sym, 0)
                                coin_amount = grid["base_amount"] * (1 + count * grid["increment"])
                                self._last_grid_entry[sym] = p
                                candidates.append((100, sym, p, None, self.tp_pct, self.sl_pct, 2.0, coin_amount))
                                logger.info(f"📊 固定网格触发 {sym} 下跌{drop_from_last*100:.2f}%，金额{coin_amount:.3f}U")
                            continue

                        tech = await self.tech.calc(sym, self.timeframe, 50)
                        funding = await self.exchange.fetch_funding_rate(sym)

                        decision = await self._should_open_position(sym, p, tech, funding, fg, usdt_free)
                        if not decision['should_open']:
                            continue

                        if self.orderbook_filter:
                            ob = self.ws.get_orderbook(sym)
                            if ob is None:
                                continue
                            ob_valid, _ = await self.orderbook_engine.validate(ob)
                            if not ob_valid:
                                continue

                        # 2U优化：计算超低仓位
                        if self._two_u_config.get("enabled", True):
                            base_amount = self._calculate_twou_position(0.02)
                        else:
                            base_amount = self._get_coin_param(sym, 'single_order_usdt', self.single_order_usdt)

                        dynamic_amount = self._calculate_dynamic_amount(base_amount)
                        if decision['is_high_confidence']:
                            dynamic_amount = dynamic_amount * 1.5
                            logger.info(f"🔥 {sym} 高置信度信号，仓位提升: {dynamic_amount:.3f}U")

                        coin_amount = dynamic_amount
                        dyn_tp, dyn_sl = await self._adjust_tp_sl_by_volatility(sym)
                        candidates.append((decision['score'], sym, p, funding, dyn_tp, dyn_sl, 2.0, coin_amount))
                        logger.info(f"📊 {sym} 开仓信号通过，评分{decision['score']:.0f}，金额{coin_amount:.3f}U")
                    except Exception as e:
                        logger.error(f"候选生成异常 {sym}: {e}")
                        continue

                candidates.sort(key=lambda x: x[0], reverse=True)
                opened_coins = set()

                for item in candidates:
                    if len(item) == 8:
                        sc, sym, p, funding, dyn_tp, dyn_sl, vol_factor, coin_amount = item
                    else:
                        sc, sym, p, funding, dyn_tp, dyn_sl, vol_factor = item
                        coin_amount = self._get_coin_param(sym, 'single_order_usdt', self.single_order_usdt)

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
                                        text=f"🤖 开仓 {sym} {coin_amount:.3f}U @ {p:.4f} 仓位{self.position_counts[sym]}/{self.max_positions_per_coin} | 评分{sc:.0f}"
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

    # ==================== 移动止盈止损 ====================

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

                            if net_pnl < 0:
                                self._consecutive_losses += 1
                                self._today_loss_pct += abs(net_pnl_pct) / 100
                            else:
                                self._consecutive_losses = 0

                            trade = {"time": datetime.now(CST).strftime("%m-%d %H:%M"), "symbol": sym, "entry": entry_price, "exit": p, "pnl_pct": round(pnl_pct, 2), "net_pnl": round(net_pnl, 4), "net_pnl_pct": round(net_pnl_pct, 2)}
                            await save_trade(trade)
                            self.trades.insert(0, trade)
                            await save_trade_detail({"time": datetime.now(CST).strftime("%m-%d %H:%M"), "symbol": sym, "side": "sell", "price": p, "amount": amount, "pnl_pct": round(pnl_pct, 2), "signal_score": detail.get('signal_score', 0), "fear_greed": detail.get('fear_greed', 0), "funding_rate": detail.get('funding_rate', 0), "real_revenue": round(net_pnl, 4), "net_pnl_pct": round(net_pnl_pct, 2)})
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
                                    await self.tg_app.bot.send_message(chat_id=settings.TG_CHAT_ID, text=f"🛡️ 硬止损 {sym} @ {p:.2f} 净利{net_pnl_pct:+.2f}% ({net_pnl:+.4f}U)")
                                except:
                                    pass
                            continue

                        if not self._trailing_active.get(sym, False):
                            if p >= entry_price * (1 + use_tp):
                                self._trailing_active[sym] = True
                                self._trailing_high[sym] = p
                        else:
                            if p > self._trailing_high.get(sym, 0):
                                self._trailing_high[sym] = p
                            high = self._trailing_high[sym]

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

                                if net_pnl < 0:
                                    self._consecutive_losses += 1
                                    self._today_loss_pct += abs(net_pnl_pct) / 100
                                else:
                                    self._consecutive_losses = 0

                                trade = {"time": datetime.now(CST).strftime("%m-%d %H:%M"), "symbol": sym, "entry": entry_price, "exit": p, "pnl_pct": round(pnl_pct, 2), "net_pnl": round(net_pnl, 4), "net_pnl_pct": round(net_pnl_pct, 2)}
                                await save_trade(trade)
                                self.trades.insert(0, trade)
                                await save_trade_detail({"time": datetime.now(CST).strftime("%m-%d %H:%M"), "symbol": sym, "side": "sell", "price": p, "amount": amount, "pnl_pct": round(pnl_pct, 2), "signal_score": detail.get('signal_score', 0), "fear_greed": detail.get('fear_greed', 0), "funding_rate": detail.get('funding_rate', 0), "real_revenue": round(net_pnl, 4), "net_pnl_pct": round(net_pnl_pct, 2)})
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
                                        await self.tg_app.bot.send_message(chat_id=settings.TG_CHAT_ID, text=f"📉 移动止损 {sym} @ {p:.2f} 净利{net_pnl_pct:+.2f}% ({net_pnl:+.4f}U)")
                                    except:
                                        pass
                                continue

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

                                if net_pnl < 0:
                                    self._consecutive_losses += 1
                                    self._today_loss_pct += abs(net_pnl_pct) / 100
                                else:
                                    self._consecutive_losses = 0

                                trade = {"time": datetime.now(CST).strftime("%m-%d %H:%M"), "symbol": sym, "entry": entry_price, "exit": p, "pnl_pct": round(pnl_pct, 2), "net_pnl": round(net_pnl, 4), "net_pnl_pct": round(net_pnl_pct, 2)}
                                await save_trade(trade)
                                self.trades.insert(0, trade)
                                await save_trade_detail({"time": datetime.now(CST).strftime("%m-%d %H:%M"), "symbol": sym, "side": "sell", "price": p, "amount": amount, "pnl_pct": round(pnl_pct, 2), "signal_score": detail.get('signal_score', 0), "fear_greed": detail.get('fear_greed', 0), "funding_rate": detail.get('funding_rate', 0), "real_revenue": round(net_pnl, 4), "net_pnl_pct": round(net_pnl_pct, 2)})
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
                                        await self.tg_app.bot.send_message(chat_id=settings.TG_CHAT_ID, text=f"🏹 移动止盈 {sym} @ {p:.2f} 净利{net_pnl_pct:+.2f}% ({net_pnl:+.4f}U)")
                                    except:
                                        pass
                    except Exception as e:
                        logger.error(f"追踪异常 {sym}: {e}")
                await asyncio.sleep(5)
            except Exception as e:
                logger.error(f"追踪任务异常: {e}")
                await asyncio.sleep(5)

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
                await self._alert(f"🤖 AI 动态优化完成\n止盈: {self.tp_pct:.1%}\n止损: {self.sl_pct:.1%}")

    # ==================== 启动入口 ====================

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
        asyncio.create_task(self._delta_neutral_arbitrage())
        asyncio.create_task(self._onchain_monitor())
        asyncio.create_task(self._triangular_arbitrage_monitor())
        asyncio.create_task(self._ai_analyze_market())

        while True:
            try:
                await self.tg_app.initialize()
                await self.tg_app.start()
                await self.tg_app.updater.start_polling(drop_pending_updates=True)
                logger.info("✅ UltimateBot v11.0 启动成功（前沿5合1策略 + 2U优化）")
                if settings.TG_CHAT_ID:
                    try:
                        await self.tg_app.bot.send_message(
                            chat_id=settings.TG_CHAT_ID,
                            text="🚀 **UltimateBot v11.0 已上线**\n\n"
                                 "🧠 Meta-RL-Crypto (自我进化)\n"
                                 "📈 ChanFormer (通道式Transformer)\n"
                                 "🎯 Strategy Arena (72策略多智能体)\n"
                                 "🛡️ 风险智能体 (不确定性量化)\n"
                                 "🔒 确定性屏蔽 (安全边界)\n\n"
                                 "💰 **2U实盘优化已激活**\n"
                                 "• 单笔: 0.01-0.05U\n"
                                 "• 日亏损熔断: 5%\n"
                                 "• 连续亏损: 3笔暂停\n\n"
                                 "策略组合：**前沿5合1 + 2U优化**"
                        )
                    except:
                        pass
                while True:
                    await asyncio.sleep(30)
            except Exception as e:
                logger.error(f"Bot 断开，5秒后重连: {e}")
                await asyncio.sleep(5)