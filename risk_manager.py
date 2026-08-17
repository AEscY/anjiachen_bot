import time
from config import Config

class RiskManager:
    def __init__(self):
        self.positions = {}  # {symbol: {side, avg_price, open_time, highest_price, lowest_price}}

    def add_position(self, side, price, size):
        key = f"{Config.SYMBOL}_{side}"
        self.positions[key] = {
            "side": side,
            "avg_price": price,
            "open_time": time.time(),
            "highest_price": price if side == "buy" else None,
            "lowest_price": price if side == "sell" else None,
            "size": size
        }

    def check_trailing_stop(self, side, current_price):
        key = f"{Config.SYMBOL}_{side}"
        pos = self.positions.get(key)
        if not pos:
            return False
        
        # 更新最高/最低价
        if side == "buy":
            if current_price > pos["highest_price"]:
                pos["highest_price"] = current_price
            # 回撤超过设定比例则止盈
            if pos["highest_price"] and (pos["highest_price"] - current_price) / pos["highest_price"] >= Config.TRAILING_STOP_PCT:
                return True
        else:  # sell (做空)
            if pos["lowest_price"] is None or current_price < pos["lowest_price"]:
                pos["lowest_price"] = current_price
            if pos["lowest_price"] and (current_price - pos["lowest_price"]) / current_price >= Config.TRAILING_STOP_PCT:
                return True
        return False

    def check_timeout(self, side, seconds=300):
        key = f"{Config.SYMBOL}_{side}"
        pos = self.positions.get(key)
        if not pos:
            return False
        return (time.time() - pos["open_time"]) > seconds
