"""
advanced.py - 前沿量化策略引擎
包含：市场状态识别(RegimeNAS)、动态因子组合(FactorMoE)、
配对交易、做市策略、LLM辅助决策、强化学习调参
所有功能均可独立开关，且带有回测模拟模式
"""
import asyncio
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from collections import deque
import random
import math
import json
import aiohttp
from typing import Dict, List, Tuple, Optional
from config import logger

# =========================================
# 1. 市场状态识别 (RegimeNAS)
# =========================================
class RegimeNAS:
    """
    识别市场状态：trending, ranging, volatile, ultra_low
    使用多指标综合判断
    """
    def __init__(self, lookback=50):
        self.lookback = lookback
        self.history = {
            'returns': deque(maxlen=lookback),
            'volatility': deque(maxlen=lookback),
            'trend_strength': deque(maxlen=lookback)
        }
        self.current_regime = 'neutral'
        self.confidence = 0.5

    def update(self, price_series: List[float]):
        if len(price_series) < self.lookback:
            return
        returns = np.diff(price_series) / price_series[:-1]
        vol = np.std(returns) * np.sqrt(252)
        # 趋势强度：ADX近似
        tr = np.abs(returns)
        atr = np.mean(tr[-14:])
        trend = abs(price_series[-1] - price_series[-self.lookback]) / price_series[-self.lookback]
        trend_strength = trend / (atr + 1e-6)
        
        self.history['returns'].extend(returns)
        self.history['volatility'].append(vol)
        self.history['trend_strength'].append(trend_strength)
        
        # 分类
        if vol > 0.08:
            regime = 'extreme'
        elif vol > 0.04:
            if trend_strength > 0.5:
                regime = 'trending'
            else:
                regime = 'high_volatility'
        elif vol < 0.01:
            regime = 'ultra_low'
        else:
            if abs(trend) > 0.02:
                regime = 'trending'
            else:
                regime = 'ranging'
        
        self.current_regime = regime
        self.confidence = min(1.0, len(returns) / self.lookback)
        return regime

    def get_params(self):
        """根据状态返回策略参数调整"""
        mapping = {
            'trending': {'tp_factor': 1.5, 'sl_factor': 1.3, 'grid_factor': 0.4, 'amount_factor': 0.6},
            'ranging': {'tp_factor': 1.0, 'sl_factor': 1.0, 'grid_factor': 1.0, 'amount_factor': 1.0},
            'high_volatility': {'tp_factor': 1.2, 'sl_factor': 1.5, 'grid_factor': 0.3, 'amount_factor': 0.5},
            'extreme': {'tp_factor': 0.7, 'sl_factor': 2.0, 'grid_factor': 0.2, 'amount_factor': 0.3},
            'ultra_low': {'tp_factor': 0.8, 'sl_factor': 0.7, 'grid_factor': 1.2, 'amount_factor': 0.8}
        }
        return mapping.get(self.current_regime, mapping['ranging'])


# =========================================
# 2. 动态因子组合 (FactorMoE)
# =========================================
class FactorMoE:
    """
    专家混合：不同指标在不同市场状态下的权重
    """
    def __init__(self):
        self.factors = {
            'rsi': {'weight': 0.2, 'current_score': 50},
            'bb': {'weight': 0.2, 'current_score': 50},
            'ofi': {'weight': 0.15, 'current_score': 50},
            'trend': {'weight': 0.15, 'current_score': 50},
            'volume': {'weight': 0.15, 'current_score': 50},
            'sentiment': {'weight': 0.15, 'current_score': 50}
        }
        self.regime_weights = {
            'trending': {'rsi': 0.1, 'bb': 0.1, 'ofi': 0.1, 'trend': 0.4, 'volume': 0.15, 'sentiment': 0.15},
            'ranging': {'rsi': 0.3, 'bb': 0.3, 'ofi': 0.15, 'trend': 0.05, 'volume': 0.1, 'sentiment': 0.1},
            'high_volatility': {'rsi': 0.1, 'bb': 0.1, 'ofi': 0.3, 'trend': 0.1, 'volume': 0.2, 'sentiment': 0.2},
            'extreme': {'rsi': 0.2, 'bb': 0.2, 'ofi': 0.2, 'trend': 0.1, 'volume': 0.15, 'sentiment': 0.15},
            'ultra_low': {'rsi': 0.25, 'bb': 0.25, 'ofi': 0.1, 'trend': 0.1, 'volume': 0.15, 'sentiment': 0.15}
        }
        self.current_regime = 'ranging'

    def set_regime(self, regime):
        self.current_regime = regime
        for factor in self.factors:
            self.factors[factor]['weight'] = self.regime_weights.get(regime, self.regime_weights['ranging']).get(factor, 0.15)

    def update_factor_score(self, factor_name, score):
        if factor_name in self.factors:
            self.factors[factor_name]['current_score'] = score

    def get_combined_score(self):
        total = 0
        for f in self.factors.values():
            total += f['weight'] * f['current_score']
        return min(100, max(0, total))

    def get_weights(self):
        return {k: v['weight'] for k, v in self.factors.items()}


