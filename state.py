# state.py
import json, base64, asyncio
import httpx
from config import GH_TOKEN, GH_REPO, GH_STATE_BRANCH, GH_STATE_PATH

API = "https://api.github.com"
HEADERS = {"Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github+json"}

class StateStore:
    def __init__(self):
        self._sha = None

    async def _get_sha(self):
        url = f"{API}/repos/{GH_REPO}/contents/{GH_STATE_PATH}?ref={GH_STATE_BRANCH}"
        async with httpx.AsyncClient() as c:
            r = await c.get(url, headers=HEADERS)
            if r.status_code == 200:
                self._sha = r.json()["sha"]
            return self._sha

    async def load(self) -> dict:
        url = f"{API}/repos/{GH_REPO}/contents/{GH_STATE_PATH}?ref={GH_STATE_BRANCH}"
        async with httpx.AsyncClient() as c:
            r = await c.get(url, headers=HEADERS)
            if r.status_code != 200:
                return {}
            content = base64.b64decode(r.json()["content"]).decode()
            return json.loads(content)

    async def save(self, data: dict):
        await self._get_sha()
        url = f"{API}/repos/{GH_REPO}/contents/{GH_STATE_PATH}"
        payload = {
            "message": "state update",
            "content": base64.b64encode(json.dumps(data, ensure_ascii=False).encode()).decode(),
            "branch": GH_STATE_BRANCH,
        }
        if self._sha:
            payload["sha"] = self._sha
        async with httpx.AsyncClient() as c:
            r = await c.put(url, headers=HEADERS, json=payload)
            if r.status_code in (200, 201):
                self._sha = r.json()["content"]["sha"]
            return r.status_code