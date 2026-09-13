from aiogram.filters.state import StatesGroup, State


class MainSG(StatesGroup):
    menu = State()
    add_coin = State()


class CoinSG(StatesGroup):
    panel = State()
    grid = State()
    dip = State()
    grid_edit_range = State()
    grid_edit_num = State()
    dip_edit_buy = State()
    dip_edit_sell = State()
    dip_edit_base = State()