# state.py
"""
纯内存状态存储。接口与原 GitHub 版本保持一致，但不再依赖 GH_TOKEN / GH_REPO。
由于 main.py 启动时优先从 OKX API 恢复状态，此模块仅作为运行时内存缓存，
Render 重启后数据会丢失，届时由 restore_from_okx() 重新拉取。
"""

import logging

logger = logging.getLogger(__name__)


class StateStore:
    def __init__(self):
        self._cache: dict = {}

    async def load(self) -> dict:
        """返回内存中的缓存（重启后为空）"""
        return self._cache

    async def save(self, data: dict):
        """写入内存缓存，不再提交到 GitHub"""
        self._cache = data
        logger.debug("状态已更新到内存缓存")