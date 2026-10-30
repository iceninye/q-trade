"""Risk guard: every entry passes `RiskGuard.check_entry`; nothing reaches a broker without an OK.

Time is always passed in (never read from the wall clock), so the SAME object runs in the tick back-test, against the paper broker
and live. The state that must survive a restart (day-start equity, trade count, losing streak, halts) is saved after every change:
a guard that forgets today's losses when the process restarts is the classic way a daily stop fails.

Money here is USD; the router converts from the account currency (a cent account reports USC: 100 per USD).
"""
from __future__ import annotations

import bisect
import csv
import json
import os
import statistics
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .models import Quote, Signal, SymbolSpec

# Reason codes (all failing checks are reported, not only the first: the journal shows WHY trades were skipped).
KILL_SWITCH = "KILL_SWITCH"  # the kill file exists
KILLED = "KILLED"  # max drawdown hit: sticky until a human clears `killed` in the state file
DAILY_HALT = "DAILY_HALT"  # today's loss reached the daily limit: no entries until the next trading day
DAILY_LOSS = "DAILY_LOSS"  # this trade could push today's worst-case loss past the limit
MAX_TRADES_DAY = "MAX_TRADES_DAY"
MIN_INTERVAL = "MIN_INTERVAL"
RATE_WINDOW = "RATE_WINDOW"
COOLDOWN = "COOLDOWN"  # after a losing streak
MAX_POSITIONS = "MAX_POSITIONS"
STALE_QUOTE = "STALE_QUOTE"
STALE_SIGNAL = "STALE_SIGNAL"
SPREAD_ABS = "SPREAD_ABS"
SPREAD_TO_STOP = "SPREAD_TO_STOP"
SPREAD_SPIKE = "SPREAD_SPIKE"
STOPS_LEVEL = "STOPS_LEVEL"
OFF_SESSION = "OFF_SESSION"
ROLLOVER = "ROLLOVER"
NEWS = "NEWS"

NY = ZoneInfo("America/New_York")


@dataclass
class RiskConfig:
    """Defaults are for a ~USD 100 account risking USD 1 (1 %) per trade. Fix them BEFORE testing, not after."""

    risk_usd: float = 1.0
    daily_max_loss_usd: float = 4.0  # 4 R: worst case = realised + floating + open risk + this trade's risk
    max_drawdown_usd: float = 20.0  # from peak equity -> KILLED (0 = off)
    max_trades_per_day: int = 20
    min_interval_s: float = 60.0
    window_s: float = 3600.0
    max_entries_per_window: int = 6
    max_consecutive_losses: int = 3
    cooldown_min: float = 60.0
    max_open_positions: int = 1
    max_spread_points: dict[str, float] = field(default_factory=dict)  # per symbol, broker points; missing = no absolute cap
    max_spread_to_stop: float = 0.2  # spread / stop distance
    spread_spike_mult: float = 2.0  # spread vs. the rolling median of recent ticks
    spread_window: int = 300  # ticks in the rolling median
    spread_min_samples: int = 50
    max_quote_age_ms: int = 3000
    max_signal_age_ms: int = 5000
    sessions_utc: tuple[str, ...] = ("07:00-16:30",)  # empty = any time
    rollover_ny: tuple[str, str] = ("16:55", "18:05")  # New York 17:00 roll: spreads blow out, swaps are charged
    news_before_min: float = 5.0
    news_after_min: float = 10.0
    news_impacts: tuple[str, ...] = ("high",)
    day_tz: str = "America/New_York"  # trading day = FX day, rolling at 17:00 New York (most brokers' server midnight)
    day_roll_hour: int = 17
    kill_file: str | None = "KILL"  # create this file to stop all entries at once


@dataclass
class RiskState:
    day: str = ""
    day_start_equity: float = 0.0
    peak_equity: float = 0.0
    trades_today: int = 0
    consecutive_losses: int = 0
    cooldown_until_ms: int = 0
    halted_day: str = ""
    killed: str = ""
    entry_times_ms: list[int] = field(default_factory=list)


