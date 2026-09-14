from aiogram.filters.state import StatesGroup, State


class AppSG(StatesGroup):
    menu = State()
    add_coin = State()
    coin_panel = State()
    grid = State()
    dip = State()
    dip_edit_tp = State()
    dip_edit_sl = State()
    dip_edit_trailing = State()
    dip_edit_spend = State()