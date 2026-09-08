import json

from pm.config import UniverseConfig
from pm.gamma import derive_tags, market_from_gamma, markets_from_event
from pm.universe import Universe
from tests.conftest import D, mk_market


def _row(cid, q, vol=1000, **kw):
    r = {"conditionId": cid, "question": q, "slug": cid, "clobTokenIds": json.dumps([f"{cid}y", f"{cid}n"]),
         "outcomes": json.dumps(["Yes", "No"]), "liquidityNum": 50000, "volume24hr": vol,
         "endDate": "2026-12-31T00:00:00Z", "acceptingOrders": True}
    r.update(kw)
    return r


def test_sports_detected_by_event_tag_series_or_vs():
    assert "sports" in derive_tags(_row("a", "Will X win?"), {"tags": [{"label": "Tennis"}]})
    assert "sports" in derive_tags(_row("a", "Will X win?"), {"series": [{"id": 1}]})
    assert "sports" in derive_tags(_row("a", "New York Mets vs. Miami Marlins"), {})
    assert "sports" in derive_tags(_row("a", "LoL: IG vs LGD (BO5)"), {"tags": [{"label": "Esports"}]})
    assert "sports" not in derive_tags(_row("a", "Will the Fed cut rates?"), {"tags": [{"label": "Economics"}]})


def test_markets_from_event_sets_group_count_and_flags():
    ev = {"id": "E1", "slug": "ev", "negRisk": True, "tags": [{"label": "Politics"}],
          "markets": [_row("c1", "A?"), _row("c2", "B?"), _row("c3", "C?", acceptingOrders=False)]}
    ms = markets_from_event(ev)
    assert len(ms) == 3 and all(m.neg_risk and m.event_id == "E1" and "politics" in m.tags for m in ms)
    assert all(m.event_market_count == 2 for m in ms)  # closed one excluded from the count


class _Gamma:
    def __init__(self, markets):
        self._m = markets

    async def active_markets(self, limit=500):
        return self._m


class _Clob:
    async def enrich_market(self, m):
        m.fee_known = True
        return m


async def test_universe_caps_groups_and_excludes_sports():
    small = [mk_market(f"s{i}", f"S{i}y", f"S{i}n", neg_risk=True, event_id="SMALL", event_count=3) for i in range(3)]
    big = [mk_market(f"b{i}", f"B{i}y", f"B{i}n", neg_risk=True, event_id="BIG", event_count=20) for i in range(20)]
    sport = mk_market("sp", "SPy", "SPn")
    sport.tags = ["sports", "tennis"]
    plain = mk_market("p1", "P1y", "P1n")
    for i, m in enumerate(big):
        m.volume_24h_usd = D(100000 - i)   # big group is the highest volume
    sport.volume_24h_usd = D(90000)
    for m in small:
        m.volume_24h_usd = D(50000)
    plain.volume_24h_usd = D(40000)
    allm = big + [sport] + small + [plain]

    cfg = UniverseConfig(max_markets=6, max_event_markets=12, min_liquidity_usd=D(0), min_volume_24h_usd=D(0))
    out = await Universe(_Gamma(allm), _Clob(), cfg).select()
    ids = [m.condition_id for m in out]
    assert "sp" not in ids                                   # sports excluded
    assert ids[0] == "b0"                                    # top-volume market kept alone...
    assert sum(1 for i in ids if i.startswith("b")) == 1     # ...and the big event contributes only one
    assert {"s0", "s1", "s2"} <= set(ids)                    # small event imported whole
    assert "p1" in ids                                       # room left for ordinary markets
    assert len(out) <= 6
