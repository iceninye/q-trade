import pytest

from qtrade import risk as R
from qtrade.models import Side
from qtrade.risk import NewsCalendar, RiskGuard, StateStore, utc_ms

from .helpers import EURUSD, T0, open_risk, quote, signal

MIN = 60_000


def check(g, t=T0, q=None, sig=None, equity=100.0, positions=0, open_r=0.0, new_r=1.0, spec=EURUSD):
    return g.check_entry(now_ms=t, signal=sig or signal(t=t), quote=q or quote(t=t), spec=spec, equity_usd=equity,
                         open_positions=positions, open_risk_usd=open_r, new_risk_usd=new_r)


def test_all_open_passes():
    assert check(RiskGuard(open_risk())).ok


def test_daily_loss_counts_floating_and_open_risk():
    g = RiskGuard(open_risk(daily_max_loss_usd=4.0))
    assert check(g, equity=100.0).ok
    assert check(g, equity=97.0, new_r=1.0).ok  # lost 3, this trade risks 1: worst case exactly 4
    d = check(g, equity=97.0, open_r=0.5, new_r=1.0)  # an open trade's risk counts too
    assert d.reasons == (R.DAILY_LOSS,)
    d = check(g, equity=96.0)  # lost 4: halted for the rest of the trading day, even if equity recovers
    assert R.DAILY_HALT in d.reasons and R.DAILY_HALT in check(g, equity=100.0).reasons


def test_trading_day_rolls_at_17_new_york_and_clears_the_halt():
    g = RiskGuard(open_risk(daily_max_loss_usd=4.0))
    check(g, equity=100.0)
    assert R.DAILY_HALT in check(g, equity=95.0).reasons
    before = utc_ms("2026-10-20T20:59:00Z")  # 16:59 New York (EDT)
    after = utc_ms("2026-10-20T21:00:00Z")  # 17:00 New York: next FX day
    assert g.day_key(before) == "2026-10-20" and g.day_key(after) == "2026-10-21"
    assert check(g, t=after, equity=95.0).ok and g.state.day_start_equity == 95.0


def test_halt_survives_a_restart(tmp_path):
    store = StateStore(str(tmp_path / "state.json"))
    g = RiskGuard(open_risk(daily_max_loss_usd=4.0), store)
    check(g, equity=100.0)
    check(g, equity=95.5)
    g2 = RiskGuard(open_risk(daily_max_loss_usd=4.0), store)  # a new process reading the same file
    assert R.DAILY_HALT in check(g2, equity=99.0).reasons and g2.state.day_start_equity == 100.0


def test_max_drawdown_kill_is_sticky():
    g = RiskGuard(open_risk(max_drawdown_usd=20.0))
    check(g, equity=120.0)
    assert R.KILLED in check(g, equity=100.0).reasons
    assert R.KILLED in check(g, t=T0 + 3 * 86_400_000, equity=150.0).reasons  # days later, richer: still killed


def test_rate_limits():
    g = RiskGuard(open_risk(min_interval_s=60, window_s=3600, max_entries_per_window=3, max_trades_per_day=4))
    assert check(g).ok  # as in the router: a check (which opens the trading day) always precedes an entry
    g.on_entry(T0)
    assert check(g, t=T0 + 30_000).reasons == (R.MIN_INTERVAL,)
    g.on_entry(T0 + MIN)
    g.on_entry(T0 + 2 * MIN)
    assert check(g, t=T0 + 3 * MIN).reasons == (R.RATE_WINDOW,)
    assert check(g, t=T0 + 61 * MIN).ok  # the first entry left the window
    g.on_entry(T0 + 61 * MIN)
    assert R.MAX_TRADES_DAY in check(g, t=T0 + 200 * MIN).reasons


def test_losing_streak_cooldown():
    g = RiskGuard(open_risk(max_consecutive_losses=3, cooldown_min=60))
    for i in range(2):
        g.on_close(T0, -1.0)
    g.on_close(T0, +0.5)  # a win resets the streak
    for i in range(3):
        g.on_close(T0, -1.0)
    assert check(g, t=T0 + MIN).reasons == (R.COOLDOWN,)
    assert check(g, t=T0 + 61 * MIN).ok


