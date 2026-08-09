"""
storage.py - SQLite 数据库管理（支持持仓状态恢复）
"""
import aiosqlite
from config import logger

DB_FILE = "bot.db"

DEFAULT_CONFIG = {
    "tp_pct": 0.08, "sl_pct": 0.05, "trailing_sl_pct": 0.02,
    "trailing_tp_pct": 0.01, "single_order_usdt": 100, "timeframe": "15m",
    "reserve_bottom": 50, "symbols": "", "orderbook_filter": True,
    "waterfall_breaker": True, "max_daily_trades": 0,
    "auto_trade_enabled": False, "auto_min_score": 75,
    "max_per_coin_usdt": 0
}

async def init_db():
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute('''CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT)''')
        await db.execute('''CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            time TEXT, symbol TEXT, entry REAL, exit REAL, pnl_pct REAL)''')
        # 新增持仓持久化表，防止重启丢失状态
        await db.execute('''CREATE TABLE IF NOT EXISTS active_positions (
            symbol TEXT PRIMARY KEY, entry_price REAL, trailing_high REAL, is_trailing_active INTEGER)''')
        await db.commit()

async def save_position_state(symbol: str, entry_price: float, trailing_high: float, is_active: bool):
    """保存活跃持仓状态"""
    try:
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute(
                "INSERT OR REPLACE INTO active_positions (symbol, entry_price, trailing_high, is_trailing_active) VALUES (?, ?, ?, ?)",
                (symbol, entry_price, trailing_high, 1 if is_active else 0)
            )
            await db.commit()
    except Exception as e:
        logger.error(f"保存持仓状态失败 {symbol}: {e}")

async def delete_position_state(symbol: str):
    """平仓后清除持仓状态"""
    try:
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute("DELETE FROM active_positions WHERE symbol = ?", (symbol,))
            await db.commit()
    except Exception as e:
        logger.error(f"清除持仓状态失败 {symbol}: {e}")

async def load_position_states():
    """重启时恢复所有持仓状态"""
    positions = {}
    try:
        async with aiosqlite.connect(DB_FILE) as db:
            async with db.execute("SELECT symbol, entry_price, trailing_high, is_trailing_active FROM active_positions") as cursor:
                async for row in cursor:
                    positions[row[0]] = {
                        "entry": row[1],
                        "high": row[2],
                        "active": bool(row[3])
                    }
    except Exception as e:
        logger.error(f"恢复持仓状态失败: {e}")
    return positions

# 保持原有 load_config, save_config, load_trades, save_trade 不变...
async def load_config():
    config = dict(DEFAULT_CONFIG)
    try:
        async with aiosqlite.connect(DB_FILE) as db:
            async with db.execute("SELECT key, value FROM config") as cursor:
                async for row in cursor:
                    key, value = row
                    if key in config:
                        if isinstance(config[key], bool): config[key] = value.lower() in ("true", "1", "yes")
                        elif isinstance(config[key], int): config[key] = int(value)
                        elif isinstance(config[key], float): config[key] = float(value)
                        else: config[key] = value
    except Exception as e: logger.error(f"加载配置失败: {e}")
    if isinstance(config.get("symbols"), str):
        config["symbols"] = [s.strip() for s in config["symbols"].split(",") if s.strip()]
    return config

async def save_config(cfg: dict):
    try:
        async with aiosqlite.connect(DB_FILE) as db:
            for key, value in cfg.items():
                val = ",".join(value) if isinstance(value, list) else (str(value).lower() if isinstance(value, bool) else str(value))
                await db.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", (key, val))
            await db.commit()
    except Exception as e: logger.error(f"保存配置失败: {e}")

async def load_trades(limit=50):
    trades = []
    try:
        async with aiosqlite.connect(DB_FILE) as db:
            async with db.execute("SELECT time, symbol, entry, exit, pnl_pct FROM trades ORDER BY id DESC LIMIT ?", (limit,)) as cursor:
                async for row in cursor:
                    trades.append({"time": row[0], "symbol": row[1], "entry": row[2], "exit": row[3], "pnl_pct": row[4]})
    except Exception as e: logger.error(f"加载交易记录失败: {e}")
    return list(reversed(trades))

async def save_trade(trade: dict):
    try:
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute("INSERT INTO trades (time, symbol, entry, exit, pnl_pct) VALUES (?, ?, ?, ?, ?)",
                            (trade["time"], trade["symbol"], trade["entry"], trade["exit"], trade["pnl_pct"]))
            await db.commit()
    except Exception as e: logger.error(f"保存交易记录失败: {e}")
