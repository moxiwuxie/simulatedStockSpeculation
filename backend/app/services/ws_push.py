"""WebSocket 实时推送"""
from __future__ import annotations
import asyncio
import json
import threading
from datetime import datetime
from typing import Set, Optional
import websockets


class MarketWebSocket:
    """WebSocket 实时行情推送"""

    def __init__(self, market_engine, host: str = "0.0.0.0", port: int = 8765):
        self._market = market_engine
        self._host = host
        self._port = port
        self._clients: Set = set()
        self._server: Optional[websockets.WebSocketServer] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._running = False
        self._thread: Optional[threading.Thread] = None

    async def _handler(self, ws):
        self._clients.add(ws)
        try:
            async for message in ws:
                # 客户端消息处理
                pass
        finally:
            self._clients.discard(ws)

    async def _broadcast_loop(self):
        while self._running:
            if self._clients:
                data = self._build_snapshot()
                dead_clients = set()
                for client in self._clients:
                    try:
                        await client.send(json.dumps(data, ensure_ascii=False, default=str))
                    except Exception:
                        dead_clients.add(client)
                self._clients -= dead_clients
            await asyncio.sleep(1)

    def _build_snapshot(self) -> dict:
        stocks = self._market.get_all_stocks()
        return {
            "type": "market",
            "time": datetime.now().strftime("%H:%M:%S"),
            "status": self._market.market_status(),
            "stocks": [
                {
                    "code": s.code,
                    "name": s.name,
                    "price": s.price,
                    "prev_close": s.prev_close,
                    "open": s.open_price,
                    "high": s.high,
                    "low": s.low,
                    "volume": s.volume,
                    "amount": s.amount,
                    "change_pct": s.change_pct(),
                    "limit_up": s.limit_up(),
                    "limit_down": s.limit_down(),
                }
                for s in stocks
            ],
        }

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)

    def _run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        async def _start():
            self._server = await websockets.serve(self._handler, self._host, self._port)
            await self._broadcast_loop()

        try:
            self._loop.run_until_complete(_start())
        except Exception:
            pass
