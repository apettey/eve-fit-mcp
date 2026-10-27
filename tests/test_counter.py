import pytest

from eve_fit_mcp import counter, doctrines, explore, fleet, goals, optimizer
from eve_fit_mcp.fitting import FitError, FitHandle

from test_doctrines import BASI, FALCON, NIGHTHAWK

SUPPORT = [{"eft": BASI, "count": 2, "label": "Basi"}]


@pytest.fixture
def enemy(tmp_path, monkeypatch):
    monkeypatch.setattr(doctrines, "DOCTRINE_DIR", str(tmp_path / "doctrines"))
    doctrines.save("enemy", [{"eft": NIGHTHAWK, "count": 6, "label": "Nighthawk"},
                             {"eft": BASI, "count": 2, "label": "Basilisk"},
                             {"eft": FALCON, "count": 1, "label": "Falcon"}])
    return "enemy"


def test_score_rewards_partial_damage(enemy):
    obj = fleet.FleetObjective(enemy, 6, [10], support=SUPPORT)
    good = FitHandle(NIGHTHAWK)
    obj.calibrate(good)
    win = obj(good)["score"]
    no_web = good.clone()
    no_web.set_module("med:5", "Sensor Booster II")
    loss = obj(no_web)["score"]
    assert win > 1.5 and loss < 0          # a clean win vs a wipe
    assert -1.01 <= loss                    # partial damage keeps it above the floor


def test_counter_fit_repairs_a_broken_fit(enemy):
    broken = FitHandle(NIGHTHAWK.replace("Stasis Webifier II", "Sensor Booster II"))
    r = counter.counter_fit(broken, enemy, count=6, distances_km=[10], support=SUPPORT,
                            racks={"med:5": ["Sensor Booster II", "Stasis Webifier II", "Medium Shield Extender II"]})
    assert r["improved"]
    assert r["score_after"]["score"] > r["score_before"]["score"]
    assert {"slot": "med:5", "from": "Sensor Booster II", "to": "Stasis Webifier II"} in r["changes"]
    assert r["battle_after"][0]["winner"] == "us"
    assert r["search"]["alternatives"][0]["score"] == pytest.approx(r["score_after"]["score"], abs=1e-3)
    r["handle"].close()


def test_counter_fit_keeps_original_when_already_best(enemy):
    r = counter.counter_fit(FitHandle(NIGHTHAWK), enemy, count=6, distances_km=[10], support=SUPPORT,
                            racks={"med:5": ["Stasis Webifier II", "Sensor Booster II"]})
    assert not r["improved"] and r["changes"] == []


def test_fleet_goal_in_sweep_and_optimizer(enemy):
    h = FitHandle(NIGHTHAWK.replace("Stasis Webifier II", "Sensor Booster II"))
    r = explore.sweep(h, {"med:5": ["Sensor Booster II", "Stasis Webifier II"]},
                      goals={"fleet:enemy@10x6": "max"}, include_eft=0)
    assert r["front"][0]["modules"]["med:5"] == {"Stasis Webifier II": 1}
    res = optimizer.optimize(h, {"fleet:enemy@10x6": 1.0}, slots=["med:5"], mode="pool",
                             pools={"med:5": ["Stasis Webifier II"]}, optimize_charges=False, time_limit_s=120)
    assert "Stasis Webifier II" in res["eft"]
    with pytest.raises(FitError):
        goals.Specials(["fleet:nonsense"])


def test_renamed_items_are_noted():
    from eve_fit_mcp import stats
    h = FitHandle(NIGHTHAWK.replace("Stasis Webifier II", "ECCM - Gravimetric II"))
    s = stats.compute(h)
    assert s["valid"]
    assert any("ECCM - Gravimetric II no longer exists" in i for i in s["issues"])


def test_counter_fit_needs_racks_for_sweep(enemy):
    with pytest.raises(FitError, match="racks"):
        counter.counter_fit(FitHandle(NIGHTHAWK), enemy, mode="sweep")


def test_changes_ignore_modules_that_only_moved():
    from eve_fit_mcp.counter import _changes
    from eve_fit_mcp.fitting import FitHandle
    a = FitHandle("[Caracal, a]\nDamage Control II\nBallistic Control System II\n\nLarge Shield Extender II\nWarp Scrambler II\n")
    b = FitHandle("[Caracal, b]\nBallistic Control System II\nDamage Control II\n\nLarge Shield Extender II\nLarge Shield Extender II\n")
    assert _changes(a, b) == [{"slot": "med:1", "from": "Warp Scrambler II", "to": "Large Shield Extender II"}]
