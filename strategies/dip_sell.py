import asyncio
import logging
import time
from strategies.base import BaseStrategy
from strategies.signals import SignalEngine
from okx_client.rest import OKXRest

logger = logging.getLogger(__name__)

class DipSellStrategy(BaseStrategy):
    def __init__(self, inst_id, params=None):
        default = {
            "basePx": 0, "buyPct": 0.98, "sellPct": 1.03,
            "maxSpend": 100,
            "use_signal": False,          # 是否启用信号模式
            "use_trend_filter": False,    # 是否启用趋势过滤
            "use_volume": False,          # 是否启用成交量确认
        }
        merged = {**default, **(params or {})}
        super().__init__(inst_id, merged)

        self.rest = OKXRest()
        self.base_px = merged["basePx"]
        self.buy_px = self.base_px * merged["buyPct"]
        self.sell_px = self.base_px * merged["sellPct"]
        self.position = 0.0
        self.cost = 0.0
        self._last_action = None
        self.total_profit = 0.0
        self.total_fee = 0.0
        self.trade_count = 0

        self.signal_engine = SignalEngine()
        self._kline_cache = []   # 缓存K线数据

    async def _fetch_klines(self):
        """获取最近的K线数据（用于计算指标）"""
        try:
            resp = await asyncio.to_thread(
                self.rest.get_candles, self.inst_id, "1H", 300
            )
            if resp.get("code") == "0" and resp.get("data"):
                # OKX返回格式: [ts, open, high, low, close, vol, ...]
                self._kline_cache = list(reversed(resp["data"]))
        except Exception as e:
            logger.error(f"获取K线失败: {e}")

    async def on_ticker(self, price, raw):
        if not self.running:
            return

        # 信号模式
        if self.params.get("use_signal"):
            await self._on_ticker_signal(price)
            return

        # 传统阈值模式（原有逻辑）
        await self._on_ticker_threshold(price)

    async def _on_ticker_signal(self, price):
        """基于多因子信号引擎的交易逻辑"""
        # 每60秒刷新一次K线数据（避免频繁请求）
        now = time.time()
        if now - getattr(self, "_last_kline_ts", 0) > 60:
            await self._fetch_klines()
            self._last_kline_ts = now

        if len(self._kline_cache) < 50:
            return

        closes = [float(k[4]) for k in self._kline_cache]
        highs = [float(k[2]) for k in self._kline_cache]
        lows = [float(k[3]) for k in self._kline_cache]
        volumes = [float(k[5]) for k in self._kline_cache]

        ind = self.signal_engine.calculate(closes, highs, lows, volumes)

        # 买入信号
        if self.position <= 0:
            if self.signal_engine.buy_signal(
                ind,
                use_trend_filter=self.params.get("use_trend_filter", False),
                use_volume=self.params.get("use_volume", False),
            ):
                spend = self.params.get("maxSpend", 100)
                r = await asyncio.to_thread(self.rest.market_buy, self.inst_id, spend)
                if r.get("code") == "0":
                    self._last_action = ("buy_signal", price)
                    logger.info(f"{self.inst_id} 信号买入 @ {price}, RSI={ind['rsi']:.1f}")
                    await self._record_fee()
                    await self._sync_position_from_okx()

        # 卖出信号
        elif self.position > 0:
            if self.signal_engine.sell_signal(ind):
                sell_size = self.position
                sell_cost = self.cost
                r = await asyncio.to_thread(self.rest.market_sell, self.inst_id, sell_size)
                if r.get("code") == "0":
                    self._last_action = ("sell_signal", price)
                    profit = price * sell_size - sell_cost
                    self.total_profit += profit
                    logger.info(f"{self.inst_id} 信号卖出 @ {price}, 获利 {profit:.4f}")
                    await self._record_fee()
                    await self._sync_position_from_okx()

    async def _on_ticker_threshold(self, price):
        """原有阈值逻辑"""
        if self.position <= 0 and price <= self.buy_px:
            spend = self.params.get("maxSpend", 100)
            r = await asyncio.to_thread(self.rest.market_buy, self.inst_id, spend)
            if r.get("code") == "0":
                self._last_action = ("buy", price)
                await self._record_fee()
                await self._sync_position_from_okx()
        elif self.position > 0 and price >= self.sell_px:
            sell_size = self.position
            sell_cost = self.cost
            r = await asyncio.to_thread(self.rest.market_sell, self.inst_id, sell_size)
            if r.get("code") == "0":
                self._last_action = ("sell", price)
                profit = price * sell_size - sell_cost
                self.total_profit += profit
                await self._record_fee()
                await self._sync_position_from_okx()

    async def _sync_position_from_okx(self):
        try:
            base_ccy = self.inst_id.split("-")[0]
            bal_resp = await asyncio.to_thread(self.rest.get_balance, "USDT")
            if bal_resp.get("code") == "0" and bal_resp.get("data"):
                details = bal_resp["data"][0].get("details", [])
                for d in details:
                    if d.get("ccy") == base_ccy:
                        self.position = float(d.get("eq", 0))
                        self.cost = float(d.get("eqUsd", 0))
                        return
            self.position = 0.0
            self.cost = 0.0
        except Exception as e:
            logger.error(f"同步持仓失败: {e}")

    async def _record_fee(self):
        try:
            await asyncio.sleep(1.5)
            fills = await asyncio.to_thread(self.rest.get_fills, "SPOT", self.inst_id, 20)
            if fills.get("code") != "0":
                return
            now_ms = int(time.time() * 1000)
            for f in fills.get("data", []):
                try:
                    ts = int(f.get("ts", 0))
                    if now_ms - ts > 30000:
                        continue
                    fee = abs(float(f.get("fee", 0)))
                    self.total_fee += fee
                    self.trade_count += 1
                except (ValueError, TypeError):
                    continue
        except Exception as e:
            logger.error(f"记录手续费失败: {e}")

    async def start(self):
        self.running = True

    async def stop(self):
        self.running = False

    def snapshot(self):
        s = super().snapshot()
        s.update({
            "base_px": self.base_px, "buy_px": self.buy_px,
            "sell_px": self.sell_px, "position": self.position,
            "last_action": self._last_action,
            "total_profit": self.total_profit,
            "total_fee": self.total_fee,
            "trade_count": self.trade_count,
        })
        return s