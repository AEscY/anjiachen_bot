from aiogram.filters.state import StatesGroup, State


class AppSG(StatesGroup):
    menu = State()
    add_coin = State()
    coin_panel = State()
    signal_detail = State()
    position_detail = State()
    params = State()
    edit_param = State()