class StateStore:
    """JSON file, replaced atomically (a crash mid-write must not wipe the daily loss)."""

    def __init__(self, path: str):
        self.path = path

    def load(self) -> RiskState:
        try:
            with open(self.path, encoding="utf-8") as f:
                return RiskState(**json.load(f))
        except FileNotFoundError:
            return RiskState()

    def save(self, state: RiskState) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(asdict(state), f, indent=1)
        os.replace(tmp, self.path)


class NewsCalendar:
    """fx-board's calendar format (docs/RUNBOOK-research-local.md 4d): `datetime_utc,event,currency,impact`, times in UTC."""

    def __init__(self, events: list[tuple[int, str, str, str]]):
        self.events = sorted(events)
        self._times = [e[0] for e in self.events]

    @classmethod
    def from_csv(cls, path: str) -> NewsCalendar:
        out = []
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                t = datetime.fromisoformat(row["datetime_utc"].replace("Z", "+00:00"))
                out.append((int(t.timestamp() * 1000), row["event"], row["currency"].upper(), row["impact"].lower()))
        return cls(out)

    def blocking(self, now_ms: int, currencies: tuple[str, ...], before_ms: int, after_ms: int, impacts: tuple[str, ...]) -> str:
        """Name of an event whose window [t - before, t + after] contains now, for one of the currencies; '' if none."""
        lo = bisect.bisect_left(self._times, now_ms - after_ms)
        hi = bisect.bisect_right(self._times, now_ms + before_ms)
        for t, name, ccy, impact in self.events[lo:hi]:
            if ccy in currencies and impact in impacts:
                return name
        return ""


@dataclass(frozen=True)
class Decision:
    ok: bool
    reasons: tuple[str, ...] = ()


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _inside(minute: int, start: int, end: int) -> bool:
    return start <= minute < end if start <= end else minute >= start or minute < end  # windows may wrap midnight


