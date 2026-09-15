import logging
import json
from aiohttp import web, WSMsgType

logger = logging.getLogger(__name__)


class Dashboard:
    """Web仪表盘：提供HTTP API和WebSocket实时推送"""

    def __init__(self, port=8080):
        self.port = port
        self.app = web.Application()
        self.app.router.add_get("/", self.index)
        self.app.router.add_get("/api/status", self.api_status)
        self.app.router.add_get("/api/positions", self.api_positions)
        self.app.router.add_get("/api/signals", self.api_signals)
        self.app.router.add_get("/api/risk", self.api_risk)
        self.app.router.add_get("/ws", self.websocket_handler)
        self._ws_clients = set()
        self._manager = None
        self._risk_manager = None

    def set_manager(self, manager, risk_manager=None):
        self._manager = manager
        self._risk_manager = risk_manager

    async def index(self, request):
        return web.FileResponse("web/static/index.html")

    async def api_status(self, request):
        if not self._manager:
            return web.json_response({"error": "not initialized"})
        data = []
        for iid in self._manager.all_inst_ids():
            d = self._manager.dips.get(iid)
            data.append({
                "inst_id": iid,
                "dip_running": bool(d and d.running),
                "position": d.position if d else 0,
                "avg_buy_price": d.avg_buy_price if d else 0,
                "total_profit": d.total_profit if d else 0,
                "total_fee": d.total_fee if d else 0,
            })
        return web.json_response(data)

    async def api_positions(self, request):
        if not self._manager:
            return web.json_response([])
        data = []
        for iid in self._manager.all_inst_ids():
            d = self._manager.dips.get(iid)
            if d and (d.position > 0 or d._pending_buy_ord_id or d._pending_sell_ord_id):
                data.append({
                    "inst_id": iid,
                    "position": d.position,
                    "avg_buy_price": d.avg_buy_price,
                    "peak_price": d.peak_price,
                    "pending_buy": d._pending_buy_ord_id,
                    "pending_sell": d._pending_sell_ord_id,
                })
        return web.json_response(data)

    async def api_signals(self, request):
        if not self._manager:
            return web.json_response([])
        data = []
        for iid in self._manager.all_inst_ids():
            d = self._manager.dips.get(iid)
            if d:
                try:
                    status = await d.get_signal_status()
                    data.append(status)
                except Exception:
                    pass
        return web.json_response(data)

    async def api_risk(self, request):
        if not self._risk_manager:
            return web.json_response({"error": "not initialized"})
        rm = self._risk_manager
        return web.json_response({
            "current_capital": rm.current_capital,
            "peak_capital": rm.peak_capital,
            "daily_pnl": rm.daily_pnl,
            "max_drawdown": rm.max_drawdown,
            "daily_loss_limit": rm.daily_loss_limit,
            "risk_triggered": rm.is_risk_triggered(),
        })

    async def websocket_handler(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self._ws_clients.add(ws)
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    pass
        finally:
            self._ws_clients.discard(ws)
        return ws

    async def broadcast(self, data: dict):
        if not self._ws_clients:
            return
        text = json.dumps(data, ensure_ascii=False)
        for ws in list(self._ws_clients):
            try:
                await ws.send_str(text)
            except Exception:
                self._ws_clients.discard(ws)

    async def start(self):
        runner = web.AppRunner(self.app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", self.port)
        await site.start()
        logger.info(f"Web仪表盘已启动: http://0.0.0.0:{self.port}")
        return runner