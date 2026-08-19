"""SQLite persistence with schema migration and atomic runtime-state updates."""
import aiosqlite
import json
from datetime import datetime, timezone, timedelta
from config import logger

DB_FILE = "bot.db"
CST = timezone(timedelta(hours=8))

DEFAULT_CONFIG = {
    "tp_pct": 0.08, "sl_pct": 0.05, "trailing_sl_pct": 0.02,
    "trailing_tp_pct": 0.01, "single_order_usdt": 1.0, "timeframe": "5m",
    "reserve_bottom": 10, "symbols": "", "orderbook_filter": True,
    "waterfall_breaker": True, "max_daily_trades": 20,
    "auto_trade_enabled": False, "auto_min_score": 65,
    "max_per_coin_usdt": 50, "max_daily_loss_pct": 0.05,
    "max_total_allocated_pct": 0.8, "max_positions_per_coin": 8,
    "grid_configs": "{}", "coin_configs": "{}", "max_drawdown_pct": 0.12,
}

async def _columns(db, table):
    async with db.execute(f"PRAGMA table_info({table})") as cur:
        return {row[1] for row in await cur.fetchall()}

async def init_db():
    async with aiosqlite.connect(DB_FILE, timeout=30.0) as db:
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute("PRAGMA foreign_keys=ON;")
        await db.execute('CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT)')
        await db.execute('''CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            time TEXT, symbol TEXT, entry REAL, exit REAL, pnl_pct REAL,
            net_pnl REAL DEFAULT 0, net_pnl_pct REAL DEFAULT 0)''')
        await db.execute('''CREATE TABLE IF NOT EXISTS trade_details (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            time TEXT, symbol TEXT, side TEXT, price REAL, amount REAL,
            signal_score INTEGER, fear_greed INTEGER, funding_rate REAL,
            pnl_pct REAL, real_cost REAL DEFAULT 0, real_revenue REAL DEFAULT 0,
            fee REAL DEFAULT 0, fee_currency TEXT DEFAULT '', order_id TEXT DEFAULT '',
            net_pnl_pct REAL DEFAULT 0)''')
        await db.execute('CREATE TABLE IF NOT EXISTS runtime_state (key TEXT PRIMARY KEY, value TEXT)')
        cols = await _columns(db, 'trade_details')
        migrations = {
            'fee': 'ALTER TABLE trade_details ADD COLUMN fee REAL DEFAULT 0',
            'fee_currency': "ALTER TABLE trade_details ADD COLUMN fee_currency TEXT DEFAULT ''",
            'order_id': "ALTER TABLE trade_details ADD COLUMN order_id TEXT DEFAULT ''",
        }
        for col, sql in migrations.items():
            if col not in cols:
                await db.execute(sql)
        await db.execute('CREATE INDEX IF NOT EXISTS idx_trades_time ON trades(time)')
        await db.execute('CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol)')
        await db.execute('CREATE INDEX IF NOT EXISTS idx_details_time ON trade_details(time)')
        await db.execute('CREATE INDEX IF NOT EXISTS idx_details_symbol ON trade_details(symbol)')
        await db.execute('CREATE INDEX IF NOT EXISTS idx_details_side ON trade_details(side)')
        await db.commit()

async def load_config():
    config = dict(DEFAULT_CONFIG)
    try:
        async with aiosqlite.connect(DB_FILE, timeout=30.0) as db:
            async with db.execute('SELECT key, value FROM config') as cur:
                async for key, value in cur:
                    if key not in config:
                        continue
                    if isinstance(config[key], bool): config[key] = value.lower() in ('true','1','yes')
                    elif isinstance(config[key], int): config[key] = int(value)
                    elif isinstance(config[key], float): config[key] = float(value)
                    else: config[key] = value
    except Exception as e:
        logger.error(f'加载配置失败: {e}')
    if isinstance(config.get('symbols'), str):
        config['symbols'] = [x.strip() for x in config['symbols'].split(',') if x.strip()]
    return config

async def save_config(cfg):
    try:
        async with aiosqlite.connect(DB_FILE, timeout=30.0) as db:
            await db.execute('BEGIN IMMEDIATE')
            for key, value in cfg.items():
                if isinstance(value, list): value = ','.join(value)
                elif isinstance(value, bool): value = str(value).lower()
                await db.execute('INSERT OR REPLACE INTO config(key,value) VALUES(?,?)', (key, str(value)))
            await db.commit()
    except Exception as e:
        logger.error(f'保存配置失败: {e}')

async def load_trades(limit=50):
    try:
        async with aiosqlite.connect(DB_FILE, timeout=30.0) as db:
            async with db.execute('SELECT time,symbol,entry,exit,pnl_pct,net_pnl,net_pnl_pct FROM trades ORDER BY id DESC LIMIT ?', (limit,)) as cur:
                rows = await cur.fetchall()
        return [{"time":r[0],"symbol":r[1],"entry":r[2],"exit":r[3],"pnl_pct":r[4],"net_pnl":r[5],"net_pnl_pct":r[6]} for r in reversed(rows)]
    except Exception as e:
        logger.error(f'加载交易记录失败: {e}')
        return []

