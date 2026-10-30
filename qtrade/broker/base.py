"""The broker interface: what the router needs, nothing more. PaperBroker (back-test, dry run) and MT5Broker implement it;
a crypto exchange adapter would too."""
from __future__ import annotations

from typing import Protocol

from ..models import Account, Closed, OrderIntent, OrderResult, Position, Quote, SymbolSpec


class Broker(Protocol):
    def quote(self, symbol: str) -> Quote | None: ...

    def spec(self, symbol: str) -> SymbolSpec: ...

    def account(self) -> Account: ...

    def positions(self) -> list[Position]:
        """Open positions placed by THIS system (MT5: filtered by magic number)."""
        ...

    def place(self, intent: OrderIntent) -> OrderResult:
        """Market order with the stop-loss and take-profit attached on the broker's server (never only in this process)."""
        ...

    def close(self, ticket: int) -> OrderResult: ...

    def closed(self, ticket: int) -> Closed | None:
        """Realised result of a position that is no longer open; None while the broker's history does not have it yet."""
        ...
