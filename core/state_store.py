import json
import base64
import logging
import os
from typing import Optional
import httpx

logger = logging.getLogger(__name__)

GIST_ID = os.environ.get("GIST_ID", "")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GIST_FILENAME = "state.json"

API_BASE = "https://api.github.com"


class StateStore:
    """
    使用 GitHub Gist 作为持久化存储。
    通过 httpx 直接调用 GitHub Gist API，不依赖第三方库。
    """

    def __init__(self, gist_id: str = GIST_ID, token: str = GITHUB_TOKEN,
                 filename: str = GIST_FILENAME):
        self.gist_id = gist_id
        self.token = token
        self.filename = filename
        self.enabled = bool(gist_id and token)
        self._cache: dict = {}
        self._client: Optional[httpx.AsyncClient] = None
        self._etag: Optional[str] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            headers = {
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "X-GitHub-Api-Version": "2022-11-28",
            }
            self._client = httpx.AsyncClient(headers=headers, timeout=15.0)
        return self._client

    async def init(self):
        """从 Gist 加载初始状态"""
        if not self.enabled:
            logger.warning("GIST_ID 或 GITHUB_TOKEN 未配置，状态持久化已禁用")
            return

        try:
            client = await self._get_client()
            r = await client.get(f"{API_BASE}/gists/{self.gist_id}")
            if r.status_code == 200:
                data = r.json()
                files = data.get("files", {})
                if self.filename in files:
                    content = files[self.filename].get("content", "{}")
                    try:
                        self._cache = json.loads(content) if content else {}
                        logger.info(f"StateStore 已从 Gist 加载 {len(self._cache)} 项")
                    except json.JSONDecodeError:
                        logger.warning("Gist 中的 state.json 不是有效 JSON，使用空状态")
                        self._cache = {}
                else:
                    logger.info("Gist 中暂无 state.json，使用空状态")
                    self._cache = {}
            else:
                logger.error(f"读取 Gist 失败: {r.status_code} {r.text[:200]}")
        except Exception as e:
            logger.error(f"初始化 StateStore 失败: {e}")

    async def _sync_to_gist(self):
        """将内存缓存同步到 Gist"""
        if not self.enabled:
            return
        try:
            client = await self._get_client()
            content = json.dumps(self._cache, ensure_ascii=False, indent=2)
            payload = {
                "files": {
                    self.filename: {
                        "content": content
                    }
                }
            }
            r = await client.patch(f"{API_BASE}/gists/{self.gist_id}", json=payload)
            if r.status_code != 200:
                logger.error(f"同步 Gist 失败: {r.status_code} {r.text[:200]}")
        except Exception as e:
            logger.error(f"同步 Gist 异常: {e}")

    async def load_strategy(self, inst_id: str) -> dict:
        return self._cache.get(f"strategy_{inst_id}", {})

    async def save_strategy(self, inst_id: str, data: dict):
        self._cache[f"strategy_{inst_id}"] = data
        await self._sync_to_gist()

    async def load_risk(self) -> dict:
        return self._cache.get("risk", {})

    async def save_risk(self, data: dict):
        self._cache["risk"] = data
        await self._sync_to_gist()

    async def save_all(self, manager):
        """保存所有策略和风控状态"""
        for inst_id, dip in manager.dips.items():
            self._cache[f"strategy_{inst_id}"] = dip.snapshot()

        rm = manager.risk_manager
        self._cache["risk"] = {
            "initial_capital": rm.initial_capital,
            "peak_capital": rm.peak_capital,
            "current_capital": rm.current_capital,
            "daily_pnl": rm.daily_pnl,
            "today": str(rm.today),
        }
        await self._sync_to_gist()
        logger.info("所有状态已同步到 Gist")

    async def close(self):
        try:
            await self._sync_to_gist()
        finally:
            if self._client and not self._client.is_closed:
                await self._client.aclose()
                self._client = None
        logger.info("StateStore 已关闭")