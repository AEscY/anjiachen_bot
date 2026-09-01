"""SQLite persistence with schema migration and atomic runtime-state updates."""
import aiosqlite
import json
from datetime import datetime, timezone, timedelta
from config import logger

DB_FILE = "bot.db"
CST = timezone(timedelta(hours=8))

# 默认值改为从参数注册表动态生成，而非在此硬编码罗列。
# 背景：此前本字典与 bot.py 的默认值两处不一致（bot.py 写 1.5%/1%，这里是 8%/5%），
# 而 load_config 以本字典为准，导致代码里写的默认值从未真正生效。
# 现在单一数据源在 core/params.py，新增参数自动生效，不会再出现漏写。
def _build_default_config() -> dict:
    cfg = {}
    try:
        from core.params import defaults
        cfg.update(defaults())
    except Exception:
        # 兜底：注册表不可用时至少保证核心风控参数存在
        cfg.update({
            "tp_pct": 0.015, "sl_pct": 0.01, "max_daily_loss_pct": 0.05,
            "max_drawdown_pct": 0.12, "max_total_allocated_pct": 0.8,
        })
    # 非注册表项（结构性状态，不适合做成可调参数）
    cfg.update({
        "symbols": "", "timeframe": "5m", "auto_trade_enabled": False,
        "orderbook_filter": True, "coin_configs": "{}",
    })
    return cfg


