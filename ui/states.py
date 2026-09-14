from aiogram.filters.state import StatesGroup, State


class AppSG(StatesGroup):
    # 主菜单
    menu = State()
    add_coin = State()

    # 币种面板
    coin_panel = State()

    # 网格
    grid = State()
    grid_edit_range = State()
    grid_edit_num = State()

    # 低吸高卖
    dip = State()
    dip_edit_buy = State()
    dip_edit_sell = State()
    dip_edit_base = State()