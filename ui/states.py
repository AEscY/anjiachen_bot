# ui/states.py
from aiogram.filters.state import StatesGroup, State

class MainSG(StatesGroup):
    menu = State()

class GridSG(StatesGroup):
    panel = State()
    edit_range = State()
    edit_grid_num = State()

class DipSG(StatesGroup):
    panel = State()
    edit_buy_pct = State()
    edit_sell_pct = State()