DEFAULT_CONFIG = _build_default_config()

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
            time TEXT, ts TEXT, symbol TEXT, entry REAL, exit REAL, pnl_pct REAL,
            net_pnl REAL DEFAULT 0, net_pnl_pct REAL DEFAULT 0)''')
        await db.execute('''CREATE TABLE IF NOT EXISTS trade_details (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            time TEXT, ts TEXT, symbol TEXT, side TEXT, price REAL, amount REAL,
            signal_score INTEGER, fear_greed INTEGER, funding_rate REAL,
            pnl_pct REAL, real_cost REAL DEFAULT 0, real_revenue REAL DEFAULT 0,
            fee REAL DEFAULT 0, fee_currency TEXT DEFAULT '', order_id TEXT DEFAULT '',
            net_pnl_pct REAL DEFAULT 0)''')
        await db.execute('CREATE TABLE IF NOT EXISTS runtime_state (key TEXT PRIMARY KEY, value TEXT)')

        # ---- 迁移：补齐后续版本新增列（幂等）----
        for table, migrations in (
            ('trade_details', {
                'fee': 'ALTER TABLE trade_details ADD COLUMN fee REAL DEFAULT 0',
                'fee_currency': "ALTER TABLE trade_details ADD COLUMN fee_currency TEXT DEFAULT ''",
                'order_id': "ALTER TABLE trade_details ADD COLUMN order_id TEXT DEFAULT ''",
                # ISO 时间戳列：SQLite 日期函数只认 'YYYY-MM-DD HH:MM:SS'
                'ts': "ALTER TABLE trade_details ADD COLUMN ts TEXT",
            }),
            ('trades', {
                'ts': 'ALTER TABLE trades ADD COLUMN ts TEXT',
            }),
        ):
            cols = await _columns(db, table)
            for col, sql in migrations.items():
                if col not in cols:
                    await db.execute(sql)

        # ---- 回填历史数据的 ISO 时间（旧格式 'MM-DD HH:MM' 无法被 strftime 解析）----
        await _backfill_ts(db)

        await db.execute('CREATE INDEX IF NOT EXISTS idx_trades_time ON trades(time)')
        await db.execute('CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(ts)')
        await db.execute('CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol)')
        await db.execute('CREATE INDEX IF NOT EXISTS idx_details_time ON trade_details(time)')
        await db.execute('CREATE INDEX IF NOT EXISTS idx_details_ts ON trade_details(ts)')
        await db.execute('CREATE INDEX IF NOT EXISTS idx_details_symbol ON trade_details(symbol)')
        await db.execute('CREATE INDEX IF NOT EXISTS idx_details_side ON trade_details(side)')
        await db.commit()


def _guess_year(time_str: str) -> int:
    """旧格式 'MM-DD HH:MM' 无年份，按'不晚于今天'推断，避免跨年误判。"""
    now = datetime.now(CST)
    try:
        mm, dd = time_str.split(' ')[0].split('-')
        mm, dd = int(mm), int(dd)
    except Exception:
        return now.year
    # 若该日期在今天是"未来"，说明属于去年
    if (mm, dd) > (now.month, now.day):
        return now.year - 1
    return now.year


async def _backfill_ts(db):
    """把 time='MM-DD HH:MM' 的历史行补成 ISO 'YYYY-MM-DD HH:MM:SS'。"""
    try:
        async with db.execute(
            "SELECT id, time FROM trade_details WHERE ts IS NULL AND time IS NOT NULL"
        ) as cur:
            rows = await cur.fetchall()
        for rid, t in rows:
            if not t:
                continue
            if len(t) >= 19 and t[4] == '-':      # 已是 ISO
                iso = t[:19]
            else:
                try:
                    iso = f"{_guess_year(t)}-{t.replace('/', '-')}:00"
                    datetime.strptime(iso, '%Y-%m-%d %H:%M:%S')
                except Exception:
                    continue
            await db.execute('UPDATE trade_details SET ts=? WHERE id=?', (iso, rid))

        async with db.execute(
            "SELECT id, time FROM trades WHERE ts IS NULL AND time IS NOT NULL"
        ) as cur:
            rows = await cur.fetchall()
        for rid, t in rows:
            if not t or (len(t) >= 19 and t[4] == '-'):
                if t:
                    await db.execute('UPDATE trades SET ts=? WHERE id=?', (t[:19], rid))
                continue
            try:
                iso = f"{_guess_year(t)}-{t.replace('/', '-')}:00"
                datetime.strptime(iso, '%Y-%m-%d %H:%M:%S')
                await db.execute('UPDATE trades SET ts=? WHERE id=?', (iso, rid))
            except Exception:
                continue
    except Exception as e:
        logger.error(f'回填时间戳失败: {e}')


def now_parts():
    """返回 (显示用 'MM-DD HH:MM', 查询用 ISO 'YYYY-MM-DD HH:MM:SS')"""
    n = datetime.now(CST)
    return n.strftime('%m-%d %H:%M'), n.strftime('%Y-%m-%d %H:%M:%S')

def _coerce(raw, default):
    """按默认值类型解析字符串；解析失败则回退默认值，避免整份配置因单项损坏而丢失。"""
    if raw is None:
        return default
    if isinstance(default, bool):
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in ('true', '1', 'yes', 'on')
    if isinstance(default, int):
        try:
            return int(float(raw))      # 兼容 "10.5" / "10"
        except (TypeError, ValueError):
            logger.warning(f'配置项解析失败，回退默认值: {raw!r} -> {default}')
            return default
    if isinstance(default, float):
        try:
            return float(raw)
        except (TypeError, ValueError):
            logger.warning(f'配置项解析失败，回退默认值: {raw!r} -> {default}')
            return default
    return raw


async def load_config():
    config = dict(DEFAULT_CONFIG)
    try:
        async with aiosqlite.connect(DB_FILE, timeout=30.0) as db:
            async with db.execute('SELECT key, value FROM config') as cur:
                async for key, value in cur:
                    if key not in config:
                        continue
                    config[key] = _coerce(value, config[key])
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
        ts = trade.get('ts') or datetime.now(CST).strftime('%Y-%m-%d %H:%M:%S')
        async with aiosqlite.connect(DB_FILE, timeout=30.0) as db:
            await db.execute('INSERT INTO trades(time,ts,symbol,entry,exit,pnl_pct,net_pnl,net_pnl_pct) VALUES(?,?,?,?,?,?,?,?)',
                (trade['time'],ts,trade['symbol'],trade['entry'],trade['exit'],trade['pnl_pct'],trade.get('net_pnl',0),trade.get('net_pnl_pct',0)))
            await db.commit()
    except Exception as e: logger.error(f'保存交易记录失败: {e}')

async def save_trade_detail(detail):
    try:
        ts = detail.get('ts') or datetime.now(CST).strftime('%Y-%m-%d %H:%M:%S')
        async with aiosqlite.connect(DB_FILE, timeout=30.0) as db:
            await db.execute('''INSERT INTO trade_details
                (time,ts,symbol,side,price,amount,signal_score,fear_greed,funding_rate,pnl_pct,
                 real_cost,real_revenue,fee,fee_currency,order_id,net_pnl_pct)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (detail['time'],ts,detail['symbol'],detail['side'],detail['price'],detail['amount'],
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
    # 用 ISO 列 ts 做日期过滤；SQLite 的 strftime 无法解析旧格式 'MM-DD HH:MM'
    today=datetime.now(CST).strftime('%Y-%m-%d')
    try:
        async with aiosqlite.connect(DB_FILE, timeout=30.0) as db:
            async with db.execute("SELECT net_pnl_pct FROM trade_details WHERE side='sell' AND COALESCE(ts, time) LIKE ? ORDER BY id DESC",(f'{today}%',)) as cur: rows=await cur.fetchall()
        pnls=[float(r[0]) for r in rows if r[0] is not None]
        if not pnls: return None
        wins=[p for p in pnls if p>0]; losses=[p for p in pnls if p<0]
        return {'total':len(pnls),'wins':len(wins),'losses':len(losses),'win_rate':len(wins)/len(pnls),'avg_win_pct':sum(wins)/len(wins) if wins else 0,'avg_loss_pct':sum(losses)/len(losses) if losses else 0,'total_pnl_sum':sum(pnls),'pnls':pnls}
    except Exception as e:
        logger.error(f'获取今日交易失败: {e}'); return None

async def export_db_to_json():
    """
    导出全库为 JSON。

    早期版本漏了 runtime_state —— 而持仓账本（position_lots）和
    网格状态都存在这张表里。这意味着备份恢复后仓位依然是空的，
    等于备份了个寂寞。这里补上，并写入版本号以便恢复时校验。
    """
    try:
        async with aiosqlite.connect(DB_FILE, timeout=30.0) as db:
            db.row_factory=aiosqlite.Row
            async with db.execute('SELECT * FROM config') as cur: configs=[dict(r) async for r in cur]
            async with db.execute('SELECT * FROM trades') as cur: trades=[dict(r) async for r in cur]
            async with db.execute('SELECT * FROM trade_details') as cur: details=[dict(r) async for r in cur]
            async with db.execute('SELECT * FROM runtime_state') as cur: state=[dict(r) async for r in cur]
        return json.dumps({
            'version': 2,
            'exported_at': now_parts()[1],
            'config': configs, 'trades': trades,
            'details': details, 'runtime_state': state,
        }, indent=2)
    except Exception as e:
        logger.error(f'导出数据库失败: {e}'); return None


async def import_db_from_json(data):
    """
    从 JSON 备份恢复数据库。

    与 export 对应：恢复 config / trades / trade_details / runtime_state 四张表。
    兼容 v1 旧备份（无 runtime_state 字段），但会明确告警——
    那种备份恢复不出持仓，必须靠启动对账兜底。
    """
    try:
        payload = json.loads(data)
    except Exception as e:
        logger.error(f'备份文件解析失败: {e}')
        return False

    version = payload.get('version', 1)
    rows_state = payload.get('runtime_state')
    if version < 2 or rows_state is None:
        logger.warning('⚠️ 备份为旧格式(v1)，不含持仓账本(runtime_state)，'
                       '恢复后仓位可能为空，请用 /reconcile 核对')

    try:
        async with aiosqlite.connect(DB_FILE, timeout=30.0) as db:
            await db.execute('BEGIN IMMEDIATE')

            for rows, table, cols in (
                (payload.get('config'), 'config', ('key', 'value')),
                (payload.get('trades'), 'trades', None),
                (payload.get('details'), 'trade_details', None),
                (rows_state, 'runtime_state', ('key', 'value')),
            ):
                if not rows:
                    continue
                if cols:
                    # 键值表：按主键覆盖
                    k, v = cols
                    for r in rows:
                        await db.execute(
                            f'INSERT OR REPLACE INTO {table}({k},{v}) VALUES(?,?)',
                            (r.get(k), r.get(v)))
                else:
                    # 自增表：显式带 id 覆盖，避免重复插入
                    for r in rows:
                        keys = [x for x in r.keys() if r.get(x) is not None]
                        if not keys:
                            continue
                        ph = ','.join('?' * len(keys))
                        cols_sql = ','.join(keys)
                        await db.execute(
                            f'INSERT OR REPLACE INTO {table}({cols_sql}) VALUES({ph})',
                            tuple(r[x] for x in keys))

            await db.commit()
        return True
    except Exception as e:
        logger.error(f'导入数据库失败: {e}')
        return False

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
