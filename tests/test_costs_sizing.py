import dataclasses

import pytest

from qtrade.costs import breakeven_winrate, commission_in_price, cost_r, round_trip_cost, table
from qtrade.sizing import loss_per_lot, size_for_risk

from .helpers import EURUSD, XAUUSD


def test_breakeven_winrate_and_cost_in_r():
    assert breakeven_winrate(1.0, 0.0) == pytest.approx(0.5)
    assert breakeven_winrate(1.5, 0.6) == pytest.approx(0.64)  # EURUSD 1m from the real-bar table in docs/GUIDE.md
    assert cost_r(round_trip_cost(0.00002, 0.00007, 0.0), 0.0003) == pytest.approx(0.3)


def test_commission_in_price():
    assert commission_in_price(EURUSD) == pytest.approx(0.00007)  # $7 per lot / $10 per pip = 0.7 pip


def test_table_has_a_row_per_stop():
    lines = table(0.00009, [0.0002, 0.001], [1, 2], unit=0.0001).splitlines()
    assert len(lines) == 4 and lines[2].split()[0] == "2"


def no_comm(spec):
    return dataclasses.replace(spec, commission_per_lot_rt=0.0)


@pytest.mark.parametrize("stop_pips, volume", [(10, 0.01), (5, 0.02), (3, 0.03), (2, 0.05)])
def test_one_dollar_on_eurusd(stop_pips, volume):
    s = size_for_risk(no_comm(EURUSD), stop_pips * 0.0001, 1.0)
    assert s.volume == pytest.approx(volume) and s.risk <= 1.0 + 1e-9  # 3 pips: 0.0333 -> 0.03 (floor, never up)


def test_min_lot_is_never_rounded_up():
    s = size_for_risk(no_comm(EURUSD), 0.0020, 1.0)  # 20 pips: 0.005 lot needed, 0.01 risks $2
    assert s.volume == 0 and s.reason == "MIN_LOT_RISK" and s.risk == pytest.approx(2.0)
    s = size_for_risk(no_comm(XAUUSD), 3.0, 1.0)  # gold: 0.01 lot = 1 oz, a $3 stop risks $3
    assert s.volume == 0 and s.risk == pytest.approx(3.0)


def test_overshoot_accepts_the_minimum_within_tolerance():
    s = size_for_risk(no_comm(EURUSD), 0.0011, 1.0, overshoot=0.15)  # 0.01 lot risks $1.10
    assert s.volume == 0.01 and s.risk == pytest.approx(1.1)


def test_commission_and_slippage_are_part_of_the_risk():
    assert loss_per_lot(EURUSD, 0.0003, 0.00002) == pytest.approx(32 + 7)  # 3.2 pips x $10 + $7
    s = size_for_risk(EURUSD, 0.0003, 1.0)  # $37 per lot -> 0.02 lot ($0.74); without commission it would be 0.03
    assert s.volume == pytest.approx(0.02) and s.risk == pytest.approx(0.74)


def test_cent_account_has_100x_finer_granularity():
    cent = dataclasses.replace(no_comm(EURUSD), symbol="EURUSDc")  # tick_value 1.0 is in US CENTS on a cent account
    s = size_for_risk(cent, 0.0020, 100.0)  # $1 = 100 USC, 20 pips -> 0.5 cent lot (= 0.005 standard lot)
    assert s.volume == pytest.approx(0.5) and s.risk == pytest.approx(100.0)
