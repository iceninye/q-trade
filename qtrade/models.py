"""Plain data types shared by the risk guard, the router, the brokers and the back-tester.

Money is in the ACCOUNT currency unless a name ends in ``_usd``. Prices and distances are in price units (EURUSD 0.0001 = 1 pip).
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"

    @property
    def sign(self) -> int:
        return 1 if self is Side.BUY else -1


@dataclass(frozen=True)
class Quote:
    symbol: str
    bid: float
    ask: float
    time_ms: int

    @property
    def spread(self) -> float:
        return self.ask - self.bid

    @property
    def mid(self) -> float:
        return (self.ask + self.bid) / 2

    def entry_price(self, side: Side) -> float:
        """A market buy fills at the ask, a sell at the bid."""
        return self.ask if side is Side.BUY else self.bid

    def exit_price(self, side: Side) -> float:
        """Closing a buy sells at the bid; closing a sell buys at the ask. Stops trigger on this side too."""
        return self.bid if side is Side.BUY else self.ask


@dataclass(frozen=True)
class SymbolSpec:
    """What the broker says about a symbol (MT5 ``symbol_info``) plus the one thing it does not say: the commission."""

    symbol: str
    point: float  # smallest quoted increment (MT5 `point`)
    tick_size: float  # MT5 `trade_tick_size`
    tick_value: float  # account-currency P/L of one tick_size move for 1.0 lot (MT5 `trade_tick_value`)
    volume_min: float
    volume_step: float
    volume_max: float
    stops_level_points: int = 0  # minimum SL/TP distance from the price (MT5 `trade_stops_level`)
    commission_per_lot_rt: float = 0.0  # account currency, round trip, per 1.0 lot: from the broker's fee page

    @property
    def value_per_price_unit(self) -> float:
        """Account-currency P/L of a 1.0 price move for 1.0 lot."""
        return self.tick_value / self.tick_size

    @property
    def currencies(self) -> tuple[str, str]:
        s = self.symbol.upper()
        return s[:3], s[3:6]


@dataclass(frozen=True)
class Signal:
    """A strategy's wish. Distances, not prices: the router re-anchors them to the BROKER's live quote, because a TradingView
    (or any other) feed is not the broker's feed and an absolute stop computed elsewhere can sit on the wrong side of the spread."""

    id: str  # idempotency key: the same id is never executed twice
    symbol: str
    side: Side
    sl_dist: float
    tp_dist: float
    created_ms: int  # when the strategy produced it (Pine `timenow`, tick time in a back-test)
    strategy: str = ""


@dataclass(frozen=True)
class OrderIntent:
    signal_id: str
    symbol: str
    side: Side
    volume: float
    price: float  # the price the decision was made at
    sl: float
    tp: float
    risk: float  # account currency lost if the stop fills at its price, commission included
    max_slippage: float  # price units (MT5 `deviation` = this / point)


@dataclass(frozen=True)
class OrderResult:
    ok: bool
    price: float = 0.0
    volume: float = 0.0
    ticket: int = 0
    code: str = ""  # broker return code or our reason
    time_ms: int = 0


@dataclass(frozen=True)
class Position:
    ticket: int
    symbol: str
    side: Side
    volume: float
    open_price: float
    sl: float
    tp: float
    open_ms: int


@dataclass(frozen=True)
class Account:
    equity: float
    balance: float
    currency: str = "USD"


@dataclass(frozen=True)
class Closed:
    """A position the broker no longer reports as open, with its realised P/L (commission and swap included)."""

    ticket: int
    close_ms: int
    pnl: float
    exit_price: float = 0.0
