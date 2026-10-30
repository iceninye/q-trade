"""Demo strategy: the load that exercises the plumbing, NOT an edge.

EMA(9/21) cross on closed 1-minute bars of the mid price, stop = 1.5 x ATR(14), target = 1.5 x stop. The same rule is in
pine/qscalp_alert.pine and mql5/Experts/QScalp.mq5, so the three paths can be compared trade for trade. fx-board found no edge in
seven public rules of this kind on 1h bars after costs; expect this one to lose about the cost per trade.
"""
from __future__ import annotations

from .models import Quote, Side, Signal


class EmaCrossAtr:
    def __init__(self, symbol: str, fast: int = 9, slow: int = 21, atr_len: int = 14, atr_mult: float = 1.5,
                 rr: float = 1.5, bar_ms: int = 60_000):
        self.symbol, self.bar_ms = symbol, bar_ms
        self.a_fast, self.a_slow = 2 / (fast + 1), 2 / (slow + 1)
        self.atr_len, self.atr_mult, self.rr = atr_len, atr_mult, rr
        self.warmup = 3 * slow
        self._bar = None  # [start_ms, open, high, low, close]
        self._prev_close = None
        self.fast = self.slow = self.atr = None
        self.bars = 0

    def on_quote(self, q: Quote) -> Signal | None:
        start = q.time_ms // self.bar_ms * self.bar_ms
        px = q.mid
        sig = None
        if self._bar is not None and start != self._bar[0]:
            sig = self._close_bar(q.time_ms)  # first tick of a new bar = the bar before it is final (no look-ahead)
        if self._bar is None or start != self._bar[0]:
            self._bar = [start, px, px, px, px]
        else:
            b = self._bar
            b[2], b[3], b[4] = max(b[2], px), min(b[3], px), px
        return sig

    def _close_bar(self, now_ms: int) -> Signal | None:
        start, _o, h, low, c = self._bar
        pc = self._prev_close
        tr = h - low if pc is None else max(h - low, abs(h - pc), abs(low - pc))
        self._prev_close = c
        self.bars += 1
        if self.fast is None:
            self.fast = self.slow = c
            self.atr = tr
            return None
        pf, ps = self.fast, self.slow
        self.fast += self.a_fast * (c - self.fast)
        self.slow += self.a_slow * (c - self.slow)
        self.atr += (tr - self.atr) / self.atr_len  # Wilder's smoothing, as ta.atr / iATR
        if self.bars < self.warmup or not self.atr > 0:
            return None
        side = Side.BUY if pf <= ps and self.fast > self.slow else Side.SELL if pf >= ps and self.fast < self.slow else None
        if side is None:
            return None
        d = self.atr_mult * self.atr
        return Signal(f"{self.symbol}-{start}-{side.value}", self.symbol, side, d, self.rr * d, now_ms, "ema_cross_atr")