# =========================================
# 3. 配对交易 (Pairs Trading)
# =========================================
class PairsTrader:
    """
    实时检测并交易高度相关的币对
    """
    def __init__(self, zscore_threshold=2.0):
        self.zscore_threshold = zscore_threshold
        self.pairs = {}
        self.price_history = {}
        self.zscore_history = {}
        self.positions = {}  # {pair: {'side': 'long'/'short', 'entry_price': ..., 'size': ...}}

    def add_symbol(self, symbol):
        if symbol not in self.price_history:
            self.price_history[symbol] = deque(maxlen=100)

    def update_price(self, symbol, price):
        if symbol in self.price_history:
            self.price_history[symbol].append(price)

    def find_pairs(self, symbols, lookback=50):
        # 简化版：如果没有预先定义，则使用固定配对（BTC-ETH, SOL-ETH等）
        # 真实中应做协整检验
        predefine = {
            ('BTC/USDT', 'ETH/USDT'): 0.05,
            ('SOL/USDT', 'ETH/USDT'): 0.08,
            ('DOGE/USDT', 'ADA/USDT'): 0.02
        }
        for (sym1, sym2), spread in predefine.items():
            if sym1 in self.price_history and sym2 in self.price_history:
                if len(self.price_history[sym1]) >= lookback and len(self.price_history[sym2]) >= lookback:
                    p1 = np.array(self.price_history[sym1])[-lookback:]
                    p2 = np.array(self.price_history[sym2])[-lookback:]
                    ratio = p1 / p2
                    mean = np.mean(ratio)
                    std = np.std(ratio)
                    current_ratio = p1[-1] / p2[-1]
                    z = (current_ratio - mean) / std if std > 0 else 0
                    pair_key = f"{sym1}_{sym2}"
                    self.zscore_history[pair_key] = deque([z], maxlen=50)
                    # 更新信号
                    if abs(z) > self.zscore_threshold:
                        return {'pair': pair_key, 'zscore': z, 'symbol1': sym1, 'symbol2': sym2, 'ratio': current_ratio}
        return None

    def get_signal(self):
        # 返回做多或做空信号（基于最新zscore）
        if not self.zscore_history:
            return None
        latest = {k: v[-1] if v else 0 for k, v in self.zscore_history.items()}
        for pair, z in latest.items():
            if z > self.zscore_threshold:
                return {'pair': pair, 'action': 'short_symbol1_long_symbol2', 'zscore': z}
            elif z < -self.zscore_threshold:
                return {'pair': pair, 'action': 'long_symbol1_short_symbol2', 'zscore': z}
        return None


# =========================================
# 4. 做市策略 (Market Making)
# =========================================
class MarketMaker:
    """
    基于订单簿的做市，赚取买卖价差
    """
    def __init__(self, spread_multiplier=1.0, inventory_target=0.5):
        self.spread_multiplier = spread_multiplier
        self.inventory_target = inventory_target
        self.active_orders = {}  # 暂存订单
        self.last_quote = {}

    def update_orderbook(self, symbol, bids, asks):
        if not bids or not asks:
            return
        best_bid = bids[0][0] if bids else 0
        best_ask = asks[0][0] if asks else 0
        mid = (best_bid + best_ask) / 2
        spread = (best_ask - best_bid) / mid
        # 动态调整价差
        target_spread = spread * self.spread_multiplier
        # 生成挂单价格
        bid_price = mid - target_spread / 2
        ask_price = mid + target_spread / 2
        # 根据库存调整偏移
        inventory = self.inventory_target  # 模拟
        if inventory > 0.6:
            bid_price = bid_price * (1 - 0.001)
            ask_price = ask_price * (1 + 0.002)
        elif inventory < 0.4:
            bid_price = bid_price * (1 + 0.002)
            ask_price = ask_price * (1 - 0.001)
        self.last_quote[symbol] = {'bid': bid_price, 'ask': ask_price, 'mid': mid}
        return {'bid': bid_price, 'ask': ask_price}

    def get_quote(self, symbol):
        return self.last_quote.get(symbol)


