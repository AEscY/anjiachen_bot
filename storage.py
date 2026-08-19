"""SQLite persistence for bot configuration, fills, closed trades and runtime state."""
import aiosqlite
import json
from datetime import datetime, timezone, timedelta
from config import logger

DB_FILE = "bot.db"
CST = timezone(timedelta(hours=8))
DEFAULT_CONFIG = {
    "tp_pct": 0.08, "sl_pct": 0.05, "trailing_sl_pct": 0.02, "trailing_tp_pct": 0.01,
    "single_order_usdt": 100, "timeframe": "15m", "reserve_bottom": 50, "symbols": "",
    "orderbook_filter": True, "waterfall_breaker": True, "max_daily_trades": 0,
    "auto_trade_enabled": False, "auto_min_score": 75, "max_per_coin_usdt": 0,
    "max_daily_loss_pct": 0.05, "max_total_allocated_pct": 1.0,
    "max_positions_per_coin": 18, "grid_configs": "{}", "coin_configs": "{}", "max_drawdown_pct": 0.15,
}

async def init_db():
    async with aiosqlite.connect(DB_FILE, timeout=30.0) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT)")
        await db.execute("""CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT, time TEXT, symbol TEXT, entry REAL, exit REAL,
            pnl_pct REAL, net_pnl REAL DEFAULT 0, net_pnl_pct REAL DEFAULT 0)""")
        await db.execute("""CREATE TABLE IF NOT EXISTS trade_details (
            id INTEGER PRIMARY KEY AUTOINCREMENT, time TEXT, symbol TEXT, side TEXT, price REAL,
            amount REAL, signal_score INTEGER, fear_greed INTEGER, funding_rate REAL, pnl_pct REAL,
            real_cost REAL DEFAULT 0, real_revenue REAL DEFAULT 0, fee REAL DEFAULT 0,
            fee_currency TEXT DEFAULT '', order_id TEXT DEFAULT '', net_pnl_pct REAL DEFAULT 0)""")
        await db.execute("CREATE TABLE IF NOT EXISTS runtime_state (key TEXT PRIMARY KEY, value TEXT)")
        # Backward-compatible migration for databases created before fill fee/order tracking.
        async with db.execute("PRAGMA table_info(trade_details)") as cursor:
            columns = {row[1] async for row in cursor}
        for name, definition in (("fee", "REAL DEFAULT 0"), ("fee_currency", "TEXT DEFAULT ''"), ("order_id", "TEXT DEFAULT ''")):
            if name not in columns:
                await db.execute(f"ALTER TABLE trade_details ADD COLUMN {name} {definition}")
        for sql in (
            "CREATE INDEX IF NOT EXISTS idx_trades_time ON trades(time)",
            "CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol)",
            "CREATE INDEX IF NOT EXISTS idx_details_time ON trade_details(time)",
            "CREATE INDEX IF NOT EXISTS idx_details_symbol ON trade_details(symbol)",
            "CREATE INDEX IF NOT EXISTS idx_details_side ON trade_details(side)",
        ):
            await db.execute(sql)
        await db.commit()

async def load_config():
    config = dict(DEFAULT_CONFIG)
    try:
        async with aiosqlite.connect(DB_FILE, timeout=30.0) as db:
            async with db.execute("SELECT key, value FROM config") as cursor:
                async for key, value in cursor:
                    if key not in config: continue
                    try:
                        if isinstance(config[key], bool): config[key] = value.lower() in ("true", "1", "yes")
                        elif isinstance(config[key], int): config[key] = int(value)
                        elif isinstance(config[key], float): config[key] = float(value)
                        else: config[key] = value
                    except (ValueError, TypeError): logger.warning(f"忽略无效配置: {key}={value!r}")
    except Exception as e: logger.error(f"加载配置失败: {e}")
    if isinstance(config.get("symbols"), str): config["symbols"] = [s.strip() for s in config["symbols"].split(",") if s.strip()]
    return config

async def save_config(cfg: dict):
    try:
        async with aiosqlite.connect(DB_FILE, timeout=30.0) as db:
            await db.execute("BEGIN IMMEDIATE")
            for key, value in cfg.items():
                if isinstance(value, list): value = ",".join(value)
                elif isinstance(value, bool): value = str(value).lower()
                await db.execute("INSERT OR REPLACE INTO config (key,value) VALUES (?,?)", (key, str(value)))
            await db.commit()
    except Exception as e: logger.error(f"保存配置失败: {e}")

async def load_trades(limit=50):
    trades=[]
    try:
        async with aiosqlite.connect(DB_FILE, timeout=30.0) as db:
            async with db.execute("SELECT time,symbol,entry,exit,pnl_pct,net_pnl,net_pnl_pct FROM trades ORDER BY id DESC LIMIT ?", (limit,)) as c:
                async for r in c: trades.append({"time":r[0],"symbol":r[1],"entry":r[2],"exit":r[3],"pnl_pct":r[4],"net_pnl":r[5],"net_pnl_pct":r[6]})
    except Exception as e: logger.error(f"加载交易记录失败: {e}")
    return list(reversed(trades))

