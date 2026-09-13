# ui/states.py
from aiogram.filters.state import StatesGroup, State


class MainSG(StatesGroup):
    menu     = State()
    add_coin = State()


class CoinSG(StatesGroup):
    panel = State()   # 币种总览
    grid  = State()   # 网格控制
    dip   = State()   # 低吸高卖控制