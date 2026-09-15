import okx.Trade as Trade
import okx.Account as Account
import okx.MarketData as MarketData
import okx.PublicData as PublicData
from config import OKX_API_KEY, OKX_SECRET_KEY, OKX_PASSPHRASE, OKX_DEMO

FLAG = "1" if OKX_DEMO else "0"


class OKXRest:
    def __init__(self):
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

    # ==================== 现货交易 ====================
    def limit_buy(self, inst_id, price, size):
        return self.trade.place_order(
            instId=inst_id, tdMode="cash", side="buy",
            ordType="limit", px=str(price), sz=str(size),
        )

    def limit_sell(self, inst_id, price, size):
        return self.trade.place_order(
            instId=inst_id, tdMode="cash", side="sell",
            ordType="limit", px=str(price), sz=str(size),
        )

    def cancel_order(self, inst_id, ord_id):
        return self.trade.cancel_order(instId=inst_id, ordId=ord_id)

    def get_pending_orders(self, inst_id=None, ord_type=None):
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