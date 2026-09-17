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

    # ==================== 市价交易 ====================
    def market_buy(self, inst_id, quote_sz):
        """按 USDT 金额市价买入"""
        return self.trade.place_order(
            instId=inst_id,
            tdMode="cash",
            side="buy",
            ordType="market",
            tgtCcy="quote_ccy",
            sz=str(quote_sz),
        )

    def market_sell(self, inst_id, base_sz):
        """按币种数量市价卖出"""
        return self.trade.place_order(
            instId=inst_id,
            tdMode="cash",
            side="sell",
            ordType="market",
            sz=str(base_sz),
        )

    # ==================== 账户 ====================
    def get_balance(self, ccy="USDT"):
        return self.account.get_account_balance(ccy=ccy)

    # ==================== 成交明细 ====================
    def get_fills(self, inst_type="SPOT", inst_id=None, limit=100):
        kwargs = {"instType": inst_type, "limit": str(limit)}
        if inst_id:
            kwargs["instId"] = inst_id
        return self.trade.get_fills(**kwargs)