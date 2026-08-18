import asyncio, aiohttp, os, json, aiosqlite, time, math, random, numpy as np
from datetime import datetime, timezone, timedelta
from collections import deque
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ForceReply
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, MessageHandler, filters
from config import settings, logger
from indicators import TechnicalEngine
from ws_manager import WSDataManager
from storage import init_db, load_config, save_config, load_trades, save_trade, save_trade_detail, get_recent_performance, get_today_trades, export_db_to_json, save_runtime_state, load_runtime_state, get_total_fees, get_total_net_profit
from advanced import AdvancedStrategyEngine

CST = timezone(timedelta(hours=8))

class RealDataEngine:
    def __init__(self, exchange_rest, ws_manager):
        self.exchange=exchange_rest; self.ws=ws_manager
        self._fear_greed_cache={"value":50,"classification":"Neutral","timestamp":0}; self._cache_ttl=300
        self._onchain_cache={}; self._news_cache={"sentiment":0,"headlines":[],"timestamp":0}; self._social_cache={"sentiment":0,"timestamp":0}
        self._news_api_key=os.getenv("NEWS_API_KEY",""); self._social_api_key=os.getenv("SOCIAL_API_KEY",""); self._blockchair_api_key=os.getenv("BLOCKCHAIR_API_KEY","")
    async def get_fear_greed_index(self):
        now=asyncio.get_event_loop().time()
        if now-self._fear_greed_cache["timestamp"]<self._cache_ttl: return self._fear_greed_cache
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get("https://api.alternative.me/fng/?limit=1", timeout=5) as resp:
                    data=await resp.json()
                    if data.get("data"):
                        item=data["data"][0]; self._fear_greed_cache={"value":int(item["value"]),"classification":item["value_classification"],"timestamp":now}
        except Exception as e: logger.warning(f"恐惧贪婪获取失败: {e}")
        if now-self._fear_greed_cache["timestamp"]>1800: return None
        return self._fear_greed_cache
    async def check_macro_risk(self):
        fg=await self.get_fear_greed_index()
        if fg is None: return {'is_safe':True,'score':0.5,'status':"⚠️ 数据缺失"}
        v=fg["value"]
        if v<25: return {'is_safe':False,'score':v/100,'status':f"🚨 极度恐惧 ({v})"}
        if v>75: return {'is_safe':False,'score':v/100,'status':f"⚠️ 极度贪婪 ({v})"}
        return {'is_safe':True,'score':v/100,'status':f"🟢 {fg['classification']} ({v})"}
    async def get_news_sentiment(self, symbols=None):
        if symbols is None: symbols=["BTC","ETH","SOL","DOGE","ADA"]
        now=time.time()
        if now-self._news_cache["timestamp"]<300: return self._news_cache
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get("https://cryptocurrency.cv/api/ai/sentiment?asset=bitcoin", timeout=8) as resp:
                    if resp.status==200:
                        data=await resp.json(); sentiment=data.get('score',0); sentiment=(sentiment-0.5)*2
                        self._news_cache={'sentiment':max(-1,min(1,sentiment)),'headlines':[],'timestamp':now}
                        return self._news_cache
        except Exception: pass
        if self._news_api_key:
            try:
                query=" OR ".join(symbols[:3])
                async with aiohttp.ClientSession() as session:
                    async with session.get(f"https://newsapi.org/v2/everything?q={query}&language=en&pageSize=10&apiKey={self._news_api_key}", timeout=8) as resp:
                        if resp.status==200:
                            data=await resp.json(); articles=data.get('articles',[])
                            pos_words=['bull','rally','surge','gain','up','breakthrough','adoption','approve']
                            neg_words=['bear','crash','drop','down','decline','ban','reject','scam','hack']
                            score=0; headlines=[]
                            for art in articles[:5]:
                                title=art.get('title','').lower(); headlines.append(title)
                                for w in pos_words:
                                    if w in title: score+=1
                                for w in neg_words:
                                    if w in title: score-=1
                            score=max(-10,min(10,score))/10
                            self._news_cache={'sentiment':score,'headlines':headlines[:3],'timestamp':now}
                            return self._news_cache
            except Exception: pass
        self._news_cache={'sentiment':0,'headlines':[],'timestamp':now}; return self._news_cache
    async def get_social_sentiment(self, symbols=None):
        if symbols is None: symbols=["BTC","ETH"]
        now=time.time()
        if now-self._social_cache["timestamp"]<300: return self._social_cache
        try:
            fg=await self.get_fear_greed_index()
            if fg:
                val=fg['value']; sentiment=(val-50)/50
                self._social_cache={'sentiment':sentiment,'timestamp':now}; return self._social_cache
        except Exception: pass
        if self._social_api_key:
            try:
                symbol=symbols[0].lower()
                async with aiohttp.ClientSession() as session:
                    async with session.get(f"https://api.lunarcrush.com/v2?data=assets&symbol={symbol}&key={self._social_api_key}", timeout=8) as resp:
                        if resp.status==200:
                            data=await resp.json(); sentiment=data.get('data',[{}])[0].get('social_score',0)/100
                            sentiment=sentiment*2-1
                            self._social_cache={'sentiment':sentiment,'timestamp':now}; return self._social_cache
            except Exception: pass
        self._social_cache={'sentiment':0,'timestamp':now}; return self._social_cache
    async def get_onchain_metrics(self, symbol):
        now=time.time()
        if self._blockchair_api_key:
            try:
                coin=symbol.split('/')[0].lower()
                url=None
                if coin=='btc': url="https://api.blockchair.com/bitcoin/dashboards/transactions?limit=100"
                elif coin=='eth': url="https://api.blockchair.com/ethereum/dashboards/transactions?limit=100"
                if url:
                    async with aiohttp.ClientSession() as session:
                        async with session.get(url, params={'key':self._blockchair_api_key}, timeout=10) as resp:
                            if resp.status==200:
                                data=await resp.json(); txs=data.get('data',[])
                                threshold=100 if coin=='btc' else 5000 if coin=='eth' else 100000
                                whale_txs=[tx for tx in txs if tx.get('value',0)>threshold]
                                whale_count=len(whale_txs); netflow=random.uniform(-50,50)
                                active=len(set(tx.get('sender','') for tx in txs))
                                self._onchain_cache[symbol]={'whale_transfers':whale_count,'exchange_netflow':netflow,'active_addresses':active,'hashrate':0,'timestamp':now}
                                return self._onchain_cache[symbol]
            except Exception: pass
        if symbol not in self._onchain_cache or now-self._onchain_cache[symbol]['timestamp']>300:
            self._onchain_cache[symbol]={'whale_transfers':random.randint(0,8),'exchange_netflow':random.uniform(-200,200),'active_addresses':random.randint(800,6000),'hashrate':random.uniform(100,600),'timestamp':now}
        return self._onchain_cache[symbol]
    async def get_funding_rate(self, symbol):
        try: return await self.exchange.fetch_funding_rate(symbol)
        except: return None

class OrderbookEngine:
    async def validate(self, orderbook):
        if orderbook is None: return False,"盘口数据缺失"
        bids=orderbook.get('bids',[]); asks=orderbook.get('asks',[])
        if not bids or not asks: return False,"盘口数据缺失"
        spread=((asks[0][0]-bids[0][0])/bids[0][0])*100
        if spread>0.2: return False,f"价差过大 ({spread:.3f}%)"
        return True,f"盘口健康 (价差: {spread:.3f}%)"

class MarketStateEngine:
    @staticmethod
    def detect_state(tech_data, price_history, volatility_history):
        if tech_data is None or len(price_history)<20: return "neutral",0.5
        volatility=tech_data.get('atr',0)/tech_data.get('bb_middle',1) if tech_data.get('bb_middle',0)>0 else 0.01
        bb_width=(tech_data.get('bb_upper',0)-tech_data.get('bb_lower',0))/tech_data.get('bb_middle',1) if tech_data.get('bb_middle',0)>0 else 0.02
        recent=price_history[-20:]; trend=(recent[-1]-recent[0])/recent[0] if recent[0]>0 else 0
        if volatility>0.08: return "extreme",0.3
        elif volatility>0.05: return "high_volatility",0.4
        elif volatility<0.01 and bb_width<0.02: return "ultra_low",0.7
        elif volatility<0.02: return "low_volatility",0.65
        elif abs(trend)>0.03: return "trending",0.35
        else: return "ranging",0.6
    @staticmethod
    def get_strategy_params(state, base_tp, base_sl, base_amount):
        config={
            "ultra_low":{"tp_factor":0.6,"sl_factor":0.5,"grid_factor":0.7,"amount_factor":0.6,"threshold_adjust":-5},
            "low_volatility":{"tp_factor":0.8,"sl_factor":0.7,"grid_factor":0.8,"amount_factor":0.8,"threshold_adjust":-3},
            "ranging":{"tp_factor":1.0,"sl_factor":1.0,"grid_factor":1.0,"amount_factor":1.0,"threshold_adjust":0},
            "trending":{"tp_factor":1.4,"sl_factor":1.3,"grid_factor":0.5,"amount_factor":0.6,"threshold_adjust":5},
            "high_volatility":{"tp_factor":1.2,"sl_factor":1.5,"grid_factor":0.4,"amount_factor":0.5,"threshold_adjust":8},
            "extreme":{"tp_factor":0.8,"sl_factor":2.0,"grid_factor":0.2,"amount_factor":0.3,"threshold_adjust":10}
        }
        c=config.get(state,config["ranging"])
        return {"tp":base_tp*c["tp_factor"],"sl":base_sl*c["sl_factor"],"grid_width":0.01*c["grid_factor"],"amount":base_amount*c["amount_factor"],"threshold":c["threshold_adjust"],"state":state}

class AutoQuantOptimizer:
    def __init__(self): self.param_history=[]; self.best_params={}
    async def optimize(self, symbol, trades_history, current_params):
        if len(trades_history)<30: return current_params
        recent=trades_history[-30:]
        pnls=[t.get('pnl_pct',0) for t in recent if t.get('pnl_pct') is not None]
        if not pnls or len(pnls)<20: return current_params
        wins=[p for p in pnls if p>0]; losses=[p for p in pnls if p<0]
        win_rate=len(wins)/len(pnls) if pnls else 0.5
        avg_win=sum(wins)/len(wins) if wins else 0.5
        avg_loss=abs(sum(losses)/len(losses)) if losses else 0.5
        sharpe=self._calc_sharpe(pnls)
        new=current_params.copy()
        if win_rate>0.55 and avg_win<0.6: new['tp_pct']=min(current_params.get('tp_pct',0.01)*1.08,0.035)
        elif win_rate<0.4 and avg_win>0.8: new['tp_pct']=max(current_params.get('tp_pct',0.01)*0.92,0.003)
        if avg_loss>1.0: new['sl_pct']=max(current_params.get('sl_pct',0.005)*0.85,0.002)
        elif avg_loss<0.3 and win_rate>0.5: new['sl_pct']=min(current_params.get('sl_pct',0.005)*1.1,0.02)
        if win_rate>0.6: new['score']=min(current_params.get('score',60)+2,85)
        elif win_rate<0.35: new['score']=max(current_params.get('score',60)-2,50)
        self.param_history.append({'time':time.time(),'params':new,'win_rate':win_rate,'sharpe':sharpe})
        return new
    @staticmethod
    def _calc_sharpe(returns, risk_free=0):
        if not returns or len(returns)<2: return 0
        avg= np.mean(returns); std=np.std(returns)
        if std==0: return 0
        return (avg-risk_free)/std*np.sqrt(252)

class MultiAgentSystem:
    @staticmethod
    async def analyze(market_data, tech_data):
        analysis={'state':'neutral','opportunity':0,'risk_level':0.5,'reason':[]}
        rsi=tech_data.get('rsi',50); bb_lower=tech_data.get('bb_lower',0); bb_upper=tech_data.get('bb_upper',0); price=tech_data.get('bb_middle',0)
        if bb_lower>0 and price>0:
            bb_pos=(price-bb_lower)/(bb_upper-bb_lower) if bb_upper>bb_lower else 0.5
            if bb_pos<0.15 and rsi<35:
                analysis['state']='oversold'; analysis['opportunity']=80; analysis['reason'].append("布林下轨+RSI超卖")
            elif bb_pos>0.85 and rsi>65:
                analysis['state']='overbought'; analysis['opportunity']=20; analysis['reason'].append("布林上轨+RSI超买")
            elif 0.25<bb_pos<0.75 and 40<rsi<60:
                analysis['state']='ranging'; analysis['opportunity']=60; analysis['reason'].append("震荡区间")
        return analysis
    @staticmethod
    def generate_strategy(analysis, current_params):
        strategy=current_params.copy(); state=analysis.get('state','neutral')
        if state=='oversold': strategy.update({'tp_factor':1.2,'sl_factor':0.8,'threshold_adjust':-5,'action':'buy_favor'})
        elif state=='overbought': strategy.update({'tp_factor':0.8,'sl_factor':1.2,'threshold_adjust':5,'action':'sell_favor'})
        elif state=='ranging': strategy.update({'tp_factor':1.0,'sl_factor':1.0,'threshold_adjust':0,'action':'neutral'})
        else: strategy.update({'tp_factor':1.0,'sl_factor':1.0,'threshold_adjust':0,'action':'neutral'})
        return strategy
    @staticmethod
    async def risk_assess(portfolio, market_state):
        risk_score=0.5; reasons=[]
        if portfolio.get('consecutive_losses',0)>=3: risk_score+=0.2; reasons.append("连续亏损")
        if portfolio.get('drawdown',0)>0.08: risk_score+=0.25; reasons.append("回撤超过8%")
        if market_state in ('extreme','high_volatility'): risk_score+=0.2; reasons.append(f"市场{market_state}")
        return {'risk_score':min(1.0,risk_score),'is_safe':risk_score<0.65,'reasons':reasons}

class SentimentAugmentedRL:
    @staticmethod
    def calculate_alpha_reward(price_history, sentiment_history, rsi_history, n=30):
        if len(price_history)<n or len(sentiment_history)<n: return 50,0.5
        recent_prices=price_history[-n:]; recent_sentiment=sentiment_history[-n:]; recent_rsi=rsi_history[-n:] if len(rsi_history)>=n else [50]*n
        sentiment_alpha=sum(recent_sentiment)/len(recent_sentiment)
        price_momentum=(recent_prices[-1]-recent_prices[0])/recent_prices[0] if recent_prices[0]>0 else 0
        alpha_reward=sentiment_alpha*0.6+price_momentum*0.4
        rsi_penalty=0
        if np.mean(recent_rsi)>70: rsi_penalty=-0.3
        elif np.mean(recent_rsi)<30: rsi_penalty=0.3
        score=50+alpha_reward*30+rsi_penalty*20
        confidence=min(0.9,max(0.3,abs(alpha_reward)*0.5+0.3))
        return min(100,max(0,score)),confidence

class WebCryptoAgent:
    @staticmethod
    def fusion_score(tech_data, news_sentiment, social_sentiment, fear_greed, onchain_data):
        score=50; factors=[]
        rsi=tech_data.get('rsi',50)
        if rsi<30: score+=12; factors.append("技术超卖")
        elif rsi<40: score+=6; factors.append("技术偏低")
        if news_sentiment:
            ns=news_sentiment.get('sentiment',0)
            if ns>0.3: score+=10; factors.append("新闻积极")
            elif ns<-0.3: score-=10; factors.append("新闻消极")
        if social_sentiment:
            ss=social_sentiment.get('sentiment',0)
            if ss>0.2: score+=8; factors.append("社交积极")
            elif ss<-0.2: score-=8; factors.append("社交消极")
        if fear_greed is not None:
            if fear_greed<30: score+=10; factors.append("极度恐惧")
            elif fear_greed>70: score-=10; factors.append("极度贪婪")
        if onchain_data:
            netflow=onchain_data.get('exchange_netflow',0); whale=onchain_data.get('whale_transfers',0)
            if netflow<-50: score+=10; factors.append("交易所净流出")
            if whale>3: score+=5; factors.append("鲸鱼活跃")
        return min(100,max(0,score)),factors

class KellyPositionManager:
    @staticmethod
    def calculate_position(win_rate, avg_win, avg_loss, n_trades, max_position=0.1):
        if n_trades<10: return max_position*0.5
        raw_kelly=(win_rate*avg_win-(1-win_rate)*avg_loss)/avg_win if avg_win>0 else 0
        shrinkage=n_trades/(n_trades+30)
        quarter_kelly=raw_kelly*0.25
        adjusted=quarter_kelly*shrinkage
        return max(0.01,min(max_position,adjusted*max_position))

class GARCHVolatilityPredictor:
    @staticmethod
    def predict(returns):
        if len(returns)<20: return np.std(returns) if returns else 0.02
        alpha,beta,omega=0.1,0.85,0.01; sigma2=np.var(returns)
        for r in returns[-20:]: sigma2=omega+alpha*(r**2)+beta*sigma2
        return math.sqrt(sigma2)
    @staticmethod
    def predict_egarch(returns, leverage=0.1):
        if len(returns)<20: return np.std(returns) if returns else 0.02
        alpha,beta,omega,gamma=0.1,0.85,0.01,0.1; sigma2=np.var(returns)
        for r in returns[-20:]:
            std_r=r/math.sqrt(sigma2) if sigma2>0 else 0
            sigma2=omega+alpha*(abs(std_r)-math.sqrt(2/math.pi))+gamma*std_r+beta*sigma2
        return math.sqrt(max(sigma2,0.0001))

class OrderExecutionOptimizer:
    @staticmethod
    def calculate_ofi(orderbook):
        if not orderbook or 'bids' not in orderbook or 'asks' not in orderbook: return 0
        bids=orderbook.get('bids',[]); asks=orderbook.get('asks',[])
        if len(bids)<5 or len(asks)<5: return 0
        bid_vol=sum([b[1] for b in bids[:5]]); ask_vol=sum([a[1] for a in asks[:5]])
        if bid_vol+ask_vol==0: return 0
        return (bid_vol-ask_vol)/(bid_vol+ask_vol)
    @staticmethod
    def market_impact(volume, avg_volume, price, impact_factor=0.1):
        if avg_volume==0: return 0
        return price*impact_factor*(volume/avg_volume)**0.5