def test_max_positions():
    assert check(RiskGuard(open_risk(max_open_positions=1)), positions=1).reasons == (R.MAX_POSITIONS,)


def test_stale_quote_and_signal():
    g = RiskGuard(open_risk(max_quote_age_ms=3000, max_signal_age_ms=5000))
    assert check(g, t=T0 + 4000, q=quote(t=T0), sig=signal(t=T0 + 4000)).reasons == (R.STALE_QUOTE,)
    assert check(g, t=T0 + 6000, q=quote(t=T0 + 6000), sig=signal(t=T0)).reasons == (R.STALE_SIGNAL,)


def test_spread_checks():
    g = RiskGuard(open_risk(max_spread_points={"EURUSD": 15}, max_spread_to_stop=0.2, spread_spike_mult=2.0,
                            spread_min_samples=10))
    assert check(g, q=quote(spread=0.00016), sig=signal(sl=0.0010)).reasons == (R.SPREAD_ABS,)
    assert R.SPREAD_TO_STOP in check(g, q=quote(spread=0.00007), sig=signal(sl=0.0003)).reasons  # 0.7 / 3 pips > 0.2
    for _ in range(20):
        g.observe(quote(spread=0.00002))
    d = check(g, q=quote(spread=0.00005))  # 2.5 x the median of 0.2 pip
    assert d.reasons == (R.SPREAD_SPIKE,)


def test_stops_level():
    import dataclasses

    spec = dataclasses.replace(EURUSD, stops_level_points=50)  # 5 pips minimum distance
    assert check(RiskGuard(open_risk()), spec=spec).reasons == (R.STOPS_LEVEL,)


def test_session_rollover_and_news(tmp_path):
    g = RiskGuard(open_risk(sessions_utc=("07:00-16:30",)))
    assert check(g, t=utc_ms("2026-10-20T06:59:00Z")).reasons == (R.OFF_SESSION,)
    g = RiskGuard(open_risk(rollover_ny=("16:55", "18:05")))
    assert check(g, t=utc_ms("2026-10-20T21:30:00Z")).reasons == (R.ROLLOVER,)  # 17:30 New York
    cal = tmp_path / "calendar.csv"
    cal.write_text("datetime_utc,event,currency,impact\n2026-10-20T12:30:00Z,CPI y/y,USD,high\n"
                   "2026-10-20T09:00:00Z,Some speech,JPY,high\n", encoding="utf-8")
    g = RiskGuard(open_risk(news_before_min=5, news_after_min=10), calendar=NewsCalendar.from_csv(str(cal)))
    assert check(g).ok  # the JPY event does not concern EURUSD
    assert check(g, t=utc_ms("2026-10-20T12:26:00Z")).reasons == (R.NEWS,)
    assert check(g, t=utc_ms("2026-10-20T12:39:00Z")).reasons == (R.NEWS,)
    assert check(g, t=utc_ms("2026-10-20T12:41:00Z")).ok


def test_kill_file(tmp_path):
    kill = tmp_path / "KILL"
    g = RiskGuard(open_risk(kill_file=str(kill)))
    assert check(g).ok
    kill.write_text("")
    assert check(g).reasons == (R.KILL_SWITCH,)


def test_every_failing_check_is_reported():
    g = RiskGuard(open_risk(max_open_positions=1, max_spread_points={"EURUSD": 1}))
    assert set(check(g, positions=1, sig=signal(side=Side.SELL)).reasons) == {R.MAX_POSITIONS, R.SPREAD_ABS}


@pytest.mark.parametrize("hhmm, inside", [("23:30", True), ("00:30", True), ("12:00", False)])
def test_session_window_may_wrap_midnight(hhmm, inside):
    g = RiskGuard(open_risk(sessions_utc=("22:00-02:00",)))
    assert check(g, t=utc_ms(f"2026-10-20T{hhmm}:00Z")).ok is inside