async def save_trade(trade):
    try:
        async with aiosqlite.connect(DB_FILE, timeout=30.0) as db:
            await db.execute('INSERT INTO trades(time,symbol,entry,exit,pnl_pct,net_pnl,net_pnl_pct) VALUES(?,?,?,?,?,?,?)',
                (trade['time'],trade['symbol'],trade['entry'],trade['exit'],trade['pnl_pct'],trade.get('net_pnl',0),trade.get('net_pnl_pct',0)))
            await db.commit()
    except Exception as e: logger.error(f'保存交易记录失败: {e}')

async def save_trade_detail(detail):
    try:
        async with aiosqlite.connect(DB_FILE, timeout=30.0) as db:
            await db.execute('''INSERT INTO trade_details
                (time,symbol,side,price,amount,signal_score,fear_greed,funding_rate,pnl_pct,
                 real_cost,real_revenue,fee,fee_currency,order_id,net_pnl_pct)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (detail['time'],detail['symbol'],detail['side'],detail['price'],detail['amount'],
                 detail.get('signal_score',0),detail.get('fear_greed',0),detail.get('funding_rate',0),
                 detail.get('pnl_pct',0),detail.get('real_cost',0),detail.get('real_revenue',0),
                 detail.get('fee',0),detail.get('fee_currency',''),detail.get('order_id',''),detail.get('net_pnl_pct',0)))
            await db.commit()
    except Exception as e: logger.error(f'保存交易详情失败: {e}')

async def get_recent_performance(num=50):
    try:
        async with aiosqlite.connect(DB_FILE, timeout=30.0) as db:
            async with db.execute("SELECT net_pnl_pct FROM trade_details WHERE side='sell' ORDER BY id DESC LIMIT ?",(num,)) as cur: rows=await cur.fetchall()
        pnls=[float(r[0]) for r in rows if r[0] is not None]
        if not pnls: return None
        wins=[p for p in pnls if p>0]; losses=[p for p in pnls if p<0]
        return {'total':len(pnls),'wins':len(wins),'losses':len(losses),'win_rate':len(wins)/len(pnls),'avg_win_pct':sum(wins)/len(wins) if wins else 0,'avg_loss_pct':sum(losses)/len(losses) if losses else 0,'pnls':pnls}
    except Exception as e:
        logger.error(f'获取近期表现失败: {e}'); return None

async def get_today_trades():
    today=datetime.now(CST).strftime('%m-%d')
    try:
        async with aiosqlite.connect(DB_FILE, timeout=30.0) as db:
            async with db.execute("SELECT net_pnl_pct FROM trade_details WHERE side='sell' AND strftime('%m-%d',time)=? ORDER BY id DESC",(today,)) as cur: rows=await cur.fetchall()
        pnls=[float(r[0]) for r in rows if r[0] is not None]
        if not pnls: return None
        wins=[p for p in pnls if p>0]; losses=[p for p in pnls if p<0]
        return {'total':len(pnls),'wins':len(wins),'losses':len(losses),'win_rate':len(wins)/len(pnls),'avg_win_pct':sum(wins)/len(wins) if wins else 0,'avg_loss_pct':sum(losses)/len(losses) if losses else 0,'total_pnl_sum':sum(pnls),'pnls':pnls}
    except Exception as e:
        logger.error(f'获取今日交易失败: {e}'); return None

async def export_db_to_json():
    try:
        async with aiosqlite.connect(DB_FILE, timeout=30.0) as db:
            db.row_factory=aiosqlite.Row
            async with db.execute('SELECT * FROM config') as cur: configs=[dict(r) async for r in cur]
            async with db.execute('SELECT * FROM trades') as cur: trades=[dict(r) async for r in cur]
            async with db.execute('SELECT * FROM trade_details') as cur: details=[dict(r) async for r in cur]
        return json.dumps({'config':configs,'trades':trades,'details':details},indent=2)
    except Exception as e:
        logger.error(f'导出数据库失败: {e}'); return None

async def save_runtime_state(state):
    try:
        async with aiosqlite.connect(DB_FILE, timeout=30.0) as db:
            await db.execute('BEGIN IMMEDIATE')
            for key,value in state.items():
                value=json.dumps(value,separators=(',',':')) if isinstance(value,(dict,list)) else str(value)
                await db.execute('INSERT OR REPLACE INTO runtime_state(key,value) VALUES(?,?)',(key,value))
            await db.commit()
    except Exception as e: logger.error(f'保存运行时状态失败: {e}')

async def load_runtime_state():
    state={}
    try:
        async with aiosqlite.connect(DB_FILE, timeout=30.0) as db:
            async with db.execute('SELECT key,value FROM runtime_state') as cur:
                async for key,value in cur:
                    try: state[key]=json.loads(value)
                    except Exception: state[key]=value
    except Exception as e: logger.error(f'加载运行时状态失败: {e}')
    return state

async def get_total_fees():
    try:
        async with aiosqlite.connect(DB_FILE, timeout=30.0) as db:
            async with db.execute('SELECT COALESCE(SUM(fee),0) FROM trade_details') as cur: row=await cur.fetchone()
        return round(float(row[0] or 0),8)
    except Exception as e: logger.error(f'获取手续费失败: {e}'); return 0.0

async def get_total_net_profit():
    try:
        async with aiosqlite.connect(DB_FILE, timeout=30.0) as db:
            async with db.execute('SELECT COALESCE(SUM(net_pnl),0) FROM trades') as cur: row=await cur.fetchone()
        return round(float(row[0] or 0),8)
    except Exception as e: logger.error(f'获取净收益失败: {e}'); return 0.0