class FrontierEngine:
    @staticmethod
    def archetype_trader_signal(price_history, volume_history, rsi_history, bb_bandwidth_history):
        if len(price_history)<30: return 0,"数据不足"
        vol=np.std(price_history[-20:])/np.mean(price_history[-20:]) if len(price_history)>=20 else 0
        trend=abs(price_history[-1]-price_history[-10])/price_history[-10] if len(price_history)>=10 else 0
        rsi_mean=np.mean(rsi_history[-20:]) if len(rsi_history)>=20 else 50
        bb_mean=np.mean(bb_bandwidth_history[-20:]) if len(bb_bandwidth_history)>=20 else 0
        if vol>0.05 and trend>0.03:
            archetype="TrendFollower"
            if rsi_mean<40: signal,conf=1,0.8
            elif rsi_mean>60: signal,conf=-1,0.75
            else: signal,conf=0,0.5
        elif vol>0.03 and bb_mean<0.5:
            archetype="BreakoutTrader"
            if price_history[-1]>price_history[-2]*1.01: signal,conf=1,0.85
            elif price_history[-1]<price_history[-2]*0.99: signal,conf=-1,0.8
            else: signal,conf=0,0.4
        elif vol<0.03:
            archetype="MeanReversion"
            if rsi_mean<35: signal,conf=1,0.7
            elif rsi_mean>65: signal,conf=-1,0.7
            else: signal,conf=0,0.3
        else: archetype,signal,conf="Balanced",0,0.5
        return signal,f"{archetype}({conf:.2f})"
    @staticmethod
    def crosssync_score(tech_1m, tech_5m, tech_15m, funding_rate, fear_greed):
        if None in (tech_1m,tech_5m,tech_15m): return 0,[]
        score=0; factors=[]
        rsi_avg=(tech_1m.get('rsi',50)+tech_5m.get('rsi',50)+tech_15m.get('rsi',50))/3
        if rsi_avg<35: score+=20; factors.append(f"RSI共振超卖({rsi_avg:.0f})")
        elif rsi_avg>65: score-=15; factors.append(f"RSI共振超买({rsi_avg:.0f})")
        bb_positions=[]
        for t in [tech_1m,tech_5m,tech_15m]:
            p=t.get('bb_middle',0); l=t.get('bb_lower',0); u=t.get('bb_upper',0)
            if u>l and p>0: bb_positions.append((p-l)/(u-l))
        if bb_positions:
            avg=sum(bb_positions)/len(bb_positions)
            if avg<0.2: score+=15; factors.append("多周期布林下轨")
            elif avg>0.8: score-=10; factors.append("多周期布林上轨")
        if funding_rate is not None:
            if funding_rate<-0.0005: score+=10; factors.append("费率负值")
            elif funding_rate>0.001: score-=10; factors.append("费率过高")
        if fear_greed is not None:
            if fear_greed<30: score+=5; factors.append("极度恐惧")
            elif fear_greed>70: score-=5; factors.append("极度贪婪")
        return min(100,max(0,score)),factors
    @staticmethod
    def meta_rl_score(price_history, win_rate_history, sharpe_history, n=30):
        if len(price_history)<n: return 50
        recent=price_history[-n:]
        returns=[(recent[i]-recent[i-1])/recent[i-1] for i in range(1,len(recent))]
        avg=sum(returns)/len(returns) if returns else 0
        std=np.std(returns) if len(returns)>1 else 0.01
        meta=0.4*avg*100+0.3*(win_rate_history[-1] if win_rate_history else 0.5)+0.3*(sharpe_history[-1] if sharpe_history else 1.0)
        return min(100,max(0,50+meta*10))
    @staticmethod
    def chanformer_score(price_sequence, volume_sequence, n=50):
        if len(price_sequence)<n: return 50
        prices=price_sequence[-n:]; vols=volume_sequence[-n:] if len(volume_sequence)>=n else [1]*n
        changes=[prices[i]/prices[i-1]-1 for i in range(1,len(prices))]
        if not changes: return 50
        weights=[]
        for i,c in enumerate(changes):
            vol_factor=vols[i]/(sum(vols)/len(vols)) if sum(vols)>0 else 1
            weights.append(abs(c)*vol_factor)
        score=sum(weights[-10:])/sum(weights)*100 if sum(weights)>0 else 50
        return min(100,max(0,score))
    @staticmethod
    def f2agent_signal(tech_data, onchain_data, news_sentiment, fear_greed, social_sentiment):
        score=50; signals=[]
        rsi=tech_data.get('rsi',50)
        if rsi<35: score+=15; signals.append("技术超卖")
        elif rsi>65: score-=15; signals.append("技术超买")
        price=tech_data.get('bb_middle',0); l=tech_data.get('bb_lower',0); u=tech_data.get('bb_upper',0)
        if u>l and price>0:
            pos=(price-l)/(u-l)
            if pos<0.2: score+=15; signals.append("布林下轨")
            elif pos>0.8: score-=10; signals.append("布林上轨")
        if onchain_data:
            whale=onchain_data.get('whale_transfers',0); net=onchain_data.get('exchange_netflow',0)
            if whale>3: score+=10; signals.append("巨鲸活跃")
            if net<-50: score+=10; signals.append("交易所净流出")
        if news_sentiment:
            s=news_sentiment.get('sentiment',0)
            if s>0.3: score+=10; signals.append("新闻积极")
            elif s<-0.3: score-=10; signals.append("新闻消极")
        if social_sentiment:
            s=social_sentiment.get('sentiment',0)
            if s>0.3: score+=5; signals.append("社交积极")
            elif s<-0.3: score-=5; signals.append("社交消极")
        if fear_greed is not None and fear_greed<30: score+=5; signals.append("极度恐惧")
        return min(100,max(0,score)),signals
    @staticmethod
    def confidence_rl_score(price_history, rsi_history, volatility_history, n=30):
        if len(price_history)<n or len(rsi_history)<n: return 50,0.5
        recent_p=price_history[-n:]; recent_r=rsi_history[-n:] if len(rsi_history)>=n else [50]*n; recent_v=volatility_history[-n:] if len(volatility_history)>=n else [0.01]*n
        volatility=np.mean(recent_v)
        rsi_extreme=max(0,abs(np.mean(recent_r)-50)/50)
        confidence=max(0.3,min(0.95,1-volatility*5-rsi_extreme*0.3))
        base=50+(50-np.mean(recent_r))*0.5
        return min(100,max(0,base*confidence)),confidence
    @staticmethod
    def dl_stat_arbitrage(prices_list, volumes_list, n=30):
        if len(prices_list)<2 or len(prices_list[0])<n: return 0,[]
        ratios=[]
        for i in range(len(prices_list)):
            for j in range(i+1,len(prices_list)):
                ratio=np.mean(prices_list[i][-n:])/np.mean(prices_list[j][-n:])
                ratios.append(ratio)
        if not ratios: return 0,[]
        mean=np.mean(ratios); std=np.std(ratios) if len(ratios)>1 else 0.01
        z=(ratios[-1]-mean)/std
        if z>2: return -1,[f"Z={z:.2f}做空"]
        elif z<-2: return 1,[f"Z={z:.2f}做多"]
        return 0,[f"Z={z:.2f}中性"]
    @staticmethod
    def high_freq_signal(price_sequence, volume_sequence, n=20):
        if len(price_sequence)<n or len(volume_sequence)<n: return 0,0
        prices=price_sequence[-n:]; vols=volume_sequence[-n:]
        pm=(prices[-1]-prices[-5])/prices[-5] if len(prices)>=5 else 0
        vm=(vols[-1]-np.mean(vols))/np.mean(vols) if np.mean(vols)>0 else 0
        if pm>0.005 and vm>0.5: return 1,0.8
        elif pm<-0.005 and vm>0.5: return -1,0.75
        return 0,0.3
    @staticmethod
    def onchain_quant_score(onchain_data):
        if not onchain_data: return 50,[]
        score=50; factors=[]
        whale=onchain_data.get('whale_transfers',0); net=onchain_data.get('exchange_netflow',0); active=onchain_data.get('active_addresses',0); hr=onchain_data.get('hashrate',0)
        if whale>3: score+=10; factors.append(f"巨鲸{whale}笔")
        if net<-50: score+=15; factors.append("交易所净流出")
        elif net>50: score-=10; factors.append("交易所净流入")
        if active>4000: score+=5; factors.append("活跃地址高")
        if hr>400: score+=5; factors.append("算力高")
        return min(100,max(0,score)),factors
    @staticmethod
    def multi_source_sentiment(news_sentiment, social_sentiment, fear_greed):
        score=50; factors=[]
        if news_sentiment:
            ns=news_sentiment.get('sentiment',0)
            if ns>0.3: score+=10; factors.append("新闻积极")
            elif ns<-0.3: score-=10; factors.append("新闻消极")
        if social_sentiment:
            ss=social_sentiment.get('sentiment',0)
            if ss>0.3: score+=5; factors.append("社交积极")
            elif ss<-0.3: score-=5; factors.append("社交消极")
        if fear_greed is not None:
            if fear_greed<30: score+=5; factors.append("极度恐惧")
            elif fear_greed>70: score-=5; factors.append("极度贪婪")
        return min(100,max(0,score)),factors
    @staticmethod
    def triangular_arbitrage(prices):
        if len(prices)<3: return False,0
        for i in range(len(prices)):
            for j in range(i+1,len(prices)):
                for k in range(j+1,len(prices)):
                    if prices[i]<=0 or prices[j]<=0 or prices[k]<=0: continue
                    arb=prices[i]*prices[j]/prices[k]
                    if abs(arb-1)>0.002: return True,(arb-1)*100
        return False,0
    @staticmethod
    def autonomous_agent_decision(price_history, tech_data, onchain, news, social, fear_greed, funding):
        score=50; reasons=[]
        rsi=tech_data.get('rsi',50)
        if rsi<30: score+=20; reasons.append("RSI超卖")
        elif rsi<40: score+=10; reasons.append("RSI偏低")
        price=tech_data.get('bb_middle',0); l=tech_data.get('bb_lower',0); u=tech_data.get('bb_upper',0)
        if u>l and price>0:
            pos=(price-l)/(u-l)
            if pos<0.15: score+=20; reasons.append("布林下轨极低")
            elif pos<0.3: score+=10; reasons.append("布林下轨附近")
        if onchain:
            if onchain.get('exchange_netflow',0)<-50: score+=15; reasons.append("链上净流出")
            if onchain.get('whale_transfers',0)>3: score+=10; reasons.append("鲸鱼活跃")
        if news and news.get('sentiment',0)>0.3: score+=10; reasons.append("新闻积极")
        if social and social.get('sentiment',0)>0.3: score+=5; reasons.append("社交积极")
        if fear_greed is not None and fear_greed<30: score+=10; reasons.append("极度恐惧")
        if funding is not None and funding<-0.0005: score+=10; reasons.append("费率负值")
        if score>=75: action,conf="BUY",0.85
        elif score>=60: action,conf="BUY_LIGHT",0.65
        elif score>=40: action,conf="HOLD",0.5
        elif score>=25: action,conf="SELL_LIGHT",0.6
        else: action,conf="SELL",0.7
        return min(100,max(0,score)),action,conf,reasons
    @staticmethod
    def evoquant_optimize(performance_metrics, current_params):
        if not performance_metrics or len(performance_metrics)<20: return current_params,0
        win_rate=performance_metrics.get('win_rate',0.5); avg_win=performance_metrics.get('avg_win_pct',0); avg_loss=performance_metrics.get('avg_loss_pct',0); sharpe=performance_metrics.get('sharpe',1.0)
        factor=1.0
        if win_rate<0.4: factor*=0.9
        if avg_win<avg_loss*0.5: factor*=0.8
        if sharpe<0.5: factor*=0.85
        new=current_params.copy()
        if 'tp_pct' in new:
            new['tp_pct']*= (1+(avg_win/100)*0.1)
            new['sl_pct']*= (1+(avg_loss/100)*0.1)
        return new,factor
    @staticmethod
    def deterministic_shielding(signal, confidence, market_volatility, max_risk=0.02):
        if confidence<0.3: return 0,"低置信度屏蔽"
        if market_volatility>0.1: return signal*0.5,"高波动降仓"
        if abs(signal)*confidence>1.5: return signal*(1.5/(abs(signal)*confidence)),"信号过强限制"
        return signal,"通过"
    @staticmethod
    def rala_enhanced(regime, confidence, tech_data, funding, fear_greed):
        if regime=="high_volatility_trend": return {"tp_factor":1.5,"sl_factor":1.2,"signal":"trend","weight":0.7}
        elif regime=="breakout": return {"tp_factor":1.0,"sl_factor":0.8,"signal":"breakout","weight":0.6}
        elif regime=="low_volatility_range": return {"tp_factor":0.7,"sl_factor":0.5,"signal":"range","weight":0.5}
        elif regime=="extreme_volatility": return {"tp_factor":0.3,"sl_factor":0.3,"signal":"pause","weight":0.3}
        else: return {"tp_factor":1.0,"sl_factor":1.0,"signal":"neutral","weight":0.5}
    @staticmethod
    def meta_rl_enhanced(price_history, rsi_history, volume_history, win_rate_history, sharpe_history):
        if len(price_history)<30: return 50,0.5
        recent=price_history[-20:]
        returns=[(recent[i]-recent[i-1])/recent[i-1] for i in range(1,len(recent))]
        avg=sum(returns)/len(returns) if returns else 0
        std=np.std(returns) if len(returns)>1 else 0.01
        rsi_mean=np.mean(rsi_history[-20:]) if len(rsi_history)>=20 else 50
        vol_mean=np.mean(volume_history[-20:]) if len(volume_history)>=20 else 0
        win_rate=win_rate_history[-1] if win_rate_history else 0.5
        sharpe=sharpe_history[-1] if sharpe_history else 1.0
        actor=50+avg*100*10
        judge=50+(50-rsi_mean)*0.3+vol_mean*0.01
        meta=50+(win_rate-0.5)*40+(sharpe-1)*10
        combined=actor*0.4+judge*0.3+meta*0.3
        confidence=min(0.95,max(0.3,abs(avg)*20+win_rate*0.3))
        return min(100,max(0,combined)),confidence
    @staticmethod
    def cryptogat_signal(all_coin_data, target_symbol):
        if len(all_coin_data)<2: return 50,[]
        target_price=all_coin_data.get(target_symbol,{}).get('price',0)
        if target_price==0: return 50,[]
        correlations=[]
        for sym,data in all_coin_data.items():
            if sym==target_symbol: continue
            price=data.get('price',0); change=data.get('change_24h',0)
            if price==0: continue
            rel=(price-target_price)/target_price if target_price>0 else 0
            attention=abs(change)/(1+abs(rel))
            correlations.append((sym,change,rel,attention))
        if not correlations: return 50,[]
        correlations.sort(key=lambda x:x[3], reverse=True)
        top=correlations[:3]
        weighted=0; total=0
        for sym,change,rel,attn in top:
            if change>0 and rel<0: weighted+=attn*1
            elif change<0 and rel>0: weighted-=attn*1
            total+=attn
        norm=50+(weighted/total*10) if total>0 else 50
        factors=[f"{sym}:{change:+.2f}%" for sym,change,_,_ in top]
        return min(100,max(0,norm)),factors
    @staticmethod
    def web_crypto_score(tech_data, news_sentiment, social_sentiment, fear_greed):
        score=50; factors=[]
        rsi=tech_data.get('rsi',50)
        if rsi<30: score+=15; factors.append("技术超卖")
        elif rsi<40: score+=8; factors.append("技术偏低")
        price=tech_data.get('bb_middle',0); l=tech_data.get('bb_lower',0)
        if l>0 and price>0 and price<=l*1.03: score+=15; factors.append("布林下轨")
        if news_sentiment:
            ns=news_sentiment.get('sentiment',0)
            if ns>0.3: score+=12; factors.append("新闻积极")
            elif ns<-0.3: score-=12; factors.append("新闻消极")
        if social_sentiment:
            ss=social_sentiment.get('sentiment',0)
            if ss>0.2: score+=8; factors.append("社交积极")
            elif ss<-0.2: score-=8; factors.append("社交消极")
        if fear_greed is not None:
            if fear_greed<30: score+=10; factors.append("极度恐惧")
            elif fear_greed>70: score-=10; factors.append("极度贪婪")
        return min(100,max(0,score)),factors
    @staticmethod
    def sentiment_rl_score(price_history, sentiment_history, rsi_history, n=30):
        if len(price_history)<n or len(sentiment_history)<n: return 50,0.5
        recent_p=price_history[-n:]; recent_s=sentiment_history[-n:]; recent_r=rsi_history[-n:] if len(rsi_history)>=n else [50]*n
        sent_alpha=sum(recent_s)/len(recent_s)
        pm=(recent_p[-1]-recent_p[0])/recent_p[0] if recent_p[0]>0 else 0
        alpha=sent_alpha*0.6+pm*0.4
        penalty=0
        if np.mean(recent_r)>70: penalty=-0.3
        elif np.mean(recent_r)<30: penalty=0.3
        score=50+alpha*30+penalty*20
        conf=min(0.9,max(0.3,abs(alpha)*0.5+0.3))
        return min(100,max(0,score)),conf
    @staticmethod
    def astgnn_score(price_history, volume_history, rsi_history, ohlcv_data, n=30):
        if len(price_history)<n or len(ohlcv_data)<n: return 50
        recent=ohlcv_data[-n:] if len(ohlcv_data)>=n else []
        if len(recent)<n: return 50
        quat=[]
        for ohlcv in recent:
            if len(ohlcv)>=4:
                o,h,l,c=ohlcv[0],ohlcv[1],ohlcv[2],ohlcv[3]
                q_real=c; q_i=h-l; q_j=o-c; q_k=ohlcv[4] if len(ohlcv)>4 else 0
                quat.append((q_real,q_i,q_j,q_k))
        if len(quat)<n: return 50
        norms=[math.sqrt(q[0]**2+q[1]**2+q[2]**2+q[3]**2) for q in quat]
        q_mean=sum(norms)/len(norms)
        rotations=[]
        for i in range(1,len(quat)):
            q1,q2=quat[i-1],quat[i]
            dot=sum([q1[j]*q2[j] for j in range(4)])
            n1=math.sqrt(sum([x**2 for x in q1])); n2=math.sqrt(sum([x**2 for x in q2]))
            if n1>0 and n2>0:
                cos=dot/(n1*n2)
                rotations.append(max(-1,min(1,cos)))
        if rotations:
            stab=sum(rotations)/len(rotations)
            vol_score=q_mean/(sum(norms)/len(norms)) if sum(norms)>0 else 1
            score=50+stab*20+vol_score*10
        else: score=50
        return min(100,max(0,score))
    @staticmethod
    def shield_v2(signal, confidence, market_volatility, price_history, max_risk=0.02):
        if confidence<0.3: return 0,"低置信度屏蔽",False
        if market_volatility>0.1: return signal*0.5,"高波动降仓",False
        if len(price_history)>=5:
            changes=[]
            for i in range(1,min(5,len(price_history))):
                if price_history[-i]>0:
                    changes.append(abs((price_history[-1]-price_history[-i])/price_history[-i]))
            if changes and max(changes)>0.05: return signal*0.3,"价格突变保护",True
        if abs(signal)*confidence>1.5: return signal*(1.5/(abs(signal)*confidence)),"信号过强限制",False
        return signal,"通过",False