async def save_trade(trade: dict):
    try:
        async with aiosqlite.connect(DB_FILE, timeout=30.0) as db:
            await db.execute("INSERT INTO trades (time,symbol,entry,exit,pnl_pct,net_pnl,net_pnl_pct) VALUES (?,?,?,?,?,?,?)", (trade["time"],trade["symbol"],trade["entry"],trade["exit"],trade["pnl_pct"],trade.get("net_pnl",0),trade.get("net_pnl_pct",0)))
            await db.commit()
    except Exception as e: logger.error(f"保存交易记录失败: {e}")

async def save_trade_detail(detail: dict):
    try:
        async with aiosqlite.connect(DB_FILE, timeout=30.0) as db:
            await db.execute("""INSERT INTO trade_details
                (time,symbol,side,price,amount,signal_score,fear_greed,funding_rate,pnl_pct,real_cost,real_revenue,fee,fee_currency,order_id,net_pnl_pct)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (detail["time"],detail["symbol"],detail["side"],detail["price"],detail["amount"],detail.get("signal_score",0),detail.get("fear_greed",0),detail.get("funding_rate",0),detail.get("pnl_pct",0),detail.get("real_cost",0),detail.get("real_revenue",0),detail.get("fee",0),detail.get("fee_currency",""),detail.get("order_id",""),detail.get("net_pnl_pct",0)))
            await db.commit()
    except Exception as e: logger.error(f"保存交易详情失败: {e}")

async def get_recent_performance(num=50):
    try:
        async with aiosqlite.connect(DB_FILE, timeout=30.0) as db:
            async with db.execute("SELECT net_pnl_pct FROM trade_details WHERE side='sell' ORDER BY id DESC LIMIT ?", (num,)) as c: rows=await c.fetchall()
        pnls=[r[0] for r in rows if r[0] is not None]
        if not pnls: return None
        wins=sum(p>0 for p in pnls); total=len(pnls)
        return {"total":total,"wins":wins,"losses":total-wins,"win_rate":wins/total,"avg_win_pct":sum(p for p in pnls if p>0)/wins if wins else 0,"avg_loss_pct":sum(p for p in pnls if p<0)/(total-wins) if total>wins else 0,"pnls":pnls}
    except Exception as e: logger.error(f"获取近期表现失败: {e}"); return None

async def get_today_trades():
    today=datetime.now(CST).strftime("%m-%d")
    try:
        async with aiosqlite.connect(DB_FILE, timeout=30.0) as db:
            async with db.execute("SELECT net_pnl_pct FROM trade_details WHERE side='sell' AND strftime('%m-%d',time)=? ORDER BY id DESC", (today,)) as c: rows=await c.fetchall()
        pnls=[r[0] for r in rows if r[0] is not None]
        if not pnls: return None
        wins=sum(p>0 for p in pnls); total=len(pnls)
        return {"total":total,"wins":wins,"losses":total-wins,"win_rate":wins/total,"avg_win_pct":sum(p for p in pnls if p>0)/wins if wins else 0,"avg_loss_pct":sum(p for p in pnls if p<0)/(total-wins) if total>wins else 0,"total_pnl_sum":sum(pnls),"pnls":pnls}
    except Exception as e: logger.error(f"获取今日交易失败: {e}"); return None

async def export_db_to_json():
    try:
        async with aiosqlite.connect(DB_FILE, timeout=30.0) as db:
            db.row_factory=aiosqlite.Row
            async with db.execute("SELECT * FROM config") as c: configs=[dict(r) async for r in c]
            async with db.execute("SELECT * FROM trades") as c: trades=[dict(r) async for r in c]
            async with db.execute("SELECT * FROM trade_details") as c: details=[dict(r) async for r in c]
        return json.dumps({"config":configs,"trades":trades,"details":details}, indent=2)
    except Exception as e: logger.error(f"导出数据库失败: {e}"); return None

async def save_runtime_state(state: dict):
    try:
        async with aiosqlite.connect(DB_FILE, timeout=30.0) as db:
            await db.execute("BEGIN IMMEDIATE")
            for key,value in state.items():
                val=json.dumps(value,separators=(",",":"),ensure_ascii=False) if isinstance(value,(dict,list)) else str(value)
                await db.execute("INSERT OR REPLACE INTO runtime_state (key,value) VALUES (?,?)",(key,val))
            await db.commit()
    except Exception as e: logger.error(f"保存运行时状态失败: {e}")

async def load_runtime_state():
    state={}
    try:
        async with aiosqlite.connect(DB_FILE, timeout=30.0) as db:
            async with db.execute("SELECT key,value FROM runtime_state") as c:
                async for key,value in c:
                    try: state[key]=json.loads(value)
                    except (ValueError,TypeError): state[key]=value
    except Exception as e: logger.error(f"加载运行时状态失败: {e}")
    return state

async def get_total_fees():
    try:
        async with aiosqlite.connect(DB_FILE, timeout=30.0) as db:
            async with db.execute("SELECT SUM(fee) FROM trade_details") as c: row=await c.fetchone()
        return round(float(row[0] or 0.0),8) if row else 0.0
    except Exception as e: logger.error(f"获取手续费失败: {e}"); return 0.0

async def get_total_net_profit():
    try:
        async with aiosqlite.connect(DB_FILE, timeout=30.0) as db:
            async with db.execute("SELECT SUM(net_pnl) FROM trades WHERE net_pnl IS NOT NULL") as c: row=await c.fetchone()
        return round(float(row[0] or 0.0),8) if row else 0.0
    except Exception as e: logger.error(f"获取净收益失败: {e}"); return 0.0
