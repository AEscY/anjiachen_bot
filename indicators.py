import numpy as np
import pandas as pd

class Indicators:
    def __init__(self, prices_high=None, prices_low=None, prices_close=None):
        self.high = np.array(prices_high) if prices_high else None
        self.low = np.array(prices_low) if prices_low else None
        self.close = np.array(prices_close) if prices_close else None

    @staticmethod
    def calculate_atr(high, low, close, period=14):
        """计算平均真实波幅 (ATR)"""
        tr1 = high[1:] - low[1:]
        tr2 = abs(high[1:] - close[:-1])
        tr3 = abs(low[1:] - close[:-1])
        tr = np.maximum(np.maximum(tr1, tr2), tr3)
        atr = np.zeros(len(close))
        atr[period] = np.mean(tr[:period])
        for i in range(period+1, len(close)):
            atr[i] = (atr[i-1] * (period - 1) + tr[i-1]) / period
        return atr

    @staticmethod
    def get_dynamic_grid_prices(current_price, atr_value, grid_num, scale):
        """根据ATR实时生成动态网格挂单价"""
        if atr_value < 0.01:
            atr_value = current_price * 0.002  # 防止波动率太小网格密死
        
        # 网格间距 = ATR * 缩放系数
        step = atr_value * scale
        
        buy_prices = []
        sell_prices = []
        for i in range(1, grid_num + 1):
            buy_prices.append(current_price - step * i)
            sell_prices.append(current_price + step * i)
        
        # 过滤掉太离谱的价格（防止网格超过10%）
        buy_prices = [p for p in buy_prices if p > current_price * 0.9]
        sell_prices = [p for p in sell_prices if p < current_price * 1.1]
        
        return buy_prices, sell_prices