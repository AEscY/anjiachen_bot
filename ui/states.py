from aiogram.filters.state import StatesGroup, State


class AppSG(StatesGroup):
    menu = State()
    add_coin = State()
    coin_panel = State()
    grid = State()
    dip = State()
    params = State()        # 参数列表
    edit_param = State()    # 编辑单个参数