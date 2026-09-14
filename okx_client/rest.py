import okx.Grid as Grid
import okx.Trade as Trade
import okx.Account as Account
import okx.MarketData as MarketData
import okx.PublicData as PublicData
from config import OKX_API_KEY, OKX_SECRET_KEY, OKX_PASSPHRASE, OKX_DEMO

FLAG = "1" if OKX_DEMO else "0"


class OKXRest:
    def __init__(self):
        self.grid = Grid.GridAPI(OKX_API_KEY, OKX_SECRET_KEY, OKX_PASSPHRASE, False, FLAG)
        self.trade = Trade.TradeAPI(OKX_API_KEY, OKX_SECRET_KEY, OKX_PASSPHRASE, False, FLAG)
        self.account = Account.AccountAPI(OKX_API_KEY, OKX_SECRET_KEY, OKX_PASSPHRASE, False, FLAG)
        self.market = MarketData.MarketAPI(flag=FLAG)
        self.public = PublicData.PublicAPI(flag=FLAG)

    # ==================== 行情 ====================
    def get_ticker(self, inst_id):
        return self.market.get_ticker(instId=inst_id)

    def get_candles(self, inst_id, bar="1H", limit=20):
        return self.market.get_candlesticks(instId=inst_id, bar=bar, limit=str(limit))

    # ==================== 产品规格 ====================
    def get_instruments(self, inst_type="SPOT", inst_id=None):
        kwargs = {"instType": inst_type}
        if inst_id:
            kwargs["instId"] = inst_id
        return self.public.get_instruments(**kwargs)

    def get_min_investment(self, inst_id, algo_ord_type="grid",
                           min_px=None, max_px=None, grid_num=None,
                           investment_type="quote"):
        kwargs = {
            "instId": inst_id,
            "algoOrdType": algo_ord_type,
            "investmentType": investment_type,
        }
        if min_px is not None:
            kwargs["minPx"] = str(min_px)
        if max_px is not None:
            kwargs["maxPx"] = str(max_px)
        if grid_num is not None:
            kwargs["gridNum"] = str(grid_num)
        return self.grid.grid_min_investment(**kwargs)

    # ==================== 网格 ====================
    def create_spot_grid(self, inst_id, min_px, max_px, grid_num, quote_sz,
                         tp_px=None, sl_px=None):
        kwargs = {
            "instId": inst_id,
            "algoOrdType": "grid",
            "maxPx": str(max_px),
            "minPx": str(min_px),
            "gridNum": str(grid_num),
            "quoteSz": str(quote_sz),
            "runType": "1",
        }
        if tp_px is not None:
            kwargs["tpTriggerPx"] = str(tp_px)
        if sl_px is not None:
            kwargs["slTriggerPx"] = str(sl_px)
        return self.grid.grid_order_algo(**kwargs)

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
    def limit_buy(self, inst_id, price, size):
        """限价买入（Maker 订单）"""
        return self.trade.place_order(
            instId=inst_id, tdMode="cash", side="buy",
            ordType="limit", px=str(price), sz=str(size),
        )

    def limit_sell(self, inst_id, price, size):
        """限价卖出（Maker 订单）"""
        return self.trade.place_order(
            instId=inst_id, tdMode="cash", side="sell",
            ordType="limit", px=str(price), sz=str(size),
        )

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

    def cancel_order(self, inst_id, ord_id):
        """撤销指定订单"""
        return self.trade.cancel_order(instId=inst_id, ordId=ord_id)

    # ==================== 订单查询 ====================
    def get_pending_orders(self, inst_id=None, ord_type=None):
        """获取未成交订单列表（用于启动恢复）"""
        kwargs = {}
        if inst_id:
            kwargs["instId"] = inst_id
        if ord_type:
            kwargs["ordType"] = ord_type
        return self.trade.get_orders_pending(**kwargs)

    def get_order_detail(self, inst_id, ord_id):
        return self.trade.get_order(instId=inst_id, ordId=ord_id)

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

    def get_bills(self, inst_type="SPOT", limit=100):
        return self.account.get_bills(instType=inst_type, limit=str(limit))