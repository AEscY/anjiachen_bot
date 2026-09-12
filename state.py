async def persist_loop():
    while True:
        await asyncio.sleep(1800)  # 30分钟
        await store.save({
            "grid": dlg.GRID.snapshot() if dlg.GRID else {},
            "dip": dlg.DIP.snapshot() if dlg.DIP else {},
        })