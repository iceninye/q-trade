"""TradingView webhook receiver (FastAPI).

TradingView's constraints shape this file: only ports 80/443, IPv4, the request is CANCELLED if we take longer than 3 seconds,
custom headers cannot be set (so the shared secret travels in the JSON body), and a missed delivery is not retried for you. So:

  - the handler only validates and enqueues, and answers at once; ONE worker thread owns the router and the broker (orders are
    serial: no two signals race through the risk guard, and the MT5 package is only ever called from one thread);
  - the signal id is the idempotency key (journal primary key): a duplicate never becomes a second order;
  - stale signals are dropped by the guard (Pine `timenow` vs. this machine's clock: keep the VPS clock synced);
  - /health reports the worker's last loop time and the guard state, for an external monitor that alerts when it goes quiet.
"""
from __future__ import annotations

import hmac
import json
import logging
import queue
import threading
import time
from contextlib import asynccontextmanager
from typing import Callable, Literal

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError

from .models import Side, Signal
from .router import Router

log = logging.getLogger("qtrade.webhook")


class TVAlert(BaseModel):
    secret: str
    id: str = Field(min_length=1, max_length=64)
    symbol: str = Field(min_length=3, max_length=20)
    side: Literal["buy", "sell"]
    sl_dist: float = Field(gt=0)
    tp_dist: float = Field(gt=0)
    fire_ms: int = Field(gt=0)  # Pine `timenow` when the alert fired
    strategy: str = Field(default="", max_length=32)
    price: float | None = Field(default=None, gt=0)  # chart close: used ONLY by the paper dry run (live uses the broker's quote)

    def signal(self) -> Signal:
        return Signal(self.id, self.symbol.upper(), Side(self.side), self.sl_dist, self.tp_dist, self.fire_ms, self.strategy)


Hook = Callable[[Router, TVAlert], None]


class Worker(threading.Thread):
    def __init__(self, make_router: Callable[[], Router], q: queue.Queue, sync_every_s: float = 1.0, before: Hook | None = None):
        super().__init__(name="qtrade-worker", daemon=True)
        self.make_router, self.q, self.sync_every_s, self.before = make_router, q, sync_every_s, before
        self.router: Router | None = None
        self.last_loop_ms = 0
        self.last_error = ""
        self.stopping = threading.Event()

    def run(self) -> None:
        try:
            self.router = self.make_router()  # created in this thread: every broker call happens here
        except Exception as e:  # e.g. MT5 not running: /health shows worker_alive false and this error
            self.last_error = f"start-up: {type(e).__name__}: {e}"
            log.exception("worker could not start")
            return
        next_sync = 0.0
        while not self.stopping.is_set():
            try:
                alert = self.q.get(timeout=self.sync_every_s)
            except queue.Empty:
                alert = None
            try:
                if alert is not None:
                    if self.before:
                        self.before(self.router, alert)
                    log.info("signal %s -> %s", alert.id, self.router.handle(alert.signal()))
                if time.monotonic() >= next_sync:
                    self.router.sync()
                    next_sync = time.monotonic() + self.sync_every_s
            except Exception as e:  # keep serving; the error is visible on /health and in the log
                self.last_error = f"{type(e).__name__}: {e}"
                log.exception("worker error")
            self.last_loop_ms = int(time.time() * 1000)


def create_app(make_router: Callable[[], Router], secret: str, max_queue: int = 100, before: Hook | None = None) -> FastAPI:
    """before(router, alert): runs in the worker before each signal (the paper dry run feeds the alert's price as a quote)."""
    if len(secret) < 16:
        raise ValueError("webhook secret must be at least 16 characters")
    q: queue.Queue[TVAlert] = queue.Queue(maxsize=max_queue)
    worker = Worker(make_router, q, before=before)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        worker.start()
        yield
        worker.stopping.set()

    app = FastAPI(title="q-trade webhook", lifespan=lifespan)
    app.state.worker = worker

    @app.post("/tv")
    async def tv(request: Request) -> dict:
        raw = await request.body()  # TradingView sends text/plain unless the message is valid JSON: parse it ourselves
        try:
            alert = TVAlert.model_validate(json.loads(raw))
        except (ValueError, ValidationError):
            raise HTTPException(422, "invalid alert payload") from None  # no echo of the body: it holds the secret
        if not hmac.compare_digest(alert.secret.encode(), secret.encode()):
            raise HTTPException(401, "bad secret")
        try:
            q.put_nowait(alert)
        except queue.Full:
            raise HTTPException(503, "queue full") from None
        return {"queued": alert.id}

    @app.get("/health")
    def health() -> dict:
        r = worker.router
        broker_health = getattr(r.broker, "health", None) if r else None
        return {
            "worker_alive": worker.is_alive(),
            "last_loop_ms": worker.last_loop_ms,
            "last_error": worker.last_error,
            "queue": q.qsize(),
            "guard": r.guard.status() if r else None,
            "journal": r.journal.counts() if r else None,
            "broker": broker_health() if broker_health else None,
        }

    return app
