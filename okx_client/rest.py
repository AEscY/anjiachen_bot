def get_balance(self, ccy="USDT"):
    """获取账户余额"""
    return self.account.get_account_balance(ccy=ccy)

def get_positions(self, inst_id=None):
    """获取持仓。注意：OKX的/account/positions主要针对衍生品，现货持仓需从balance解析"""
    kwargs = {"instType": "SPOT"}
    if inst_id:
        kwargs["instId"] = inst_id
    return self.account.get_positions(**kwargs)

def get_pending_grids(self, inst_id=None):
    """获取运行中的网格策略列表"""
    kwargs = {"algoOrdType": "grid"}
    if inst_id:
        kwargs["instId"] = inst_id
    return self.grid.grid_orders_algo_pending(**kwargs)

def get_grid_details(self, algo_id, inst_id):
    """获取指定网格策略的详情"""
    return self.grid.grid_orders_algo_details(algoId=algo_id, instId=inst_id)