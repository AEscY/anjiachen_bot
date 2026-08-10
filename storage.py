"""
storage.py - SQLite 数据库管理（新增状态持久化 + 交易详情 + 学习统计）
"""
import aiosqlite
import json
from datetime import datetime, timezone, timedelta
from config import logger

DB_FILE = "bot.db"
CST = timezone(timedelta(hours=8))

DEFAULT_CONFIG = {
    "tp_pct": 0.08, "sl_pct": 0.05, "trailing_sl_pct": 0.02,
    "trailing_tp_pct": 0.01, "single_order_usdt": 100, "timeframe": "15m",
    "reserve_bottom": 50, "symbols": "", "orderbook_filter": True,
    "waterfall_breaker": True, "max_daily_trades": 0,
    "auto_trade_enabled": False, "auto_min_score": 75,
    "max_per_coin_usdt": 0, "max_daily_loss_pct": 0.05,
    "max_total_allocated_pct": 1.0, "max_positions_per_coin": 18
}

async def init_db():
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute('''CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT)''')
        await db.execute('''CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            time TEXT, symbol TEXT, entry REAL, exit REAL, pnl_pct REAL,
            net_pnl REAL DEFAULT 0, net_pnl_pct REAL DEFAULT 0)''')
        await db.execute('''CREATE TABLE IF NOT EXISTS trade_details (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            time TEXT, symbol TEXT, side TEXT,
            price REAL, amount REAL, signal_score INTEGER,
            fear_greed INTEGER, funding_rate REAL, pnl_pct REAL,
            real_cost REAL DEFAULT 0, real_revenue REAL DEFAULT 0)''')
        # 新增：运行时状态表
        await db.execute('''CREATE TABLE IF NOT EXISTS runtime_state (
            key TEXT PRIMARY KEY, value TEXT)''')
        await db.commit()

async def load_config():
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
    if isinstance(config.get("symbols"), str):
        config["symbols"] = [s.strip() for s in config["symbols"].split(",") if s.strip()]
    return config

async def save_config(cfg: dict):
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
    trades = []
    try:
        async with aiosqlite.connect(DB_FILE) as db:
            async with db.execute(
                "SELECT time, symbol, entry, exit, pnl_pct, net_pnl, net_pnl_pct FROM trades ORDER BY id DESC LIMIT ?",
                (limit,)
            ) as cursor:
                async for row in cursor:
                    trades.append({
                        "time": row[0], "symbol": row[1],
                        "entry": row[2], "exit": row[3], "pnl_pct": row[4],
                        "net_pnl": row[5], "net_pnl_pct": row[6]
                    })
    except Exception as e:
        logger.error(f"加载交易记录失败: {e}")
    return list(reversed(trades))

async def save_trade(trade: dict):
    try:
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute(
                "INSERT INTO trades (time, symbol, entry, exit, pnl_pct, net_pnl, net_pnl_pct) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (trade["time"], trade["symbol"], trade["entry"], trade["exit"], trade["pnl_pct"],
                 trade.get("net_pnl", 0), trade.get("net_pnl_pct", 0))
            )
            await db.commit()
    except Exception as e:
        logger.error(f"保存交易记录失败: {e}")

async def save_trade_detail(detail: dict):
    try:
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute(
                "INSERT INTO trade_details (time, symbol, side, price, amount, signal_score, fear_greed, funding_rate, pnl_pct, real_cost, real_revenue) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (detail["time"], detail["symbol"], detail["side"], detail["price"], detail["amount"],
                 detail.get("signal_score", 0), detail.get("fear_greed", 0), detail.get("funding_rate", 0),
                 detail.get("pnl_pct", 0), detail.get("real_cost", 0), detail.get("real_revenue", 0))
            )
            await db.commit()
    except Exception as e:
        logger.error(f"保存交易详情失败: {e}")

async def get_recent_performance(num=10):
    try:
        async with aiosqlite.connect(DB_FILE) as db:
            async with db.execute(
                "SELECT net_pnl_pct FROM trade_details WHERE side='sell' AND net_pnl_pct IS NOT NULL ORDER BY id DESC LIMIT ?",
                (num,)
            ) as cursor:
                rows = await cursor.fetchall()
            if not rows:
                return None
            pnls = [row[0] for row in rows if row[0] is not None]
            if not pnls:
                return None
            wins = sum(1 for p in pnls if p > 0)
            total = len(pnls)
            avg_win = sum(p for p in pnls if p > 0) / wins if wins > 0 else 0
            avg_loss = sum(p for p in pnls if p < 0) / (total - wins) if total - wins > 0 else 0
            return {
                "total": total, "wins": wins, "losses": total - wins,
                "win_rate": wins / total if total > 0 else 0,
                "avg_win_pct": avg_win, "avg_loss_pct": avg_loss, "pnls": pnls
            }
    except Exception as e:
        logger.error(f"获取近期表现失败: {e}")
        return None

async def get_today_trades():
    today_str = datetime.now(CST).strftime("%m-%d")
    try:
        async with aiosqlite.connect(DB_FILE) as db:
            async with db.execute(
                "SELECT net_pnl_pct FROM trade_details WHERE side='sell' AND net_pnl_pct IS NOT NULL AND time LIKE ? ORDER BY id DESC",
                (today_str + '%',)
            ) as cursor:
                rows = await cursor.fetchall()
            if not rows:
                return None
            pnls = [row[0] for row in rows if row[0] is not None]
            if not pnls:
                return None
            wins = sum(1 for p in pnls if p > 0)
            total = len(pnls)
            total_pnl = sum(pnls)
            avg_win = sum(p for p in pnls if p > 0) / wins if wins > 0 else 0
            avg_loss = sum(p for p in pnls if p < 0) / (total - wins) if total - wins > 0 else 0
            return {
                "total": total, "wins": wins, "losses": total - wins,
                "win_rate": wins / total if total > 0 else 0,
                "avg_win_pct": avg_win, "avg_loss_pct": avg_loss,
                "total_pnl_sum": total_pnl, "pnls": pnls
            }
    except Exception as e:
        logger.error(f"获取今日交易失败: {e}")
        return None

async def export_db_to_json():
    try:
        async with aiosqlite.connect(DB_FILE) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM config") as cursor:
                configs = [dict(row) async for row in cursor]
            async with db.execute("SELECT * FROM trades") as cursor:
                trades = [dict(row) async for row in cursor]
            async with db.execute("SELECT * FROM trade_details") as cursor:
                details = [dict(row) async for row in cursor]
        return json.dumps({"config": configs, "trades": trades, "details": details}, indent=2)
    except Exception as e:
        logger.error(f"导出数据库失败: {e}")
        return None

# ========== 新增：运行时状态持久化 ==========
async def save_runtime_state(state: dict):
    """保存运行时状态（仓位计数、入场价、峰值资产等）"""
    try:
        async with aiosqlite.connect(DB_FILE) as db:
            for key, value in state.items():
                await db.execute(
                    "INSERT OR REPLACE INTO runtime_state (key, value) VALUES (?, ?)",
                    (key, json.dumps(value) if not isinstance(value, str) else value)
                )
            await db.commit()
    except Exception as e:
        logger.error(f"保存运行时状态失败: {e}")

async def load_runtime_state():
    """恢复运行时状态"""
    state = {}
    try:
        async with aiosqlite.connect(DB_FILE) as db:
            async with db.execute("SELECT key, value FROM runtime_state") as cursor:
                async for row in cursor:
                    key, value = row
                    try:
                        state[key] = json.loads(value)
                    except:
                        state[key] = value
    except Exception as e:
        logger.error(f"加载运行时状态失败: {e}")
    return state