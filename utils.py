# utils.py
import math

def round_to_tick(price: float, tick: float) -> float:
    return round(price / tick) * tick

def pct_change(a: float, b: float) -> float:
    return (a - b) / b if b else 0.0