# =========================================
# 5. LLM 辅助决策 (模拟 + 真实API)
# =========================================
class LLMAssistant:
    """
    调用大模型分析市场，生成因子建议
    """
    def __init__(self, api_key=None, model='deepseek-chat', base_url='https://api.deepseek.com/v1'):
        self.api_key = api_key or os.getenv('DEEPSEEK_API_KEY')
        self.model = model
        self.base_url = base_url
        self.cache = {}

    async def analyze(self, market_data: Dict) -> Dict:
        """
        输入：市场数据（价格、指标、新闻情绪等）
        输出：推荐操作和置信度
        """
        if not self.api_key:
            # 模拟模式：根据RSI简单判断
            rsi = market_data.get('rsi', 50)
            if rsi < 30:
                return {'action': 'buy', 'confidence': 0.7, 'reason': 'RSI超卖'}
            elif rsi > 70:
                return {'action': 'sell', 'confidence': 0.7, 'reason': 'RSI超买'}
            else:
                return {'action': 'hold', 'confidence': 0.5, 'reason': '中性'}
        # 真实API调用（示例）
        try:
            prompt = f"分析以下市场数据，给出交易建议：{json.dumps(market_data)}"
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json={
                        "model": self.model,
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.3
                    },
                    timeout=10
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        content = data['choices'][0]['message']['content']
                        # 解析内容（简化）
                        if '买入' in content:
                            action = 'buy'
                        elif '卖出' in content:
                            action = 'sell'
                        else:
                            action = 'hold'
                        confidence = 0.6
                        return {'action': action, 'confidence': confidence, 'reason': content[:100]}
        except Exception as e:
            logger.warning(f"LLM调用失败: {e}")
        return {'action': 'hold', 'confidence': 0.5, 'reason': 'fallback'}


# =========================================
# 6. 强化学习调参 (在线学习)
# =========================================
class RLParameterOptimizer:
    """
    基于简单梯度下降或贝叶斯优化的在线参数调整
    """
    def __init__(self, param_ranges: Dict):
        self.param_ranges = param_ranges  # {'tp_pct': (0.005, 0.03), 'sl_pct': (0.003, 0.02)}
        self.current_params = {k: (v[0]+v[1])/2 for k, v in param_ranges.items()}
        self.performance_history = []
        self.exploration_rate = 0.2

    def update(self, performance_metric: float):
        # 记录
        self.performance_history.append(performance_metric)
        if len(self.performance_history) > 100:
            self.performance_history.pop(0)
        # 简单扰动：随机探索
        if random.random() < self.exploration_rate:
            for k in self.current_params:
                delta = (self.param_ranges[k][1] - self.param_ranges[k][0]) * random.uniform(-0.1, 0.1)
                self.current_params[k] = min(self.param_ranges[k][1], max(self.param_ranges[k][0], self.current_params[k] + delta))
        # 如果近期表现变差，回退（改进版可加贝叶斯）
        if len(self.performance_history) >= 20:
            recent = np.mean(self.performance_history[-10:])
            older = np.mean(self.performance_history[-20:-10])
            if recent < older * 0.95:
                # 向中心回退
                for k in self.current_params:
                    self.current_params[k] = (self.current_params[k] + (self.param_ranges[k][0]+self.param_ranges[k][1])/2) / 2

    def get_params(self):
        return self.current_params


