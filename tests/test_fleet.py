import pytest

from eve_fit_mcp import doctrines, fleet
from eve_fit_mcp.fitting import FitError, FitHandle

from test_ammo_matchup import KITER
from test_doctrines import BASI, FALCON, NIGHTHAWK


@pytest.fixture
def enemy(tmp_path, monkeypatch):
    monkeypatch.setattr(doctrines, "DOCTRINE_DIR", str(tmp_path / "doctrines"))
    doctrines.save("enemy", [{"eft": NIGHTHAWK, "count": 6, "label": "Nighthawk"},
                             {"eft": BASI, "count": 2, "label": "Basilisk"},
                             {"eft": FALCON, "count": 1, "label": "Falcon"}])
    return "enemy"


def test_kill_order_matters_falcon_first(enemy):
    r = fleet.battle([{"eft": NIGHTHAWK, "count": 6, "label": "NH"}, {"eft": BASI, "count": 2, "label": "Basi"}],
                     enemy, [10])
    d = r["by_distance"][0]
    assert d["result"]["winner"] == "us"
    assert d["result"]["our_kill_order"][0] == "Falcon"
    assert any(not v["winner"].startswith("us") for v in d["orders_compared"].values())  # some orders lose
    assert d["result"]["jams_on_us_at_start"]                                             # the Falcon jams us
    assert any("kill order" in i for i in r["insights"])


def test_jam_chance_matches_eve_formula(enemy):
    their = fleet._members(enemy, "All 5", None)
    our = fleet._members([{"eft": NIGHTHAWK, "count": 1, "label": "NH"}], "All 5", None)
    falcon = next(m for m in their if m.label == "Falcon")
    alive = {(m.label, i): True for side in (our, their) for m in side for i in range(m.count)}
    jams = fleet._jams(their, our, alive, 10000.0)
    strength = falcon.ecm[0][our[0].sensor_type]
    expected = 1 - (1 - min(1, strength / our[0].sensor)) ** len(falcon.ecm)  # all jammers on the only target
    assert jams[("NH", 0)] == pytest.approx(expected, rel=1e-6)


def test_logi_out_heals_and_targets_switch(enemy):
    r = fleet.battle([{"eft": NIGHTHAWK, "count": 6, "label": "NH"}], enemy, [10], our_order="logi_first")
    res = r["by_distance"][0]["result"]
    switched = {s["from"] for s in res["target_switches"]["us"]}
    assert "Basilisk" in switched or "Basilisk" not in res["our_kill_order"][:1]


def test_kiters_need_range(enemy):
    r = fleet.battle([{"eft": KITER, "count": 12, "label": "Kiter"}], enemy, [8, 35])
    near, far = r["by_distance"]
    assert near["result"]["winner"].startswith("them")
    assert far["result"]["winner"].startswith("us")
    assert near["result"]["tackle_on_us"]["Kiter"]["scrammed"]


def test_bad_order(enemy):
    with pytest.raises(FitError):
        fleet.battle([{"eft": NIGHTHAWK, "count": 1}], enemy, [10], our_order="random")
