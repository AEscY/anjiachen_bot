"""
storage.py - SQLite 数据库管理（修复 net_pnl_pct 列缺失 / 日期查询逻辑 / WAL 模式）
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
    "max_total_allocated_pct": 1.0, "max_positions_per_coin": 18,
    "grid_configs": "{}", "coin_configs": "{}",
    "max_drawdown_pct": 0.15
}

async def init_db():
    async with aiosqlite.connect(DB_FILE, timeout=30.0) as db:
        # ✅ 开启 WAL 模式（写前日志），大幅减少锁库概率
        await db.execute("PRAGMA journal_mode=WAL;")
        
        await db.execute('''CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT)''')
        
        await db.execute('''CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            time TEXT, symbol TEXT, entry REAL, exit REAL, pnl_pct REAL,
            net_pnl REAL DEFAULT 0, net_pnl_pct REAL DEFAULT 0)''')
        
        # ✅ 修复：新增 net_pnl_pct 列（之前仅存在于 trades 表，此处补齐）
        await db.execute('''CREATE TABLE IF NOT EXISTS trade_details (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            time TEXT, symbol TEXT, side TEXT,
            price REAL, amount REAL, signal_score INTEGER,
            fear_greed INTEGER, funding_rate REAL, pnl_pct REAL,
            real_cost REAL DEFAULT 0, real_revenue REAL DEFAULT 0,
            net_pnl_pct REAL DEFAULT 0)''')
        
        await db.execute('''CREATE TABLE IF NOT EXISTS runtime_state (
            key TEXT PRIMARY KEY, value TEXT)''')
        
        # 索引优化
        await db.execute("CREATE INDEX IF NOT EXISTS idx_trades_time ON trades(time)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_details_time ON trade_details(time)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_details_symbol ON trade_details(symbol)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_details_side ON trade_details(side)")
        await db.commit()

async def load_config():
    config = dict(DEFAULT_CONFIG)
    try:
        async with aiosqlite.connect(DB_FILE, timeout=30.0) as db:
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
        async with aiosqlite.connect(DB_FILE, timeout=30.0) as db:
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
        async with aiosqlite.connect(DB_FILE, timeout=30.0) as db:
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
        async with aiosqlite.connect(DB_FILE, timeout=30.0) as db:
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
        async with aiosqlite.connect(DB_FILE, timeout=30.0) as db:
            await db.execute(
                """INSERT INTO trade_details 
                (time, symbol, side, price, amount, signal_score, fear_greed, 
                 funding_rate, pnl_pct, real_cost, real_revenue, net_pnl_pct) 
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (detail["time"], detail["symbol"], detail["side"], detail["price"], detail["amount"],
                 detail.get("signal_score", 0), detail.get("fear_greed", 0), detail.get("funding_rate", 0),
                 detail.get("pnl_pct", 0), detail.get("real_cost", 0), detail.get("real_revenue", 0),
                 detail.get("net_pnl_pct", 0))
            )
            await db.commit()
    except Exception as e:
        logger.error(f"保存交易详情失败: {e}")

# ==================== 修复 1：net_pnl_pct 列查询 ====================
async def get_recent_performance(num=50):
    try:
        async with aiosqlite.connect(DB_FILE, timeout=30.0) as db:
            # ✅ 修复：trade_details 现已包含 net_pnl_pct 列
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

# ==================== 修复 2：日期匹配改用 strftime ====================
async def get_today_trades():
    today_str = datetime.now(CST).strftime("%m-%d")
    try:
        async with aiosqlite.connect(DB_FILE, timeout=30.0) as db:
            # ✅ 修复：使用 strftime 函数按日期过滤，兼容所有时间格式
            async with db.execute(
                """SELECT net_pnl_pct FROM trade_details 
                   WHERE side='sell' AND net_pnl_pct IS NOT NULL 
                   AND strftime('%m-%d', time) = ? 
                   ORDER BY id DESC""",
                (today_str,)
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
        async with aiosqlite.connect(DB_FILE, timeout=30.0) as db:
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

# ========== 运行时状态持久化 ==========
async def save_runtime_state(state: dict):
    try:
        async with aiosqlite.connect(DB_FILE, timeout=30.0) as db:
            for key, value in state.items():
                val_str = json.dumps(value) if isinstance(value, (dict, list)) else str(value)
                await db.execute(
                    "INSERT OR REPLACE INTO runtime_state (key, value) VALUES (?, ?)",
                    (key, val_str)
                )
            await db.commit()
    except Exception as e:
        logger.error(f"保存运行时状态失败: {e}")

async def load_runtime_state():
    state = {}
    try:
        async with aiosqlite.connect(DB_FILE, timeout=30.0) as db:
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
    
    
    async def get_total_fees():
    """获取累计手续费（从 trade_details 的 real_cost 总和估算）"""
    try:
        async with aiosqlite.connect(DB_FILE, timeout=30.0) as db:
            async with db.execute(
                "SELECT SUM(real_cost) FROM trade_details WHERE side='buy'"
            ) as cursor:
                row = await cursor.fetchone()
                buy_cost = row[0] if row and row[0] else 0
            async with db.execute(
                "SELECT SUM(real_revenue) FROM trade_details WHERE side='sell'"
            ) as cursor:
                row = await cursor.fetchone()
                sell_revenue = row[0] if row and row[0] else 0
            # 估算手续费 ≈ (买入成本 + 卖出收入) * 0.001 (0.1%)
            # 更精确：可以从 exchange 获取实际费率，但这里用估算
            fees = (buy_cost + sell_revenue) * 0.001
            return round(fees, 4)
    except Exception as e:
        logger.error(f"获取手续费失败: {e}")
        return 0.0

           async def get_total_net_profit():
    """获取累计净收益（从 trade_details 的 real_revenue - real_cost 总和）"""
    try:
        async with aiosqlite.connect(DB_FILE, timeout=30.0) as db:
            async with db.execute(
                "SELECT SUM(real_revenue - real_cost) FROM trade_details WHERE side='sell'"
            ) as cursor:
                row = await cursor.fetchone()
                return round(row[0] if row and row[0] else 0.0, 4)
    except Exception as e:
        logger.error(f"获取净收益失败: {e}")
        return 0.0
    
    
    