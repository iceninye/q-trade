from qtrade.config import DEFAULT_SPECS
from qtrade.models import Quote, Side, Signal
from qtrade.risk import RiskConfig, utc_ms

EURUSD = DEFAULT_SPECS["EURUSD"]  # $10 per pip per lot, min 0.01, commission $7 / lot round trip
XAUUSD = DEFAULT_SPECS["XAUUSD"]
T0 = utc_ms("2026-10-20T09:00:00Z")  # a Tuesday, London session, no roll-over


def quote(bid=1.10000, spread=0.00002, t=T0, sym="EURUSD"):
    return Quote(sym, bid, round(bid + spread, 6), t)


def signal(id="s1", side=Side.BUY, sl=0.0003, tp=0.00045, t=T0, sym="EURUSD"):
    return Signal(id, sym, side, sl, tp, t, "test")


def open_risk(**kw):
    """A RiskConfig with every limit wide open except the ones a test names."""
    base = dict(daily_max_loss_usd=1e9, max_drawdown_usd=0, max_trades_per_day=10**6, min_interval_s=0,
                max_entries_per_window=10**6, max_consecutive_losses=10**6, max_open_positions=10**6, max_spread_to_stop=10,
                spread_spike_mult=1e9, sessions_utc=(), rollover_ny=(), kill_file=None)
    base.update(kw)
    return RiskConfig(**base)