class RiskGuard:
    def __init__(self, cfg: RiskConfig, store: StateStore | None = None, calendar: NewsCalendar | None = None):
        self.cfg = cfg
        self.store = store
        self.state = store.load() if store else RiskState()
        self.calendar = calendar
        self._day_tz = ZoneInfo(cfg.day_tz)
        self._sessions = [tuple(_minutes(x) for x in w.split("-")) for w in cfg.sessions_utc]
        self._rollover = tuple(_minutes(x) for x in cfg.rollover_ny) if cfg.rollover_ny else None
        self._spreads: dict[str, deque[float]] = {}

    # -- inputs -------------------------------------------------------------------------------------------------------------
    def observe(self, quote: Quote) -> None:
        """Feed every tick (or every poll): the rolling median spread is what a 'spread spike' is measured against."""
        dq = self._spreads.get(quote.symbol)
        if dq is None:
            dq = self._spreads[quote.symbol] = deque(maxlen=self.cfg.spread_window)
        dq.append(quote.spread)

    def median_spread(self, symbol: str) -> float | None:
        dq = self._spreads.get(symbol)
        return statistics.median(dq) if dq and len(dq) >= self.cfg.spread_min_samples else None

    def day_key(self, now_ms: int) -> str:
        t = datetime.fromtimestamp(now_ms / 1000, self._day_tz) + timedelta(hours=(24 - self.cfg.day_roll_hour) % 24)
        return t.date().isoformat()

    def update(self, now_ms: int, equity_usd: float) -> None:
        """Day roll-over, daily halt and drawdown kill. Called by check_entry and by the router's periodic sync."""
        s, cfg, dirty = self.state, self.cfg, False
        day = self.day_key(now_ms)
        if day != s.day:
            s.day, s.day_start_equity, s.trades_today, dirty = day, equity_usd, 0, True
        if equity_usd > s.peak_equity:
            s.peak_equity = equity_usd  # saved with the next change; losing a peak on a crash only makes the kill later
        if s.halted_day != day and s.day_start_equity - equity_usd >= cfg.daily_max_loss_usd:
            s.halted_day, dirty = day, True
        if cfg.max_drawdown_usd and not s.killed and s.peak_equity - equity_usd >= cfg.max_drawdown_usd:
            s.killed, dirty = f"MAX_DRAWDOWN peak {s.peak_equity:.2f} equity {equity_usd:.2f}", True
        if dirty:
            self._save()

    # -- the gate -----------------------------------------------------------------------------------------------------------
    def check_entry(self, *, now_ms: int, signal: Signal, quote: Quote, spec: SymbolSpec, equity_usd: float,
                    open_positions: int, open_risk_usd: float, new_risk_usd: float) -> Decision:
        self.update(now_ms, equity_usd)
        cfg, s, r = self.cfg, self.state, []

        if cfg.kill_file and os.path.exists(cfg.kill_file):
            r.append(KILL_SWITCH)
        if s.killed:
            r.append(KILLED)
        if s.halted_day == s.day:
            r.append(DAILY_HALT)
        elif (s.day_start_equity - equity_usd) + open_risk_usd + new_risk_usd > cfg.daily_max_loss_usd + 1e-9:
            r.append(DAILY_LOSS)

        if s.trades_today >= cfg.max_trades_per_day:
            r.append(MAX_TRADES_DAY)
        recent = [t for t in s.entry_times_ms if now_ms - t < cfg.window_s * 1000]
        if recent and now_ms - recent[-1] < cfg.min_interval_s * 1000:
            r.append(MIN_INTERVAL)
        if len(recent) >= cfg.max_entries_per_window:
            r.append(RATE_WINDOW)
        if now_ms < s.cooldown_until_ms:
            r.append(COOLDOWN)
        if open_positions >= cfg.max_open_positions:
            r.append(MAX_POSITIONS)

        if now_ms - quote.time_ms > cfg.max_quote_age_ms:
            r.append(STALE_QUOTE)
        if now_ms - signal.created_ms > cfg.max_signal_age_ms:
            r.append(STALE_SIGNAL)

        spread = quote.spread
        cap = cfg.max_spread_points.get(signal.symbol)
        if cap and spread > cap * spec.point + 1e-12:
            r.append(SPREAD_ABS)
        if spread > cfg.max_spread_to_stop * signal.sl_dist:
            r.append(SPREAD_TO_STOP)
        med = self.median_spread(signal.symbol)
        if med is not None and med > 0 and spread > cfg.spread_spike_mult * med:
            r.append(SPREAD_SPIKE)
        if spec.stops_level_points and min(signal.sl_dist, signal.tp_dist) < spec.stops_level_points * spec.point:
            r.append(STOPS_LEVEL)

        minute_utc = (now_ms // 60000) % 1440
        if self._sessions and not any(_inside(minute_utc, a, b) for a, b in self._sessions):
            r.append(OFF_SESSION)
        if self._rollover:
            ny = datetime.fromtimestamp(now_ms / 1000, NY)
            if _inside(ny.hour * 60 + ny.minute, *self._rollover):
                r.append(ROLLOVER)
        if self.calendar:
            ccys = spec.currencies
            if self.calendar.blocking(now_ms, ccys, int(cfg.news_before_min * 60000), int(cfg.news_after_min * 60000), cfg.news_impacts):
                r.append(NEWS)
        return Decision(not r, tuple(r))

    # -- outcomes -----------------------------------------------------------------------------------------------------------
    def on_entry(self, now_ms: int) -> None:
        s, keep = self.state, max(self.cfg.window_s, self.cfg.min_interval_s) * 1000
        s.entry_times_ms = [t for t in s.entry_times_ms if now_ms - t < keep] + [now_ms]
        s.trades_today += 1
        self._save()

    def on_close(self, now_ms: int, pnl_usd: float) -> None:
        s = self.state
        if pnl_usd < 0:
            s.consecutive_losses += 1
            if s.consecutive_losses >= self.cfg.max_consecutive_losses:
                s.cooldown_until_ms, s.consecutive_losses = now_ms + int(self.cfg.cooldown_min * 60000), 0
        else:
            s.consecutive_losses = 0
        self._save()

    def status(self) -> dict:
        return {**asdict(self.state), "spread_median": {k: self.median_spread(k) for k in self._spreads}}

    def _save(self) -> None:
        if self.store:
            self.store.save(self.state)


def utc_ms(iso: str) -> int:
    """'2026-10-19T08:00:00Z' -> epoch milliseconds (tests and configs)."""
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp() * 1000)
