import asyncio
import logging
from collections import defaultdict
from typing import Callable, Any

logger = logging.getLogger(__name__)


class EventBus:
    """
    轻量级异步事件总线。
    参考 AIOQuant 的异步事件驱动架构：
    行情、策略、风控、执行通过事件解耦，实现毫秒级响应。
    """

    def __init__(self):
        self._subscribers: dict = defaultdict(list)
        self._queue: asyncio.Queue = asyncio.Queue()
        self._running = False

    def subscribe(self, event_type: type, handler: Callable):
        """订阅事件"""
        self._subscribers[event_type].append(handler)

    def unsubscribe(self, event_type: type, handler: Callable):
        if handler in self._subscribers[event_type]:
            self._subscribers[event_type].remove(handler)

    async def publish(self, event: Any):
        """发布事件（非阻塞）"""
        await self._queue.put(event)

    async def start(self):
        """启动事件循环"""
        self._running = True
        logger.info("事件总线已启动")
        while self._running:
            try:
                event = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except Exception:
                continue

            event_type = type(event)
            handlers = self._subscribers.get(event_type, [])
            for handler in handlers:
                try:
                    if asyncio.iscoroutinefunction(handler):
                        asyncio.create_task(handler(event))
                    else:
                        handler(event)
                except Exception as e:
                    logger.error(f"事件处理失败 {event_type.__name__}: {e}")

    async def stop(self):
        self._running = False


# 全局事件总线实例
bus = EventBus()