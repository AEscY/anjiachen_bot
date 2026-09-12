# okx_client/rest.py (在 OKXRest 类中添加)

def get_balance(self, ccy="USDT"):
    """获取账户余额和权益信息"""
    return self.account.get_account_balance(ccy=ccy)
    # 对应 GET /api/v5/account/balance[reference:0]

def get_positions(self, inst_id=None, inst_type="SPOT"):
    """获取当前持仓"""
    kwargs = {"instType": inst_type}
    if inst_id:
        kwargs["instId"] = inst_id
    return self.account.get_positions(**kwargs)
    # 对应 GET /api/v5/account/positions[reference:1]

def get_pending_grids(self, inst_id=None):
    """获取运行中的网格策略列表"""
    kwargs = {"algoOrdType": "grid"}
    if inst_id:
        kwargs["instId"] = inst_id
    return self.grid.grid_order_algo_list(**kwargs)
    # 对应 GET /api/v5/tradingBot/grid/orders-algo-pending[reference:2]

def get_grid_details(self, algo_id, inst_id):
    """获取指定网格策略的详细信息"""
    return self.grid.grid_order_algo_details(algoId=algo_id, instId=inst_id)
    # 对应 GET /api/v5/tradingBot/grid/orders-algo-details[reference:3]