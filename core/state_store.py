import json
import logging
import aiosqlite

logger = logging.getLogger(__name__)

DB_PATH = "state.db"


class StateStore:
    """SQLite 状态持久化，参考 CanJee/trading-bot 的持久化设计"""

    def __init__(self, db_path=DB_PATH):
        self.db_path = db_path
        self._conn = None

    async def init(self):
        self._conn = await aiosqlite.connect(self.db_path)
        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS strategy_state (
                inst_id TEXT PRIMARY KEY,
                position REAL DEFAULT 0,
                avg_buy_price REAL DEFAULT 0,
                peak_price REAL DEFAULT 0,
                total_profit REAL DEFAULT 0,
                total_fee REAL DEFAULT 0,
                trade_count INTEGER DEFAULT 0,
                last_action TEXT DEFAULT '',
                pending_buy TEXT DEFAULT '',
                pending_sell TEXT DEFAULT '',
                batch_tp TEXT DEFAULT '[]',
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS risk_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                initial_capital REAL DEFAULT 0,
                peak_capital REAL DEFAULT 0,
                current_capital REAL DEFAULT 0,
                daily_pnl REAL DEFAULT 0,
                today TEXT DEFAULT '',
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await self._conn.commit()
        logger.info(f"StateStore 已初始化: {self.db_path}")

    async def save_strategy(self, inst_id, data: dict):
        if not self._conn:
            return
        batch_tp = json.dumps(list(data.get("batch_tp_triggered", [])))
        await self._conn.execute("""
            INSERT OR REPLACE INTO strategy_state
            (inst_id, position, avg_buy_price, peak_price, total_profit,
             total_fee, trade_count, last_action, pending_buy, pending_sell,
             batch_tp, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """, (
            inst_id,
            data.get("position", 0),
            data.get("avg_buy_price", 0),
            data.get("peak_price", 0),
            data.get("total_profit", 0),
            data.get("total_fee", 0),
            data.get("trade_count", 0),
            json.dumps(data.get("last_action")) if data.get("last_action") else "",
            str(data.get("pending_buy", "") or ""),
            str(data.get("pending_sell", "") or ""),
            batch_tp,
        ))
        await self._conn.commit()

    async def load_strategy(self, inst_id: str) -> dict:
        if not self._conn:
            return {}
        async with self._conn.execute(
            "SELECT * FROM strategy_state WHERE inst_id = ?", (inst_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return {}
            cols = [d[0] for d in cursor.description]
            data = dict(zip(cols, row))
            try:
                if data.get("last_action"):
                    data["last_action"] = json.loads(data["last_action"])
                data["batch_tp_triggered"] = json.loads(data.get("batch_tp", "[]"))
            except (json.JSONDecodeError, TypeError):
                pass
            return data

    async def save_risk(self, rm_data: dict):
        if not self._conn:
            return
        await self._conn.execute("""
            INSERT OR REPLACE INTO risk_state
            (id, initial_capital, peak_capital, current_capital, daily_pnl, today, updated_at)
            VALUES (1, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """, (
            rm_data.get("initial_capital", 0),
            rm_data.get("peak_capital", 0),
            rm_data.get("current_capital", 0),
            rm_data.get("daily_pnl", 0),
            rm_data.get("today", ""),
        ))
        await self._conn.commit()

    async def load_risk(self) -> dict:
        if not self._conn:
            return {}
        async with self._conn.execute("SELECT * FROM risk_state WHERE id = 1") as cursor:
            row = await cursor.fetchone()
            if not row:
                return {}
            cols = [d[0] for d in cursor.description]
            return dict(zip(cols, row))

    async def close(self):
        if self._conn:
            await self._conn.close()
            self._conn = None