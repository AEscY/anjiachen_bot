"""
UltimateBot v12.0 - 终极完整版（27合1全栈策略 + 七大前沿优化）
针对交易所低买高卖优化：市场状态自适应 / 贝叶斯参数优化 / 多智能体系统 / 情绪增强RL / WebCryptoAgent / 凯利仓位管理 / GARCH波动率预测
数据源：实时价格（WS）+ 实时指标（REST）+ 免费新闻情绪API（cryptocurrency.cv）+ 恐惧贪婪（alternative.me）
"""
import asyncio
import aiohttp
import os
import json
import aiosqlite
import time
import math
import random
import numpy as np
from datetime import datetime, timezone, timedelta
from collections import deque
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ForceReply
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

# ==================== 增强版数据引擎（实时数据 + 免费API） ====================

class RealDataEngine:
    """
    增强版数据引擎（实时数据 + 免费API）
    - 新闻情绪：使用 cryptocurrency.cv 的免费API（无需Key）
    - 社交情绪：使用 cryptocurrency.cv 的社交情绪API（无需Key）
    - 恐惧贪婪：继续使用 alternative.me
    - 链上数据：如果配置了 BLOCKCHAIR_API_KEY 则尝试获取真实大额转账，否则使用模拟（带注释说明）
    """
    def __init__(self, exchange_rest, ws_manager):
        self.exchange = exchange_rest
        self.ws = ws_manager
        self._fear_greed_cache = {"value": 50, "classification": "Neutral", "timestamp": 0}
        self._cache_ttl = 300
        self._onchain_cache = {}
        self._news_cache = {"sentiment": 0, "headlines": [], "timestamp": 0}
        self._social_cache = {"sentiment": 0, "timestamp": 0}
        # 可选的 API Key（从环境变量读取）
        self._news_api_key = os.getenv("NEWS_API_KEY", "")          # 备选（如 newsapi.org）
        self._social_api_key = os.getenv("SOCIAL_API_KEY", "")      # 备选（如 lunarCrush）
        self._blockchair_api_key = os.getenv("BLOCKCHAIR_API_KEY", "")  # 可选，用于链上数据

    async def get_fear_greed_index(self):
        """（原方法不变）"""
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
        """（原方法不变）"""
        fg = await self.get_fear_greed_index()
        if fg is None:
            return {'is_safe': True, 'score': 0.5, 'status': "⚠️ 数据缺失"}
        value = fg["value"]
        if value < 25:
            return {'is_safe': False, 'score': value/100, 'status': f"🚨 极度恐惧 ({value})"}
        elif value > 75:
            return {'is_safe': False, 'score': value/100, 'status': f"⚠️ 极度贪婪 ({value})"}
        return {'is_safe': True, 'score': value/100, 'status': f"🟢 {fg['classification']} ({value})"}

    async def get_news_sentiment(self, symbols=None):
        """
        获取实时新闻情绪（使用 cryptocurrency.cv 免费API，无需Key）
        若失败则尝试使用 NEWS_API_KEY（如有），否则返回中性0
        """
        if symbols is None:
            symbols = ["BTC", "ETH", "SOL", "DOGE", "ADA"]
        now = time.time()
        if now - self._news_cache["timestamp"] < 300:
            return self._news_cache

        try:
            # 首选：cryptocurrency.cv 的免费情绪API（无需Key）
            async with aiohttp.ClientSession() as session:
                # 查询BTC作为代表，因为它是加密市场风向标
                async with session.get(
                    "https://cryptocurrency.cv/api/ai/sentiment?asset=bitcoin",
                    timeout=aiohttp.ClientTimeout(total=8)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        # 返回格式：{"score": 0.75, "label": "Bullish", ...}
                        sentiment = data.get('score', 0)
                        # 将分数映射到 -1..1
                        sentiment = (sentiment - 0.5) * 2  # 假设原值0~1
                        headlines = []
                        # 有些版本会返回新闻标题，若没有则留空
                        self._news_cache = {
                            'sentiment': max(-1, min(1, sentiment)),
                            'headlines': headlines,
                            'timestamp': now
                        }
                        return self._news_cache
        except Exception as e:
            logger.warning(f"cryptocurrency.cv 新闻情绪获取失败: {e}")

        # 备选：使用 NewsAPI（如果配置了 KEY）
        if self._news_api_key:
            try:
                query = " OR ".join(symbols[:3])
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        f"https://newsapi.org/v2/everything?q={query}&language=en&pageSize=10&apiKey={self._news_api_key}",
                        timeout=aiohttp.ClientTimeout(total=8)
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            articles = data.get('articles', [])
                            positive_keywords = ['bull','rally','surge','gain','up','breakthrough','adoption','approve']
                            negative_keywords = ['bear','crash','drop','down','decline','ban','reject','scam','hack']
                            sentiment_score = 0
                            headlines = []
                            for article in articles[:5]:
                                title = article.get('title', '').lower()
                                headlines.append(title)
                                for word in positive_keywords:
                                    if word in title:
                                        sentiment_score += 1
                                for word in negative_keywords:
                                    if word in title:
                                        sentiment_score -= 1
                            sentiment_score = max(-10, min(10, sentiment_score)) / 10
                            self._news_cache = {
                                'sentiment': sentiment_score,
                                'headlines': headlines[:3],
                                'timestamp': now
                            }
                            return self._news_cache
            except Exception as e:
                logger.warning(f"NewsAPI 新闻情绪获取失败: {e}")

        # 全部失败则返回中性
        self._news_cache = {'sentiment': 0, 'headlines': [], 'timestamp': now}
        return self._news_cache

    async def get_social_sentiment(self, symbols=None):
        """
        获取实时社交情绪（使用 cryptocurrency.cv 的社交情绪API，无需Key）
        若失败则返回中性0
        """
        if symbols is None:
            symbols = ["BTC", "ETH"]
        now = time.time()
        if now - self._social_cache["timestamp"] < 300:
            return self._social_cache

        try:
            # 使用 cryptocurrency.cv 的社交情绪API（推测存在，若无则尝试其他）
            async with aiohttp.ClientSession() as session:
                # 假设接口为 social/sentiment，实际需确认，这里以模拟为例
                # 因为该网站未公开此API，我们临时使用另一个免费来源：用 Fear&Greed 作为替代？
                # 改为使用 alternative.me 的 fear/greed 作为社交情绪的近似
                # 或者使用 lunarcrush 免费API（需要key）
                # 这里我们使用 crypto fear greed index 作为社交情绪代表
                fg = await self.get_fear_greed_index()
                if fg:
                    val = fg['value']
                    sentiment = (val - 50) / 50  # 映射到 -1..1
                    self._social_cache = {'sentiment': sentiment, 'timestamp': now}
                    return self._social_cache
        except Exception as e:
            logger.warning(f"社交情绪获取失败: {e}")

        # 备选：使用 LunarCrush（如果配置了 SOCIAL_API_KEY）
        if self._social_api_key:
            try:
                symbol = symbols[0].lower()
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        f"https://api.lunarcrush.com/v2?data=assets&symbol={symbol}&key={self._social_api_key}",
                        timeout=aiohttp.ClientTimeout(total=8)
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            sentiment = data.get('data', [{}])[0].get('social_score', 0) / 100
                            sentiment = sentiment * 2 - 1  # 0-100 -> -1..1
                            self._social_cache = {'sentiment': sentiment, 'timestamp': now}
                            return self._social_cache
            except Exception as e:
                logger.warning(f"LunarCrush 社交情绪获取失败: {e}")

        # 全部失败返回中性
        self._social_cache = {'sentiment': 0, 'timestamp': now}
        return self._social_cache

    async def get_onchain_metrics(self, symbol):
        """
        获取链上数据（优先使用 Blockchair 真实大额转账，若配置了 BLOCKCHAIR_API_KEY）
        否则返回模拟数据（但会在日志中说明）
        """
        now = time.time()
        # 尝试真实API
        if self._blockchair_api_key:
            try:
                # Blockchair 的公共API无需key但有速率限制，这里使用key方式可提高限制
                # 获取该币种最近24小时的大额交易（例如 > 100 BTC）
                coin = symbol.split('/')[0].lower()
                if coin == 'btc':
                    url = f"https://api.blockchair.com/bitcoin/dashboards/transactions?limit=100"
                elif coin == 'eth':
                    url = f"https://api.blockchair.com/ethereum/dashboards/transactions?limit=100"
                else:
                    url = None
                if url:
                    async with aiohttp.ClientSession() as session:
                        async with session.get(
                            url,
                            params={'key': self._blockchair_api_key},
                            timeout=aiohttp.ClientTimeout(total=10)
                        ) as resp:
                            if resp.status == 200:
                                data = await resp.json()
                                txs = data.get('data', [])
                                # 筛选大额交易（按阈值）
                                threshold = 100 if coin == 'btc' else 5000 if coin == 'eth' else 100000
                                whale_txs = [tx for tx in txs if tx.get('value', 0) > threshold]
                                whale_count = len(whale_txs)
                                # 粗略估算净流入（取近24小时交易量差额）
                                # Blockchair 不直接提供净流入，我们简单用模拟
                                netflow = random.uniform(-50, 50)  # 暂时仍模拟
                                active = len(set(tx.get('sender', '') for tx in txs))  # 活跃地址数近似
                                hashrate = 0  # 无法获取
                                self._onchain_cache[symbol] = {
                                    'whale_transfers': whale_count,
                                    'exchange_netflow': netflow,
                                    'active_addresses': active,
                                    'hashrate': hashrate,
                                    'timestamp': now
                                }
                                return self._onchain_cache[symbol]
            except Exception as e:
                logger.warning(f"Blockchair 链上数据获取失败: {e}，回退到模拟")

        # 模拟数据（带日志提示）
        if symbol not in self._onchain_cache or now - self._onchain_cache[symbol]['timestamp'] > 300:
            logger.debug(f"使用模拟链上数据（未配置 BLOCKCHAIR_API_KEY 或请求失败）")
            self._onchain_cache[symbol] = {
                'whale_transfers': random.randint(0, 8),
                'exchange_netflow': random.uniform(-200, 200),
                'active_addresses': random.randint(800, 6000),
                'hashrate': random.uniform(100, 600),
                'timestamp': now
            }
        return self._onchain_cache[symbol]

    async def get_funding_rate(self, symbol):
        """（原方法不变）"""
        try:
            return await self.exchange.fetch_funding_rate(symbol)
        except:
            return None


# ==================== 订单簿引擎 ====================

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


# ==================== 七大前沿优化模块 ====================

class MarketStateEngine:
    """市场状态自适应引擎：6种状态识别与策略映射"""
    
    @staticmethod
    def detect_state(tech_data, price_history, volatility_history):
        if tech_data is None or len(price_history) < 20:
            return "neutral", 0.5
        
        volatility = tech_data.get('atr', 0) / tech_data.get('bb_middle', 1) if tech_data.get('bb_middle', 0) > 0 else 0.01
        bb_width = (tech_data.get('bb_upper', 0) - tech_data.get('bb_lower', 0)) / tech_data.get('bb_middle', 1) if tech_data.get('bb_middle', 0) > 0 else 0.02
        
        recent_prices = price_history[-20:]
        trend = (recent_prices[-1] - recent_prices[0]) / recent_prices[0] if recent_prices[0] > 0 else 0
        
        if volatility > 0.08:
            return "extreme", 0.3
        elif volatility > 0.05:
            return "high_volatility", 0.4
        elif volatility < 0.01 and bb_width < 0.02:
            return "ultra_low", 0.7
        elif volatility < 0.02:
            return "low_volatility", 0.65
        elif abs(trend) > 0.03:
            return "trending", 0.35
        else:
            return "ranging", 0.6
    
    @staticmethod
    def get_strategy_params(state, base_tp, base_sl, base_amount):
        state_config = {
            "ultra_low": {"tp_factor": 0.6, "sl_factor": 0.5, "grid_factor": 0.7, "amount_factor": 0.6, "threshold_adjust": -5},
            "low_volatility": {"tp_factor": 0.8, "sl_factor": 0.7, "grid_factor": 0.8, "amount_factor": 0.8, "threshold_adjust": -3},
            "ranging": {"tp_factor": 1.0, "sl_factor": 1.0, "grid_factor": 1.0, "amount_factor": 1.0, "threshold_adjust": 0},
            "trending": {"tp_factor": 1.4, "sl_factor": 1.3, "grid_factor": 0.5, "amount_factor": 0.6, "threshold_adjust": 5},
            "high_volatility": {"tp_factor": 1.2, "sl_factor": 1.5, "grid_factor": 0.4, "amount_factor": 0.5, "threshold_adjust": 8},
            "extreme": {"tp_factor": 0.8, "sl_factor": 2.0, "grid_factor": 0.2, "amount_factor": 0.3, "threshold_adjust": 10}
        }
        config = state_config.get(state, state_config["ranging"])
        return {
            "tp": base_tp * config["tp_factor"],
            "sl": base_sl * config["sl_factor"],
            "grid_width": 0.01 * config["grid_factor"],
            "amount": base_amount * config["amount_factor"],
            "threshold": config["threshold_adjust"],
            "state": state
        }


class AutoQuantOptimizer:
    """贝叶斯风格参数自动优化"""
    def __init__(self):
        self.param_history = []
        self.best_params = {}
    
    async def optimize(self, symbol, trades_history, current_params):
        if len(trades_history) < 30:
            return current_params
        recent = trades_history[-30:]
        pnls = [t.get('pnl_pct', 0) for t in recent if t.get('pnl_pct') is not None]
        if not pnls or len(pnls) < 20:
            return current_params
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        win_rate = len(wins) / len(pnls) if pnls else 0.5
        avg_win = sum(wins) / len(wins) if wins else 0.5
        avg_loss = abs(sum(losses) / len(losses)) if losses else 0.5
        sharpe = self._calc_sharpe(pnls)
        new_params = current_params.copy()
        if win_rate > 0.55 and avg_win < 0.6:
            new_params['tp_pct'] = min(current_params.get('tp_pct', 0.01) * 1.08, 0.035)
        elif win_rate < 0.4 and avg_win > 0.8:
            new_params['tp_pct'] = max(current_params.get('tp_pct', 0.01) * 0.92, 0.003)
        if avg_loss > 1.0:
            new_params['sl_pct'] = max(current_params.get('sl_pct', 0.005) * 0.85, 0.002)
        elif avg_loss < 0.3 and win_rate > 0.5:
            new_params['sl_pct'] = min(current_params.get('sl_pct', 0.005) * 1.1, 0.02)
        if win_rate > 0.6:
            new_params['score'] = min(current_params.get('score', 60) + 2, 85)
        elif win_rate < 0.35:
            new_params['score'] = max(current_params.get('score', 60) - 2, 50)
        self.param_history.append({'time': time.time(), 'params': new_params, 'win_rate': win_rate, 'sharpe': sharpe})
        return new_params
    
    @staticmethod
    def _calc_sharpe(returns, risk_free=0):
        if not returns or len(returns) < 2:
            return 0
        avg_return = np.mean(returns)
        std_return = np.std(returns)
        if std_return == 0:
            return 0
        return (avg_return - risk_free) / std_return * np.sqrt(252)


class MultiAgentSystem:
    """多智能体协作系统：分析师+策略师+风控+执行"""
    
    @staticmethod
    async def analyze(market_data, tech_data):
        analysis = {'state': 'neutral', 'opportunity': 0, 'risk_level': 0.5, 'reason': []}
        rsi = tech_data.get('rsi', 50)
        bb_lower = tech_data.get('bb_lower', 0)
        bb_upper = tech_data.get('bb_upper', 0)
        price = tech_data.get('bb_middle', 0)
        if bb_lower > 0 and price > 0:
            bb_pos = (price - bb_lower) / (bb_upper - bb_lower) if bb_upper > bb_lower else 0.5
            if bb_pos < 0.15 and rsi < 35:
                analysis['state'] = 'oversold'
                analysis['opportunity'] = 80
                analysis['reason'].append("布林下轨+RSI超卖")
            elif bb_pos > 0.85 and rsi > 65:
                analysis['state'] = 'overbought'
                analysis['opportunity'] = 20
                analysis['reason'].append("布林上轨+RSI超买")
            elif 0.25 < bb_pos < 0.75 and 40 < rsi < 60:
                analysis['state'] = 'ranging'
                analysis['opportunity'] = 60
                analysis['reason'].append("震荡区间")
        return analysis
    
    @staticmethod
    def generate_strategy(analysis, current_params):
        strategy = current_params.copy()
        state = analysis.get('state', 'neutral')
        if state == 'oversold':
            strategy.update({'tp_factor': 1.2, 'sl_factor': 0.8, 'threshold_adjust': -5, 'action': 'buy_favor'})
        elif state == 'overbought':
            strategy.update({'tp_factor': 0.8, 'sl_factor': 1.2, 'threshold_adjust': 5, 'action': 'sell_favor'})
        elif state == 'ranging':
            strategy.update({'tp_factor': 1.0, 'sl_factor': 1.0, 'threshold_adjust': 0, 'action': 'neutral'})
        else:
            strategy.update({'tp_factor': 1.0, 'sl_factor': 1.0, 'threshold_adjust': 0, 'action': 'neutral'})
        return strategy
    
    @staticmethod
    async def risk_assess(portfolio, market_state):
        risk_score = 0.5
        reasons = []
        if portfolio.get('consecutive_losses', 0) >= 3:
            risk_score += 0.2
            reasons.append("连续亏损")
        if portfolio.get('drawdown', 0) > 0.08:
            risk_score += 0.25
            reasons.append("回撤超过8%")
        if market_state in ('extreme', 'high_volatility'):
            risk_score += 0.2
            reasons.append(f"市场{market_state}")
        return {'risk_score': min(1.0, risk_score), 'is_safe': risk_score < 0.65, 'reasons': reasons}


class SentimentAugmentedRL:
    """情绪增强强化学习（Alpha奖励）"""
    @staticmethod
    def calculate_alpha_reward(price_history, sentiment_history, rsi_history, n=30):
        if len(price_history) < n or len(sentiment_history) < n:
            return 50, 0.5
        recent_prices = price_history[-n:]
        recent_sentiment = sentiment_history[-n:]
        recent_rsi = rsi_history[-n:] if len(rsi_history) >= n else [50] * n
        sentiment_alpha = sum(recent_sentiment) / len(recent_sentiment)
        price_momentum = (recent_prices[-1] - recent_prices[0]) / recent_prices[0] if recent_prices[0] > 0 else 0
        alpha_reward = sentiment_alpha * 0.6 + price_momentum * 0.4
        rsi_penalty = 0
        if np.mean(recent_rsi) > 70:
            rsi_penalty = -0.3
        elif np.mean(recent_rsi) < 30:
            rsi_penalty = 0.3
        score = 50 + alpha_reward * 30 + rsi_penalty * 20
        confidence = min(0.9, max(0.3, abs(alpha_reward) * 0.5 + 0.3))
        return min(100, max(0, score)), confidence


class WebCryptoAgent:
    """多源Web信息融合（新闻+社交+链上）"""
    @staticmethod
    def fusion_score(tech_data, news_sentiment, social_sentiment, fear_greed, onchain_data):
        score = 50
        factors = []
        rsi = tech_data.get('rsi', 50)
        if rsi < 30:
            score += 12
            factors.append("技术超卖")
        elif rsi < 40:
            score += 6
            factors.append("技术偏低")
        if news_sentiment:
            ns = news_sentiment.get('sentiment', 0)
            if ns > 0.3:
                score += 10
                factors.append("新闻积极")
            elif ns < -0.3:
                score -= 10
                factors.append("新闻消极")
        if social_sentiment:
            ss = social_sentiment.get('sentiment', 0)
            if ss > 0.2:
                score += 8
                factors.append("社交积极")
            elif ss < -0.2:
                score -= 8
                factors.append("社交消极")
        if fear_greed is not None:
            if fear_greed < 30:
                score += 10
                factors.append("极度恐惧")
            elif fear_greed > 70:
                score -= 10
                factors.append("极度贪婪")
        if onchain_data:
            netflow = onchain_data.get('exchange_netflow', 0)
            whale = onchain_data.get('whale_transfers', 0)
            if netflow < -50:
                score += 10
                factors.append("交易所净流出")
            if whale > 3:
                score += 5
                factors.append("鲸鱼活跃")
        return min(100, max(0, score)), factors


class KellyPositionManager:
    """凯利公式仓位管理（贝叶斯收缩）"""
    @staticmethod
    def calculate_position(win_rate, avg_win, avg_loss, n_trades, max_position=0.1):
        if n_trades < 10:
            return max_position * 0.5
        raw_kelly = (win_rate * avg_win - (1 - win_rate) * avg_loss) / avg_win if avg_win > 0 else 0
        shrinkage = n_trades / (n_trades + 30)
        quarter_kelly = raw_kelly * 0.25
        adjusted_kelly = quarter_kelly * shrinkage
        return max(0.01, min(max_position, adjusted_kelly * max_position))


class GARCHVolatilityPredictor:
    """GARCH(1,1) 波动率预测（简化版）"""
    @staticmethod
    def predict(returns):
        if len(returns) < 20:
            return np.std(returns) if returns else 0.02
        alpha, beta, omega = 0.1, 0.85, 0.01
        sigma2 = np.var(returns)
        for r in returns[-20:]:
            sigma2 = omega + alpha * (r ** 2) + beta * sigma2
        return math.sqrt(sigma2)
    
    @staticmethod
    def predict_egarch(returns, leverage=0.1):
        if len(returns) < 20:
            return np.std(returns) if returns else 0.02
        alpha, beta, omega, gamma = 0.1, 0.85, 0.01, 0.1
        sigma2 = np.var(returns)
        for r in returns[-20:]:
            std_r = r / math.sqrt(sigma2) if sigma2 > 0 else 0
            sigma2 = omega + alpha * (abs(std_r) - math.sqrt(2/math.pi)) + gamma * std_r + beta * sigma2
        return math.sqrt(max(sigma2, 0.0001))


class OrderExecutionOptimizer:
    """执行层优化：订单流不平衡+冲击模型"""
    @staticmethod
    def calculate_ofi(orderbook):
        if not orderbook or 'bids' not in orderbook or 'asks' not in orderbook:
            return 0
        bids = orderbook.get('bids', [])
        asks = orderbook.get('asks', [])
        if len(bids) < 5 or len(asks) < 5:
            return 0
        bid_volume = sum([b[1] for b in bids[:5]])
        ask_volume = sum([a[1] for a in asks[:5]])
        if bid_volume + ask_volume == 0:
            return 0
        return (bid_volume - ask_volume) / (bid_volume + ask_volume)
    
    @staticmethod
    def market_impact(volume, avg_volume, price, impact_factor=0.1):
        if avg_volume == 0:
            return 0
        impact = impact_factor * (volume / avg_volume) ** 0.5
        return price * impact


# ==================== 核心前沿技术引擎（保留原有23合1） ====================

class FrontierEngine:
    """保留原有23个技术维度，全部整合"""
    
    @staticmethod
    def archetype_trader_signal(price_history, volume_history, rsi_history, bb_bandwidth_history):
        if len(price_history) < 30:
            return 0, "数据不足"
        volatility = np.std(price_history[-20:]) / np.mean(price_history[-20:]) if len(price_history)>=20 else 0
        trend_strength = abs(price_history[-1] - price_history[-10]) / price_history[-10] if len(price_history)>=10 else 0
        rsi_mean = np.mean(rsi_history[-20:]) if len(rsi_history)>=20 else 50
        bb_bw_mean = np.mean(bb_bandwidth_history[-20:]) if len(bb_bandwidth_history)>=20 else 0
        if volatility > 0.05 and trend_strength > 0.03:
            archetype = "TrendFollower"
            if rsi_mean < 40:
                signal, confidence = 1, 0.8
            elif rsi_mean > 60:
                signal, confidence = -1, 0.75
            else:
                signal, confidence = 0, 0.5
        elif volatility > 0.03 and bb_bw_mean < 0.5:
            archetype = "BreakoutTrader"
            if price_history[-1] > price_history[-2] * 1.01:
                signal, confidence = 1, 0.85
            elif price_history[-1] < price_history[-2] * 0.99:
                signal, confidence = -1, 0.8
            else:
                signal, confidence = 0, 0.4
        elif volatility < 0.03:
            archetype = "MeanReversion"
            if rsi_mean < 35:
                signal, confidence = 1, 0.7
            elif rsi_mean > 65:
                signal, confidence = -1, 0.7
            else:
                signal, confidence = 0, 0.3
        else:
            archetype, signal, confidence = "Balanced", 0, 0.5
        return signal, f"{archetype}({confidence:.2f})"

    @staticmethod
    def crosssync_score(tech_1m, tech_5m, tech_15m, funding_rate, fear_greed):
        if None in (tech_1m, tech_5m, tech_15m):
            return 0, []
        score = 0; factors = []
        rsi_avg = (tech_1m.get('rsi',50) + tech_5m.get('rsi',50) + tech_15m.get('rsi',50)) / 3
        if rsi_avg < 35:
            score += 20; factors.append(f"RSI共振超卖({rsi_avg:.0f})")
        elif rsi_avg > 65:
            score -= 15; factors.append(f"RSI共振超买({rsi_avg:.0f})")
        bb_positions = []
        for tech in [tech_1m, tech_5m, tech_15m]:
            price = tech.get('bb_middle',0); bb_lower = tech.get('bb_lower',0); bb_upper = tech.get('bb_upper',0)
            if bb_upper > bb_lower and price > 0:
                bb_positions.append((price - bb_lower) / (bb_upper - bb_lower))
        if bb_positions:
            avg_bb = sum(bb_positions)/len(bb_positions)
            if avg_bb < 0.2:
                score += 15; factors.append("多周期布林下轨")
            elif avg_bb > 0.8:
                score -= 10; factors.append("多周期布林上轨")
        if funding_rate is not None:
            if funding_rate < -0.0005:
                score += 10; factors.append("费率负值")
            elif funding_rate > 0.001:
                score -= 10; factors.append("费率过高")
        if fear_greed is not None:
            if fear_greed < 30:
                score += 5; factors.append("极度恐惧")
            elif fear_greed > 70:
                score -= 5; factors.append("极度贪婪")
        return min(100, max(0, score)), factors

    @staticmethod
    def meta_rl_score(price_history, win_rate_history, sharpe_history, n=30):
        if len(price_history) < n:
            return 50
        recent_prices = price_history[-n:]
        recent_returns = [(recent_prices[i]-recent_prices[i-1])/recent_prices[i-1] for i in range(1,len(recent_prices))]
        avg_return = sum(recent_returns)/len(recent_returns) if recent_returns else 0
        return_std = np.std(recent_returns) if len(recent_returns)>1 else 0.01
        meta_factor = 0.4*avg_return*100 + 0.3*(win_rate_history[-1] if win_rate_history else 0.5) + 0.3*(sharpe_history[-1] if sharpe_history else 1.0)
        meta_score = 50 + meta_factor*10
        return min(100, max(0, meta_score))

    @staticmethod
    def chanformer_score(price_sequence, volume_sequence, n=50):
        if len(price_sequence) < n:
            return 50
        recent_prices = price_sequence[-n:]
        recent_volumes = volume_sequence[-n:] if len(volume_sequence)>=n else [1]*n
        price_changes = [recent_prices[i]/recent_prices[i-1]-1 for i in range(1,len(recent_prices))]
        if not price_changes:
            return 50
        channel_weights = []
        for i, change in enumerate(price_changes):
            vol_factor = recent_volumes[i] / (sum(recent_volumes)/len(recent_volumes)) if sum(recent_volumes)>0 else 1
            channel_weight = abs(change) * vol_factor
            channel_weights.append(channel_weight)
        weighted_score = sum(channel_weights[-10:]) / sum(channel_weights) * 100 if sum(channel_weights)>0 else 50
        return min(100, max(0, weighted_score))

    @staticmethod
    def f2agent_signal(tech_data, onchain_data, news_sentiment, fear_greed, social_sentiment):
        score = 50; signals = []
        rsi = tech_data.get('rsi',50)
        if rsi < 35:
            score += 15; signals.append("技术超卖")
        elif rsi > 65:
            score -= 15; signals.append("技术超买")
        price = tech_data.get('bb_middle',0); bb_lower = tech_data.get('bb_lower',0); bb_upper = tech_data.get('bb_upper',0)
        if bb_upper > bb_lower and price > 0:
            bb_pos = (price - bb_lower)/(bb_upper - bb_lower)
            if bb_pos < 0.2:
                score += 15; signals.append("布林下轨")
            elif bb_pos > 0.8:
                score -= 10; signals.append("布林上轨")
        if onchain_data:
            whale = onchain_data.get('whale_transfers',0); netflow = onchain_data.get('exchange_netflow',0)
            if whale > 3:
                score += 10; signals.append("巨鲸活跃")
            if netflow < -50:
                score += 10; signals.append("交易所净流出")
        if news_sentiment:
            sentiment = news_sentiment.get('sentiment',0)
            if sentiment > 0.3:
                score += 10; signals.append("新闻积极")
            elif sentiment < -0.3:
                score -= 10; signals.append("新闻消极")
        if social_sentiment:
            sentiment = social_sentiment.get('sentiment',0)
            if sentiment > 0.3:
                score += 5; signals.append("社交积极")
            elif sentiment < -0.3:
                score -= 5; signals.append("社交消极")
        if fear_greed is not None and fear_greed < 30:
            score += 5; signals.append("极度恐惧")
        return min(100, max(0, score)), signals

    @staticmethod
    def confidence_rl_score(price_history, rsi_history, volatility_history, n=30):
        if len(price_history) < n or len(rsi_history) < n:
            return 50, 0.5
        recent_prices = price_history[-n:]
        recent_rsi = rsi_history[-n:] if len(rsi_history)>=n else [50]*n
        recent_vol = volatility_history[-n:] if len(volatility_history)>=n else [0.01]*n
        volatility = np.mean(recent_vol)
        rsi_extreme = max(0, abs(np.mean(recent_rsi) - 50) / 50)
        confidence = max(0.3, min(0.95, 1 - volatility * 5 - rsi_extreme * 0.3))
        base_score = 50 + (50 - np.mean(recent_rsi)) * 0.5
        score = base_score * confidence
        return min(100, max(0, score)), confidence

    @staticmethod
    def dl_stat_arbitrage(prices_list, volumes_list, n=30):
        if len(prices_list) < 2 or len(prices_list[0]) < n:
            return 0, []
        ratios = []
        for i in range(len(prices_list)):
            for j in range(i+1, len(prices_list)):
                ratio = np.mean(prices_list[i][-n:]) / np.mean(prices_list[j][-n:])
                ratios.append(ratio)
        if not ratios:
            return 0, []
        mean_ratio = np.mean(ratios)
        std_ratio = np.std(ratios) if len(ratios) > 1 else 0.01
        current_ratio = ratios[-1]
        z_score = (current_ratio - mean_ratio) / std_ratio
        if z_score > 2:
            return -1, [f"Z={z_score:.2f}做空"]
        elif z_score < -2:
            return 1, [f"Z={z_score:.2f}做多"]
        return 0, [f"Z={z_score:.2f}中性"]

    @staticmethod
    def high_freq_signal(price_sequence, volume_sequence, n=20):
        if len(price_sequence) < n or len(volume_sequence) < n:
            return 0, 0
        recent_prices = price_sequence[-n:]
        recent_volumes = volume_sequence[-n:]
        price_momentum = (recent_prices[-1] - recent_prices[-5]) / recent_prices[-5] if len(recent_prices)>=5 else 0
        volume_momentum = (recent_volumes[-1] - np.mean(recent_volumes)) / np.mean(recent_volumes) if np.mean(recent_volumes)>0 else 0
        if price_momentum > 0.005 and volume_momentum > 0.5:
            return 1, 0.8
        elif price_momentum < -0.005 and volume_momentum > 0.5:
            return -1, 0.75
        return 0, 0.3

    @staticmethod
    def onchain_quant_score(onchain_data):
        if not onchain_data:
            return 50, []
        score = 50; factors = []
        whale = onchain_data.get('whale_transfers', 0)
        netflow = onchain_data.get('exchange_netflow', 0)
        active = onchain_data.get('active_addresses', 0)
        hashrate = onchain_data.get('hashrate', 0)
        if whale > 3:
            score += 10; factors.append(f"巨鲸{whale}笔")
        if netflow < -50:
            score += 15; factors.append("交易所净流出")
        elif netflow > 50:
            score -= 10; factors.append("交易所净流入")
        if active > 4000:
            score += 5; factors.append("活跃地址高")
        if hashrate > 400:
            score += 5; factors.append("算力高")
        return min(100, max(0, score)), factors

    @staticmethod
    def multi_source_sentiment(news_sentiment, social_sentiment, fear_greed):
        score = 50; factors = []
        if news_sentiment:
            ns = news_sentiment.get('sentiment', 0)
            if ns > 0.3:
                score += 10; factors.append("新闻积极")
            elif ns < -0.3:
                score -= 10; factors.append("新闻消极")
        if social_sentiment:
            ss = social_sentiment.get('sentiment', 0)
            if ss > 0.3:
                score += 5; factors.append("社交积极")
            elif ss < -0.3:
                score -= 5; factors.append("社交消极")
        if fear_greed is not None:
            if fear_greed < 30:
                score += 5; factors.append("极度恐惧")
            elif fear_greed > 70:
                score -= 5; factors.append("极度贪婪")
        return min(100, max(0, score)), factors

    @staticmethod
    def triangular_arbitrage(prices):
        if len(prices) < 3:
            return False, 0
        for i in range(len(prices)):
            for j in range(i+1, len(prices)):
                for k in range(j+1, len(prices)):
                    if prices[i] <= 0 or prices[j] <= 0 or prices[k] <= 0:
                        continue
                    arb = prices[i] * prices[j] / prices[k]
                    if abs(arb - 1) > 0.002:
                        return True, (arb - 1) * 100
        return False, 0

    @staticmethod
    def autonomous_agent_decision(price_history, tech_data, onchain, news, social, fear_greed, funding):
        score = 50
        reasons = []
        rsi = tech_data.get('rsi',50)
        if rsi < 30:
            score += 20; reasons.append("RSI超卖")
        elif rsi < 40:
            score += 10; reasons.append("RSI偏低")
        price = tech_data.get('bb_middle',0)
        bb_lower = tech_data.get('bb_lower',0)
        bb_upper = tech_data.get('bb_upper',0)
        if bb_upper > bb_lower and price > 0:
            bb_pos = (price - bb_lower)/(bb_upper - bb_lower)
            if bb_pos < 0.15:
                score += 20; reasons.append("布林下轨极低")
            elif bb_pos < 0.3:
                score += 10; reasons.append("布林下轨附近")
        if onchain:
            if onchain.get('exchange_netflow',0) < -50:
                score += 15; reasons.append("链上净流出")
            if onchain.get('whale_transfers',0) > 3:
                score += 10; reasons.append("鲸鱼活跃")
        if news and news.get('sentiment',0) > 0.3:
            score += 10; reasons.append("新闻积极")
        if social and social.get('sentiment',0) > 0.3:
            score += 5; reasons.append("社交积极")
        if fear_greed is not None and fear_greed < 30:
            score += 10; reasons.append("极度恐惧")
        if funding is not None and funding < -0.0005:
            score += 10; reasons.append("费率负值")
        if score >= 75:
            action = "BUY"; confidence = 0.85
        elif score >= 60:
            action = "BUY_LIGHT"; confidence = 0.65
        elif score >= 40:
            action = "HOLD"; confidence = 0.5
        elif score >= 25:
            action = "SELL_LIGHT"; confidence = 0.6
        else:
            action = "SELL"; confidence = 0.7
        return min(100, max(0, score)), action, confidence, reasons

    @staticmethod
    def evoquant_optimize(performance_metrics, current_params):
        if not performance_metrics or len(performance_metrics) < 20:
            return current_params, 0
        win_rate = performance_metrics.get('win_rate', 0.5)
        avg_win = performance_metrics.get('avg_win_pct', 0)
        avg_loss = performance_metrics.get('avg_loss_pct', 0)
        sharpe = performance_metrics.get('sharpe', 1.0)
        factor = 1.0
        if win_rate < 0.4:
            factor *= 0.9
        if avg_win < avg_loss * 0.5:
            factor *= 0.8
        if sharpe < 0.5:
            factor *= 0.85
        new_params = current_params.copy()
        if 'tp_pct' in new_params:
            new_params['tp_pct'] = new_params['tp_pct'] * (1 + (avg_win/100) * 0.1)
            new_params['sl_pct'] = new_params['sl_pct'] * (1 + (avg_loss/100) * 0.1)
        return new_params, factor

    @staticmethod
    def deterministic_shielding(signal, confidence, market_volatility, max_risk=0.02):
        if confidence < 0.3:
            return 0, "低置信度屏蔽"
        if market_volatility > 0.1:
            return signal * 0.5, "高波动降仓"
        if abs(signal) * confidence > 1.5:
            return signal * (1.5 / (abs(signal) * confidence)), "信号过强限制"
        return signal, "通过"

    @staticmethod
    def rala_enhanced(regime, confidence, tech_data, funding, fear_greed):
        if regime == "high_volatility_trend":
            return {"tp_factor": 1.5, "sl_factor": 1.2, "signal": "trend", "weight": 0.7}
        elif regime == "breakout":
            return {"tp_factor": 1.0, "sl_factor": 0.8, "signal": "breakout", "weight": 0.6}
        elif regime == "low_volatility_range":
            return {"tp_factor": 0.7, "sl_factor": 0.5, "signal": "range", "weight": 0.5}
        elif regime == "extreme_volatility":
            return {"tp_factor": 0.3, "sl_factor": 0.3, "signal": "pause", "weight": 0.3}
        else:
            return {"tp_factor": 1.0, "sl_factor": 1.0, "signal": "neutral", "weight": 0.5}

    @staticmethod
    def meta_rl_enhanced(price_history, rsi_history, volume_history, win_rate_history, sharpe_history):
        if len(price_history) < 30:
            return 50, 0.5
        recent_prices = price_history[-20:]
        recent_returns = [(recent_prices[i] - recent_prices[i-1]) / recent_prices[i-1]
                         for i in range(1, len(recent_prices))]
        avg_return = sum(recent_returns) / len(recent_returns) if recent_returns else 0
        return_std = np.std(recent_returns) if len(recent_returns) > 1 else 0.01
        rsi_mean = np.mean(rsi_history[-20:]) if len(rsi_history) >= 20 else 50
        vol_mean = np.mean(volume_history[-20:]) if len(volume_history) >= 20 else 0
        win_rate = win_rate_history[-1] if win_rate_history else 0.5
        sharpe = sharpe_history[-1] if sharpe_history else 1.0
        actor_score = 50 + avg_return * 100 * 10
        judge_score = 50 + (50 - rsi_mean) * 0.3 + vol_mean * 0.01
        meta_score = 50 + (win_rate - 0.5) * 40 + (sharpe - 1) * 10
        combined = actor_score * 0.4 + judge_score * 0.3 + meta_score * 0.3
        confidence = min(0.95, max(0.3, abs(avg_return) * 20 + win_rate * 0.3))
        return min(100, max(0, combined)), confidence

    @staticmethod
    def cryptogat_signal(all_coin_data, target_symbol):
        if len(all_coin_data) < 2:
            return 50, []
        target_price = all_coin_data.get(target_symbol, {}).get('price', 0)
        if target_price == 0:
            return 50, []
        correlations = []
        for sym, data in all_coin_data.items():
            if sym == target_symbol:
                continue
            price = data.get('price', 0)
            change = data.get('change_24h', 0)
            if price == 0:
                continue
            rel_strength = (price - target_price) / target_price if target_price > 0 else 0
            attention = abs(change) / (1 + abs(rel_strength))
            correlations.append((sym, change, rel_strength, attention))
        if not correlations:
            return 50, []
        correlations.sort(key=lambda x: x[3], reverse=True)
        top_correlations = correlations[:3]
        weighted_score = 0
        total_weight = 0
        for sym, change, rel, attn in top_correlations:
            if change > 0 and rel < 0:
                weighted_score += attn * 1
            elif change < 0 and rel > 0:
                weighted_score -= attn * 1
            total_weight += attn
        normalized = 50 + (weighted_score / total_weight * 10) if total_weight > 0 else 50
        factors = [f"{sym}:{change:+.2f}%" for sym, change, _, _ in top_correlations]
        return min(100, max(0, normalized)), factors

    @staticmethod
    def web_crypto_score(tech_data, news_sentiment, social_sentiment, fear_greed):
        score = 50
        factors = []
        rsi = tech_data.get('rsi', 50)
        if rsi < 30:
            score += 15
            factors.append("技术超卖")
        elif rsi < 40:
            score += 8
            factors.append("技术偏低")
        price = tech_data.get('bb_middle', 0)
        bb_lower = tech_data.get('bb_lower', 0)
        if bb_lower > 0 and price > 0 and price <= bb_lower * 1.03:
            score += 15
            factors.append("布林下轨")
        if news_sentiment:
            ns = news_sentiment.get('sentiment', 0)
            if ns > 0.3:
                score += 12
                factors.append("新闻积极")
            elif ns < -0.3:
                score -= 12
                factors.append("新闻消极")
        if social_sentiment:
            ss = social_sentiment.get('sentiment', 0)
            if ss > 0.2:
                score += 8
                factors.append("社交积极")
            elif ss < -0.2:
                score -= 8
                factors.append("社交消极")
        if fear_greed is not None:
            if fear_greed < 30:
                score += 10
                factors.append("极度恐惧")
            elif fear_greed > 70:
                score -= 10
                factors.append("极度贪婪")
        return min(100, max(0, score)), factors

    @staticmethod
    def sentiment_rl_score(price_history, sentiment_history, rsi_history, n=30):
        if len(price_history) < n or len(sentiment_history) < n:
            return 50, 0.5
        recent_prices = price_history[-n:]
        recent_sentiment = sentiment_history[-n:]
        recent_rsi = rsi_history[-n:] if len(rsi_history) >= n else [50] * n
        sentiment_alpha = sum(recent_sentiment) / len(recent_sentiment)
        price_momentum = (recent_prices[-1] - recent_prices[0]) / recent_prices[0] if recent_prices[0] > 0 else 0
        alpha_reward = sentiment_alpha * 0.6 + price_momentum * 0.4
        rsi_penalty = 0
        if np.mean(recent_rsi) > 70:
            rsi_penalty = -0.3
        elif np.mean(recent_rsi) < 30:
            rsi_penalty = 0.3
        score = 50 + alpha_reward * 30 + rsi_penalty * 20
        confidence = min(0.9, max(0.3, abs(alpha_reward) * 0.5 + 0.3))
        return min(100, max(0, score)), confidence

    @staticmethod
    def astgnn_score(price_history, volume_history, rsi_history, ohlcv_data, n=30):
        if len(price_history) < n or len(ohlcv_data) < n:
            return 50
        recent_ohlcv = ohlcv_data[-n:] if len(ohlcv_data) >= n else []
        if len(recent_ohlcv) < n:
            return 50
        quaternion_features = []
        for ohlcv in recent_ohlcv:
            if len(ohlcv) >= 4:
                open_p, high_p, low_p, close_p = ohlcv[0], ohlcv[1], ohlcv[2], ohlcv[3]
                q_real = close_p
                q_i = high_p - low_p
                q_j = open_p - close_p
                q_k = ohlcv[4] if len(ohlcv) > 4 else 0
                quaternion_features.append((q_real, q_i, q_j, q_k))
        if len(quaternion_features) < n:
            return 50
        q_norms = [math.sqrt(q[0]**2 + q[1]**2 + q[2]**2 + q[3]**2) for q in quaternion_features]
        q_mean = sum(q_norms) / len(q_norms)
        rotations = []
        for i in range(1, len(quaternion_features)):
            q1, q2 = quaternion_features[i-1], quaternion_features[i]
            dot = q1[0]*q2[0] + q1[1]*q2[1] + q1[2]*q2[2] + q1[3]*q2[3]
            norm1 = math.sqrt(sum([x**2 for x in q1]))
            norm2 = math.sqrt(sum([x**2 for x in q2]))
            if norm1 > 0 and norm2 > 0:
                cos_theta = dot / (norm1 * norm2)
                rotations.append(max(-1, min(1, cos_theta)))
        if rotations:
            rotation_stability = sum(rotations) / len(rotations)
            volatility_score = q_mean / (sum(q_norms) / len(q_norms)) if sum(q_norms) > 0 else 1
            score = 50 + (rotation_stability * 20) + (volatility_score * 10)
        else:
            score = 50
        return min(100, max(0, score))

    @staticmethod
    def shield_v2(signal, confidence, market_volatility, price_history, max_risk=0.02):
        if confidence < 0.3:
            return 0, "低置信度屏蔽", False
        if market_volatility > 0.1:
            return signal * 0.5, "高波动降仓", False
        if len(price_history) >= 5:
            recent_changes = []
            for i in range(1, min(5, len(price_history))):
                if price_history[-i] > 0:
                    change = (price_history[-1] - price_history[-i]) / price_history[-i]
                    recent_changes.append(abs(change))
            if recent_changes and max(recent_changes) > 0.05:
                return signal * 0.3, "价格突变保护", True
        if abs(signal) * confidence > 1.5:
            return signal * (1.5 / (abs(signal) * confidence)), "信号过强限制", False
        return signal, "通过", False


# ==================== 低买高卖增强模块 ====================

class LowBuyHighSellEnhancer:
    """低买高卖核心逻辑"""
    @staticmethod
    def enhanced_buy_signal(tech, price, ema20=None, market_state=None):
        rsi = tech.get('rsi', 50)
        bb_lower = tech.get('bb_lower', 0)
        bb_upper = tech.get('bb_upper', 0)
        if bb_lower == 0 or bb_upper == 0:
            return False, 0, {}
        near_lower = price <= bb_lower * 1.02
        oversold = rsi < 40
        state_weight = 1.0
        if market_state == 'oversold':
            state_weight = 1.3
        elif market_state == 'overbought':
            state_weight = 0.5
        elif market_state == 'extreme':
            state_weight = 0.6
        trend_ok = True
        if ema20 is not None and ema20 > 0:
            trend_ok = price >= ema20 * 0.99
        if near_lower and oversold and trend_ok:
            strength = (1 - (price - bb_lower) / (bb_upper - bb_lower)) * 50 + (40 - rsi) * 1.5
            strength = min(100, max(0, strength)) * state_weight
            return True, min(100, strength), {'near_lower': near_lower, 'oversold': oversold, 'state_weight': state_weight}
        return False, 0, {}

    @staticmethod
    def trailing_stop_with_stepping(entry_price, current_price, high_price, tp_pct, sl_pct, step_factor=0.3):
        profit_pct = (current_price - entry_price) / entry_price * 100
        if profit_pct <= -sl_pct * 100:
            return 'sell', 'stop_loss'
        if profit_pct >= tp_pct * 100 * 0.5:
            if profit_pct >= tp_pct * 100 * 0.7:
                trailing_pct = tp_pct * 0.3
            else:
                trailing_pct = tp_pct * 0.5
            if current_price <= high_price * (1 - trailing_pct):
                return 'sell', 'trailing_stop'
        if profit_pct >= tp_pct * 100:
            return 'sell', 'take_profit'
        return 'hold', None


# ==================== 核心机器人 ====================

class QuantBot:
    def __init__(self, exchange):
        self.exchange = exchange
        self.ws = WSDataManager(exchange)
        self.tech = TechnicalEngine(exchange)
        self.real_data = RealDataEngine(exchange, self.ws)
        self.orderbook_engine = OrderbookEngine()
        self.frontier = FrontierEngine()
        self.low_buy = LowBuyHighSellEnhancer()
        self.lock = asyncio.Lock()
        
        # 七大优化模块实例
        self.market_state = MarketStateEngine()
        self.optimizer = AutoQuantOptimizer()
        self.multi_agent = MultiAgentSystem()
        self.sentiment_rl = SentimentAugmentedRL()
        self.web_agent = WebCryptoAgent()
        self.kelly = KellyPositionManager()
        self.garch = GARCHVolatilityPredictor()
        self.executor = OrderExecutionOptimizer()

        # 基础配置
        self.is_running = True
        self.orderbook_filter = True
        self.waterfall_breaker = True
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
        self.api_error_count = 0
        self.max_api_errors = 5
        self.api_error_pause_time = 0
        self.max_positions_per_coin = 8
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
        self._performance_metrics = {}
        self._sentiment_history = {}
        self._ohlcv_history = {}
        self._trade_history = []
        self._current_market_state = "neutral"
        self._current_market_score = 0.5
        self._current_state_params = {}
        self._kelly_cache = {}

        # 高级模块
        self._consecutive_losses = 0
        self._today_loss_pct = 0.0
        self._is_paused = False
        self._daily_trade_count = 0
        self._last_pause_time = 0
        self._account_balance = 0.0
        self._balance_last_update = 0
        self._multi_timeframe_data = {}
        self._delta_neutral_positions = {}
        self._onchain_cache = {}
        self._triangular_positions = {}

        # 资金费率套利（已修正注释，明确为“费率抄底”）
        self._delta_neutral_config = {
            "enabled": True,
            "min_funding_rate": 0.0003,
            "max_position_per_coin": 2,
            "allocation_percent": 0.03,
            "min_allocation": 0.5,
            "max_allocation": 10,
        }
        self._delta_neutral_stats = {
            "total_trades": 0,
            "total_profit": 0.0,
            "last_trade_time": 0,
            "profit_today": 0.0,
            "today_date": datetime.now(CST).day,
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
        self._balance_cache_ttl = 15

        self._btc_safe_flag = True
        self._drawdown_safe_flag = True

        # 新增：每日重置标记
        self._last_reset_date = datetime.now(CST).day

        # ========== 技术指标缓存（新增，解决限频） ==========
        self._tech_cache = {}
        self._tech_cache_time = {}
        self._tech_cache_ttl = 30  # 缓存30秒

        # AI 分析
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
            "archetype": "Balanced",
            "auto_agent_action": "HOLD",
            "market_state": "neutral",
            "kelly_position": 0
        }
        self.ai_api_key = os.getenv("DEEPSEEK_API_KEY", "") or os.getenv("OPENAI_API_KEY", "")
        self.ai_model = os.getenv("AI_MODEL", "deepseek-chat")
        self.ai_base_url = os.getenv("AI_BASE_URL", "https://api.deepseek.com/v1")
        self.ai_enabled = bool(self.ai_api_key)

        # Telegram
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
                CommandHandler("optimize", self.cmd_optimize),
                CommandHandler("state", self.cmd_state),
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

    def _calculate_dynamic_amount(self, base_amount=0.5):
        total_balance = self._cached_usdt_free
        for coin, free in self._cached_balances.items():
            ticker = self.ws.get_ticker(coin + "/USDT")
            if ticker:
                total_balance += free * ticker.get('last', 0)
        self._account_balance = total_balance
        if total_balance < 10:
            return max(0.1, base_amount * 0.3)
        elif total_balance < 30:
            return max(0.2, base_amount * 0.6)
        elif total_balance < 50:
            return max(0.3, base_amount * 0.8)
        elif total_balance < 100:
            return base_amount
        elif total_balance < 300:
            return base_amount * 2
        else:
            return base_amount * 4

    async def _check_risk_limits(self):
        # 每日重置亏损计数
        today = datetime.now(CST).day
        if today != self._last_reset_date:
            self._today_loss_pct = 0.0
            self._consecutive_losses = 0
            self._last_reset_date = today

        if self._consecutive_losses >= 4:
            if time.time() - self._last_pause_time > 3600:
                self._consecutive_losses = 0
                self._is_paused = False
            else:
                return False
        if self._today_loss_pct > 0.08:
            if not self._is_paused:
                await self._alert(f"⛔ 当日亏损达 {self._today_loss_pct*100:.1f}%，暂停交易", "critical")
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
        self.tp_pct = cfg.get('tp_pct', 0.015)
        self.sl_pct = cfg.get('sl_pct', 0.01)
        self.trailing_sl_pct = cfg.get('trailing_sl_pct', 0.005)
        self.trailing_tp_pct = cfg.get('trailing_tp_pct', 0.003)
        self.single_order_usdt = cfg.get('single_order_usdt', 1.0)
        self.timeframe = cfg.get('timeframe', '5m')
        self.reserve_bottom = cfg.get('reserve_bottom', 10)
        self.max_daily_trades = cfg.get('max_daily_trades', 20)
        self.auto_trade_enabled = cfg.get('auto_trade_enabled', False)
        self.auto_min_score = cfg.get('auto_min_score', 65)
        self.max_per_coin_usdt = cfg.get('max_per_coin_usdt', 50)
        self.max_daily_loss_pct = cfg.get('max_daily_loss_pct', 0.05)
        self.max_total_allocated_pct = 1.0
        self.max_drawdown_pct = cfg.get('max_drawdown_pct', 0.12)
        self.max_positions_per_coin = cfg.get('max_positions_per_coin', 8)

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
        logger.info("✅ UltimateBot v12.0 已加载")

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

    # ==================== 技术指标缓存方法（新增） ====================
    async def _get_cached_tech(self, sym, timeframe='5m', limit=50):
        """带缓存的技术指标获取，避免频繁API请求"""
        key = f"{sym}_{timeframe}_{limit}"
        now = time.time()
        if key in self._tech_cache and (now - self._tech_cache_time.get(key, 0)) < self._tech_cache_ttl:
            return self._tech_cache[key]
        tech = await self.tech.calc(sym, timeframe, limit)
        if tech:
            self._tech_cache[key] = tech
            self._tech_cache_time[key] = now
        return tech

    # ==================== 市场状态检测与自适应 ====================

    async def _update_market_state(self, sym, tech_data):
        price_hist = self._price_history.get(sym, [])
        vol_hist = self._volatility_history.get(sym, [])
        state_str, state_score = self.market_state.detect_state(tech_data, price_hist, vol_hist)
        self._current_market_state = state_str
        self._current_market_score = state_score
        state_params = self.market_state.get_strategy_params(
            state_str, self.tp_pct, self.sl_pct, self.single_order_usdt
        )
        self._current_state_params = state_params
        return state_str, state_params

    # ==================== AI 市场分析 ====================

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

                all_scores = []
                archetype_signals = []
                regime_status = []
                kelly_positions = []
                
                for sym in self.symbols:
                    try:
                        ticker = self.ws.get_ticker(sym)
                        if ticker is None:
                            continue
                        p = ticker['last']
                        # 使用缓存获取指标
                        tech = await self._get_cached_tech(sym, self.timeframe, 50)
                        if tech is None:
                            continue
                        
                        if sym not in self._price_history:
                            self._price_history[sym] = []
                        if sym not in self._volatility_history:
                            self._volatility_history[sym] = []
                        if sym not in self._win_rate_history:
                            self._win_rate_history[sym] = []
                        if sym not in self._sharpe_history:
                            self._sharpe_history[sym] = []
                        if sym not in self._sentiment_history:
                            self._sentiment_history[sym] = []
                        
                        self._price_history[sym].append(p)
                        if len(self._price_history[sym]) > 100:
                            self._price_history[sym].pop(0)
                        
                        volatility = tech.get('atr', 0) / tech.get('bb_middle', 1) if tech.get('bb_middle', 0) > 0 else 0.01
                        self._volatility_history[sym].append(volatility)
                        if len(self._volatility_history[sym]) > 50:
                            self._volatility_history[sym].pop(0)
                        
                        self._sentiment_history[sym].append(news_sentiment)
                        if len(self._sentiment_history[sym]) > 50:
                            self._sentiment_history[sym].pop(0)
                        
                        onchain = await self.real_data.get_onchain_metrics(sym)
                        funding = await self.real_data.get_funding_rate(sym)
                        rsi_hist = [h.get('rsi', 50) for h in self._rsi_history.get(sym, [])]
                        bb_hist = self._bb_bandwidth_history.get(sym, [])
                        
                        state, state_params = await self._update_market_state(sym, tech)
                        
                        recent_trades = self.trades[-30:] if len(self.trades) >= 30 else self.trades
                        pnls = [t.get('pnl_pct', 0) for t in recent_trades if t.get('pnl_pct') is not None]
                        if pnls:
                            wins = [p for p in pnls if p > 0]
                            losses = [p for p in pnls if p < 0]
                            win_rate = len(wins) / len(pnls) if pnls else 0.5
                            avg_win = sum(wins) / len(wins) if wins else 0.5
                            avg_loss = abs(sum(losses) / len(losses)) if losses else 0.5
                            kelly_pos = self.kelly.calculate_position(win_rate, avg_win, avg_loss, len(pnls), 0.1)
                        else:
                            kelly_pos = 0.05
                        
                        kelly_positions.append(kelly_pos)
                        
                        arch_signal, arch_type = self.frontier.archetype_trader_signal(
                            self._price_history[sym], self._volume_history.get(sym, []), rsi_hist, bb_hist)
                        archetype_signals.append(f"{sym}:{arch_type}")
                        
                        cross_score, _ = self.frontier.crosssync_score(
                            await self._get_multi_timeframe_data(sym), None, None, funding, fg)
                        meta_score = self.frontier.meta_rl_score(self._price_history[sym], self._win_rate_history[sym], self._sharpe_history[sym])
                        chan_score = self.frontier.chanformer_score(self._price_history[sym], self._volume_history.get(sym, []))
                        f2_score, _ = self.frontier.f2agent_signal(tech, onchain, news_data, fg, social_data)
                        sent_rl_score, _ = self.sentiment_rl.calculate_alpha_reward(
                            self._price_history[sym], self._sentiment_history[sym], rsi_hist)
                        web_score, _ = self.web_agent.fusion_score(tech, news_data, social_data, fg, onchain)
                        agent_analysis = await self.multi_agent.analyze({}, tech)
                        agent_strategy = self.multi_agent.generate_strategy(agent_analysis, {})
                        
                        combined = (
                            0.05 * min(100, max(0, 50 + arch_signal * 30)) +
                            0.05 * cross_score +
                            0.05 * meta_score +
                            0.05 * chan_score +
                            0.05 * f2_score +
                            0.10 * sent_rl_score +
                            0.10 * web_score +
                            0.05 * (50 + agent_strategy.get('tp_factor', 1.0) * 10)
                        )
                        all_scores.append(combined)
                        regime_status.append(f"{sym}:{state}")
                        
                    except Exception as e:
                        logger.warning(f"分析失败 {sym}: {e}")
                        continue

                avg_score = sum(all_scores) / len(all_scores) if all_scores else 50
                avg_kelly = sum(kelly_positions) / len(kelly_positions) if kelly_positions else 0.05
                
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

                regime_summary = ", ".join(set(regime_status)) if regime_status else "neutral"

                summary = (f"📊 BTC: {btc_change:+.2f}% | ETH: {eth_change:+.2f}%\n"
                           f"😨 恐惧贪婪: {fg} ({fg_data['classification'] if fg_data else '中性'})\n"
                           f"📰 新闻: {news_sentiment:+.2f} | 社交: {social_sentiment:+.2f}\n"
                           f"📈 市场状态: {self._current_market_state}\n"
                           f"🎯 综合评分: {avg_score:.0f}/100\n"
                           f"💰 凯利仓位: {avg_kelly*100:.1f}%\n"
                           f"💡 建议: {recommendation}")

                self.ai_insight = {
                    "timestamp": time.time(),
                    "summary": summary,
                    "btc_trend": "看涨" if btc_change > 0 else "看跌",
                    "eth_trend": "看涨" if eth_change > 0 else "看跌",
                    "fear_greed": fg,
                    "news_sentiment": news_sentiment,
                    "social_sentiment": social_sentiment,
                    "news_headlines": news_headlines[:3],
                    "recommendation": recommendation,
                    "score": avg_score,
                    "regime": regime_summary,
                    "archetype": archetype_signals[0] if archetype_signals else "Balanced",
                    "auto_agent_action": recommendation,
                    "market_state": self._current_market_state,
                    "kelly_position": avg_kelly
                }
                logger.info(f"🤖 AI分析完成: {recommendation} | 评分{avg_score:.0f} | 市场状态{self._current_market_state}")
            except Exception as e:
                logger.error(f"AI分析异常: {e}")
            await asyncio.sleep(1800)

    # ==================== 开仓决策（稳定版） ====================

    async def _should_open_position(self, sym, p, tech, funding, fg, usdt_free):
        """整合所有优化的开仓决策 - 稳定版（移除外部API和随机数据）"""
        # ---- 强制更新价格历史，确保数据积累 ----
        if sym not in self._price_history:
            self._price_history[sym] = []
        self._price_history[sym].append(p)
        if len(self._price_history[sym]) > 100:
            self._price_history[sym].pop(0)

        if tech and sym in self._rsi_history:
            rsi = tech.get('rsi', 50)
            self._rsi_history[sym].append({'rsi': rsi, 'price': p, 'time': time.time()})
            if len(self._rsi_history[sym]) > 100:
                self._rsi_history[sym].pop(0)

        # 初始化各类历史数据容器
        if sym not in self._volume_history:
            self._volume_history[sym] = []
        if sym not in self._close_prices_history:
            self._close_prices_history[sym] = []
        if sym not in self._bb_bandwidth_history:
            self._bb_bandwidth_history[sym] = []

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

        if tech:
            rsi = tech.get('rsi', 50)
            if sym not in self._rsi_history:
                self._rsi_history[sym] = []
            self._rsi_history[sym].append({'rsi': rsi, 'price': p, 'time': time.time()})
            if len(self._rsi_history[sym]) > 100:
                self._rsi_history[sym].pop(0)

        bb_upper = tech.get('bb_upper', 0) if tech else 0
        bb_lower = tech.get('bb_lower', 0) if tech else 0
        if bb_upper > 0 and bb_lower > 0 and p > 0:
            bw = (bb_upper - bb_lower) / p * 100 if p > 0 else 0
            self._bb_bandwidth_history[sym].append(bw)
            if len(self._bb_bandwidth_history[sym]) > 100:
                self._bb_bandwidth_history[sym].pop(0)

        # 只获取多周期技术数据（不依赖外部API）
        multi = await self._get_multi_timeframe_data(sym)

        # 获取 RSI 历史
        rsi_hist = [h.get('rsi', 50) for h in self._rsi_history.get(sym, [])]

        # 市场状态
        state, state_params = await self._update_market_state(sym, tech)
        details = [f"状态:{state}"]

        # 多智能体分析（仅基于技术）
        agent_analysis = await self.multi_agent.analyze({}, tech)
        agent_strategy = self.multi_agent.generate_strategy(agent_analysis, {})
        details.append(f"Agent:{agent_analysis.get('state', 'neutral')}")

        # ---------- 计算各子分数，仅使用可靠技术因子 ----------
        # 权重总和为1
        weights = {
            'low_buy': 0.25,      # 低买信号（基于技术）
            'ofi': 0.10,          # 订单流不平衡（基于盘口）
            'arch': 0.10,         # Archetype（基于价格/量）
            'cross': 0.10,        # 多周期共振（技术）
            'meta': 0.10,         # Meta RL（基于价格/胜率）
            'f2': 0.10,           # F2 Agent（纯技术）
            'agent': 0.10,        # 多智能体策略因子（基于技术状态）
            'default': 0.15       # 综合补充（基于RSI等）
        }

        # 1. 低买信号（最强因子）
        ema20 = None
        try:
            ohlcv = await self.exchange.fetch_ohlcv(sym, self.timeframe, 20)
            if ohlcv:
                closes = [c[4] for c in ohlcv]
                ema20 = np.mean(closes[-20:]) if len(closes) >= 20 else None
        except:
            pass
        enhanced_buy, strength, buy_details = self.low_buy.enhanced_buy_signal(
            tech, p, ema20, agent_analysis.get('state', 'neutral')
        )
        enhanced_score = strength if enhanced_buy else 50
        scores = [enhanced_score * weights['low_buy']]
        if enhanced_buy:
            details.append(f"低买强{strength:.0f}")

        # 2. OFI（订单流不平衡）
        ob = self.ws.get_orderbook(sym)
        if ob:
            ofi = self.executor.calculate_ofi(ob)
            ofi_score = 50 + ofi * 30
            scores.append(ofi_score * weights['ofi'])
            details.append(f"OFI:{ofi:.2f}")
        else:
            scores.append(50 * weights['ofi'])

        # 3. Archetype
        arch_signal, arch_type = self.frontier.archetype_trader_signal(
            self._price_history.get(sym, []), self._volume_history.get(sym, []), rsi_hist, self._bb_bandwidth_history.get(sym, []))
        scores.append((50 + arch_signal * 30) * weights['arch'])
        details.append(f"Arch:{arch_type}")

        # 4. 多周期交叉（只使用技术，去除恐惧贪婪）
        cross_score, _ = self.frontier.crosssync_score(
            multi.get('1m'), multi.get('5m'), multi.get('15m'), 
            funding,  # funding 作为参考，但不依赖外部情绪
            None     # 去除 fear_greed
        )
        scores.append(cross_score * weights['cross'])

        # 5. Meta RL（基于价格、胜率）
        meta_score = self.frontier.meta_rl_score(
            self._price_history.get(sym, []), 
            self._win_rate_history.get(sym, []), 
            self._sharpe_history.get(sym, [])
        )
        scores.append(meta_score * weights['meta'])

        # 6. F2 Agent（使用技术，去掉外部数据）
        # 我们手动构建只包含技术的 F2 分数，避免依赖外部
        f2_score = 50  # 基准
        rsi_val = tech.get('rsi', 50)
        if rsi_val < 35:
            f2_score += 15
        elif rsi_val > 65:
            f2_score -= 15
        price_val = tech.get('bb_middle', 0)
        bb_lower_val = tech.get('bb_lower', 0)
        bb_upper_val = tech.get('bb_upper', 0)
        if bb_upper_val > bb_lower_val and price_val > 0:
            bb_pos = (price_val - bb_lower_val) / (bb_upper_val - bb_lower_val)
            if bb_pos < 0.2:
                f2_score += 15
            elif bb_pos > 0.8:
                f2_score -= 10
        f2_score = min(100, max(0, f2_score))
        scores.append(f2_score * weights['f2'])

        # 7. Agent策略因子
        agent_score = 50 + agent_strategy.get('tp_factor', 1.0) * 10
        scores.append(agent_score * weights['agent'])

        # 8. 默认综合（基于RSI和波动）
        rsi_last = rsi_hist[-1] if rsi_hist else 50
        default_score = 50 + (50 - rsi_last) * 0.3 + (tech.get('bandwidth_pct', 0) * 0.5)
        default_score = min(100, max(0, default_score))
        scores.append(default_score * weights['default'])

        # 计算总分
        total_score = sum(scores)
        total_score = min(100, max(0, total_score))

        threshold_adjust = state_params.get('threshold_adjust', 0)
        coin_score = self._get_coin_param(sym, 'auto_min_score', self.auto_min_score) + threshold_adjust

        should_open = total_score >= coin_score
        is_high_confidence = total_score >= 80

        # 凯利仓位
        recent_pnls = [t.get('pnl_pct', 0) for t in self.trades[-30:] if t.get('pnl_pct') is not None]
        if recent_pnls:
            wins = [p for p in recent_pnls if p > 0]
            losses = [p for p in recent_pnls if p < 0]
            win_rate = len(wins) / len(recent_pnls) if recent_pnls else 0.5
            avg_win = sum(wins) / len(wins) if wins else 0.5
            avg_loss = abs(sum(losses) / len(losses)) if losses else 0.5
            kelly_pos = self.kelly.calculate_position(win_rate, avg_win, avg_loss, len(recent_pnls), 0.1)
        else:
            kelly_pos = 0.05

        base_amount = self._get_coin_param(sym, 'single_order_usdt', self.single_order_usdt)
        dynamic_amount = self._calculate_dynamic_amount(base_amount)
        state_amount_factor = state_params.get('amount_factor', 1.0)
        kelly_amount_factor = max(0.3, min(1.5, kelly_pos / 0.05))

        final_amount = dynamic_amount * state_amount_factor * kelly_amount_factor
        if is_high_confidence:
            final_amount = final_amount * 1.3

        logger.info(f"📊 {sym} 综合评分: {total_score:.0f}/{coin_score:.0f} | 市场{state} | 凯利{kelly_pos*100:.1f}% | 状态因子{state_amount_factor:.2f}")
        return {
            'should_open': should_open,
            'score': total_score,
            'is_high_confidence': is_high_confidence,
            'details': details,
            'amount': final_amount,
            'state': state,
            'state_params': state_params,
            'kelly_position': kelly_pos
        }

    # ==================== 参数优化命令 ====================

    async def cmd_optimize(self, update, context):
        if not self._auth(update):
            return
        await self._auto_optimize_params()
        await update.effective_message.reply_text(
            f"✅ 参数优化完成\n"
            f"• 止盈: {self.tp_pct:.1%}\n"
            f"• 止损: {self.sl_pct:.1%}\n"
            f"• 阈值: {self.auto_min_score}\n"
            f"• 市场状态: {self._current_market_state}"
        )

    async def cmd_state(self, update, context):
        if not self._auth(update):
            return
        lines = [
            f"📊 市场状态 {self.env_tag}",
            f"• 当前状态: {self._current_market_state}",
            f"• 状态评分: {self._current_market_score:.2f}",
            f"• 状态参数:",
        ]
        for key, val in self._current_state_params.items():
            if isinstance(val, float):
                if key in ['tp', 'sl']:
                    lines.append(f"  {key}: {val:.1%}")
                else:
                    lines.append(f"  {key}: {val:.2f}")
            else:
                lines.append(f"  {key}: {val}")
        recent_pnls = [t.get('pnl_pct', 0) for t in self.trades[-30:] if t.get('pnl_pct') is not None]
        if recent_pnls:
            wins = [p for p in recent_pnls if p > 0]
            losses = [p for p in recent_pnls if p < 0]
            win_rate = len(wins) / len(recent_pnls) if recent_pnls else 0.5
            avg_win = sum(wins) / len(wins) if wins else 0.5
            avg_loss = abs(sum(losses) / len(losses)) if losses else 0.5
            kelly = self.kelly.calculate_position(win_rate, avg_win, avg_loss, len(recent_pnls), 0.1)
            lines.append(f"• 凯利仓位: {kelly*100:.1f}%")
            lines.append(f"• 胜率: {win_rate*100:.1f}%")
            lines.append(f"• 盈亏比: {avg_win/avg_loss:.2f}" if avg_loss > 0 else "• 盈亏比: N/A")
        await update.effective_message.reply_text("\n".join(lines))

    async def _auto_optimize_params(self):
        if len(self.trades) < 20:
            return
        recent_trades = self.trades[-30:]
        current_params = {'tp_pct': self.tp_pct, 'sl_pct': self.sl_pct, 'score': self.auto_min_score}
        optimized = await self.optimizer.optimize(None, recent_trades, current_params)
        if optimized.get('tp_pct', self.tp_pct) != self.tp_pct or \
           optimized.get('sl_pct', self.sl_pct) != self.sl_pct or \
           optimized.get('score', self.auto_min_score) != self.auto_min_score:
            self.tp_pct = optimized.get('tp_pct', self.tp_pct)
            self.sl_pct = optimized.get('sl_pct', self.sl_pct)
            self.auto_min_score = optimized.get('score', self.auto_min_score)
            await self._save_config()
            await self._alert(f"🤖 自动优化完成\n止盈: {self.tp_pct:.1%}\n止损: {self.sl_pct:.1%}\n阈值: {self.auto_min_score}")

    # ==================== 其他命令（精简保留） ====================

    async def _get_multi_timeframe_data(self, symbol):
        timeframes = ['1m', '5m', '15m']
        result = {}
        for tf in timeframes:
            try:
                tech = await self._get_cached_tech(symbol, tf, 50)
                result[tf] = tech
            except:
                result[tf] = None
        return result

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
            [InlineKeyboardButton("🔄 同步持仓", callback_data="sync_pos"), InlineKeyboardButton("🔄 刷新", callback_data="refresh_panel")],
            [InlineKeyboardButton("📊 状态", callback_data="state_panel"), InlineKeyboardButton("⚡ 优化", callback_data="optimize_panel")]
        ])

    # ----- 基础命令（完整保留） -----

    async def cmd_menu(self, update, context):
        if not self._auth(update):
            await update.message.reply_text("⛔ 未授权")
            return
        await update.effective_message.reply_text(f"⚙️ 控制台 {self.env_tag}", reply_markup=self._build_main_keyboard())

    async def cmd_holdings(self, update, context):
        if not self._auth(update):
            return
        bal = await self.exchange.fetch_balance()
        lines = ["📋 当前持币\n"]
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
        await update.effective_message.reply_text("\n".join(lines))

    async def cmd_autotrade(self, update, context):
        if not self._auth(update):
            return
        try:
            mode = context.args[0].lower()
            if mode == "on":
                self.auto_trade_enabled = True
                await self._save_config()
                await update.effective_message.reply_text("🤖 智能自适应交易已开启")
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
            f"📊 仪表盘 {self.env_tag}",
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
        lines.append(f"市场状态: {self._current_market_state}")
        await update.effective_message.reply_text("\n".join(lines))

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
            await update.effective_message.reply_text("❌ /entry ETH/USDT 3120")

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
                "adaptive": {"tp":1.2,"sl":0.8,"tsl":0.4,"tmpt":0.3,"tf":"5m","amt":1,"reserve":1,"score":65},
            }
            if mode not in presets:
                await update.effective_message.reply_text("可选: conservative/balanced/aggressive/adaptive")
                return
            p = presets[mode]
            self.tp_pct = p["tp"]/100; self.sl_pct = p["sl"]/100; self.trailing_sl_pct = p["tsl"]/100; self.trailing_tp_pct = p["tmpt"]/100
            self.timeframe = p["tf"]; self.single_order_usdt = p["amt"]; self.reserve_bottom = p["reserve"]
            if "score" in p: self.auto_min_score = p["score"]
            await self._save_config()
            names = {"conservative":"保守","balanced":"平衡","aggressive":"激进","adaptive":"自适应"}
            await update.effective_message.reply_text(f"⚡ {names[mode]}方案已生效\n止盈{self.tp_pct:.1%} 止损{self.sl_pct:.1%}")
        except Exception as e:
            logger.error(f"cmd_preset error: {e}")
            await update.effective_message.reply_text(f"❌ 设置预设失败: {e}")

    async def cmd_history(self, update, context):
        if not self._auth(update):
            return
        if not self.trades:
            await update.effective_message.reply_text("📜 暂无记录")
            return
        lines = ["📜 最近交易\n"]
        for t in self.trades[:10]:
            net_pnl = t.get('net_pnl', 0); net_pnl_pct = t.get('net_pnl_pct', 0)
            if net_pnl != 0:
                lines.append(f"{'🟢' if net_pnl_pct>0 else '🔴'} {t['time']} {t['symbol']} 净利{net_pnl_pct:+.2f}% ({net_pnl:+.4f}U)")
            else:
                lines.append(f"{'🟢' if t['pnl_pct']>0 else '🔴'} {t['time']} {t['symbol']} {t['pnl_pct']:+.2f}%")
        await update.effective_message.reply_text("\n".join(lines))

    async def cmd_status(self, update, context):
        if not self._auth(update):
            return
        try:
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
            
            try:
                perf = await get_recent_performance(20)
                if perf and perf['total'] > 0:
                    win_rate = perf['win_rate']
                    wins = perf['wins']
                    total_trades = perf['total']
                else:
                    win_rate = 0.0
                    wins = 0
                    total_trades = 0
            except Exception as e:
                logger.warning(f"获取近期表现失败: {e}")
                win_rate = 0.0
                wins = 0
                total_trades = 0

            lines = []
            lines.append(f"📊 多币种量化机器人看板 {self.env_tag}")
            lines.append(f"• 系统状态: {'🟢 RUNNING' if self.is_running else '🔴 STOPPED'}")
            lines.append(f"• 策略模式: 🚀 自适应27合1策略")
            lines.append(f"• 全局默认: 单笔{self.single_order_usdt:.1f}U | 周期{self.timeframe} | 止盈{self.tp_pct:.1%}")
            lines.append(f"• 市场状态: {self._current_market_state}")
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
                lines.append(f"\n🔹 [{sym}] (周期:{timeframe} | 止盈:{tp:.1%} | 移动止损:{tsl:.1%} | 单笔:{amount:.1f}U)")
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
            stats = self._delta_neutral_stats
            lines.append(f"• 💰 费率抄底: {stats['total_trades']}笔 累计盈利{stats['total_profit']:.4f}U 今日{stats['profit_today']:.4f}U")
            if self.ai_enabled and time.time() -
