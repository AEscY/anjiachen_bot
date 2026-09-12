# okx_client/rest.py
import okx.Grid as Grid
import okx.Trade as Trade
import okx.Account as Account
from config import OKX_API_KEY, OKX_SECRET_KEY, OKX_PASSPHRASE, OKX_DEMO

FLAG = "1" if OKX_DEMO else "0"

class OKXRest:
    def __init__(self):
        self.grid = Grid.GridAPI(OKX_API_KEY, OKX_SECRET_KEY, OKX_PASSPHRASE, False, FLAG)
        self.trade = Trade.TradeAPI(OKX_API_KEY, OKX_SECRET_KEY, OKX_PASSPHRASE, False, FLAG)
        self.account = Account.AccountAPI(OKX_API_KEY, OKX_SECRET_KEY, OKX_PASSPHRASE, False, FLAG)

    # --- 现货网格 ---
    def create_spot_grid(self, inst_id, min_px, max_px, grid_num, quote_sz):
        return self.grid.grid_order_algo(
            instId=inst_id, algoOrdType="grid",
            maxPx=str(max_px), minPx=str(min_px),
            gridNum=str(grid_num), quoteSz=str(quote_sz),
            triggerType="1", runType="1"
        )

    def stop_grid(self, algo_id, inst_id):
        return self.grid.grid_stop_algo(
            algoId=algo_id, instId=inst_id, algoOrdType="grid", stopType="1"
        )

    def amend_grid(self, algo_id, inst_id, **kwargs):
        return self.grid.grid_amend_algo(algoId=algo_id, instId=inst_id, **kwargs)

    # --- 现货下单（低吸高卖用）---
    def market_buy(self, inst_id, quote_sz):
        return self.trade.place_order(
            instId=inst_id, tdMode="cash", side="buy",
            ordType="market", tgtCcy="quote_ccy", sz=str(quote_sz)
        )

    def market_sell(self, inst_id, base_sz):
        return self.trade.place_order(
            instId=inst_id, tdMode="cash", side="sell",
            ordType="market", sz=str(base_sz)
        )

    def get_balance(self, ccy="USDT"):
        return self.account.get_account_balance(ccy=ccy)