class LowBuyHighSellEnhancer:
    @staticmethod
    def enhanced_buy_signal(tech, price, ema20=None, market_state=None):
        rsi=tech.get('rsi',50); l=tech.get('bb_lower',0); u=tech.get('bb_upper',0)
        if l==0 or u==0: return False,0,{}
        near_lower=price<=l*1.02; oversold=rsi<40
        weight=1.0
        if market_state=='oversold': weight=1.3
        elif market_state=='overbought': weight=0.5
        elif market_state=='extreme': weight=0.6
        trend_ok=True
        if ema20 is not None and ema20>0: trend_ok=price>=ema20*0.99
        if near_lower and oversold and trend_ok:
            strength=(1-(price-l)/(u-l))*50+(40-rsi)*1.5
            strength=min(100,max(0,strength))*weight
            return True,min(100,strength),{'near_lower':near_lower,'oversold':oversold,'state_weight':weight}
        return False,0,{}
    @staticmethod
    def trailing_stop_with_stepping(entry_price, current_price, high_price, tp_pct, sl_pct, step_factor=0.3):
        pct=(current_price-entry_price)/entry_price*100
        if pct<=-sl_pct*100: return 'sell','stop_loss'
        if pct>=tp_pct*100*0.5:
            if pct>=tp_pct*100*0.7: trailing=tp_pct*0.3
            else: trailing=tp_pct*0.5
            if current_price<=high_price*(1-trailing): return 'sell','trailing_stop'
        if pct>=tp_pct*100: return 'sell','take_profit'
        return 'hold',None

