import okx.Grid as Grid
import okx.Trade as Trade
import okx.Account as Account
import okx.MarketData as MarketData
from config import OKX_API_KEY, OKX_SECRET_KEY, OKX_PASSPHRASE, OKX_DEMO

FLAG = "1" if OKX_DEMO else "0"


class OKXRest:
    def __init__(self):
        self.grid = Grid.GridAPI(OKX_API_KEY, OKX_SECRET_KEY, OKX_PASSPHRASE, False, FLAG)
        self.trade = Trade.TradeAPI(OKX_API_KEY, OKX_SECRET_KEY, OKX_PASSPHRASE, False, FLAG)
        self.account = Account.AccountAPI(OKX_API_KEY, OKX_SECRET_KEY, OKX_PASSPHRASE, False, FLAG)
        self.market = MarketData.MarketAPI(flag=FLAG)

    # ==================== 行情 ====================
    def get_ticker(self, inst_id):
        return self.market.get_ticker(instId=inst_id)

    def get_candles(self, inst_id, bar="1H", limit=20):
        """获取K线数据，用于计算ATR"""
        return self.market.get_candlesticks(instId=inst_id, bar=bar, limit=str(limit))

    # ==================== 网格 ====================
    def create_spot_grid(self, inst_id, min_px, max_px, grid_num, quote_sz):
        return self.grid.grid_order_algo(
            instId=inst_id,
            algoOrdType="grid",
            maxPx=str(max_px),
            minPx=str(min_px),
            gridNum=str(grid_num),
            quoteSz=str(quote_sz),
            runType="1",
        )

    def stop_grid(self, algo_id, inst_id):
        return self.grid.grid_stop_algo(
            algoId=algo_id, instId=inst_id,
            algoOrdType="grid", stopType="1",
        )

    def get_pending_grids(self, inst_id=None):
        kwargs = {"algoOrdType": "grid"}
        if inst_id:
            kwargs["instId"] = inst_id
        return self.grid.grid_orders_algo_pending(**kwargs)

    def get_grid_details(self, algo_id, inst_id):
        return self.grid.grid_orders_algo_details(algoId=algo_id, instId=inst_id)

    # ==================== 现货交易 ====================
    def market_buy(self, inst_id, quote_sz):
        return self.trade.place_order(
            instId=inst_id, tdMode="cash", side="buy",
            ordType="market", tgtCcy="quote_ccy", sz=str(quote_sz),
        )

    def market_sell(self, inst_id, base_sz):
        return self.trade.place_order(
            instId=inst_id, tdMode="cash", side="sell",
            ordType="market", sz=str(base_sz),
        )

    # ==================== 账户 ====================
    def get_balance(self, ccy="USDT"):
        return self.account.get_account_balance(ccy=ccy)

    def get_positions(self, inst_id=None):
        kwargs = {"instType": "SPOT"}
        if inst_id:
            kwargs["instId"] = inst_id
        return self.account.get_positions(**kwargs)

    # ==================== 成交明细 ====================
    def get_fills(self, inst_type="SPOT", inst_id=None, limit=100):
        kwargs = {"instType": inst_type, "limit": str(limit)}
        if inst_id:
            kwargs["instId"] = inst_id
        return self.trade.get_fills(**kwargs)

    def get_fills_history(self, inst_type="SPOT", inst_id=None, limit=100):
        kwargs = {"instType": inst_type, "limit": str(limit)}
        if inst_id:
            kwargs["instId"] = inst_id
        return self.trade.get_fills_history(**kwargs)

    # ==================== 账户账单 ====================
    def get_bills(self, inst_type="SPOT", limit=100):
        return self.account.get_bills(instType=inst_type, limit=str(limit))