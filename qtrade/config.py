"""config.toml -> RiskConfig, RouterConfig, symbol specs and a broker. Secrets (MT5 password, webhook secret) are read from the
environment variables the file names, never from the file itself."""
from __future__ import annotations

import dataclasses
import os
import tomllib

from .models import SymbolSpec
from .risk import NewsCalendar, RiskConfig, RiskGuard, StateStore
from .router import RouterConfig

# Typical MT5 values for a standard (USD) account, used when config.toml has no [paper.specs.<SYM>]. CHECK your broker's
# symbol_info(): contract sizes differ (some quote gold in 1 oz or 10 oz lots), and cent accounts report tick_value in US cents.
DEFAULT_SPECS = {
    "EURUSD": SymbolSpec("EURUSD", 0.00001, 0.00001, 1.0, 0.01, 0.01, 100.0, 0, 7.0),  # 1 lot = 100,000 EUR; $10 per pip
    "XAUUSD": SymbolSpec("XAUUSD", 0.01, 0.01, 1.0, 0.01, 0.01, 100.0, 0, 7.0),  # 1 lot = 100 oz; $1 per $0.01
}


def load(path: str | None) -> dict:
    if not path:
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f)


def _fields(cls, d: dict) -> dict:
    """Keep the keys the dataclass knows; TOML lists become tuples where the dataclass default is a tuple."""
    names = {f.name: f for f in dataclasses.fields(cls)}
    out = {}
    for k, v in d.items():
        if k in names:
            out[k] = tuple(v) if isinstance(v, list) else v
    return out


def risk_config(c: dict) -> RiskConfig:
    return RiskConfig(**_fields(RiskConfig, c.get("risk", {})))


def router_config(c: dict) -> RouterConfig:
    return RouterConfig(**_fields(RouterConfig, c.get("router", {})))


def specs(c: dict) -> dict[str, SymbolSpec]:
    comm = c.get("broker", {}).get("commission_per_lot_rt", {})
    out = {k: dataclasses.replace(v, commission_per_lot_rt=comm.get(k, v.commission_per_lot_rt)) for k, v in DEFAULT_SPECS.items()}
    for sym, d in c.get("paper", {}).get("specs", {}).items():
        out[sym] = SymbolSpec(symbol=sym, commission_per_lot_rt=comm.get(sym, 0.0), **d)
    return out


def guard(c: dict) -> RiskGuard:
    files = c.get("files", {})
    cal = files.get("calendar")
    return RiskGuard(risk_config(c), StateStore(files.get("state", "state.json")), NewsCalendar.from_csv(cal) if cal else None)


def broker(c: dict):
    b = c.get("broker", {})
    kind = b.get("kind", "paper")
    if kind == "paper":
        from .broker.paper import PaperBroker

        p = c.get("paper", {})
        return PaperBroker(specs(c), p.get("balance", 100.0), p.get("currency", "USD"))
    if kind == "mt5":
        from .broker.mt5 import MT5Broker

        return MT5Broker(login=b.get("login", 0), password=os.environ.get(b.get("password_env", "MT5_PASSWORD"), ""),
                         server=b.get("server", ""), path=b.get("path", ""), magic=b.get("magic", 20261030),
                         symbol_map=b.get("symbol_map"), commission_per_lot_rt=b.get("commission_per_lot_rt"),
                         server_utc_offset_h=b.get("server_utc_offset_h"))
    raise ValueError(f"unknown broker kind {kind!r}")