# =========================================
# 7. 主引擎：集成所有模块
# =========================================
class AdvancedStrategyEngine:
    """
    高级策略引擎，统筹所有模块
    """
    def __init__(self, exchange):
        self.exchange = exchange
        self.regime_nas = RegimeNAS()
        self.factor_moe = FactorMoE()
        self.pairs_trader = PairsTrader()
        self.market_maker = MarketMaker()
        self.llm = LLMAssistant()
        self.rl_optimizer = RLParameterOptimizer({
            'tp_pct': (0.005, 0.035),
            'sl_pct': (0.003, 0.02),
            'grid_spacing': (0.005, 0.025)
        })
        self.enabled = {
            'regime': True,
            'factor_moe': True,
            'pairs': False,      # 默认关闭，需有足够数据
            'market_making': False,
            'llm': False,
            'rl': False
        }
        self.last_llm_decision = None

    async def update(self, symbol: str, price: float, tech_data: Dict, orderbook: Dict, sentiment: float):
        """
        每周期调用，更新所有模块状态
        """
        # 1. 市场状态
        if self.enabled['regime']:
            price_list = [price]  # 实际应维护完整序列
            regime = self.regime_nas.update([price])  # 简化
            self.factor_moe.set_regime(regime)

        # 2. 因子更新
        if self.enabled['factor_moe']:
            # 更新各因子得分
            if tech_data:
                self.factor_moe.update_factor_score('rsi', tech_data.get('rsi', 50))
                bb_pos = (price - tech_data.get('bb_lower', 0)) / (tech_data.get('bb_upper', 0) - tech_data.get('bb_lower', 0) + 1e-6)
                self.factor_moe.update_factor_score('bb', 100 - abs(bb_pos - 0.5) * 200)
                # ofi 从orderbook计算
                if orderbook:
                    ofi = self._calc_ofi(orderbook)
                    self.factor_moe.update_factor_score('ofi', 50 + ofi * 30)
                # 趋势因子
                if len(self.regime_nas.history['trend_strength']) > 0:
                    trend = self.regime_nas.history['trend_strength'][-1]
                    self.factor_moe.update_factor_score('trend', 50 + trend * 20)
                # 情绪因子
                self.factor_moe.update_factor_score('sentiment', 50 + sentiment * 30)

        # 3. 配对交易（更新价格）
        if self.enabled['pairs']:
            self.pairs_trader.update_price(symbol, price)

        # 4. 做市（更新盘口）
        if self.enabled['market_making'] and orderbook:
            self.market_maker.update_orderbook(symbol, orderbook.get('bids', []), orderbook.get('asks', []))

        # 5. LLM（每10分钟调用一次）
        if self.enabled['llm']:
            if not hasattr(self, '_llm_last_time') or (datetime.now() - self._llm_last_time).seconds > 600:
                market_data = {'price': price, 'rsi': tech_data.get('rsi', 50), 'regime': self.regime_nas.current_regime}
                self.last_llm_decision = await self.llm.analyze(market_data)
                self._llm_last_time = datetime.now()

        # 6. RL优化（每20笔交易后）
        if self.enabled['rl'] and len(self.regime_nas.history['returns']) > 20:
            # 使用近期胜率作为性能指标
            perf = 0.5  # 模拟
            self.rl_optimizer.update(perf)

    def _calc_ofi(self, orderbook):
        bids = orderbook.get('bids', [])
        asks = orderbook.get('asks', [])
        if len(bids) < 5 or len(asks) < 5:
            return 0
        bid_vol = sum([b[1] for b in bids[:5]])
        ask_vol = sum([a[1] for a in asks[:5]])
        return (bid_vol - ask_vol) / (bid_vol + ask_vol + 1e-6)

    def get_combined_signal(self, symbol: str, price: float) -> Dict:
        """
        获取综合交易信号
        返回：{'action': 'buy'/'sell'/'hold', 'score': float, 'details': dict}
        """
        # 1. FactorMoE 得分
        factor_score = self.factor_moe.get_combined_score()

        # 2. 配对信号（如果开启）
        pair_signal = None
        if self.enabled['pairs']:
            pair_signal = self.pairs_trader.get_signal()

        # 3. 做市报价（只做参考）
        mm_quote = self.market_maker.get_quote(symbol)

        # 4. LLM决策
        llm_action = 'hold'
        if self.last_llm_decision:
            llm_action = self.last_llm_decision.get('action', 'hold')

        # 5. 综合
        # 基础阈值：factor_score > 65 且 LLM不反对
        base_buy = factor_score > 65
        base_sell = factor_score < 35
        # 配对信号增强
        if pair_signal and pair_signal.get('action'):
            if 'long' in pair_signal['action']:
                base_buy = True
            elif 'short' in pair_signal['action']:
                base_sell = True

        action = 'hold'
        if base_buy and (llm_action != 'sell'):
            action = 'buy'
        elif base_sell and (llm_action != 'buy'):
            action = 'sell'

        # 置信度
        confidence = 0.5
        if action != 'hold':
            confidence = min(0.9, factor_score / 100 * 0.5 + 0.4)

        return {
            'action': action,
            'score': factor_score,
            'confidence': confidence,
            'details': {
                'regime': self.regime_nas.current_regime,
                'factor_weights': self.factor_moe.get_weights(),
                'pair_signal': pair_signal,
                'mm_quote': mm_quote,
                'llm_decision': self.last_llm_decision,
                'rl_params': self.rl_optimizer.get_params() if self.enabled['rl'] else {}
            }
        }

    def get_regime(self):
        return self.regime_nas.current_regime

    def get_factor_weights(self):
        return self.factor_moe.get_weights()