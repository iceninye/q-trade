import dataclasses

import pytest

from qtrade.broker.paper import PaperBroker
from qtrade.journal import Journal
from qtrade.models import Side
from qtrade.risk import RiskGuard
from qtrade.router import Router, RouterConfig

from .helpers import EURUSD, T0, open_risk, quote, signal


def setup(balance=100.0, currency="USD", risk=None, cfg=None, journal=None, spec=EURUSD):
    broker = PaperBroker({"EURUSD": spec}, balance, currency)
    now = [T0]
    broker.set_quote(quote(t=T0))
    router = Router(broker, RiskGuard(risk or open_risk()), journal or Journal(), cfg or RouterConfig(), clock=lambda: now[0])
    return broker, router, now


def test_places_with_stops_on_the_exit_side_and_one_dollar_risk():
    broker, router, _ = setup()
    out = router.handle(signal(sl=0.0003, tp=0.00045))  # bid 1.10000 ask 1.10002
    assert out["status"] == "placed"
    p = broker.positions()[0]
    assert p.open_price == 1.10002 and p.sl == pytest.approx(1.09970) and p.tp == pytest.approx(1.10045)
    # at risk: 3.2 pips (stop + spread) x $10 x 0.02 lot + $7 x 0.02 = $0.78 <= $1
    assert p.volume == pytest.approx(0.02)
    assert router.open[p.ticket][1] == pytest.approx(0.78)


def test_duplicate_signal_id_is_never_executed_twice():
    broker, router, _ = setup()
    assert router.handle(signal("dup"))["status"] == "placed"
    assert router.handle(signal("dup"))["status"] == "duplicate"
    assert len(broker.positions()) == 1


def test_rejection_is_journaled_with_reasons():
    j = Journal()
    broker, router, _ = setup(journal=j, risk=open_risk(max_open_positions=1))
    router.handle(signal("a"))
    out = router.handle(signal("b", side=Side.SELL))
    assert out == {"id": "b", "status": "rejected", "reasons": ["MAX_POSITIONS"]}
    assert j.reasons() == {"MAX_POSITIONS": 1} and j.counts() == {"placed": 1, "rejected": 1}


def test_min_lot_rejection_comes_from_sizing():
    broker, router, _ = setup()
    out = router.handle(signal(sl=0.0030, tp=0.0045))  # 30 pips: 0.01 lot would risk ~$3.1
    assert out["status"] == "rejected" and out["reasons"] == ["MIN_LOT_RISK"]


def test_stop_out_reaches_the_journal_and_the_guard():
    j = Journal()
    broker, router, now = setup(journal=j)
    router.handle(signal(sl=0.0003, tp=0.00045))
    now[0] = T0 + 60_000
    closed = broker.set_quote(quote(bid=1.09960, t=now[0]))  # gaps 1 pip through the stop
    assert len(closed) == 1
    router.sync()
    tr = j.trades()[0]
    assert tr["pnl_usd"] == pytest.approx(-(0.00042 * 100_000 * 0.02 + 7 * 0.02))  # 4.2 pips x $2/pip + commission
    assert tr["r"] < -1.0  # the gap made it worse than 1 R: stops are not guaranteed prices
    assert router.guard.state.consecutive_losses == 1 and router.open == {}


def test_time_stop_closes_old_positions():
    broker, router, now = setup(cfg=RouterConfig(max_hold_s=300))
    router.handle(signal())
    now[0] = T0 + 301_000
    broker.set_quote(quote(t=now[0]))
    router.sync()  # closes
    assert broker.positions() == []
    router.sync()  # picks the close up
    assert router.open == {}


def test_open_positions_are_remembered_across_restarts(tmp_path):
    path = str(tmp_path / "j.sqlite")
    broker, router, _ = setup(journal=Journal(path))
    router.handle(signal())
    restarted = Router(broker, RiskGuard(open_risk()), Journal(path), RouterConfig(), clock=lambda: T0)
    assert list(restarted.open) == [broker.positions()[0].ticket]


def test_cent_account_money_is_converted():
    cent = dataclasses.replace(EURUSD, symbol="EURUSD", commission_per_lot_rt=0.0)  # tick_value in US cents
    broker, router, _ = setup(balance=10_000.0, currency="USC", spec=cent)  # $100 as 10,000 USC
    out = router.handle(signal(sl=0.0018, tp=0.0027))  # 18 pips + 0.2 spread: $1 buys 0.54 cent lot
    assert out["status"] == "placed" and out["volume"] == pytest.approx(0.54)
    assert router.open[out["ticket"]][1] == pytest.approx(0.54 * 182 / 100)  # 0.54 x 182 USC, recorded in USD


def test_unknown_account_currency_needs_a_factor():
    broker, router, _ = setup(currency="HKD")
    with pytest.raises(ValueError, match="usd_per_account_unit"):
        router.handle(signal())
