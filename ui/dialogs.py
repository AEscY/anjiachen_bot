async def on_enter_grid(cb: CallbackQuery, button, manager: DialogManager):
    inst_id = manager.dialog_data.get("inst_id")
    if not inst_id:
        await cb.answer("会话已过期，请点击菜单重新选择币种", show_alert=True)
        return
    await manager.switch_to(CoinSG.grid)

async def on_enter_dip(cb: CallbackQuery, button, manager: DialogManager):
    inst_id = manager.dialog_data.get("inst_id")
    if not inst_id:
        await cb.answer("会话已过期，请点击菜单重新选择币种", show_alert=True)
        return
    await manager.switch_to(CoinSG.dip)

async def on_delete_coin(cb: CallbackQuery, button, manager: DialogManager):
    inst_id = manager.dialog_data.get("inst_id")
    if not inst_id:
        await cb.answer("会话已过期，请点击菜单重新选择币种", show_alert=True)
        return
    if not MANAGER:
        await cb.answer("策略管理器未初始化", show_alert=True)
        return
    ok, text = await MANAGER.remove_inst(inst_id)
    if ok and PUB_WS:
        await PUB_WS.unsubscribe(inst_id)
    await cb.answer(text, show_alert=True)
    await manager.start(MainSG.menu)