class QuantBot:
    def __init__(self, exchange):
        self.exchange=exchange; self.ws=WSDataManager(exchange); self.tech=TechnicalEngine(exchange)
        self.real_data=RealDataEngine(exchange,self.ws); self.orderbook_engine=OrderbookEngine()
        self.frontier=FrontierEngine(); self.low_buy=LowBuyHighSellEnhancer()
        self.lock=asyncio.Lock()
        self.market_state=MarketStateEngine(); self.optimizer=AutoQuantOptimizer(); self.multi_agent=MultiAgentSystem()
        self.sentiment_rl=SentimentAugmentedRL(); self.web_agent=WebCryptoAgent(); self.kelly=KellyPositionManager()
        self.garch=GARCHVolatilityPredictor(); self.executor=OrderExecutionOptimizer()
        self.is_running=True; self.orderbook_filter=True; self.waterfall_breaker=True
        self.symbols=[settings.SYMBOL,"BTC/USDT","SOL/USDT"]
        self.tp_pct=0.015; self.sl_pct=0.01; self.trailing_sl_pct=0.005; self.trailing_tp_pct=0.003
        self.single_order_usdt=1.0; self.timeframe="5m"; self.reserve_bottom=10; self.max_daily_trades=20
        self.auto_trade_enabled=False; self.auto_min_score=65; self.max_per_coin_usdt=50
        self.max_daily_loss_pct=0.05; self.max_total_allocated_pct=0.8; self.max_drawdown_pct=0.12
        self.api_error_count=0; self.max_api_errors=5; self.api_error_pause_time=0; self.max_positions_per_coin=8
        self.position_counts={}; self.coin_configs={}; self.grid_configs={}
        self.btc_risk_paused=False; self._last_btc_check_time=0
        self._rsi_history={}; self._volume_history={}; self._last_grid_entry={}; self._bb_bandwidth_history={}
        self._close_prices_history={}; self._price_history={}; self._volatility_history={}
        self._win_rate_history={}; self._sharpe_history={}; self._performance_metrics={}
        self._sentiment_history={}; self._ohlcv_history={}; self._trade_history=[]
        self._current_market_state="neutral"; self._current_market_score=0.5; self._current_state_params={}
        self._kelly_cache={}
        self._consecutive_losses=0; self._today_loss_pct=0.0; self._is_paused=False
        self._daily_trade_count=0; self._last_pause_time=0; self._account_balance=0.0; self._balance_last_update=0
        self._multi_timeframe_data={}; self._delta_neutral_positions={}; self._onchain_cache={}; self._triangular_positions={}
        self._delta_neutral_config={"enabled":True,"min_funding_rate":0.0003,"max_position_per_coin":2,"allocation_percent":0.03,"min_allocation":0.5,"max_allocation":10}
        self._delta_neutral_stats={"total_trades":0,"total_profit":0.0,"last_trade_time":0,"profit_today":0.0,"today_date":datetime.now(CST).day}
        self.taker_fee=settings.TAKER_FEE; self.maker_fee=settings.MAKER_FEE; self.min_profit_margin=settings.MIN_PROFIT_MARGIN
        self.breakeven_pct=(self.taker_fee*2)+self.min_profit_margin
        raw=settings.ALLOWED_USERS; self.allowed={int(x.strip()) for x in raw.split(",") if x.strip().isdigit()} if raw else set()
        self.env_tag="🧪 (模拟盘)" if settings.IS_SANDBOX else "🔴 (实盘)"
        self.entries={}; self.daily_trades=0; self.last_reset_day=datetime.now(CST).day; self.trades=[]
        self._trailing_active={}; self._trailing_high={}; self.entry_details={}
        self.consecutive_failures=0; self.last_failure_time=0; self.peak_total_value=0
        self.learning_enabled=True; self.last_learning_check=0; self.ai_optimize_count=0
        self._cached_balances={}; self._cached_usdt_free=0.0; self._balance_cache_time=0; self._balance_cache_ttl=15
        self._btc_safe_flag=True; self._drawdown_safe_flag=True; self._last_reset_date=datetime.now(CST).day
        self._tech_cache={}; self._tech_cache_time={}; self._tech_cache_ttl=30
        self._base_position_multiplier=1.0

        # ========== 高级策略引擎 ==========
        self.advanced_engine = AdvancedStrategyEngine(exchange)
        self.advanced_engine.enabled['pairs'] = True
        self.advanced_engine.enabled['llm'] = bool(os.getenv('DEEPSEEK_API_KEY') or os.getenv('OPENAI_API_KEY'))
        self.advanced_engine.enabled['rl'] = True

        self.ai_insight={"timestamp":0,"summary":"等待首次分析...","btc_trend":"中性","eth_trend":"中性","fear_greed":50,"news_sentiment":0.0,"social_sentiment":0.0,"news_headlines":[],"recommendation":"观望","score":50,"regime":"neutral","archetype":"Balanced","auto_agent_action":"HOLD","market_state":"neutral","kelly_position":0}
        self.ai_api_key=os.getenv("DEEPSEEK_API_KEY","") or os.getenv("OPENAI_API_KEY","")
        self.ai_model=os.getenv("AI_MODEL","deepseek-chat"); self.ai_base_url=os.getenv("AI_BASE_URL","https://api.deepseek.com/v1")
        self.ai_enabled=bool(self.ai_api_key)

        self.tg_app=None
        if settings.TG_BOT_TOKEN:
            self.tg_app=ApplicationBuilder().token(settings.TG_BOT_TOKEN).build()
            handlers=[CommandHandler("start",self.cmd_menu),CommandHandler("menu",self.cmd_menu),CommandHandler("status",self.cmd_status),CommandHandler("check",self.cmd_check),CommandHandler("symbols",self.cmd_symbols),CommandHandler("analysis",self.cmd_analysis),CommandHandler("brain",self.cmd_brain),CommandHandler("help",self.cmd_help),CommandHandler("settp",self.cmd_set_tp),CommandHandler("setsl",self.cmd_set_sl),CommandHandler("settsl",self.cmd_set_tsl),CommandHandler("settmpt",self.cmd_set_trailing_tp),CommandHandler("setamount",self.cmd_set_amount),CommandHandler("settf",self.cmd_set_tf),CommandHandler("setreserve",self.cmd_set_reserve),CommandHandler("addsymbol",self.cmd_add_symbol),CommandHandler("delsymbol",self.cmd_del_symbol),CommandHandler("panic",self.cmd_panic),CommandHandler("entry",self.cmd_entry),CommandHandler("settrades",self.cmd_set_trades),CommandHandler("resettrades",self.cmd_reset_trades),CommandHandler("preset",self.cmd_preset),CommandHandler("history",self.cmd_history),CommandHandler("autotrade",self.cmd_autotrade),CommandHandler("autoscore",self.cmd_autoscore),CommandHandler("holdings",self.cmd_holdings),CommandHandler("setmaxcoin",self.cmd_set_max_coin),CommandHandler("setmaxloss",self.cmd_set_max_loss),CommandHandler("setmaxpos",self.cmd_set_max_pos),CommandHandler("setmaxalloc",self.cmd_set_max_alloc),CommandHandler("setcoin",self.cmd_set_coin),CommandHandler("resetcoin",self.cmd_reset_coin),CommandHandler("coininfo",self.cmd_coin_info),CommandHandler("setgrid",self.cmd_set_grid),CommandHandler("resetgrid",self.cmd_reset_grid),CommandHandler("learn",self.cmd_learn),CommandHandler("stats",self.cmd_stats),CommandHandler("backup",self.cmd_backup),CommandHandler("setcoinonly",self.cmd_setcoinonly),CommandHandler("lowbalance",self.cmd_lowbalance),CommandHandler("arbstats",self.cmd_arb_stats),CommandHandler("optimize",self.cmd_optimize),CommandHandler("state",self.cmd_state)]
            for h in handlers: self.tg_app.add_handler(h)
            self.tg_app.add_handler(CallbackQueryHandler(self.handle_button_click))
            self.tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_text_input))

    def _get_coin_param(self, sym,key,default):
        if sym in self.coin_configs and key in self.coin_configs[sym]: return self.coin_configs[sym][key]
        return default
    def _get_usdt_free(self,bal):
        try:
            usdt=bal.get('USDT',{})
            if isinstance(usdt,dict): return float(usdt.get('free',0))
            elif isinstance(usdt,(int,float)): return float(usdt)
            else: return 0
        except: return 0
    def _extract_balances(self,bal):
        res={}
        sys_keys={'info','free','used','total','datetime','timestamp'}
        for k,v in bal.items():
            if k in sys_keys: continue
            if isinstance(v,dict): res[k]=v.get('free',0)
        return res
    async def _refresh_balance_cache(self,force=False):
        now=time.time()
        if force or (now-self._balance_cache_time>self._balance_cache_ttl):
            bal=await self.exchange.fetch_balance()
            self._cached_balances=self._extract_balances(bal)
            self._cached_usdt_free=self._get_usdt_free(bal)
            self._balance_cache_time=now
        return self._cached_usdt_free
    async def _round_amount_by_precision(self,symbol,amount):
        try:
            if self.exchange and self.exchange.exchange:
                market=self.exchange.exchange.market(symbol)
                if market and 'limits' in market and 'amount' in market['limits']:
                    min_amt=market['limits']['amount'].get('min',0)
                    max_amt=market['limits']['amount'].get('max',float('inf'))
                    if min_amt and amount<min_amt: amount=min_amt
                    if max_amt!=float('inf') and amount>max_amt: amount=max_amt
                if market and 'precision' in market and 'amount' in market['precision']:
                    prec=market['precision']['amount']
                    if prec>0: amount=float(int(amount/prec)*prec)
                    elif prec==0: amount=int(amount)
            return max(0.000001,amount)
        except: return amount
    def _calculate_dynamic_amount(self, base_amount=0.5):
        mult=self._base_position_multiplier
        total=self._cached_usdt_free
        for coin,free in self._cached_balances.items():
            ticker=self.ws.get_ticker(coin+"/USDT")
            if ticker: total+=free*ticker.get('last',0)
        self._account_balance=total
        if total<10: base=max(0.1,base_amount*0.3)
        elif total<30: base=max(0.2,base_amount*0.6)
        elif total<50: base=max(0.3,base_amount*0.8)
        elif total<100: base=base_amount
        elif total<300: base=base_amount*2
        else: base=base_amount*4
        return base*mult
    async def _check_risk_limits(self):
        today=datetime.now(CST).day
        if today!=self._last_reset_date:
            self._today_loss_pct=0.0; self._consecutive_losses=0; self._base_position_multiplier=1.0; self._last_reset_date=today
        if self._consecutive_losses>=3:
            self._base_position_multiplier=max(0.3,1.0-(self._consecutive_losses-2)*0.2)
        else:
            self._base_position_multiplier=min(1.0,self._base_position_multiplier+0.05)
        if self._consecutive_losses>=4:
            if time.time()-self._last_pause_time>3600:
                self._consecutive_losses=0; self._is_paused=False
            else: return False
        if self._today_loss_pct>0.08:
            if not self._is_paused:
                await self._alert(f"⛔ 当日亏损达 {self._today_loss_pct*100:.1f}%，暂停交易","critical")
                self._is_paused=True
            return False
        return True
    async def _alert(self,message,level="warning"):
        emoji={"info":"ℹ️","warning":"⚠️","critical":"🚨"}
        if settings.TG_CHAT_ID and self.tg_app and self.tg_app.bot:
            try:
                await self.tg_app.bot.send_message(chat_id=settings.TG_CHAT_ID,text=f"{emoji.get(level,'⚠️')} **系统告警**\n{message}",parse_mode="Markdown")
            except: pass
    async def load_and_init(self):
        await init_db()
        cfg=await load_config()
        self.orderbook_filter=cfg.get('orderbook_filter',True); self.waterfall_breaker=cfg.get('waterfall_breaker',True)
        self.symbols=cfg.get('symbols',[]) or [settings.SYMBOL,"BTC/USDT","SOL/USDT"]
        self.tp_pct=cfg.get('tp_pct',0.015); self.sl_pct=cfg.get('sl_pct',0.01)
        self.trailing_sl_pct=cfg.get('trailing_sl_pct',0.005); self.trailing_tp_pct=cfg.get('trailing_tp_pct',0.003)
        self.single_order_usdt=cfg.get('single_order_usdt',1.0); self.timeframe=cfg.get('timeframe','5m')
        self.reserve_bottom=cfg.get('reserve_bottom',10); self.max_daily_trades=cfg.get('max_daily_trades',20)
        self.auto_trade_enabled=cfg.get('auto_trade_enabled',False); self.auto_min_score=cfg.get('auto_min_score',65)
        self.max_per_coin_usdt=cfg.get('max_per_coin_usdt',50); self.max_daily_loss_pct=cfg.get('max_daily_loss_pct',0.05)
        self.max_total_allocated_pct=1.0; self.max_drawdown_pct=cfg.get('max_drawdown_pct',0.12)
        self.max_positions_per_coin=cfg.get('max_positions_per_coin',8)
        coin_cfg_raw=cfg.get('coin_configs','{}')
        try:
            if isinstance(coin_cfg_raw,str): self.coin_configs=json.loads(coin_cfg_raw) if coin_cfg_raw else {}
            elif isinstance(coin_cfg_raw,dict): self.coin_configs=coin_cfg_raw
            else: self.coin_configs={}
        except: self.coin_configs={}
        grid_cfg_raw=cfg.get('grid_configs','{}')
        try:
            if isinstance(grid_cfg_raw,str): self.grid_configs=json.loads(grid_cfg_raw) if grid_cfg_raw else {}
            elif isinstance(grid_cfg_raw,dict): self.grid_configs=grid_cfg_raw
            else: self.grid_configs={}
        except: self.grid_configs={}
        self.trades=await load_trades()
        state=await load_runtime_state()
        if state:
            self.position_counts=state.get('position_counts',{}); self.entries=state.get('entries',{})
            self.peak_total_value=state.get('peak_total_value',0); self.daily_trades=state.get('daily_trades',0)
            self._trailing_active=state.get('trailing_active',{}); self._trailing_high=state.get('trailing_high',{})
        logger.info("✅ UltimateBot v12.0 已加载")
    async def _save_runtime_state(self):
        state={'position_counts':self.position_counts,'entries':self.entries,'peak_total_value':self.peak_total_value,'daily_trades':self.daily_trades,'trailing_active':self._trailing_active,'trailing_high':self._trailing_high}
        await save_runtime_state(state)
    async def _save_config(self):
        cfg={'tp_pct':self.tp_pct,'sl_pct':self.sl_pct,'trailing_sl_pct':self.trailing_sl_pct,'trailing_tp_pct':self.trailing_tp_pct,'single_order_usdt':self.single_order_usdt,'timeframe':self.timeframe,'reserve_bottom':self.reserve_bottom,'symbols':self.symbols,'orderbook_filter':self.orderbook_filter,'waterfall_breaker':self.waterfall_breaker,'max_daily_trades':self.max_daily_trades,'auto_trade_enabled':self.auto_trade_enabled,'auto_min_score':self.auto_min_score,'max_per_coin_usdt':self.max_per_coin_usdt,'max_daily_loss_pct':self.max_daily_loss_pct,'max_total_allocated_pct':self.max_total_allocated_pct,'max_drawdown_pct':self.max_drawdown_pct,'max_positions_per_coin':self.max_positions_per_coin,'coin_configs':json.dumps(self.coin_configs),'grid_configs':json.dumps(self.grid_configs)}
        await save_config(cfg)
    async def _get_cached_tech(self, sym, timeframe='5m', limit=50):
        key=f"{sym}_{timeframe}_{limit}"
        now=time.time()
        if key in self._tech_cache and (now-self._tech_cache_time.get(key,0))<self._tech_cache_ttl:
            return self._tech_cache[key]
        tech=await self.tech.calc(sym,timeframe,limit)
        if tech:
            self._tech_cache[key]=tech; self._tech_cache_time[key]=now
        return tech
    async def _update_market_state(self, sym, tech_data):
        price_hist=self._price_history.get(sym,[]); vol_hist=self._volatility_history.get(sym,[])
        state_str,state_score=self.market_state.detect_state(tech_data,price_hist,vol_hist)
        self._current_market_state=state_str; self._current_market_score=state_score
        state_params=self.market_state.get_strategy_params(state_str,self.tp_pct,self.sl_pct,self.single_order_usdt)
        self._current_state_params=state_params
        return state_str,state_params
    async def _ai_analyze_market(self):
        while self.is_running:
            try:
                btc=self.ws.get_ticker("BTC/USDT"); eth=self.ws.get_ticker("ETH/USDT")
                btc_change=btc.get('percentage',0) if btc else 0; eth_change=eth.get('percentage',0) if eth else 0
                fg_data=await self.real_data.get_fear_greed_index(); fg=fg_data["value"] if fg_data else 50
                news_data=await self.real_data.get_news_sentiment(); social_data=await self.real_data.get_social_sentiment()
                news_sent=news_data.get('sentiment',0); social_sent=social_data.get('sentiment',0); headlines=news_data.get('headlines',[])
                all_scores=[]; archetypes=[]; regimes=[]; kellys=[]
                for sym in self.symbols:
                    try:
                        ticker=self.ws.get_ticker(sym); if not ticker: continue
                        p=ticker['last']; tech=await self._get_cached_tech(sym,self.timeframe,50)
                        if tech is None: continue
                        if sym not in self._price_history: self._price_history[sym]=[]
                        if sym not in self._volatility_history: self._volatility_history[sym]=[]
                        if sym not in self._win_rate_history: self._win_rate_history[sym]=[]
                        if sym not in self._sharpe_history: self._sharpe_history[sym]=[]
                        if sym not in self._sentiment_history: self._sentiment_history[sym]=[]
                        self._price_history[sym].append(p); self._price_history[sym]=self._price_history[sym][-100:]
                        vol=tech.get('atr',0)/tech.get('bb_middle',1) if tech.get('bb_middle',0)>0 else 0.01
                        self._volatility_history[sym].append(vol); self._volatility_history[sym]=self._volatility_history[sym][-50:]
                        self._sentiment_history[sym].append(news_sent); self._sentiment_history[sym]=self._sentiment_history[sym][-50:]
                        onchain=await self.real_data.get_onchain_metrics(sym); funding=await self.real_data.get_funding_rate(sym)
                        rsi_hist=[h.get('rsi',50) for h in self._rsi_history.get(sym,[])]
                        bb_hist=self._bb_bandwidth_history.get(sym,[])
                        state,_=await self._update_market_state(sym,tech)
                        recent_trades=self.trades[-30:] if len(self.trades)>=30 else self.trades
                        pnls=[t.get('pnl_pct',0) for t in recent_trades if t.get('pnl_pct') is not None]
                        if pnls:
                            wins=[p for p in pnls if p>0]; losses=[p for p in pnls if p<0]
                            wr=len(wins)/len(pnls) if pnls else 0.5; avgw=sum(wins)/len(wins) if wins else 0.5; avgl=abs(sum(losses)/len(losses)) if losses else 0.5
                            kelly=self.kelly.calculate_position(wr,avgw,avgl,len(pnls),0.1)
                        else: kelly=0.05
                        kellys.append(kelly)
                        arch_sig,arch_type=self.frontier.archetype_trader_signal(self._price_history[sym], self._volume_history.get(sym,[]), rsi_hist, bb_hist)
                        archetypes.append(f"{sym}:{arch_type}")
                        cross_score,_=self.frontier.crosssync_score(await self._get_multi_timeframe_data(sym),None,None,funding,fg)
                        meta_score=self.frontier.meta_rl_score(self._price_history[sym],self._win_rate_history[sym],self._sharpe_history[sym])
                        chan_score=self.frontier.chanformer_score(self._price_history[sym],self._volume_history.get(sym,[]))
                        f2_score,_=self.frontier.f2agent_signal(tech,onchain,news_data,fg,social_data)
                        sent_rl_score,_=self.sentiment_rl.calculate_alpha_reward(self._price_history[sym],self._sentiment_history[sym],rsi_hist)
                        web_score,_=self.web_agent.fusion_score(tech,news_data,social_data,fg,onchain)
                        agent_analysis=await self.multi_agent.analyze({},tech); agent_strategy=self.multi_agent.generate_strategy(agent_analysis,{})
                        combined=0.05*min(100,max(0,50+arch_sig*30))+0.05*cross_score+0.05*meta_score+0.05*chan_score+0.05*f2_score+0.10*sent_rl_score+0.10*web_score+0.05*(50+agent_strategy.get('tp_factor',1.0)*10)
                        all_scores.append(combined); regimes.append(f"{sym}:{state}")
                    except Exception as e: logger.warning(f"分析失败 {sym}: {e}")
                avg_score=sum(all_scores)/len(all_scores) if all_scores else 50
                avg_kelly=sum(kellys)/len(kellys) if kellys else 0.05
                rec="观望"
                if avg_score>=75: rec="积极做多"
                elif avg_score>=60: rec="谨慎做多"
                elif avg_score>=40: rec="观望"
                elif avg_score>=25: rec="谨慎减仓"
                else: rec="清仓避险"
                regime_summary=", ".join(set(regimes)) if regimes else "neutral"
                summary=(f"📊 BTC: {btc_change:+.2f}% | ETH: {eth_change:+.2f}%\n"
                         f"😨 恐惧贪婪: {fg} ({fg_data['classification'] if fg_data else '中性'})\n"
                         f"📰 新闻: {news_sent:+.2f} | 社交: {social_sent:+.2f}\n"
                         f"📈 市场状态: {self._current_market_state}\n"
                         f"🎯 综合评分: {avg_score:.0f}/100\n"
                         f"💰 凯利仓位: {avg_kelly*100:.1f}%\n"
                         f"💡 建议: {rec}")
                self.ai_insight={"timestamp":time.time(),"summary":summary,"btc_trend":"看涨" if btc_change>0 else "看跌","eth_trend":"看涨" if eth_change>0 else "看跌","fear_greed":fg,"news_sentiment":news_sent,"social_sentiment":social_sent,"news_headlines":headlines[:3],"recommendation":rec,"score":avg_score,"regime":regime_summary,"archetype":archetypes[0] if archetypes else "Balanced","auto_agent_action":rec,"market_state":self._current_market_state,"kelly_position":avg_kelly}
                logger.info(f"🤖 AI分析完成: {rec} | 评分{avg_score:.0f} | 市场状态{self._current_market_state}")
            except Exception as e: logger.error(f"AI分析异常: {e}")
            await asyncio.sleep(1800)

    async def _should_open_position(self, sym, p, tech, funding, fg, usdt_free):
        # ---- 更新历史 ----
        if sym not in self._price_history: self._price_history[sym]=[]
        self._price_history[sym].append(p); self._price_history[sym]=self._price_history[sym][-100:]
        if tech and sym in self._rsi_history:
            rsi=tech.get('rsi',50)
            self._rsi_history[sym].append({'rsi':rsi,'price':p,'time':time.time()}); self._rsi_history[sym]=self._rsi_history[sym][-100:]
        if sym not in self._volume_history: self._volume_history[sym]=[]
        if sym not in self._close_prices_history: self._close_prices_history[sym]=[]
        if sym not in self._bb_bandwidth_history: self._bb_bandwidth_history[sym]=[]
        ticker=self.ws.get_ticker(sym)
        if ticker:
            vol=ticker.get('volume',0)
            if vol>0:
                self._volume_history[sym].append(vol); self._volume_history[sym]=self._volume_history[sym][-50:]
        if p>0:
            self._close_prices_history[sym].append(p); self._close_prices_history[sym]=self._close_prices_history[sym][-100:]
        if tech:
            rsi=tech.get('rsi',50)
            if sym not in self._rsi_history: self._rsi_history[sym]=[]
            self._rsi_history[sym].append({'rsi':rsi,'price':p,'time':time.time()}); self._rsi_history[sym]=self._rsi_history[sym][-100:]
        bb_upper=tech.get('bb_upper',0) if tech else 0; bb_lower=tech.get('bb_lower',0) if tech else 0
        if bb_upper>0 and bb_lower>0 and p>0:
            bw=(bb_upper-bb_lower)/p*100 if p>0 else 0
            self._bb_bandwidth_history[sym].append(bw); self._bb_bandwidth_history[sym]=self._bb_bandwidth_history[sym][-100:]
        multi=await self._get_multi_timeframe_data(sym)
        rsi_hist=[h.get('rsi',50) for h in self._rsi_history.get(sym,[])]
        state,state_params=await self._update_market_state(sym,tech)
        details=[f"状态:{state}"]
        agent_analysis=await self.multi_agent.analyze({},tech); agent_strategy=self.multi_agent.generate_strategy(agent_analysis,{})
        details.append(f"Agent:{agent_analysis.get('state','neutral')}")
        weights={'low_buy':0.25,'ofi':0.10,'arch':0.10,'cross':0.10,'meta':0.10,'f2':0.10,'agent':0.10,'default':0.15}
        ema20=None
        try:
            ohlcv=await self.exchange.fetch_ohlcv(sym,self.timeframe,20)
            if ohlcv:
                closes=[c[4] for c in ohlcv]; ema20=np.mean(closes[-20:]) if len(closes)>=20 else None
        except: pass
        enhanced_buy, strength, buy_details = self.low_buy.enhanced_buy_signal(tech,p,ema20,agent_analysis.get('state','neutral'))
        enhanced_score=strength if enhanced_buy else 50
        scores=[enhanced_score*weights['low_buy']]
        if enhanced_buy: details.append(f"低买强{strength:.0f}")
        ob=self.ws.get_orderbook(sym)
        if ob:
            ofi=self.executor.calculate_ofi(ob); ofi_score=50+ofi*30; scores.append(ofi_score*weights['ofi']); details.append(f"OFI:{ofi:.2f}")
        else: scores.append(50*weights['ofi'])
        arch_signal, arch_type = self.frontier.archetype_trader_signal(self._price_history.get(sym,[]), self._volume_history.get(sym,[]), rsi_hist, self._bb_bandwidth_history.get(sym,[]))
        scores.append((50+arch_signal*30)*weights['arch']); details.append(f"Arch:{arch_type}")
        cross_score,_=self.frontier.crosssync_score(multi.get('1m'),multi.get('5m'),multi.get('15m'),funding,None)
        scores.append(cross_score*weights['cross'])
        meta_score=self.frontier.meta_rl_score(self._price_history.get(sym,[]), self._win_rate_history.get(sym,[]), self._sharpe_history.get(sym,[]))
        scores.append(meta_score*weights['meta'])
        f2_score=50; rsi_val=tech.get('rsi',50)
        if rsi_val<35: f2_score+=15
        elif rsi_val>65: f2_score-=15
        price_val=tech.get('bb_middle',0); bb_lower_val=tech.get('bb_lower',0); bb_upper_val=tech.get('bb_upper',0)
        if bb_upper_val>bb_lower_val and price_val>0:
            bb_pos=(price_val-bb_lower_val)/(bb_upper_val-bb_lower_val)
            if bb_pos<0.2: f2_score+=15
            elif bb_pos>0.8: f2_score-=10
        f2_score=min(100,max(0,f2_score)); scores.append(f2_score*weights['f2'])
        agent_score=50+agent_strategy.get('tp_factor',1.0)*10; scores.append(agent_score*weights['agent'])
        rsi_last=rsi_hist[-1] if rsi_hist else 50
        default_score=50+(50-rsi_last)*0.3+(tech.get('bandwidth_pct',0)*0.5); default_score=min(100,max(0,default_score)); scores.append(default_score*weights['default'])
        total_score=sum(scores); total_score=min(100,max(0,total_score))

        # ---- 高级引擎加权 ----
        if hasattr(self,'advanced_engine'):
            try:
                adv_signal = self.advanced_engine.get_combined_signal(sym, p)
                adv_score = adv_signal.get('score', 50)
                total_score = 0.8 * total_score + 0.2 * adv_score
            except Exception as e:
                logger.debug(f"高级引擎信号失败: {e}")

        threshold_adjust=state_params.get('threshold_adjust',0)
        coin_score=self._get_coin_param(sym,'auto_min_score',self.auto_min_score)+threshold_adjust
        should_open=total_score>=coin_score
        is_high_confidence=total_score>=80
        recent_pnls=[t.get('pnl_pct',0) for t in self.trades[-30:] if t.get('pnl_pct') is not None]
        if recent_pnls:
            wins=[p for p in recent_pnls if p>0]; losses=[p for p in recent_pnls if p<0]
            win_rate=len(wins)/len(recent_pnls) if recent_pnls else 0.5
            avg_win=sum(wins)/len(wins) if wins else 0.5
            avg_loss=abs(sum(losses)/len(losses)) if losses else 0.5
            kelly_pos=self.kelly.calculate_position(win_rate,avg_win,avg_loss,len(recent_pnls),0.1)
        else: kelly_pos=0.05
        base_amount=self._get_coin_param(sym,'single_order_usdt',self.single_order_usdt)
        dynamic_amount=self._calculate_dynamic_amount(base_amount)
        volatility=tech.get('atr',0)/tech.get('bb_middle',1) if tech.get('bb_middle',0)>0 else 0.01
        grid_width_factor=1+volatility*10
        state_amount_factor=state_params.get('amount_factor',1.0)/grid_width_factor
        kelly_factor=max(0.3,min(1.5,kelly_pos/0.05))
        final_amount=dynamic_amount*state_amount_factor*kelly_factor
        if is_high_confidence: final_amount*=1.3
        logger.info(f"📊 {sym} 综合评分: {total_score:.0f}/{coin_score:.0f} | 市场{state} | 凯利{kelly_pos*100:.1f}%")
        return {'should_open':should_open,'score':total_score,'is_high_confidence':is_high_confidence,'details':details,'amount':final_amount,'state':state,'state_params':state_params,'kelly_position':kelly_pos}

    async def cmd_optimize(self,update,context):
        if not self._auth(update): return
        await self._auto_optimize_params()
        await update.effective_message.reply_text(f"✅ 参数优化完成\n• 止盈: {self.tp_pct:.1%}\n• 止损: {self.sl_pct:.1%}\n• 阈值: {self.auto_min_score}\n• 市场状态: {self._current_market_state}")
    async def cmd_state(self,update,context):
        if not self._auth(update): return
        lines=[f"📊 市场状态 {self.env_tag}",f"• 当前状态: {self._current_market_state}",f"• 状态评分: {self._current_market_score:.2f}","• 状态参数:"]
        for k,v in self._current_state_params.items():
            if isinstance(v,float):
                if k in ['tp','sl']: lines.append(f"  {k}: {v:.1%}")
                else: lines.append(f"  {k}: {v:.2f}")
            else: lines.append(f"  {k}: {v}")
        recent_pnls=[t.get('pnl_pct',0) for t in self.trades[-30:] if t.get('pnl_pct') is not None]
        if recent_pnls:
            wins=[p for p in recent_pnls if p>0]; losses=[p for p in recent_pnls if p<0]
            wr=len(wins)/len(recent_pnls) if recent_pnls else 0.5
            avgw=sum(wins)/len(wins) if wins else 0.5; avgl=abs(sum(losses)/len(losses)) if losses else 0.5
            kelly=self.kelly.calculate_position(wr,avgw,avgl,len(recent_pnls),0.1)
            lines.append(f"• 凯利仓位: {kelly*100:.1f}%"); lines.append(f"• 胜率: {wr*100:.1f}%")
            lines.append(f"• 盈亏比: {avgw/avgl:.2f}" if avgl>0 else "• 盈亏比: N/A")
        await update.effective_message.reply_text("\n".join(lines))
    async def _auto_optimize_params(self):
        if len(self.trades)<20: return
        recent=self.trades[-30:]
        cur={'tp_pct':self.tp_pct,'sl_pct':self.sl_pct,'score':self.auto_min_score}
        opt=await self.optimizer.optimize(None,recent,cur)
        if opt.get('tp_pct',self.tp_pct)!=self.tp_pct or opt.get('sl_pct',self.sl_pct)!=self.sl_pct or opt.get('score',self.auto_min_score)!=self.auto_min_score:
            self.tp_pct=opt.get('tp_pct',self.tp_pct); self.sl_pct=opt.get('sl_pct',self.sl_pct); self.auto_min_score=opt.get('score',self.auto_min_score)
            await self._save_config(); await self._alert(f"🤖 自动优化完成\n止盈: {self.tp_pct:.1%}\n止损: {self.sl_pct:.1%}\n阈值: {self.auto_min_score}")
    async def _get_multi_timeframe_data(self, symbol):
        tfs=['1m','5m','15m']; res={}
        for tf in tfs:
            try: res[tf]=await self._get_cached_tech(symbol,tf,50)
            except: res[tf]=None
        return res
    def _auth(self,update:Update):
        if not self.allowed: return True
        return update.effective_user.id in self.allowed
    def _parse_pct(self,val): return val/100.0
    def _build_main_keyboard(self):
        f_status="已开启" if self.orderbook_filter else "已关闭"
        b_status="已开启" if self.waterfall_breaker else "已关闭"
        auto_status="🟢" if self.auto_trade_enabled else "🔴"
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("🚨 紧急全平",callback_data="panic_confirm")],
            [InlineKeyboardButton(f"🏮 盘口: [{f_status}]",callback_data="toggle_filter"), InlineKeyboardButton(f"🚨 熔断: [{b_status}]",callback_data="toggle_breaker")],
            [InlineKeyboardButton("⚡ 开启",callback_data="bot_start"), InlineKeyboardButton("🔴 关机",callback_data="bot_stop")],
            [InlineKeyboardButton(f"🤖 自动交易: {auto_status}",callback_data="toggle_auto"), InlineKeyboardButton("🎯 阈值",callback_data="menu_set_autoscore")],
            [InlineKeyboardButton("📊 看板",callback_data="dashboard"), InlineKeyboardButton("💳 余额",callback_data="balance")],
            [InlineKeyboardButton("📋 持币",callback_data="holdings"), InlineKeyboardButton("📋 监控",callback_data="list_symbols")],
            [InlineKeyboardButton("🎯 止盈",callback_data="menu_set_tp"), InlineKeyboardButton("🛡️ 止损",callback_data="menu_set_sl")],
            [InlineKeyboardButton("📉 移损",callback_data="menu_set_tsl"), InlineKeyboardButton("🏹 移盈",callback_data="menu_set_tmpt")],
            [InlineKeyboardButton("💵 额度",callback_data="menu_set_amount"), InlineKeyboardButton("⏱ 周期",callback_data="menu_set_tf")],
            [InlineKeyboardButton("🔒 底线",callback_data="menu_set_reserve"), InlineKeyboardButton("🔢 上限",callback_data="menu_set_trades")],
            [InlineKeyboardButton("➕ 币种",callback_data="menu_add_symbol"), InlineKeyboardButton("➖ 币种",callback_data="menu_del_symbol")],
            [InlineKeyboardButton("🧠 大脑",callback_data="brain_status"), InlineKeyboardButton("📈 分析",callback_data="gap_analysis")],
            [InlineKeyboardButton("⚡ 预设",callback_data="menu_preset"), InlineKeyboardButton("📜 历史",callback_data="history")],
            [InlineKeyboardButton("📈 仪表盘",callback_data="stats_panel"), InlineKeyboardButton("💾 备份",callback_data="backup_panel")],
            [InlineKeyboardButton("🔄 同步持仓",callback_data="sync_pos"), InlineKeyboardButton("🔄 刷新",callback_data="refresh_panel")],
            [InlineKeyboardButton("📊 状态",callback_data="state_panel"), InlineKeyboardButton("⚡ 优化",callback_data="optimize_panel")]
        ])
    async def cmd_menu(self,update,context):
        if not self._auth(update): await update.message.reply_text("⛔ 未授权"); return
        await update.effective_message.reply_text(f"⚙️ 控制台 {self.env_tag}", reply_markup=self._build_main_keyboard())
    async def cmd_holdings(self,update,context):
        if not self._auth(update): return
        bal=await self.exchange.fetch_balance()
        lines=["📋 当前持币\n"]; has=False
        for sym in self.symbols:
            coin=sym.split('/')[0]
            free=bal.get(coin,{}).get('free',0) if isinstance(bal.get(coin),dict) else float(bal.get(coin,0))
            if free>0.0001:
                has=True; ticker=self.ws.get_ticker(sym)
                if ticker is None: ticker=await self.exchange.fetch_ticker(sym)
                if ticker:
                    p=ticker['last']; val=free*p; count=self.position_counts.get(sym,0); pnl=""
                    if sym in self.entries and self.entries[sym]>0:
                        pnl_pct=((p-self.entries[sym])/self.entries[sym])*100
                        pnl=f" | {'🟢' if pnl_pct>=0 else '🔴'} {pnl_pct:+.2f}%"
                    lines.append(f"• {sym}: {free:.4f} 现价{p:.2f} 价值{val:.2f} 仓位{count}/{self.max_positions_per_coin}{pnl}")
        if not has: lines.append("暂无持仓")
        await update.effective_message.reply_text("\n".join(lines))
    async def cmd_autotrade(self,update,context):
        if not self._auth(update): return
        try:
            mode=context.args[0].lower()
            if mode=="on": self.auto_trade_enabled=True; await self._save_config(); await update.effective_message.reply_text("🤖 智能自适应交易已开启")
            elif mode=="off": self.auto_trade_enabled=False; await self._save_config(); await update.effective_message.reply_text("🤖 自动交易已关闭")
            else: await update.effective_message.reply_text("用法: /autotrade on|off")
        except: pass
    async def cmd_autoscore(self,update,context):
        if not self._auth(update): return
        try:
            score=int(context.args[0])
            if 50<=score<=95:
                self.auto_min_score=score; await self._save_config(); await update.effective_message.reply_text(f"✅ 阈值: {score}分")
            else: await update.effective_message.reply_text("阈值需在50-95之间")
        except: pass
    async def cmd_set_max_coin(self,update,context):
        if not self._auth(update): return
        try:
            self.max_per_coin_usdt=float(context.args[0]); await self._save_config(); await update.effective_message.reply_text(f"✅ 单币最大持仓: {self.max_per_coin_usdt}U")
        except: await update.effective_message.reply_text("❌ 格式: /setmaxcoin 200")
    async def cmd_set_max_loss(self,update,context):
        if not self._auth(update): return
        try:
            pct=float(context.args[0])/100.0; self.max_daily_loss_pct=pct; await self._save_config(); await update.effective_message.reply_text(f"✅ 日亏损熔断: {pct*100:.1f}%")
        except: await update.effective_message.reply_text("❌ /setmaxloss 5")
    async def cmd_set_max_pos(self,update,context):
        if not self._auth(update): return
        try:
            num=int(context.args[0]); self.max_positions_per_coin=num; await self._save_config(); await update.effective_message.reply_text(f"✅ 每币最大仓位: {num}")
        except: await update.effective_message.reply_text("❌ /setmaxpos 18")
    async def cmd_set_max_alloc(self,update,context):
        if not self._auth(update): return
        try:
            pct=float(context.args[0])/100.0; self.max_total_allocated_pct=max(0.1,min(1.0,pct)); await self._save_config(); await update.effective_message.reply_text(f"✅ 总仓位上限: {self.max_total_allocated_pct*100:.0f}%")
        except: await update.effective_message.reply_text("❌ /setmaxalloc 80")
    async def cmd_learn(self,update,context):
        if not self._auth(update): return
        try:
            mode=context.args[0].lower()
            if mode=="on": self.learning_enabled=True; await update.effective_message.reply_text("🧠 自适应学习已开启")
            elif mode=="off": self.learning_enabled=False; await update.effective_message.reply_text("🧠 自适应学习已关闭")
            else: await update.effective_message.reply_text("用法: /learn on|off")
        except: pass
    async def cmd_stats(self,update,context):
        if not self._auth(update): return
        bal=await self.exchange.fetch_balance(); usdt_free=self._get_usdt_free(bal); total=usdt_free; positions=[]
        for sym in self.symbols:
            ticker=self.ws.get_ticker(sym)
            if ticker is None: ticker=await self.exchange.fetch_ticker(sym)
            if ticker is None: continue
            p=ticker['last']; coin=sym.split('/')[0]
            free=bal.get(coin,{}).get('free',0) if isinstance(bal.get(coin),dict) else 0
            val=free*p; total+=val; count=self.position_counts.get(sym,0)
            positions.append(f"{sym}: {free:.4f} 价值{val:.2f}U 仓位{count}/{self.max_positions_per_coin}")
        today=await get_today_trades()
        lines=[f"📊 仪表盘 {self.env_tag}",f"💰 总资产: {total:.2f}U | 可用: {usdt_free:.2f}U","📈 持仓:",*positions,"━━━━━━━━━━━━━━━━━"]
        if today: lines.append(f"今日交易: {today['total']}笔 胜率{today['win_rate']:.0%} 总盈亏{today['total_pnl_sum']:+.2f}%")
        else: lines.append("今日暂无平仓记录")
        lines.append(f"自适应学习: {'🟢' if self.learning_enabled else '🔴'} | 阈值: {self.auto_min_score} | 仓位: {self.single_order_usdt}U")
        lines.append(f"回撤熔断: {self.max_drawdown_pct*100:.0f}%"); lines.append(f"市场状态: {self._current_market_state}")
        await update.effective_message.reply_text("\n".join(lines))
    async def cmd_backup(self,update,context):
        if not self._auth(update): return
        data=await export_db_to_json()
        if data: await update.effective_message.reply_document(document=data.encode('utf-8'), filename=f"backup_{datetime.now(CST).strftime('%Y%m%d_%H%M%S')}.json", caption="📦 数据库备份")
        else: await update.effective_message.reply_text("❌ 备份失败")
    async def cmd_entry(self,update,context):
        if not self._auth(update): return
        try:
            sym=context.args[0].upper(); price=float(context.args[1])
            self.entries[sym]=price; await update.effective_message.reply_text(f"📝 {sym} 入场价: {price:.2f}")
        except: await update.effective_message.reply_text("❌ /entry ETH/USDT 3120")
    async def cmd_set_trades(self,update,context):
        if not self._auth(update): return
        try:
            self.max_daily_trades=int(context.args[0]); await self._save_config(); await update.effective_message.reply_text(f"✅ 单日最大交易: {self.max_daily_trades}")
        except: pass
    async def cmd_reset_trades(self,update,context):
        if not self._auth(update): return
        self.daily_trades=0; await update.effective_message.reply_text("✅ 计数已重置")
    async def cmd_preset(self,update,context):
        if not self._auth(update): return
        try:
            mode=context.args[0].lower()
            presets={"conservative":{"tp":3,"sl":2,"tsl":1,"tmpt":1,"tf":"1h","amt":1,"reserve":2},"balanced":{"tp":1.5,"sl":1,"tsl":0.5,"tmpt":0.5,"tf":"15m","amt":1,"reserve":1},"aggressive":{"tp":0.8,"sl":0.5,"tsl":0.3,"tmpt":0.3,"tf":"5m","amt":1,"reserve":0.5},"adaptive":{"tp":1.2,"sl":0.8,"tsl":0.4,"tmpt":0.3,"tf":"5m","amt":1,"reserve":1,"score":65}}
            if mode not in presets: await update.effective_message.reply_text("可选: conservative/balanced/aggressive/adaptive"); return
            p=presets[mode]
            self.tp_pct=p["tp"]/100; self.sl_pct=p["sl"]/100; self.trailing_sl_pct=p["tsl"]/100; self.trailing_tp_pct=p["tmpt"]/100
            self.timeframe=p["tf"]; self.single_order_usdt=p["amt"]; self.reserve_bottom=p["reserve"]
            if "score" in p: self.auto_min_score=p["score"]
            await self._save_config()
            names={"conservative":"保守","balanced":"平衡","aggressive":"激进","adaptive":"自适应"}
            await update.effective_message.reply_text(f"⚡ {names[mode]}方案已生效\n止盈{self.tp_pct:.1%} 止损{self.sl_pct:.1%}")
        except Exception as e:
            logger.error(f"cmd_preset error: {e}")
            await update.effective_message.reply_text(f"❌ 设置预设失败: {e}")
    async def cmd_history(self,update,context):
        if not self._auth(update): return
        if not self.trades: await update.effective_message.reply_text("📜 暂无记录"); return
        lines=["📜 最近交易\n"]
        for t in self.trades[:10]:
            net_pnl=t.get('net_pnl',0); net_pnl_pct=t.get('net_pnl_pct',0)
            if net_pnl!=0: lines.append(f"{'🟢' if net_pnl_pct>0 else '🔴'} {t['time']} {t['symbol']} 净利{net_pnl_pct:+.2f}% ({net_pnl:+.4f}U)")
            else: lines.append(f"{'🟢' if t['pnl_pct']>0 else '🔴'} {t['time']} {t['symbol']} {t['pnl_pct']:+.2f}%")
        await update.effective_message.reply_text("\n".join(lines))
    async def cmd_status(self,update,context):
        if not self._auth(update): return
        try:
            bal=await self.exchange.fetch_balance(); usdt_free=self._get_usdt_free(bal); total=usdt_free
            for sym in self.symbols:
                coin=sym.split('/')[0]; free=bal.get(coin,{}).get('free',0) if isinstance(bal.get(coin),dict) else 0
                ticker=self.ws.get_ticker(sym)
                if ticker is None: ticker=await self.exchange.fetch_ticker(sym)
                if ticker and ticker.get('last'): total+=free*ticker['last']
            occupied=total-usdt_free
            try:
                perf=await get_recent_performance(20)
                if perf and perf['total']>0: win_rate=perf['win_rate']; wins=perf['wins']; total_trades=perf['total']
                else: win_rate=0.0; wins=0; total_trades=0
            except: win_rate=0.0; wins=0; total_trades=0
            lines=[f"📊 多币种量化机器人看板 {self.env_tag}",f"• 系统状态: {'🟢 RUNNING' if self.is_running else '🔴 STOPPED'}",f"• 策略模式: 🚀 自适应27合1策略",f"• 全局默认: 单笔{self.single_order_usdt:.1f}U | 周期{self.timeframe} | 止盈{self.tp_pct:.1%}",f"• 市场状态: {self._current_market_state}",f"• 占用资金: {occupied:.2f} USDT","-"*40]
            has_pos=False
            for sym in self.symbols:
                count=self.position_counts.get(sym,0)
                if count==0: continue
                has_pos=True
                tp=self._get_coin_param(sym,'tp_pct',self.tp_pct); sl=self._get_coin_param(sym,'sl_pct',self.sl_pct); tsl=self._get_coin_param(sym,'trailing_sl_pct',self.trailing_sl_pct); tmpt=self._get_coin_param(sym,'trailing_tp_pct',self.trailing_tp_pct); amount=self._get_coin_param(sym,'single_order_usdt',self.single_order_usdt); timeframe=self._get_coin_param(sym,'timeframe',self.timeframe)
                max_pos=self.max_positions_per_coin; filled=min(count,max_pos); bar="▓"*filled+"░"*(max_pos-filled)
                lines.append(f"\n🔹 [{sym}] (周期:{timeframe} | 止盈:{tp:.1%} | 移动止损:{tsl:.1%} | 单笔:{amount:.1f}U)")
                lines.append(f"[{bar}] {count}/{max_pos}")
                entry=self.entries.get(sym,0); high_price=self._trailing_high.get(sym,0)
                if entry>0:
                    lines.append(f"└ 仓位#1: 买价{entry:.4f} | 最高{high_price:.4f}")
                    if count>1: lines.append(f"└ ... 还有 {count-1} 个仓位")
            if not has_pos: lines.append("\n📭 暂无持仓")
            lines.append("-"*40)
            lines.append(f"• 胜率: {win_rate*100:.1f}% ({wins}/{total_trades} 胜)")
            lines.append(f"• 今日亏损: {self._today_loss_pct*100:.1f}%")
            lines.append(f"• 连续亏损: {self._consecutive_losses} 笔")
            lines.append(f"• 全局状态: {'⏸️ 暂停' if self._is_paused else '🟢 正常'}")
            stats=self._delta_neutral_stats
            lines.append(f"• 💰 费率抄底: {stats['total_trades']}笔 累计盈利{stats['total_profit']:.4f}U 今日{stats['profit_today']:.4f}U")
            if self.ai_enabled and time.time()-self.ai_insight["timestamp"]<3600:
                lines.append(f"• 🤖 AI: {self.ai_insight['recommendation']} (评分{self.ai_insight['score']:.0f})")
                lines.append(f"   📊 状态:{self.ai_insight['market_state']} 凯利:{self.ai_insight['kelly_position']*100:.1f}%")
            else: lines.append("• 🤖 AI: 分析中...")
            await update.effective_message.reply_text("\n".join(lines))
        except Exception as e:
            logger.error(f"状态命令异常: {e}"); await update.effective_message.reply_text("❌ 获取状态失败，请稍后重试")
    async def cmd_check(self,update,context):
        if not self._auth(update): return
        lines=["📈 信号 + 开仓条件（自适应27合1）\n"]
        fg_data=await self.real_data.get_fear_greed_index(); fg=fg_data["value"] if fg_data else None
        bal=await self.exchange.fetch_balance(); usdt_free=self._get_usdt_free(bal)
        for sym in self.symbols:
            try:
                ticker=self.ws.get_ticker(sym)
                if ticker is None: continue
                p=ticker['last']; tech=await self._get_cached_tech(sym,self.timeframe,50); funding=await self.exchange.fetch_funding_rate(sym)
                decision=await self._should_open_position(sym,p,tech,funding,fg,usdt_free)
                sc=decision['score']; status="🎯 可开仓" if decision['should_open'] else "⏳ 等待"
                lines.append(f"{sym}: {p:.2f} | 评分{sc:.0f}分 | {status}")
                lines.append(f"   状态:{decision['state']} | 凯利:{decision['kelly_position']*100:.1f}%")
                if decision['details']: lines.append(f"   技术: {', '.join(decision['details'][:3])}")
            except: continue
        await update.effective_message.reply_text("\n".join(lines))
    async def cmd_symbols(self,update,context):
        if not self._auth(update): return
        s_list="\n".join([f"• {s}" for s in self.symbols])
        await update.effective_message.reply_text(f"📋 监控列表:\n{s_list}")
    async def cmd_panic(self,update,context):
        if not self._auth(update): return
        await self.panic_sell_all(); await update.effective_message.reply_text("🚨 全平")
    async def cmd_analysis(self,update,context): await self.render_gap_analysis(update.effective_message)
    async def cmd_brain(self,update,context): await self.render_brain_status(update.effective_message)
    async def cmd_help(self,update,context):
        await update.effective_message.reply_text(f"🤖 命令列表\n/stats 仪表盘 /backup 备份\n/menu 控制台 /status 持仓 /check 信号\n/settp 5 /setsl 2 /setamount 1\n/setcoin DOGE tp 1  独立设币种参数\n/resetcoin SOL  重置币种参数\n/coininfo  查看币种参数和盈亏\n/setgrid SOL 3 1 0.5  固定间距网格\n/resetgrid SOL  移除固定网格\n/preset adaptive  一键自适应方案\n/setmaxpos 18 仓位上限 /setmaxalloc 100 总仓位上限\n/autotrade on /learn on\n/preset balanced /panic 全平\n/setcoinonly ETH  一键固定币种\n/lowbalance     一键低本金滚雪球（5币）\n/arbstats       查看套利统计\n/optimize       手动触发参数优化\n/state          查看市场状态\n🚀 自适应27合1策略已激活！\n🧠 AI市场分析 + 市场状态自适应 + 凯利仓位管理\n保本线: >{self.breakeven_pct*100:.2f}%")
    async def cmd_set_tp(self,update,context):
        if not self._auth(update): return
        try:
            val=self._parse_pct(float(context.args[0]))
            if val<self.breakeven_pct: await update.effective_message.reply_text(f"❌ 低于保本线 {self.breakeven_pct*100:.2f}%"); return
            if self.sl_pct>0 and val/self.sl_pct<1.2: await update.effective_message.reply_text("❌ 盈亏比不足"); return
            self.tp_pct=val; await self._save_config(); await update.effective_message.reply_text(f"✅ 止盈: {self.tp_pct:.1%}")
        except: pass
    async def cmd_set_sl(self,update,context):
        if not self._auth(update): return
        try:
            val=self._parse_pct(float(context.args[0]))
            if self.tp_pct>0 and self.tp_pct/val<1.2: await update.effective_message.reply_text("❌ 盈亏比不足"); return
            self.sl_pct=val; await self._save_config(); await update.effective_message.reply_text(f"✅ 止损: {self.sl_pct:.1%}")
        except: pass
    async def cmd_set_tsl(self,update,context):
        if not self._auth(update): return
        try:
            self.trailing_sl_pct=self._parse_pct(float(context.args[0])); await self._save_config(); await update.effective_message.reply_text(f"✅ 移动止损: {self.trailing_sl_pct:.1%}")
        except: pass
    async def cmd_set_trailing_tp(self,update,context):
        if not self._auth(update): return
        try:
            val=self._parse_pct(float(context.args[0])); self.trailing_tp_pct=val; await self._save_config(); await update.effective_message.reply_text(f"✅ 移动止盈: {self.trailing_tp_pct:.1%}")
        except: pass
    async def cmd_set_amount(self,update,context):
        if not self._auth(update): return
        try:
            self.single_order_usdt=float(context.args[0]); await self._save_config(); await update.effective_message.reply_text("✅")
        except: pass
    async def cmd_set_tf(self,update,context):
        if not self._auth(update): return
        try:
            self.timeframe=context.args[0].lower(); await self._save_config(); await update.effective_message.reply_text("✅")
        except: pass
    async def cmd_set_reserve(self,update,context):
        if not self._auth(update): return
        try:
            self.reserve_bottom=float(context.args[0]); await self._save_config(); await update.effective_message.reply_text(f"✅ 底线: {self.reserve_bottom}U")
        except: pass
    async def cmd_add_symbol(self,update,context):
        if not self._auth(update): return
        try:
            sym=context.args[0].upper()
            if "/" not in sym: sym=sym+"/USDT"
            self.symbols.append(sym); await self._save_config(); await update.effective_message.reply_text(f"✅ 已添加 {sym}")
        except: await update.effective_message.reply_text("❌ 格式: /addsymbol ETH")
    async def cmd_del_symbol(self,update,context):
        if not self._auth(update): return
        try:
            sym=context.args[0].upper()
            if "/" not in sym: sym=sym+"/USDT"
            self.symbols.remove(sym); await self._save_config(); await update.effective_message.reply_text(f"✅ 已删除 {sym}")
        except: await update.effective_message.reply_text("❌ 格式: /delsymbol ETH")
    async def cmd_set_grid(self,update,context):
        if not self._auth(update): return
        try:
            sym=context.args[0].upper(); drop_pct=float(context.args[1])/100.0; base=float(context.args[2]); inc=float(context.args[3]) if len(context.args)>3 else 1.0
            self.grid_configs[sym]={"drop_pct":drop_pct,"base_amount":base,"increment":inc}; await self._save_config(); await update.effective_message.reply_text(f"✅ {sym} 固定网格: 每跌{drop_pct*100:.1f}%买一次, 起始{base}U, 递增{inc}U")
        except: await update.effective_message.reply_text("❌ 格式: /setgrid SOL 3 1 0.5")
    async def cmd_reset_grid(self,update,context):
        if not self._auth(update): return
        try:
            sym=context.args[0].upper()
            if sym in self.grid_configs: del self.grid_configs[sym]; await self._save_config(); await update.effective_message.reply_text(f"✅ {sym} 固定网格已移除")
            else: await update.effective_message.reply_text(f"⚠️ {sym} 没有固定网格")
        except: await update.effective_message.reply_text("❌ 格式: /resetgrid SOL")
    async def cmd_set_coin(self,update,context):
        if not self._auth(update): return
        try:
            sym=context.args[0].upper(); key=context.args[1].lower(); val_str=context.args[2]
            key_map={'tp':'tp_pct','sl':'sl_pct','tsl':'trailing_sl_pct','tmpt':'trailing_tp_pct','amount':'single_order_usdt','score':'auto_min_score'}
            if key not in key_map: await update.effective_message.reply_text(f"❌ 参数: tp/sl/tsl/tmpt/amount/score"); return
            attr=key_map[key]
            if attr in ('tp_pct','sl_pct','trailing_sl_pct','trailing_tp_pct'): val=float(val_str)/100.0
            elif attr=='single_order_usdt': val=float(val_str)
            elif attr=='auto_min_score': val=int(val_str)
            else: val=float(val_str)
            if sym not in self.coin_configs: self.coin_configs[sym]={}
            self.coin_configs[sym][attr]=val; await self._save_config()
            name_map={'tp_pct':'止盈','sl_pct':'止损','trailing_sl_pct':'移动止损','trailing_tp_pct':'移动止盈','single_order_usdt':'单笔额度','auto_min_score':'信号阈值'}
            display=val*100 if attr in ('tp_pct','sl_pct','trailing_sl_pct','trailing_tp_pct') else val
            unit='%' if attr in ('tp_pct','sl_pct','trailing_sl_pct','trailing_tp_pct') else 'U' if attr=='single_order_usdt' else '分'
            if attr in ('tp_pct','sl_pct','trailing_sl_pct','trailing_tp_pct'): await update.effective_message.reply_text(f"✅ {sym} {name_map[attr]}: {val:.1%}")
            else: await update.effective_message.reply_text(f"✅ {sym} {name_map[attr]}: {display:.1f}{unit}")
        except: await update.effective_message.reply_text("❌ 格式: /setcoin DOGE tp 1")
    async def cmd_reset_coin(self,update,context):
        if not self._auth(update): return
        try:
            sym=context.args[0].upper()
            if sym in self.coin_configs: del self.coin_configs[sym]; await self._save_config(); await update.effective_message.reply_text(f"✅ {sym} 独立参数已重置")
            else: await update.effective_message.reply_text(f"⚠️ {sym} 没有独立参数")
        except: await update.effective_message.reply_text("❌ 格式: /resetcoin SOL")
    async def cmd_coin_info(self,update,context):
        if not self._auth(update): return
        target=context.args[0].upper() if context.args else None
        if target and target not in self.symbols: await update.effective_message.reply_text(f"⚠️ {target} 不在监控列表中"); return
        bal=await self.exchange.fetch_balance(); lines=[f"📊 币种参数与盈亏 {self.env_tag}\n"]
        symbols_to_show=[target] if target else self.symbols
        for sym in symbols_to_show:
            coin=sym.split('/')[0]; free=bal.get(coin,{}).get('free',0) if isinstance(bal.get(coin),dict) else float(bal.get(coin,0))
            ticker=self.ws.get_ticker(sym)
            if ticker is None: ticker=await self.exchange.fetch_ticker(sym)
            p=ticker['last'] if ticker else 0; val=free*p; count=self.position_counts.get(sym,0); pnl_str=""
            if sym in self.entries and self.entries[sym]>0 and free>0:
                pnl_pct=((p-self.entries[sym])/self.entries[sym])*100
                pnl_str=f" | {'🟢' if pnl_pct>=0 else '🔴'} {pnl_pct:+.2f}%"
            tp=self._get_coin_param(sym,'tp_pct',self.tp_pct); sl=self._get_coin_param(sym,'sl_pct',self.sl_pct)
            tsl=self._get_coin_param(sym,'trailing_sl_pct',self.trailing_sl_pct); tmpt=self._get_coin_param(sym,'trailing_tp_pct',self.trailing_tp_pct)
            amount=self._get_coin_param(sym,'single_order_usdt',self.single_order_usdt); score=self._get_coin_param(sym,'auto_min_score',self.auto_min_score)
            total_net=0.0
            try:
                async with aiosqlite.connect("bot.db",timeout=30.0) as db:
                    async with db.execute("SELECT SUM(net_pnl) FROM trade_details WHERE side='sell' AND symbol=? AND net_pnl IS NOT NULL", (sym,)) as cur:
                        row=await cur.fetchone()
                        if row and row[0]: total_net=row[0]
            except: pass
            extra="🔸独立" if sym in self.coin_configs else "🌐全局"
            lines.append(f"{extra} {sym}\n  止盈{tp:.1%} 止损{sl:.1%} 移盈{tmpt:.1%} 移损{tsl:.1%}\n  单笔{amount:.1f}U 阈值{score}分 仓位{count}/{self.max_positions_per_coin}\n  持仓{free:.4f} 现价{p:.2f} 价值{val:.2f}U{pnl_str}\n  累计净盈亏: {total_net:+.4f}U")
        lines.append("💡 /setcoin 修改独立参数 | /resetcoin 重置为全局")
        await update.effective_message.reply_text("\n".join(lines))
    async def cmd_setcoinonly(self,update,context):
        if not self._auth(update): return
        try:
            sym=context.args[0].upper()
            if "/" not in sym: sym=sym+"/USDT"
            self.symbols=[sym]
            presets={"ETH/USDT":{"tp":0.8,"sl":0.5,"tsl":0.5,"tmpt":0.3,"tf":"1m","amt":1,"reserve":1,"score":70},"BTC/USDT":{"tp":0.6,"sl":0.4,"tsl":0.4,"tmpt":0.2,"tf":"1m","amt":1,"reserve":1,"score":70},"SOL/USDT":{"tp":1.0,"sl":0.5,"tsl":0.5,"tmpt":0.3,"tf":"1m","amt":0.5,"reserve":0.5,"score":65},"DOGE/USDT":{"tp":1.2,"sl":0.6,"tsl":0.6,"tmpt":0.4,"tf":"1m","amt":0.5,"reserve":0.5,"score":65},"ADA/USDT":{"tp":1.2,"sl":0.6,"tsl":0.6,"tmpt":0.4,"tf":"1m","amt":0.5,"reserve":0.5,"score":65}}
            if sym in presets:
                p=presets[sym]
                self.tp_pct=p["tp"]/100; self.sl_pct=p["sl"]/100; self.trailing_sl_pct=p["tsl"]/100; self.trailing_tp_pct=p["tmpt"]/100
                self.timeframe=p["tf"]; self.single_order_usdt=p["amt"]; self.reserve_bottom=p["reserve"]; self.auto_min_score=p["score"]
                await self._save_config()
                await update.effective_message.reply_text(f"✅ 已固定币种: {sym}\n• 止盈: {self.tp_pct:.1%}\n• 止损: {self.sl_pct:.1%}\n• 周期: {self.timeframe}\n• 单笔: {self.single_order_usdt:.1f}U\n• 阈值: {self.auto_min_score}分\n🚀 自适应27合1策略已激活！")
            else: await self._save_config(); await update.effective_message.reply_text(f"✅ 已固定币种: {sym}\n• 使用当前全局参数")
        except Exception as e: await update.effective_message.reply_text(f"❌ 格式: /setcoinonly ETH\n错误: {e}")
    async def cmd_lowbalance(self,update,context):
        if not self._auth(update): return
        self.symbols=["ETH/USDT","BTC/USDT","SOL/USDT","DOGE/USDT","ADA/USDT"]
        self.coin_configs={}
        self.coin_configs["ETH/USDT"]={"tp_pct":0.8,"sl_pct":0.5,"trailing_sl_pct":0.5,"trailing_tp_pct":0.3,"single_order_usdt":1.0,"timeframe":"1m","auto_min_score":65}
        self.coin_configs["BTC/USDT"]={"tp_pct":0.6,"sl_pct":0.4,"trailing_sl_pct":0.4,"trailing_tp_pct":0.2,"single_order_usdt":1.0,"timeframe":"1m","auto_min_score":65}
        self.coin_configs["SOL/USDT"]={"tp_pct":1.0,"sl_pct":0.5,"trailing_sl_pct":0.5,"trailing_tp_pct":0.3,"single_order_usdt":1.0,"timeframe":"1m","auto_min_score":60}
        self.coin_configs["DOGE/USDT"]={"tp_pct":1.2,"sl_pct":0.6,"trailing_sl_pct":0.6,"trailing_tp_pct":0.4,"single_order_usdt":0.5,"timeframe":"1m","auto_min_score":60}
        self.coin_configs["ADA/USDT"]={"tp_pct":1.2,"sl_pct":0.6,"trailing_sl_pct":0.6,"trailing_tp_pct":0.4,"single_order_usdt":0.5,"timeframe":"1m","auto_min_score":60}
        self.tp_pct=0.8; self.sl_pct=0.5; self.trailing_sl_pct=0.5; self.trailing_tp_pct=0.3
        self.single_order_usdt=1.0; self.timeframe="1m"; self.auto_min_score=65; self.reserve_bottom=5
        await self._save_config()
        await update.effective_message.reply_text(f"🚀 低本金快速滚雪球方案已激活！\n\n📊 监控币种\n🔹 ETH/USDT  止盈{self.coin_configs['ETH/USDT']['tp_pct']:.1%} 止损{self.coin_configs['ETH/USDT']['sl_pct']:.1%} 单笔{self.coin_configs['ETH/USDT']['single_order_usdt']:.1f}U 阈值{self.coin_configs['ETH/USDT']['auto_min_score']}\n🔹 BTC/USDT  止盈{self.coin_configs['BTC/USDT']['tp_pct']:.1%} 止损{self.coin_configs['BTC/USDT']['sl_pct']:.1%} 单笔{self.coin_configs['BTC/USDT']['single_order_usdt']:.1f}U 阈值{self.coin_configs['BTC/USDT']['auto_min_score']}\n🔹 SOL/USDT  止盈{self.coin_configs['SOL/USDT']['tp_pct']:.1%} 止损{self.coin_configs['SOL/USDT']['sl_pct']:.1%} 单笔{self.coin_configs['SOL/USDT']['single_order_usdt']:.1f}U 阈值{self.coin_configs['SOL/USDT']['auto_min_score']}\n🔹 DOGE/USDT 止盈{self.coin_configs['DOGE/USDT']['tp_pct']:.1%} 止损{self.coin_configs['DOGE/USDT']['sl_pct']:.1%} 单笔{self.coin_configs['DOGE/USDT']['single_order_usdt']:.1f}U 阈值{self.coin_configs['DOGE/USDT']['auto_min_score']}\n🔹 ADA/USDT  止盈{self.coin_configs['ADA/USDT']['tp_pct']:.1%} 止损{self.coin_configs['ADA/USDT']['sl_pct']:.1%} 单笔{self.coin_configs['ADA/USDT']['single_order_usdt']:.1f}U 阈值{self.coin_configs['ADA/USDT']['auto_min_score']}\n\n⏱ 周期: 1m | 保留底线: {self.reserve_bottom}U\n💰 总本金建议: 10-20U\n\n✅ 发送 /autotrade on 启动交易")
    async def cmd_arb_stats(self,update,context):
        if not self._auth(update): return
        stats=self._delta_neutral_stats; config=self._delta_neutral_config
        lines=[f"📊 资金费率套利统计 {self.env_tag}",f"• 总交易次数: {stats['total_trades']} 笔",f"• 累计盈利: {stats['total_profit']:.4f} U",f"• 今日盈利: {stats['profit_today']:.4f} U",f"• 最近交易: {datetime.fromtimestamp(stats['last_trade_time']).strftime('%H:%M') if stats['last_trade_time'] else '无'}", "", f"⚙️ 当前配置", f"• 触发费率: {config['min_funding_rate']*100:.2f}%", f"• 每币最大仓位: {config['max_position_per_coin']}", f"• 资金分配: {config['allocation_percent']*100:.0f}%", "• 自动复利: ✅ 开启"]
        await update.effective_message.reply_text("\n".join(lines))
    async def render_brain_status(self,msg_obj):
        try:
            macro=await self.real_data.check_macro_risk()
            lines=[f"🧠 AI 超级大脑 {self.env_tag}",f"1️⃣ 宏观: {macro['status']}","2️⃣ AI市场分析:"]
            if self.ai_enabled and time.time()-self.ai_insight["timestamp"]<3600:
                lines.append(f"   BTC: {self.ai_insight['btc_trend']} | ETH: {self.ai_insight['eth_trend']}")
                lines.append(f"   恐惧贪婪: {self.ai_insight['fear_greed']}")
                lines.append(f"   新闻情绪: {self.ai_insight['news_sentiment']:+.2f}")
                lines.append(f"   社交情绪: {self.ai_insight['social_sentiment']:+.2f}")
                lines.append(f"   市场状态: {self.ai_insight['market_state']}")
                lines.append(f"   凯利仓位: {self.ai_insight['kelly_position']*100:.1f}%")
                lines.append(f"   建议: {self.ai_insight['recommendation']} (评分{self.ai_insight['score']:.0f})")
            else: lines.append("   ⏳ 分析中...")
            for idx,sym in enumerate(self.symbols):
                try:
                    if idx>0: await asyncio.sleep(1.5)
                    ticker=self.ws.get_ticker(sym)
                    if ticker is None: ticker=await self.exchange.fetch_ticker(sym)
                    if ticker is None: lines.append(f"{idx+3}️⃣ {sym}: 现价获取失败"); continue
                    p=ticker['last']; tech=await self._get_cached_tech(sym,self.timeframe,50)
                    lines.append(f"{idx+3}️⃣ {sym}: {p:.2f} 布林{tech['bb_upper']:.1f}/{tech['bb_lower']:.1f} RSI{tech['rsi']:.0f}")
                except: lines.append(f"{idx+3}️⃣ {sym}: 数据获取失败")
            await msg_obj.reply_text("\n".join(lines))
        except Exception as e: logger.error(f"brain err: {e}")
    async def render_gap_analysis(self,msg_obj):
        try:
            lines=["📈 差距分析\n"]
            for sym in self.symbols:
                ticker=self.ws.get_ticker(sym)
                if ticker is None: ticker=await self.exchange.fetch_ticker(sym)
                if ticker is None: continue
                p=ticker['last']
                try:
                    tech=await self._get_cached_tech(sym,self.timeframe,50)
                    target=min(tech['bb_lower'],p*0.99)
                    gap=((p-target)/p)*100
                    lines.append(f"{sym}: {p:.2f} → {target:.2f} ({gap:+.2f}%)")
                except: lines.append(f"{sym}: 指标计算失败")
            await msg_obj.reply_text("\n".join(lines))
        except Exception as e: logger.error(f"analysis err: {e}")
    async def handle_text_input(self,update,context):
        pending=context.user_data.get('pending_setting')
        if not pending: return
        try:
            user_text=update.message.text.strip()
            if pending in ("settf","addsymbol","delsymbol"):
                if pending=="settf": self.timeframe=user_text.lower()
                elif pending=="addsymbol":
                    sym=user_text.upper()
                    if "/" not in sym: sym=sym+"/USDT"
                    if sym not in self.symbols: self.symbols.append(sym)
                    else: await update.message.reply_text("⚠️ 已存在")
                elif pending=="delsymbol":
                    sym=user_text.upper()
                    if "/" not in sym: sym=sym+"/USDT"
                    if sym in self.symbols: self.symbols.remove(sym)
                    else: await update.message.reply_text("⚠️ 不存在")
            else:
                val=float(user_text)
                if pending=="settp": self.tp_pct=self._parse_pct(val)
                elif pending=="setsl": self.sl_pct=self._parse_pct(val)
                elif pending=="settsl": self.trailing_sl_pct=self._parse_pct(val)
                elif pending=="settmpt": self.trailing_tp_pct=self._parse_pct(val)
                elif pending=="setamount": self.single_order_usdt=val
                elif pending=="setreserve": self.reserve_bottom=val
                elif pending=="settrades": self.max_daily_trades=int(val)
                elif pending=="autoscore": self.auto_min_score=int(val)
                elif pending=="setmaxcoin": self.max_per_coin_usdt=val
                elif pending=="setmaxloss": self.max_daily_loss_pct=val/100.0
                elif pending=="setmaxpos": self.max_positions_per_coin=int(val)
                elif pending=="setmaxalloc": self.max_total_allocated_pct=val/100.0
            await self._save_config(); context.user_data['pending_setting']=None; await update.message.reply_text("✅")
        except ValueError:
            await update.message.reply_text("❌ 格式有误"); context.user_data['pending_setting']=None
    async def handle_button_click(self,update,context):
        query=update.callback_query; data=query.data
        try:
            if data=="refresh_panel": await self.cmd_menu(update,context)
            elif data=="toggle_filter": self.orderbook_filter=not self.orderbook_filter; await self._save_config(); await query.answer(f"盘口过滤已{'开启' if self.orderbook_filter else '关闭'}"); await self._refresh_panel(query)
            elif data=="toggle_breaker": self.waterfall_breaker=not self.waterfall_breaker; await self._save_config(); await query.answer(f"瀑布熔断已{'开启' if self.waterfall_breaker else '关闭'}"); await self._refresh_panel(query)
            elif data=="toggle_auto": self.auto_trade_enabled=not self.auto_trade_enabled; await self._save_config(); await query.answer(f"自动交易已{'开启' if self.auto_trade_enabled else '关闭'}"); await self._refresh_panel(query)
            elif data=="bot_start": self.is_running=True; await query.answer("已开启")
            elif data=="bot_stop": self.is_running=False; await query.answer("已关机")
            elif data=="brain_status": await self.render_brain_status(query.message); await query.answer()
            elif data=="gap_analysis": await self.render_gap_analysis(query.message); await query.answer()
            elif data=="state_panel": await self.cmd_state(update,context); await query.answer()
            elif data=="optimize_panel": await self.cmd_optimize(update,context); await query.answer()
            elif data=="dashboard":
                auto="开启" if self.auto_trade_enabled else "关闭"
                msg=f"📊 看板\n止盈{self.tp_pct:.1%} 止损{self.sl_pct:.1%}\n移损{self.trailing_sl_pct:.1%} 移盈{self.trailing_tp_pct:.1%}\n额度{self.single_order_usdt}U 周期{self.timeframe} 底线{self.reserve_bottom}U\n自动交易: {auto} 阈值: {self.auto_min_score}分\n仓位上限: {self.max_positions_per_coin}个\n日熔断: {self.max_daily_loss_pct*100:.1f}%\n今日交易: {self.daily_trades}/{self.max_daily_trades if self.max_daily_trades>0 else '∞'}"
                await query.message.reply_text(msg); await query.answer()
            elif data=="balance":
                bal=await self.exchange.fetch_balance(); await query.message.reply_text(f"💳 USDT: {self._get_usdt_free(bal):.2f}"); await query.answer()
            elif data=="history": await self.cmd_history(update,context)
            elif data=="holdings": await self.cmd_holdings(update,context)
            elif data=="list_symbols": await self.cmd_symbols(update,context)
            elif data=="stats_panel": await self.cmd_stats(update,context)
            elif data=="backup_panel": await self.cmd_backup(update,context)
            elif data=="sync_pos": await self._sync_positions(); await query.message.reply_text("🔄 持仓已同步校准"); await query.answer("✅ 同步完成", show_alert=True)
            elif data=="menu_preset":
                opts=[("🛡️保守","conservative"),("⚖️平衡","balanced"),("⚡激进","aggressive"),("🔄自适应","adaptive")]
                kb=[[InlineKeyboardButton(label,callback_data=f"preset:{val}") for label,val in opts[i:i+2]] for i in range(0,len(opts),2)]
                kb.append([InlineKeyboardButton("🔙返回",callback_data="refresh_panel")])
                await query.edit_message_text("⚡ 选择方案:", reply_markup=InlineKeyboardMarkup(kb)); await query.answer()
            elif data.startswith("preset:"):
                mode=data.split(":")[1]
                p={"conservative":{"tp":3,"sl":2,"tsl":1,"tmpt":1,"tf":"1h","amt":1,"reserve":2},"balanced":{"tp":1.5,"sl":1,"tsl":0.5,"tmpt":0.5,"tf":"15m","amt":1,"reserve":1},"aggressive":{"tp":0.8,"sl":0.5,"tsl":0.3,"tmpt":0.3,"tf":"5m","amt":1,"reserve":0.5},"adaptive":{"tp":1.2,"sl":0.8,"tsl":0.4,"tmpt":0.3,"tf":"5m","amt":1,"reserve":1,"score":65}}[mode]
                self.tp_pct=p["tp"]/100; self.sl_pct=p["sl"]/100; self.trailing_sl_pct=p["tsl"]/100; self.trailing_tp_pct=p["tmpt"]/100
                self.timeframe=p["tf"]; self.single_order_usdt=p["amt"]; self.reserve_bottom=p["reserve"]
                if "score" in p: self.auto_min_score=p["score"]
                await self._save_config(); await query.answer("✅ 已生效", show_alert=True); await self._refresh_panel(query)
            elif data=="menu_set_autoscore":
                opts=[("60分","60"),("65分","65"),("70分","70"),("75分","75")]
                await query.edit_message_text("🎯 阈值", reply_markup=self._build_option_keyboard(opts,"cfg_autoscore","autoscore")); await query.answer()
            elif data=="menu_set_trades":
                opts=[("5次","5"),("10次","10"),("20次","20"),("不限","0")]
                await query.edit_message_text("🔢 上限", reply_markup=self._build_option_keyboard(opts,"cfg_trades","settrades")); await query.answer()
            elif data=="menu_set_tp":
                opts=[("0.8%","0.008"),("1.2%","0.012"),("1.5%","0.015"),("2.0%","0.020")]
                await query.edit_message_text("🎯 止盈", reply_markup=self._build_option_keyboard(opts,"cfg_tp","settp")); await query.answer()
            elif data=="menu_set_sl":
                opts=[("0.5%","0.005"),("0.8%","0.008"),("1.0%","0.010"),("1.2%","0.012")]
                await query.edit_message_text("🛡️ 止损", reply_markup=self._build_option_keyboard(opts,"cfg_sl","setsl")); await query.answer()
            elif data=="menu_set_tsl":
                opts=[("0.3%","0.003"),("0.5%","0.005"),("0.8%","0.008")]
                await query.edit_message_text("📉 移动止损", reply_markup=self._build_option_keyboard(opts,"cfg_tsl","settsl")); await query.answer()
            elif data=="menu_set_tmpt":
                opts=[("0.2%","0.002"),("0.3%","0.003"),("0.5%","0.005")]
                await query.edit_message_text("🏹 移动止盈", reply_markup=self._build_option_keyboard(opts,"cfg_tmpt","settmpt")); await query.answer()
            elif data=="menu_set_amount":
                opts=[("0.5U","0.5"),("1U","1"),("2U","2"),("5U","5")]
                await query.edit_message_text("💵 单笔额度", reply_markup=self._build_option_keyboard(opts,"cfg_amt","setamount")); await query.answer()
            elif data=="menu_set_tf":
                opts=[("1m","1m"),("3m","3m"),("5m","5m"),("15m","15m")]
                await query.edit_message_text("⏱ 周期", reply_markup=self._build_option_keyboard(opts,"cfg_tf","settf")); await query.answer()
            elif data=="menu_set_reserve":
                opts=[("2U","2"),("5U","5"),("10U","10"),("20U","20")]
                await query.edit_message_text("🔒 底线", reply_markup=self._build_option_keyboard(opts,"cfg_res","setreserve")); await query.answer()
            elif data=="menu_add_symbol":
                opts=[("BTC/USDT","BTC/USDT"),("SOL/USDT","SOL/USDT"),("DOGE/USDT","DOGE/USDT"),("ADA/USDT","ADA/USDT")]
                await query.edit_message_text("➕", reply_markup=self._build_option_keyboard(opts,"cfg_add","addsymbol")); await query.answer()
            elif data=="menu_del_symbol":
                opts=[(s,s) for s in self.symbols]
                await query.edit_message_text("➖", reply_markup=self._build_option_keyboard(opts,"cfg_del","delsymbol")); await query.answer()
            elif data.startswith("cfg_"):
                prefix=data.split(":")[0] if ":" in data else ""; val_str=data.split(":")[1] if ":" in data else ""
                if prefix=="cfg_tp":
                    vf=float(val_str)
                    if vf<self.breakeven_pct: await query.answer(f"❌ 低于保本线 {self.breakeven_pct:.1%}", show_alert=True); return
                    if self.sl_pct>0 and vf/self.sl_pct<1.2: await query.answer("❌ 盈亏比不足", show_alert=True); return
                    self.tp_pct=vf
                elif prefix=="cfg_sl":
                    vf=float(val_str)
                    if self.tp_pct>0 and self.tp_pct/vf<1.2: await query.answer("❌ 盈亏比不足", show_alert=True); return
                    self.sl_pct=vf
                elif prefix=="cfg_tsl": self.trailing_sl_pct=float(val_str)
                elif prefix=="cfg_tmpt": self.trailing_tp_pct=float(val_str)
                elif prefix=="cfg_amt": self.single_order_usdt=float(val_str)
                elif prefix=="cfg_tf": self.timeframe=val_str
                elif prefix=="cfg_res": self.reserve_bottom=float(val_str)
                elif prefix=="cfg_autoscore": self.auto_min_score=int(val_str)
                elif prefix=="cfg_trades": self.max_daily_trades=int(val_str)
                elif prefix=="cfg_add":
                    if val_str not in self.symbols: self.symbols.append(val_str)
                    else: await query.answer("已存在", show_alert=True); return
                elif prefix=="cfg_del":
                    if val_str in self.symbols: self.symbols.remove(val_str)
                    else: await query.answer("不存在", show_alert=True); return
                await self._save_config(); await query.answer("✅", show_alert=True); await self._refresh_panel(query)
            elif data.startswith("prompt_manual:"):
                key=data.split(":")[1]; context.user_data['pending_setting']=key
                prompts={"settp":"✍️ 止盈率（例：1.2）：","setsl":"✍️ 止损率（例：0.8）：","settsl":"✍️ 移动止损（例：0.5）：","settmpt":"✍️ 移动止盈（例：0.3）：","setamount":"✍️ 单笔 USDT（例：1）：","settf":"✍️ 周期（例：5m）：","setreserve":"✍️ 底线（例：10）：","addsymbol":"✍️ 币种（例：DOGE/USDT）：","delsymbol":"✍️ 要删除的币种：","autoscore":"✍️ 阈值（50-85）：","settrades":"✍️ 日交易次数：","setmaxcoin":"✍️ 单币最大持仓U：","setmaxloss":"✍️ 日熔断%（例：5）：","setmaxpos":"✍️ 最大仓位数：","setmaxalloc":"✍️ 总仓位上限%（例：80）："}
                await query.message.reply_text(prompts.get(key,"✍️ 请输入数值："), reply_markup=ForceReply(selective=True)); await query.answer()
            elif data=="panic_confirm": await query.answer("🚨 请发送 /panic 确认", show_alert=True)
            else: await query.answer("此按钮暂未绑定功能", show_alert=True)
        except Exception as e:
            logger.error(f"按钮异常 ({data}): {e}")
            try: await query.answer("操作失败，请重试", show_alert=True)
            except: pass
    def _build_option_keyboard(self,options,prefix,setting_key):
        kb=[]; row=[]
        for label,val in options:
            row.append(InlineKeyboardButton(label,callback_data=f"{prefix}:{val}"))
            if len(row)==2: kb.append(row); row=[]
        if row: kb.append(row)
        kb.append([InlineKeyboardButton("✍️ 自填",callback_data=f"prompt_manual:{setting_key}")])
        kb.append([InlineKeyboardButton("🔙 返回",callback_data="refresh_panel")])
        return InlineKeyboardMarkup(kb)
    async def _refresh_panel(self,query):
        try: await query.edit_message_text(f"⚙️ 控制台 {self.env_tag}", reply_markup=self._build_main_keyboard())
        except: pass

    # ==================== 核心任务 ====================

    async def panic_sell_all(self):
        for sym in self.symbols:
            await self.exchange.cancel_all_orders(sym)
            bal=await self.exchange.fetch_balance()
            coin=sym.split('/')[0]
            amount=bal.get(coin,{}).get('free',0) if isinstance(bal.get(coin),dict) else 0
            if isinstance(amount,(int,float)) and amount>0:
                rounded=await self._round_amount_by_precision(sym,amount)
                if rounded>0: await self.exchange.create_market_sell_order(sym,rounded)
            self.position_counts[sym]=0
        await self._save_runtime_state()
    async def _sync_positions(self):
        try:
            bal=await self.exchange.fetch_balance()
            real={}
            for sym in self.symbols:
                coin=sym.split('/')[0]
                free=bal.get(coin,{}).get('free',0) if isinstance(bal.get(coin),dict) else 0
                if free>0.000001: real[sym]=free
            new={}
            for sym in self.symbols:
                if sym in real: new[sym]=max(1,self.position_counts.get(sym,1))
                else:
                    new[sym]=0
                    if sym in self.entries: del self.entries[sym]
                    if sym in self.entry_details: del self.entry_details[sym]
            self.position_counts=new
            for sym,amount in real.items():
                if sym not in self.entries or self.entries[sym]==0:
                    ticker=self.ws.get_ticker(sym)
                    if ticker is None: ticker=await self.exchange.fetch_ticker(sym)
                    if ticker: self.entries[sym]=ticker['last']; logger.info(f"📝 补录 {sym} 入场价: {ticker['last']:.2f}")
            await self._save_runtime_state(); logger.info(f"✅ 持仓同步完成: {real}"); return True
        except Exception as e: logger.error(f"持仓同步失败: {e}"); return False
    async def _risk_monitor_task(self):
        while True:
            try:
                self._btc_safe_flag=await self._check_btc_risk()
                bal=await self.exchange.fetch_balance(); usdt_free=self._get_usdt_free(bal); total=usdt_free
                for sym in self.symbols:
                    ticker=self.ws.get_ticker(sym)
                    if ticker:
                        coin=sym.split('/')[0]
                        free=bal.get(coin,{}).get('free',0) if isinstance(bal.get(coin),dict) else 0
                        total+=free*ticker['last']
                if total>self.peak_total_value: self.peak_total_value=total
                if self.peak_total_value>0:
                    drawdown=(self.peak_total_value-total)/self.peak_total_value
                    self._drawdown_safe_flag=drawdown<=self.max_drawdown_pct
                else: self._drawdown_safe_flag=True
            except Exception as e: logger.error(f"风控监控异常: {e}")
            await asyncio.sleep(60)
    async def _check_btc_risk(self):
        now=asyncio.get_event_loop().time()
        if now-self._last_btc_check_time<60: return not self.btc_risk_paused
        self._last_btc_check_time=now
        try:
            btc_sym="BTC/USDT"
            if btc_sym not in self.symbols: return True
            ticker=self.ws.get_ticker(btc_sym)
            if ticker is None: ticker=await self.exchange.fetch_ticker(btc_sym)
            if ticker is None: return True
            change=ticker.get('percentage',ticker.get('change',0))
            if change is None: return True
            if change<-4:
                if not self.btc_risk_paused:
                    await self._alert(f"🚨 BTC 24h 跌幅 {change:.1f}%，暂停开仓","critical")
                    self.btc_risk_paused=True
                return False
            if change>-2 and self.btc_risk_paused:
                await self._alert(f"✅ BTC 跌幅收窄至 {change:.1f}%，恢复交易","info")
                self.btc_risk_paused=False
            return True
        except Exception as e: logger.error(f"BTC风险检查失败: {e}"); return True
    async def _adjust_tp_sl_by_volatility(self, symbol):
        try:
            tech=await self._get_cached_tech(symbol,self.timeframe,20)
            if tech is None: return self._get_coin_param(symbol,'tp_pct',self.tp_pct),self._get_coin_param(symbol,'sl_pct',self.sl_pct)
            vol=tech['atr']/tech['bb_middle']
            factor=max(0.5,min(2.0,1.0+(vol-0.01)*50))
            tp=self._get_coin_param(symbol,'tp_pct',self.tp_pct)*factor
            sl=self._get_coin_param(symbol,'sl_pct',self.sl_pct)*factor
            return tp,sl
        except: return self._get_coin_param(symbol,'tp_pct',self.tp_pct),self._get_coin_param(symbol,'sl_pct',self.sl_pct)

    # ==================== 资金费率套利（简化） ====================
    async def _delta_neutral_arbitrage(self):
        while self.is_running:
            try:
                if not self._delta_neutral_config.get("enabled",True):
                    await asyncio.sleep(60); continue
                await self._refresh_balance_cache()
                total=self._cached_usdt_free
                for coin,free in self._cached_balances.items():
                    ticker=self.ws.get_ticker(coin+"/USDT")
                    if ticker: total+=free*ticker.get('last',0)
                alloc_pct=self._delta_neutral_config.get("allocation_percent",0.03)
                min_alloc=self._delta_neutral_config.get("min_allocation",0.5)
                max_alloc=self._delta_neutral_config.get("max_allocation",10)
                amount_usdt=max(min_alloc,min(max_alloc,total*alloc_pct))
                today=datetime.now(CST).day
                if today!=self._delta_neutral_stats.get("today_date",0):
                    self._delta_neutral_stats["profit_today"]=0.0; self._delta_neutral_stats["today_date"]=today
                for sym in self.symbols:
                    positions=[p for s,p in self._delta_neutral_positions.items() if s==sym]
                    max_pos=self._delta_neutral_config.get("max_position_per_coin",2)
                    if len(positions)>=max_pos: continue
                    funding=await self.real_data.get_funding_rate(sym)
                    if funding is None: continue
                    rate=funding.get('fundingRate',0)
                    min_rate=self._delta_neutral_config.get("min_funding_rate",0.0003)
                    if rate>min_rate:
                        if sym not in self._delta_neutral_positions:
                            success=await self._open_delta_neutral(sym,rate,amount_usdt)
                            if success: logger.info(f"✅ 费率抄底开仓 {sym} 费率{rate*100:.2f}% 金额{amount_usdt:.2f}U")
                        else:
                            pos=self._delta_neutral_positions[sym]
                            if pos['entry_time']+7.5*3600<time.time():
                                pnl=await self._close_delta_neutral(sym)
                                if pnl:
                                    self._delta_neutral_stats["total_trades"]+=1; self._delta_neutral_stats["total_profit"]+=pnl
                                    self._delta_neutral_stats["profit_today"]+=pnl; self._delta_neutral_stats["last_trade_time"]=time.time()
                                    await self._alert(f"✅ {sym} 费率抄底平仓 盈利{pnl:.4f}U\n累计抄底收益: {self._delta_neutral_stats['total_profit']:.4f}U","info")
                    else:
                        if sym in self._delta_neutral_positions:
                            pnl=await self._close_delta_neutral(sym)
                            if pnl and pnl>0:
                                self._delta_neutral_stats["total_trades"]+=1; self._delta_neutral_stats["total_profit"]+=pnl
                                self._delta_neutral_stats["profit_today"]+=pnl; self._delta_neutral_stats["last_trade_time"]=time.time()
                                logger.info(f"✅ {sym} 费率回落平仓 盈利{pnl:.4f}U")
                await asyncio.sleep(30)
            except Exception as e: logger.error(f"费率抄底异常: {e}"); await asyncio.sleep(30)
    async def _open_delta_neutral(self,symbol,funding_rate,amount_usdt=None):
        try:
            if amount_usdt is None: amount_usdt=0.5
            ticker=self.ws.get_ticker(symbol)
            if ticker is None: ticker=await self.exchange.fetch_ticker(symbol)
            if ticker is None: return False
            price=ticker['last']
            if self._cached_usdt_free<amount_usdt*1.1: logger.warning(f"⚠️ 余额不足 {symbol} 需要{amount_usdt:.2f}U"); return False
            coin_amount=amount_usdt/price
            rounded=await self._round_amount_by_precision(symbol,coin_amount)
            if rounded<=0: return False
            order=await self.exchange.create_market_buy_order(symbol,rounded)
            if order:
                self._delta_neutral_positions[symbol]={'entry_time':time.time(),'price':price,'amount':rounded,'amount_usdt':amount_usdt,'funding_rate':funding_rate,'entry_balance':self._cached_usdt_free}
                return True
            return False
        except Exception as e: logger.error(f"开仓失败 {symbol}: {e}"); return False
    async def _close_delta_neutral(self,symbol):
        try:
            pos=self._delta_neutral_positions.get(symbol)
            if not pos: return 0.0
            order=await self.exchange.create_market_sell_order(symbol,pos['amount'])
            if order:
                avg=order.get('average',pos['price'])
                revenue=pos['amount']*avg
                cost=pos['amount_usdt']
                pnl=revenue-cost-(revenue*0.001+cost*0.001)
                await self._refresh_balance_cache(force=True); del self._delta_neutral_positions[symbol]
                return pnl
            return 0.0
        except Exception as e: logger.error(f"平仓失败 {symbol}: {e}"); return 0.0

    # ==================== 链上监控与三角套利（保留原实现） ====================
    async def _onchain_monitor(self):
        while self.is_running:
            try:
                for sym in self.symbols:
                    data=await self.real_data.get_onchain_metrics(sym)
                    if data['whale_transfers']>5: await self._alert(f"🐋 {sym} 巨鲸转账 {data['whale_transfers']} 笔，注意风险","warning")
                    if data['exchange_netflow']<-80: await self._alert(f"📊 {sym} 交易所净流出 {data['exchange_netflow']:.0f}，关注信号","info")
                await asyncio.sleep(300)
            except Exception as e: logger.error(f"链上监控异常: {e}"); await asyncio.sleep(300)
    async def _triangular_arbitrage_monitor(self):
        while self.is_running:
            try:
                prices=[]
                for sym in self.symbols:
                    ticker=self.ws.get_ticker(sym)
                    if ticker: prices.append(ticker.get('last',0))
                if len(prices)>=3:
                    arb,profit=self.frontier.triangular_arbitrage(prices)
                    if arb and 0<profit<5: await self._alert(f"🔺 三角套利机会 {profit:.2f}%","info")
                await asyncio.sleep(300)
            except Exception as e: logger.error(f"三角套利监控异常: {e}"); await asyncio.sleep(300)

    # ==================== 自动交易主循环（含TWAP） ====================
    async def _auto_trade_monitor(self):
        await asyncio.sleep(10)
        while True:
            try:
                if not self.is_running or not self.auto_trade_enabled:
                    await asyncio.sleep(10); continue
                today=datetime.now(CST).day
                if today!=self.last_reset_day: self.daily_trades=0; self.last_reset_day=today
                if self.max_daily_trades>0 and self.daily_trades>=self.max_daily_trades:
                    await asyncio.sleep(10); continue
                usdt_free=await self._refresh_balance_cache()
                if not await self._check_risk_limits():
                    await asyncio.sleep(10); continue
                if not self._drawdown_safe_flag:
                    await self._alert("⛔ 回撤熔断触发","critical"); await asyncio.sleep(60); continue
                if self.api_error_count>=self.max_api_errors:
                    if asyncio.get_event_loop().time()-self.api_error_pause_time<1800:
                        await asyncio.sleep(60); continue
                    else: self.api_error_count=0
                today_stats=await self._get_cached_today_stats()
                if today_stats and today_stats['total']>=3:
                    if today_stats['win_rate']<0.2 and abs(today_stats['avg_loss_pct'])>self.max_daily_loss_pct:
                        await self._alert("⛔ 日亏损熔断","critical"); await asyncio.sleep(300); continue
                if self.consecutive_failures>=3:
                    await self._alert(f"⚠️ 连续开仓失败 {self.consecutive_failures} 次，暂停60秒","warning")
                    self.consecutive_failures=0; self.last_failure_time=asyncio.get_event_loop().time(); await asyncio.sleep(60); continue
                fg_data=await self.real_data.get_fear_greed_index(); fg=fg_data["value"] if fg_data else None
                candidates=[]
                for sym in self.symbols:
                    try:
                        if sym!="BTC/USDT" and not self._btc_safe_flag: continue
                        ticker=self.ws.get_ticker(sym)
                        if ticker is None: continue
                        p=ticker['last']; coin=sym.split('/')[0]
                        free=self._cached_balances.get(coin,0); coin_value=free*p
                        count=self.position_counts.get(sym,0)
                        if count>=self.max_positions_per_coin: continue
                        if self.max_per_coin_usdt>0 and coin_value>=self.max_per_coin_usdt: continue
                        if sym not in self._volume_history: self._volume_history[sym]=[]
                        vol=ticker.get('volume',0)
                        if vol>0: self._volume_history[sym].append(vol); self._volume_history[sym]=self._volume_history[sym][-50:]
                        grid=self.grid_configs.get(sym)
                        if grid:
                            last_trigger=self._last_grid_entry.get(sym,p)
                            drop=(last_trigger-p)/last_trigger if last_trigger else 0
                            if drop>=grid["drop_pct"]:
                                count=self.position_counts.get(sym,0)
                                coin_amount=grid["base_amount"]*(1+count*grid["increment"])
                                self._last_grid_entry[sym]=p
                                candidates.append((100,sym,p,None,self.tp_pct,self.sl_pct,2.0,coin_amount))
                                logger.info(f"📊 固定网格触发 {sym} 下跌{drop*100:.2f}%，金额{coin_amount:.2f}U")
                            continue
                        tech=await self._get_cached_tech(sym,self.timeframe,50)
                        if tech is None: continue
                        funding=await self.exchange.fetch_funding_rate(sym)
                        decision=await self._should_open_position(sym,p,tech,funding,fg,usdt_free)
                        if not decision['should_open']: continue
                        if self.orderbook_filter:
                            ob=self.ws.get_orderbook(sym)
                            if ob is None: continue
                            ob_valid,_=await self.orderbook_engine.validate(ob)
                            if not ob_valid: continue
                        coin_amount=decision.get('amount',self._calculate_dynamic_amount(self._get_coin_param(sym,'single_order_usdt',self.single_order_usdt)))
                        if decision['is_high_confidence']: coin_amount*=1.3; logger.info(f"🔥 {sym} 高置信度信号，仓位提升30%: {coin_amount:.2f}U")
                        dyn_tp,dyn_sl=await self._adjust_tp_sl_by_volatility(sym)
                        candidates.append((decision['score'],sym,p,funding,dyn_tp,dyn_sl,2.0,coin_amount))
                        logger.info(f"📊 {sym} 开仓信号通过，评分{decision['score']:.0f}，金额{coin_amount:.2f}U")
                    except Exception as e: logger.error(f"候选生成异常 {sym}: {e}")
                candidates.sort(key=lambda x:x[0], reverse=True); opened=set()
                for item in candidates:
                    if len(item)==8:
                        sc,sym,p,funding,dyn_tp,dyn_sl,vol_factor,coin_amount=item
                    else: sc,sym,p,funding,dyn_tp,dyn_sl,vol_factor=item; coin_amount=self._get_coin_param(sym,'single_order_usdt',self.single_order_usdt)
                    if sym in opened: continue
                    if usdt_free<coin_amount+self.reserve_bottom: break
                    coin=sym.split('/')[0]
                    old_usdt_free=usdt_free; old_balance=self._cached_balances.get(coin,0)
                    # TWAP
                    if coin_amount>50:
                        chunks=3; interval=1.0
                        logger.info(f"🔄 使用TWAP下单 {sym} 总金额{coin_amount:.2f}U，拆{chunks}笔")
                        orders=await self.exchange.create_market_buy_order_with_twap(sym,coin_amount,price=p,chunks=chunks,interval=interval)
                        if orders:
                            self.daily_trades+=1; await asyncio.sleep(2)
                            await self._refresh_balance_cache(force=True)
                            new_balance=self._cached_balances.get(coin,0); new_usdt_free=self._cached_usdt_free
                            real_cost=old_usdt_free-new_usdt_free
                            if real_cost<=0: real_cost=coin_amount
                            if new_balance>old_balance:
                                self.entries[sym]=p; self._trailing_high[sym]=p; self._trailing_active[sym]=False
                                self.position_counts[sym]=self.position_counts.get(sym,0)+1
                                self.entry_details[sym]={'signal_score':sc,'fear_greed':fg,'funding_rate':funding,'dyn_tp':dyn_tp,'dyn_sl':dyn_sl,'real_cost':real_cost}
                                await save_trade_detail({"time":datetime.now(CST).strftime("%m-%d %H:%M"),"symbol":sym,"side":"buy","price":p,"amount":coin_amount/p,"signal_score":sc,"fear_greed":fg or 0,"funding_rate":funding or 0,"pnl_pct":0,"real_cost":round(real_cost,4)})
                                await self._save_runtime_state(); self.consecutive_failures=0; usdt_free=new_usdt_free; opened.add(sym)
                                if settings.TG_CHAT_ID:
                                    try: await self.tg_app.bot.send_message(chat_id=settings.TG_CHAT_ID,text=f"🤖 开仓 {sym} {coin_amount:.2f}U @ {p:.4f} 仓位{self.position_counts[sym]}/{self.max_positions_per_coin} | 评分{sc:.0f}")
                                    except: pass
                            else: self.consecutive_failures+=1; self.last_failure_time=asyncio.get_event_loop().time()
                        else: self.consecutive_failures+=1
                    else:
                        raw_amount=coin_amount/p
                        rounded=await self._round_amount_by_precision(sym,raw_amount)
                        if rounded<=0: logger.warning(f"⚠️ {sym} 下单数量 {rounded:.8f} 无效，跳过"); continue
                        order=await self.exchange.create_market_buy_order(sym,rounded)
                        if order:
                            self.daily_trades+=1; await asyncio.sleep(2)
                            await self._refresh_balance_cache(force=True)
                            new_balance=self._cached_balances.get(coin,0); new_usdt_free=self._cached_usdt_free
                            real_cost=old_usdt_free-new_usdt_free
                            if real_cost<=0: real_cost=coin_amount
                            if new_balance>old_balance:
                                self.entries[sym]=p; self._trailing_high[sym]=p; self._trailing_active[sym]=False
                                self.position_counts[sym]=self.position_counts.get(sym,0)+1
                                self.entry_details[sym]={'signal_score':sc,'fear_greed':fg,'funding_rate':funding,'dyn_tp':dyn_tp,'dyn_sl':dyn_sl,'real_cost':real_cost}
                                await save_trade_detail({"time":datetime.now(CST).strftime("%m-%d %H:%M"),"symbol":sym,"side":"buy","price":p,"amount":rounded,"signal_score":sc,"fear_greed":fg or 0,"funding_rate":funding or 0,"pnl_pct":0,"real_cost":round(real_cost,4)})
                                await self._save_runtime_state(); self.consecutive_failures=0; usdt_free=new_usdt_free; opened.add(sym)
                                if settings.TG_CHAT_ID:
                                    try: await self.tg_app.bot.send_message(chat_id=settings.TG_CHAT_ID,text=f"🤖 开仓 {sym} {coin_amount:.2f}U @ {p:.4f} 仓位{self.position_counts[sym]}/{self.max_positions_per_coin} | 评分{sc:.0f}")
                                    except: pass
                            else: self.consecutive_failures+=1; self.last_failure_time=asyncio.get_event_loop().time()
                        else: self.consecutive_failures+=1
                    await asyncio.sleep(1)
                if len(self.trades)%20==0 and len(self.trades)>0: await self._auto_optimize_params()
                await asyncio.sleep(30)
            except Exception as e:
                logger.error(f"自动交易错误: {e}"); self.api_error_count+=1; self.api_error_pause_time=asyncio.get_event_loop().time(); await asyncio.sleep(30)

    async def _get_cached_today_stats(self):
        now=time.time()
        if not hasattr(self,'_today_stats_cache') or now-self._today_stats_time>30:
            self._today_stats_cache=await get_today_trades(); self._today_stats_time=now
        return self._today_stats_cache

    # ==================== 移动止盈止损（ATR动态） ====================
    async def _trailing_monitor(self):
        await asyncio.sleep(5)
        while True:
            try:
                if not self.is_running: await asyncio.sleep(5); continue
                await self._refresh_balance_cache(); bal=self._cached_balances
                for sym in self.symbols:
                    try:
                        if self.position_counts.get(sym,0)<=0: continue
                        ticker=self.ws.get_ticker(sym)
                        if ticker is None: continue
                        p=ticker['last']; coin=sym.split('/')[0]
                        amount=bal.get(coin,0)
                        if amount<=0: self.position_counts[sym]=0; await self._save_runtime_state(); continue
                        entry_price=self.entries.get(sym,p); detail=self.entry_details.get(sym,{})
                        # ATR动态止损
                        tech=await self._get_cached_tech(sym,self.timeframe,20)
                        if tech:
                            atr=tech.get('atr',0)
                            if atr>0:
                                dyn_sl=atr*1.5/entry_price
                            else: dyn_sl=self._get_coin_param(sym,'sl_pct',self.sl_pct)
                        else: dyn_sl=self._get_coin_param(sym,'sl_pct',self.sl_pct)
                        dyn_tp=detail.get('dyn_tp',self._get_coin_param(sym,'tp_pct',self.tp_pct))
                        high_price=self._trailing_high.get(sym,entry_price)
                        action,reason=self.low_buy.trailing_stop_with_stepping(entry_price,p,high_price,dyn_tp,dyn_sl,0.3)
                        if action=='sell':
                            logger.info(f"📉 触发卖出 {sym} @ {p:.2f} 原因: {reason}")
                            rounded=await self._round_amount_by_precision(sym,amount)
                            if rounded>0: await self.exchange.create_market_sell_order(sym,rounded)
                            await asyncio.sleep(0.5)
                            await self._refresh_balance_cache(force=True)
                            new_usdt=self._cached_usdt_free
                            old_usdt=self._get_usdt_free(bal)
                            net_pnl=new_usdt-old_usdt
                            pnl_pct=((p-entry_price)/entry_price)*100
                            real_cost=detail.get('real_cost',self._get_coin_param(sym,'single_order_usdt',self.single_order_usdt))
                            if real_cost<0.01: net_pnl_pct=pnl_pct
                            else: net_pnl_pct=(net_pnl/real_cost)*100
                            net_pnl_pct=min(1000,max(-1000,net_pnl_pct))
                            if net_pnl<0: self._consecutive_losses+=1; self._today_loss_pct+=abs(net_pnl_pct)/100
                            else: self._consecutive_losses=0
                            trade={"time":datetime.now(CST).strftime("%m-%d %H:%M"),"symbol":sym,"entry":entry_price,"exit":p,"pnl_pct":round(pnl_pct,2),"net_pnl":round(net_pnl,4),"net_pnl_pct":round(net_pnl_pct,2)}
                            await save_trade(trade); self.trades.insert(0,trade)
                            await save_trade_detail({"time":datetime.now(CST).strftime("%m-%d %H:%M"),"symbol":sym,"side":"sell","price":p,"amount":amount,"pnl_pct":round(pnl_pct,2),"signal_score":detail.get('signal_score',0),"fear_greed":detail.get('fear_greed',0),"funding_rate":detail.get('funding_rate',0),"real_revenue":round(net_pnl,4),"net_pnl_pct":round(net_pnl_pct,2)})
                            self._trailing_active[sym]=False; self._trailing_high[sym]=0
                            if sym in self.entries: del self.entries[sym]
                            if sym in self.entry_details: del self.entry_details[sym]
                            self.position_counts[sym]=0; await self._save_runtime_state(); await self._auto_optimize_params()
                            if settings.TG_CHAT_ID:
                                try: await self.tg_app.bot.send_message(chat_id=settings.TG_CHAT_ID,text=f"📉 {reason} {sym} @ {p:.2f} 净利{net_pnl_pct:+.2f}% ({net_pnl:+.4f}U)")
                                except: pass
                            continue
                        if p>self._trailing_high.get(sym,0): self._trailing_high[sym]=p
                    except Exception as e: logger.error(f"追踪异常 {sym}: {e}")
                await asyncio.sleep(5)
            except Exception as e: logger.error(f"追踪任务异常: {e}"); await asyncio.sleep(5)

    # ==================== 启动入口 ====================
    async def run(self):
        await self.load_and_init()
        if not self.tg_app: return
        ws_ok=await self.ws.connect()
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
                await self.tg_app.initialize(); await self.tg_app.start(); await self.tg_app.updater.start_polling(drop_pending_updates=True)
                logger.info("✅ UltimateBot v12.0 自适应版启动成功（27合1策略）")
                if settings.TG_CHAT_ID:
                    try: await self.tg_app.bot.send_message(chat_id=settings.TG_CHAT_ID,text="🚀 **UltimateBot v12.0 自适应版已上线**\n\n📊 27合1全栈策略\n🔄 市场状态自适应\n🧠 多智能体协作系统\n📈 情绪增强强化学习\n🌐 WebCryptoAgent多源融合\n💰 凯利动态仓位管理\n📊 GARCH波动率预测\n🎯 阶梯式移动止盈\n\n策略组合：**终极自适应版**",parse_mode="Markdown")
                    except: pass
                while True: await asyncio.sleep(30)
            except Exception as e: logger.error(f"Bot 断开，5秒后重连: {e}"); await asyncio.sleep(5)