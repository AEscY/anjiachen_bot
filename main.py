async def restore_from_okx():
    """从 OKX API 恢复策略状态"""
    rest = OKXRest()

    # --- 恢复网格策略 ---
    try:
        grids_resp = rest.get_pending_grids(DEFAULT_INST_ID)
        if grids_resp.get("code") == "0" and grids_resp.get("data"):
            latest = grids_resp["data"][0]
            dlg.GRID.algo_id = latest["algoId"]
            dlg.GRID.params.update({
                "minPx": float(latest.get("minPx", 0)),
                "maxPx": float(latest.get("maxPx", 0)),
                "gridNum": int(latest.get("gridNum", 0)),
            })
            dlg.GRID.running = True
            await alert(f"✅ 已从 OKX 恢复网格策略: {latest['algoId']}")
    except Exception as e:
        await alert(f"⚠️ 网格状态恢复失败: {e}")

    # --- 恢复低吸高卖策略（从余额解析现货持仓） ---
    try:
        bal_resp = rest.get_balance("USDT")
        if bal_resp.get("code") == "0" and bal_resp.get("data"):
            details = bal_resp["data"][0].get("details", [])
            base_ccy = DEFAULT_INST_ID.split("-")[0]  # BTC
            for d in details:
                if d.get("ccy") == base_ccy:
                    pos = float(d.get("eq", 0))
                    if pos > 0:
                        dlg.DIP.position = pos
                        dlg.DIP.cost = float(d.get("eqUsd", 0))
                        dlg.DIP.running = True
                        await alert(f"✅ 已恢复 {base_ccy} 持仓: {pos}")
    except Exception as e:
        await alert(f"⚠️ 持仓恢复失败: {e}")


async def main():
    # ... 初始化策略（同之前）...
    dlg.GRID = GridStrategy(DEFAULT_INST_ID)
    dlg.DIP  = DipSellStrategy(DEFAULT_INST_ID)

    # 先尝试从 OKX 恢复，失败则回退到 state.json
    try:
        await restore_from_okx()
    except Exception:
        saved = await store.load()
        if saved.get("grid"):
            dlg.GRID.params.update(saved["grid"])
        if saved.get("dip"):
            dlg.DIP.params.update(saved["dip"])

    # ... 启动 WebSocket 和 polling（同之前）...