import json
import logging
import os
from typing import Optional

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy import text

logger = logging.getLogger(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL", "")


class StateStore:
    """
    使用 Neon PostgreSQL 作为持久化存储。
    通过 SQLAlchemy + asyncpg 异步连接，适合 Render 部署环境。
    """

    def __init__(self, db_url: str = DATABASE_URL):
        self.db_url = db_url
        self.engine = None
        self.session_factory = None
        self._cache: dict = {}

    async def init(self):
        if not self.db_url:
            logger.warning("DATABASE_URL 未配置，状态持久化已禁用")
            return

        self.engine = create_async_engine(
            self.db_url,
            echo=False,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=10,
            pool_recycle=300,
            connect_args={"ssl": "require"},
        )
        self.session_factory = async_sessionmaker(
            self.engine, class_=AsyncSession, expire_on_commit=False
        )

        async with self.engine.begin() as conn:
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS strategy_state (
                    inst_id TEXT PRIMARY KEY,
                    position DOUBLE PRECISION DEFAULT 0,
                    avg_buy_price DOUBLE PRECISION DEFAULT 0,
                    peak_price DOUBLE PRECISION DEFAULT 0,
                    total_profit DOUBLE PRECISION DEFAULT 0,
                    total_fee DOUBLE PRECISION DEFAULT 0,
                    trade_count INTEGER DEFAULT 0,
                    last_action TEXT DEFAULT '',
                    pending_buy TEXT DEFAULT '',
                    pending_sell TEXT DEFAULT '',
                    batch_tp TEXT DEFAULT '[]',
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """))
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS risk_state (
                    id INTEGER PRIMARY KEY,
                    initial_capital DOUBLE PRECISION DEFAULT 0,
                    peak_capital DOUBLE PRECISION DEFAULT 0,
                    current_capital DOUBLE PRECISION DEFAULT 0,
                    daily_pnl DOUBLE PRECISION DEFAULT 0,
                    today TEXT DEFAULT '',
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """))
        logger.info("StateStore 已初始化 (Neon PostgreSQL)")

        await self._load_cache()

    async def _load_cache(self):
        """从数据库预加载所有状态到内存缓存"""
        if not self.session_factory:
            return
        async with self.session_factory() as session:
            result = await session.execute(text("SELECT * FROM strategy_state"))
            for row in result.mappings():
                inst_id = row["inst_id"]
                self._cache[f"strategy_{inst_id}"] = {
                    "position": row["position"],
                    "avg_buy_price": row["avg_buy_price"],
                    "peak_price": row["peak_price"],
                    "total_profit": row["total_profit"],
                    "total_fee": row["total_fee"],
                    "trade_count": row["trade_count"],
                    "last_action": row["last_action"],
                    "pending_buy": row["pending_buy"],
                    "pending_sell": row["pending_sell"],
                    "batch_tp": row["batch_tp"],
                }

            result = await session.execute(text("SELECT * FROM risk_state WHERE id = 1"))
            row = result.mappings().first()
            if row:
                self._cache["risk"] = {
                    "initial_capital": row["initial_capital"],
                    "peak_capital": row["peak_capital"],
                    "current_capital": row["current_capital"],
                    "daily_pnl": row["daily_pnl"],
                    "today": row["today"],
                }
        logger.info(f"StateStore 缓存已加载: {len(self._cache)} 项")

    async def load_strategy(self, inst_id: str) -> dict:
        cached = self._cache.get(f"strategy_{inst_id}")
        if cached:
            data = dict(cached)
            try:
                data["batch_tp_triggered"] = json.loads(data.get("batch_tp", "[]"))
            except json.JSONDecodeError:
                data["batch_tp_triggered"] = []
            return data
        return {}

    async def save_strategy(self, inst_id: str, data: dict):
        if not self.session_factory:
            return
        self._cache[f"strategy_{inst_id}"] = data
        batch_tp = json.dumps(data.get("batch_tp_triggered", []))
        try:
            async with self.session_factory() as session:
                await session.execute(text("""
                    INSERT INTO strategy_state
                    (inst_id, position, avg_buy_price, peak_price, total_profit,
                     total_fee, trade_count, last_action, pending_buy, pending_sell,
                     batch_tp, updated_at)
                    VALUES (:inst_id, :position, :avg_buy_price, :peak_price,
                            :total_profit, :total_fee, :trade_count, :last_action,
                            :pending_buy, :pending_sell, :batch_tp, CURRENT_TIMESTAMP)
                    ON CONFLICT (inst_id) DO UPDATE SET
                        position = EXCLUDED.position,
                        avg_buy_price = EXCLUDED.avg_buy_price,
                        peak_price = EXCLUDED.peak_price,
                        total_profit = EXCLUDED.total_profit,
                        total_fee = EXCLUDED.total_fee,
                        trade_count = EXCLUDED.trade_count,
                        last_action = EXCLUDED.last_action,
                        pending_buy = EXCLUDED.pending_buy,
                        pending_sell = EXCLUDED.pending_sell,
                        batch_tp = EXCLUDED.batch_tp,
                        updated_at = CURRENT_TIMESTAMP
                """), {
                    "inst_id": inst_id,
                    "position": data.get("position", 0.0),
                    "avg_buy_price": data.get("avg_buy_price", 0.0),
                    "peak_price": data.get("peak_price", 0.0),
                    "total_profit": data.get("total_profit", 0.0),
                    "total_fee": data.get("total_fee", 0.0),
                    "trade_count": data.get("trade_count", 0),
                    "last_action": json.dumps(data.get("last_action")) if data.get("last_action") else "",
                    "pending_buy": str(data.get("pending_buy", "") or ""),
                    "pending_sell": str(data.get("pending_sell", "") or ""),
                    "batch_tp": batch_tp,
                })
                await session.commit()
        except Exception as e:
            logger.error(f"保存 {inst_id} 到 Neon 失败: {e}")

    async def load_risk(self) -> dict:
        return self._cache.get("risk", {})

    async def save_risk(self, data: dict):
        if not self.session_factory:
            return
        self._cache["risk"] = data
        try:
            async with self.session_factory() as session:
                await session.execute(text("""
                    INSERT INTO risk_state
                    (id, initial_capital, peak_capital, current_capital,
                     daily_pnl, today, updated_at)
                    VALUES (1, :initial_capital, :peak_capital, :current_capital,
                            :daily_pnl, :today, CURRENT_TIMESTAMP)
                    ON CONFLICT (id) DO UPDATE SET
                        initial_capital = EXCLUDED.initial_capital,
                        peak_capital = EXCLUDED.peak_capital,
                        current_capital = EXCLUDED.current_capital,
                        daily_pnl = EXCLUDED.daily_pnl,
                        today = EXCLUDED.today,
                        updated_at = CURRENT_TIMESTAMP
                """), {
                    "initial_capital": data.get("initial_capital", 0.0),
                    "peak_capital": data.get("peak_capital", 0.0),
                    "current_capital": data.get("current_capital", 0.0),
                    "daily_pnl": data.get("daily_pnl", 0.0),
                    "today": data.get("today", ""),
                })
                await session.commit()
        except Exception as e:
            logger.error(f"保存风控到 Neon 失败: {e}")

    async def save_all(self, manager):
        """保存所有策略和风控状态"""
        for inst_id, dip in manager.dips.items():
            await self.save_strategy(inst_id, dip.snapshot())

        rm = manager.risk_manager
        await self.save_risk({
            "initial_capital": rm.initial_capital,
            "peak_capital": rm.peak_capital,
            "current_capital": rm.current_capital,
            "daily_pnl": rm.daily_pnl,
            "today": str(rm.today),
        })
        logger.info("所有状态已同步到 Neon")

    async def close(self):
        if self.engine:
            await self.engine.dispose()
        logger.info("StateStore 已关闭")