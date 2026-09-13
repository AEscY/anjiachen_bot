from aiogram.filters.state import StatesGroup, State


class MainSG(StatesGroup):
    menu = State()
    add_coin = State()


class CoinSG(StatesGroup):
    panel = State()
    grid = State()
    dip = State()
    # 网格参数输入状态
    grid_edit_range = State()
    grid_edit_num = State()
    # 低吸高卖参数输入状态
    dip_edit_buy = State()
    dip_edit_sell = State()
    dip_edit_base = State()