from aiogram.filters.state import StatesGroup, State


class AppSG(StatesGroup):
    menu = State()
    add_coin = State()
    coin_panel = State()
    grid = State()
    dip = State()