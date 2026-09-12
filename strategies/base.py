# strategies/base.py
from abc import ABC, abstractmethod

class BaseStrategy(ABC):
    def __init__(self, inst_id: str, params: dict):
        self.inst_id = inst_id
        self.params = params
        self.running = False

    @abstractmethod
    async def on_ticker(self, price: float, raw: dict):
        ...

    @abstractmethod
    async def start(self):
        ...

    @abstractmethod
    async def stop(self):
        ...

    def snapshot(self) -> dict:
        return {"inst_id": self.inst_id, "running": self.running, "params": self.params}