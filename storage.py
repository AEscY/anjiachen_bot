"""
storage.py - SQLite 数据库管理
"""
import aiosqlite
from config import logger

DB_FILE = "bot.db"

DEFAULT_CONFIG = {
    "tp_pct": 0.08,
    "sl_pct": 0.05,
    "trailing_sl_pct": 0.02,
    "trailing_tp_pct": 0.01,
    "single_order_usdt": 100,
    "timeframe": "15m",
    "reserve_bottom": 50,
    "symbols": "",
    "orderbook_filter": True,
    "waterfall_breaker": True,
    "max_daily_trades": 0,
    "auto_trade_enabled": False,
    "auto_min_score": 75,
    "max_per_coin_usdt": 0
}


async def init_db():
    """初始化数据库表"""
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute('''CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT)''')
        await db.execute('''CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            time TEXT,
            symbol TEXT,
            entry REAL,
            exit REAL,
            pnl_pct REAL
        )''')
        await db.commit()


async def load_config():
    """加载配置，缺失键自动填充默认值"""
    config = dict(DEFAULT_CONFIG)
    try:
        async with aiosqlite.connect(DB_FILE) as db:
            async with db.execute("SELECT key, value FROM config") as cursor:
                async for row in cursor:
                    key, value = row
                    if key in config:
                        if isinstance(config[key], bool):
                            config[key] = value.lower() in ("true", "1", "yes")
                        elif isinstance(config[key], int):
                            config[key] = int(value)
                        elif isinstance(config[key], float):
                            config[key] = float(value)
                        else:
                            config[key] = value
    except Exception as e:
        logger.error(f"加载配置失败: {e}")

    # 将 symbols 字符串转为列表
    if isinstance(config.get("symbols"), str):
        config["symbols"] = [s.strip() for s in config["symbols"].split(",") if s.strip()]
    return config


async def save_config(cfg: dict):
    """保存配置到数据库"""
    try:
        async with aiosqlite.connect(DB_FILE) as db:
            for key, value in cfg.items():
                if isinstance(value, list):
                    value = ",".join(value)
                elif isinstance(value, bool):
                    value = str(value).lower()
                await db.execute(
                    "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
                    (key, str(value))
                )
            await db.commit()
    except Exception as e:
        logger.error(f"保存配置失败: {e}")


async def load_trades(limit=50):
    """加载最近的交易记录"""
    trades = []
    try:
        async with aiosqlite.connect(DB_FILE) as db:
            async with db.execute(
                "SELECT time, symbol, entry, exit, pnl_pct FROM trades ORDER BY id DESC LIMIT ?",
                (limit,)
            ) as cursor:
                async for row in cursor:
                    trades.append({
                        "time": row[0],
                        "symbol": row[1],
                        "entry": row[2],
                        "exit": row[3],
                        "pnl_pct": row[4]
                    })
    except Exception as e:
        logger.error(f"加载交易记录失败: {e}")
    return list(reversed(trades))  # 按时间正序


async def save_trade(trade: dict):
    """保存一条交易记录"""
    try:
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute(
                "INSERT INTO trades (time, symbol, entry, exit, pnl_pct) VALUES (?, ?, ?, ?, ?)",
                (trade["time"], trade["symbol"], trade["entry"], trade["exit"], trade["pnl_pct"])
            )
            await db.commit()
    except Exception as e:
        logger.error(f"保存交易记录失败: {e}")