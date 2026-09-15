import json
import logging
from typing import Optional
from gistflow import GistFlow

logger = logging.getLogger(__name__)

# GitHub Gist 配置
GIST_ID = "YOUR_GIST_ID"  # 替换为你创建的 Secret Gist ID
GIST_TOKEN = "YOUR_GITHUB_TOKEN"  # 替换为有 gist 权限的 Personal Access Token
GIST_FILENAME = "state.json"


class StateStore:
    """
    GitHub Gist 状态持久化。
    使用 gistflow 库同步 JSON 状态，实现跨部署持久化。
    """

    def __init__(self, gist_id: str = GIST_ID, token: str = GIST_TOKEN,
                 filename: str = GIST_FILENAME):
        self.gist_id = gist_id
        self.filename = filename
        self.flow = GistFlow(token=token, gist_id=gist_id, filename=filename)
        self._cache = {}  # 内存缓存，减少 API 调用

    async def init(self):
        """从 Gist 加载初始状态"""
        try:
            data = await self.flow.load()
            if data:
                self._cache = data
                logger.info(f"StateStore 已从 Gist 加载状态: {list(data.keys())}")
            else:
                logger.info("StateStore 初始化完成，Gist 中暂无数据")
        except Exception as e:
            logger.error(f"从 Gist 加载状态失败: {e}")

    async def load_strategy(self, inst_id: str) -> dict:
        """加载指定币种的策略状态"""
        return self._cache.get(f"strategy_{inst_id}", {})

    async def save_strategy(self, inst_id: str, data: dict):
        """保存指定币种的策略状态到 Gist"""
        self._cache[f"strategy_{inst_id}"] = data
        await self._sync_to_gist()

    async def load_risk(self) -> dict:
        """加载风控状态"""
        return self._cache.get("risk", {})

    async def save_risk(self, data: dict):
        """保存风控状态到 Gist"""
        self._cache["risk"] = data
        await self._sync_to_gist()

    async def save_all(self, manager):
        """保存所有策略和风控状态（供 main.py 调用）"""
        # 保存所有币种策略
        for inst_id, dip in manager.dips.items():
            self._cache[f"strategy_{inst_id}"] = dip.snapshot()
        # 保存风控状态
        rm = manager.risk_manager
        self._cache["risk"] = {
            "initial_capital": rm.initial_capital,
            "peak_capital": rm.peak_capital,
            "current_capital": rm.current_capital,
            "daily_pnl": rm.daily_pnl,
            "today": str(rm.today),
        }
        await self._sync_to_gist()
        logger.info("所有状态已保存到 Gist")

    async def _sync_to_gist(self):
        """将内存缓存同步到 Gist"""
        try:
            await self.flow.save(self._cache)
        except Exception as e:
            logger.error(f"同步状态到 Gist 失败: {e}")

    async def close(self):
        """关闭前最后一次同步"""
        await self._sync_to_gist()
        logger.info("StateStore 已关闭")