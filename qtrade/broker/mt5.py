"""MetaTrader 5 through the official `MetaTrader5` package.

Windows only: the package talks to a RUNNING MT5 terminal on the same machine over IPC (no network API, no callbacks: poll).
Enable "Algo Trading" in the terminal, or every order returns 10027. Call this object from ONE thread only (the webhook's
worker owns it): the package's thread safety is not documented.

Time stamps: MT5 reports tick and deal times in the broker's SERVER time zone written as if it were UTC (typically UTC+2/+3).
They are shifted back to real UTC here, either with `server_utc_offset_h` from the config or, when that is not given, with an
offset estimated from the first fresh tick (rounded to 30 minutes). The guard's staleness checks are wrong without it.
"""
from __future__ import annotations

import time

from ..models import Account, Closed, OrderIntent, OrderResult, Position, Quote, Side, SymbolSpec

FILLING_FOK, FILLING_IOC = 1, 2  # SYMBOL_FILLING_* bit flags in symbol_info().filling_mode


class MT5Broker:
    def __init__(self, *, login: int = 0, password: str = "", server: str = "", path: str = "", magic: int = 20261030,
                 symbol_map: dict[str, str] | None = None, commission_per_lot_rt: dict[str, float] | None = None,
                 server_utc_offset_h: float | None = None, timeout_ms: int = 10_000):
        import MetaTrader5 as mt5  # imported here so that everything else runs on any OS

        self.mt5 = mt5
        kw = {k: v for k, v in {"login": login, "password": password, "server": server, "timeout": timeout_ms}.items() if v}
        if not (mt5.initialize(path, **kw) if path else mt5.initialize(**kw)):
            raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")
        term = mt5.terminal_info()
        if term is None or not term.trade_allowed:
            raise RuntimeError("MT5 'Algo Trading' is off in the terminal: orders would fail with retcode 10027")
        self.magic = magic
        self.to_broker = dict(symbol_map or {})  # "EURUSD" -> "EURUSDc" (cent) / "EURUSD.r" (raw) ...
        self.from_broker = {v: k for k, v in self.to_broker.items()}
        self.commission = dict(commission_per_lot_rt or {})
        self._offset_ms = None if server_utc_offset_h is None else int(server_utc_offset_h * 3_600_000)
        self._retry = {mt5.TRADE_RETCODE_REQUOTE, mt5.TRADE_RETCODE_PRICE_CHANGED, mt5.TRADE_RETCODE_PRICE_OFF}
        self._done = {mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_DONE_PARTIAL}

    # -- helpers --------------------------------------------------------------------------------------------------------
    def _name(self, symbol: str) -> str:
        return self.to_broker.get(symbol, symbol)

    def _utc(self, server_ms: int) -> int:
        if self._offset_ms is not None:
            return server_ms - self._offset_ms
        raw = server_ms - int(time.time() * 1000)
        est = round(raw / 1_800_000) * 1_800_000
        if abs(raw - est) < 60_000:  # a fresh tick: the difference IS the zone offset, keep it
            self._offset_ms = est
        return server_ms - est

    def _info(self, name: str):
        info = self.mt5.symbol_info(name)
        if info is None or not info.visible:
            self.mt5.symbol_select(name, True)  # symbols hidden in Market Watch have no ticks
            info = self.mt5.symbol_info(name)
        if info is None:
            raise KeyError(f"unknown symbol {name}")
        return info

    def _filling(self, info) -> int:
        if info.filling_mode & FILLING_FOK:
            return self.mt5.ORDER_FILLING_FOK
        if info.filling_mode & FILLING_IOC:
            return self.mt5.ORDER_FILLING_IOC
        return self.mt5.ORDER_FILLING_RETURN

    def _send(self, req: dict) -> OrderResult:
        res = self.mt5.order_send(req)
        if res is None:
            return OrderResult(False, code=f"order_send None {self.mt5.last_error()}")
        ok = res.retcode in self._done
        return OrderResult(ok, res.price, res.volume, res.order, str(res.retcode), int(time.time() * 1000))

    # -- Broker interface -----------------------------------------------------------------------------------------------
    def quote(self, symbol: str) -> Quote | None:
        t = self.mt5.symbol_info_tick(self._name(symbol))
        return None if t is None or not t.bid else Quote(symbol, t.bid, t.ask, self._utc(t.time_msc))

    def spec(self, symbol: str) -> SymbolSpec:
        i = self._info(self._name(symbol))  # re-read every time: tick_value of a cross pair moves with the rate
        return SymbolSpec(symbol, i.point, i.trade_tick_size, i.trade_tick_value, i.volume_min, i.volume_step, i.volume_max,
                          i.trade_stops_level, self.commission.get(symbol, 0.0))

    def account(self) -> Account:
        a = self.mt5.account_info()
        return Account(a.equity, a.balance, a.currency)

    def positions(self) -> list[Position]:
        out = []
        for p in self.mt5.positions_get() or ():
            if p.magic != self.magic:
                continue  # manual trades and other EAs are not ours
            side = Side.BUY if p.type == self.mt5.POSITION_TYPE_BUY else Side.SELL
            out.append(Position(p.ticket, self.from_broker.get(p.symbol, p.symbol), side, p.volume, p.price_open, p.sl, p.tp,
                                self._utc(p.time_msc)))
        return out

    def place(self, intent: OrderIntent) -> OrderResult:
        name, mt5 = self._name(intent.symbol), self.mt5
        info = self._info(name)
        sl_dist, tp_dist = abs(intent.price - intent.sl), abs(intent.tp - intent.price)
        res = OrderResult(False, code="NOT_SENT")
        for _ in range(2):  # one retry, on requote / price changed only
            tick = mt5.symbol_info_tick(name)
            price = tick.ask if intent.side is Side.BUY else tick.bid
            if abs(price - intent.price) > intent.max_slippage:
                return OrderResult(False, code="PRICE_MOVED")
            s = intent.side.sign  # keep the distances the guard approved, re-anchored on the price sent
            req = {
                "action": mt5.TRADE_ACTION_DEAL, "symbol": name, "volume": float(intent.volume),
                "type": mt5.ORDER_TYPE_BUY if intent.side is Side.BUY else mt5.ORDER_TYPE_SELL, "price": price,
                "sl": round(price - s * sl_dist, info.digits), "tp": round(price + s * tp_dist, info.digits),
                "deviation": max(0, int(round(intent.max_slippage / info.point))), "magic": self.magic,
                "comment": intent.signal_id[-31:], "type_time": mt5.ORDER_TIME_GTC, "type_filling": self._filling(info),
            }
            res = self._send(req)
            if res.ok or int(res.code or 0) not in self._retry:
                return res
        return res

    def close(self, ticket: int) -> OrderResult:
        mt5 = self.mt5
        ps = mt5.positions_get(ticket=ticket)
        if not ps:
            return OrderResult(False, code="NOT_FOUND")
        p = ps[0]
        info, tick = self._info(p.symbol), mt5.symbol_info_tick(p.symbol)
        buy = p.type == mt5.POSITION_TYPE_BUY
        return self._send({
            "action": mt5.TRADE_ACTION_DEAL, "symbol": p.symbol, "volume": p.volume, "position": ticket,
            "type": mt5.ORDER_TYPE_SELL if buy else mt5.ORDER_TYPE_BUY, "price": tick.bid if buy else tick.ask,
            "deviation": 50, "magic": self.magic, "comment": "qtrade close", "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": self._filling(info),
        })

    def closed(self, ticket: int) -> Closed | None:
        mt5 = self.mt5
        deals = mt5.history_deals_get(position=ticket) or ()
        outs = [d for d in deals if d.entry in (mt5.DEAL_ENTRY_OUT, mt5.DEAL_ENTRY_OUT_BY, mt5.DEAL_ENTRY_INOUT)]
        if not outs:
            return None  # still open, or the history has not synced yet: the router asks again on the next sync
        pnl = sum(d.profit + d.commission + d.swap + getattr(d, "fee", 0.0) for d in deals)
        last = max(outs, key=lambda d: d.time_msc)
        return Closed(ticket, self._utc(last.time_msc), pnl, last.price)

    def health(self) -> dict:
        t = self.mt5.terminal_info()
        return {"connected": bool(t and t.connected), "trade_allowed": bool(t and t.trade_allowed), "ping_ms": (t.ping_last / 1000) if t else None}
