"""Position size for a fixed money risk on a micro account.

The rule that matters on a small account: NEVER round the volume UP to the broker's minimum. 0.01 lot of XAUUSD with a $3 stop
risks $3, three times a $1 budget; a size that cannot fit the budget is a rejected trade, not a bigger one.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .models import SymbolSpec

EPS = 1e-9  # 0.03 / 0.01 = 2.9999999999999996 in floating point


@dataclass(frozen=True)
class Size:
    volume: float  # 0 when rejected
    risk: float  # account currency lost at the stop with this volume (commission and slippage allowance included)
    reason: str = ""


def loss_per_lot(spec: SymbolSpec, stop_dist: float, slippage: float = 0.0) -> float:
    """Account currency lost by 1.0 lot if the stop fills `slippage` beyond its price, round-trip commission included."""
    return (stop_dist + slippage) * spec.value_per_price_unit + spec.commission_per_lot_rt


def _steps(x: float, step: float) -> int:
    return math.floor(x / step + EPS)


def size_for_risk(spec: SymbolSpec, stop_dist: float, risk: float, slippage: float = 0.0, overshoot: float = 0.0) -> Size:
    """Largest volume (multiple of volume_step, within [volume_min, volume_max]) whose loss at the stop is <= risk.

    overshoot: accept the minimum volume when its risk is at most risk x (1 + overshoot). Default 0 = never exceed the budget.
    """
    if not (stop_dist > 0 and risk > 0):
        return Size(0.0, 0.0, "BAD_INPUT")
    per_lot = loss_per_lot(spec, stop_dist, slippage)
    digits = max(0, -int(math.floor(math.log10(spec.volume_step) + EPS)))
    vol = round(min(_steps(risk / per_lot, spec.volume_step) * spec.volume_step, spec.volume_max), digits)
    if vol + EPS < spec.volume_min:
        min_risk = spec.volume_min * per_lot
        if min_risk <= risk * (1 + overshoot) + EPS:
            return Size(spec.volume_min, min_risk)
        return Size(0.0, min_risk, "MIN_LOT_RISK")  # `risk` carries what the minimum would have risked
    return Size(vol, vol * per_lot)
