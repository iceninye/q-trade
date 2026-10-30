import json
import lzma
import struct
import time

import pytest
from fastapi.testclient import TestClient

from qtrade.broker.paper import PaperBroker
from qtrade.data.dukascopy import decode_hour
from qtrade.forward import family_z, judge
from qtrade.journal import Journal
from qtrade.models import Quote
from qtrade.risk import RiskGuard
from qtrade.router import Router, RouterConfig

from .helpers import EURUSD, open_risk

SECRET = "s3cret-s3cret-s3cret"


def wait(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def app():
    from qtrade.webhook import create_app

    journal = Journal()

    def make_router():
        broker = PaperBroker({"EURUSD": EURUSD}, 100.0)
        return Router(broker, RiskGuard(open_risk(max_open_positions=1)), journal, RouterConfig())

    def before(router, alert):  # paper dry run: the alert's price is the quote
        router.broker.set_quote(Quote(alert.symbol, alert.price, alert.price + 0.00002, int(time.time() * 1000)))

    with TestClient(create_app(make_router, SECRET, before=before)) as client:
        yield client, journal


def alert(**kw):
    a = {"secret": SECRET, "id": "EURUSD-1-buy", "symbol": "EURUSD", "side": "buy", "sl_dist": 0.0003, "tp_dist": 0.00045,
         "fire_ms": int(time.time() * 1000), "strategy": "ema", "price": 1.1}
    a.update(kw)
    return json.dumps(a)


def test_valid_alert_is_queued_and_placed_once(app):
    client, journal = app
    r = client.post("/tv", content=alert(), headers={"content-type": "text/plain"})  # TradingView may send text/plain
    assert r.status_code == 200 and r.json() == {"queued": "EURUSD-1-buy"}
    assert client.post("/tv", content=alert()).status_code == 200  # the same alert again
    assert wait(lambda: sum(journal.counts().values()) == 1 and journal.counts().get("placed") == 1)
    time.sleep(0.2)
    assert journal.counts() == {"placed": 1}  # the duplicate never became an order
    h = client.get("/health").json()
    assert h["worker_alive"] and h["guard"]["trades_today"] == 1


def test_bad_secret_and_bad_payload(app):
    client, _ = app
    assert client.post("/tv", content=alert(secret="wrong-wrong-wrong-wrong")).status_code == 401
    assert client.post("/tv", content="not json").status_code == 422
    assert client.post("/tv", content=alert(side="long")).status_code == 422
    assert client.post("/tv", content=alert(sl_dist=-1)).status_code == 422


def test_stale_alert_is_rejected_by_the_guard(app):
    client, journal = app
    client.post("/tv", content=alert(id="old", fire_ms=int(time.time() * 1000) - 60_000))
    assert wait(lambda: journal.counts().get("rejected") == 1)
    assert journal.reasons() == {"STALE_SIGNAL": 1}


def test_short_secret_is_refused():
    from qtrade.webhook import create_app

    with pytest.raises(ValueError):
        create_app(lambda: None, "short")


# -- forward rule ---------------------------------------------------------------------------------------------------------
DAY = 86_400_000


def trades(rs, per_day=5):
    return [(i // per_day * DAY + i, r) for i, r in enumerate(rs)]


def test_family_z_matches_fx_board():
    assert family_z(1) == pytest.approx(2.3263, abs=1e-4)
    assert family_z(56) == pytest.approx(3.57, abs=0.01)  # fx-board v0.7.4: 56 cells at 1 %


def test_judge_statuses():
    assert judge(trades([1, -1] * 10))["status"] == "accumulating"
    assert judge(trades([-1, -1, 0.5] * 20))["status"] == "rejected"  # can reject early
    strong = judge(trades([1.5, 1.5, -1] * 70))  # +0.67 R per trade
    assert strong["status"] == "confirming" and strong["eval_lb"] > 0
    flat = judge(trades([1, -1] * 110))
    assert flat["status"] == "unclear"


def test_confirmation_is_decided_once_on_the_first_trades():
    head = [1, -1] * 100  # the first 200: no edge
    tail = [2] * 300  # a lucky run afterwards cannot turn it into a confirmation
    assert judge(trades(head + tail))["status"] == "unclear"


# -- Dukascopy decoding ---------------------------------------------------------------------------------------------------
def test_decode_hour():
    raw = struct.pack(">IIIff", 1500, 110012, 110010, 1.0, 2.0) + struct.pack(">IIIff", 3_599_999, 110020, 110015, 1.0, 1.0)
    blob = lzma.compress(raw, format=lzma.FORMAT_ALONE)
    assert decode_hour(blob, 1_000_000, 1e5) == [(1_001_500, 1.1001, 1.10012), (4_599_999, 1.10015, 1.1002)]
    assert decode_hour(b"", 0, 1e5) == []


def test_decode_minutes_and_synthetic_ticks():
    from qtrade.data.dukascopy import decode_minutes, minutes_to_ticks

    rec = struct.Struct(">iiiiif")
    bid = rec.pack(60, 110000, 110010, 109990, 110020, 5.0) + rec.pack(120, 110010, 110010, 110010, 110010, 0.0)  # 2nd = filler
    ask = rec.pack(60, 110002, 110013, 109992, 110022, 5.0)
    b = decode_minutes(lzma.compress(bid, format=lzma.FORMAT_ALONE), 1_000, 1e5)
    a = decode_minutes(lzma.compress(ask, format=lzma.FORMAT_ALONE), 1_000, 1e5)
    assert b == {1_060: (1.1, 1.1002, 1.0999, 1.1001)}  # open, high, low, close; the flat zero-volume filler is dropped
    ticks = minutes_to_ticks(b, a, 1e-5)
    # an up bar walks open -> low -> high -> close; ask = bid + max(open spread, close spread) = 0.3 pip
    assert [t for t, _, _ in ticks] == [1_060_000, 1_080_000, 1_100_000, 1_119_000]
    assert [x for _, x, _ in ticks] == [1.1, 1.0999, 1.1002, 1.1001]
    assert all(abs(ask - bid - 0.00003) < 1e-9 for _, bid, ask in ticks)
