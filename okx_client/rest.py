# okx_client/rest.py
"""
OKX REST 客户端封装。
依赖：pip install python-okx
"""

import okx.Grid as Grid
import okx.Trade as Trade
import okx.Account as Account
from config import OKX_API_KEY, OKX_SECRET_KEY, OKX_PASSPHRASE, OKX_DEMO

# 实盘 flag="0"，模拟盘 flag="1"
FLAG = "1" if OKX_DEMO else "0"


class OKXRest:
    """OKX REST API 封装"""

    def __init__(self):
        self.grid = Grid.GridAPI(
            OKX_API_KEY, OKX_SECRET_KEY, OKX_PASSPHRASE, False, FLAG
        )
        self.trade = Trade.TradeAPI(
            OKX_API_KEY, OKX_SECRET_KEY, OKX_PASSPHRASE, False, FLAG
        )
        self.account = Account.AccountAPI(
            OKX_API_KEY, OKX_SECRET_KEY, OKX_PASSPHRASE, False, FLAG
        )

    # ==================== 网格策略 ====================
    def create_spot_grid(self, inst_id, min_px, max_px, grid_num, quote_sz):
        """创建现货网格"""
        return self.grid.grid_order_algo(
            instId=inst_id,
            algoOrdType="grid",
            maxPx=str(max_px),
            minPx=str(min_px),
            gridNum=str(grid_num),
            quoteSz=str(quote_sz),
            triggerType="1",
            runType="1",
        )

    def stop_grid(self, algo_id, inst_id):
        """停止网格"""
        return self.grid.grid_stop_algo(
            algoId=algo_id,
            instId=inst_id,
            algoOrdType="grid",
            stopType="1",
        )

    def amend_grid(self, algo_id, inst_id, **kwargs):
        """修改网格参数"""
        return self.grid.grid_amend_algo(
            algoId=algo_id, instId=inst_id, **kwargs
        )

    def get_pending_grids(self, inst_id=None):
        """获取运行中的网格策略列表"""
        kwargs = {"algoOrdType": "grid"}
        if inst_id:
            kwargs["instId"] = inst_id
        return self.grid.grid_orders_algo_pending(**kwargs)

    def get_grid_details(self, algo_id, inst_id):
        """获取指定网格策略的详情"""
        return self.grid.grid_orders_algo_details(algoId=algo_id, instId=inst_id)

    # ==================== 现货交易（低吸高卖）====================
    def market_buy(self, inst_id, quote_sz):
        """市价买入（按 USDT 金额）"""
        return self.trade.place_order(
            instId=inst_id,
            tdMode="cash",
            side="buy",
            ordType="market",
            tgtCcy="quote_ccy",
            sz=str(quote_sz),
        )

    def market_sell(self, inst_id, base_sz):
        """市价卖出（按币种数量）"""
        return self.trade.place_order(
            instId=inst_id,
            tdMode="cash",
            side="sell",
            ordType="market",
            sz=str(base_sz),
        )

    # ==================== 账户 ====================
    def get_balance(self, ccy="USDT"):
        """获取账户余额"""
        return self.account.get_account_balance(ccy=ccy)

    def get_positions(self, inst_id=None):
        """获取持仓（主要用于衍生品，现货用 get_balance）"""
        kwargs = {"instType": "SPOT"}
        if inst_id:
            kwargs["instId"] = inst_id
        return self.account.get_positions(**kwargs)