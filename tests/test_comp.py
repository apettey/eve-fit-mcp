import pytest

from eve_fit_mcp import comp, doctrines, pilots
from eve_fit_mcp.fitting import FitError

from test_ammo_matchup import KITER
from test_doctrines import BASI, FALCON, NIGHTHAWK


@pytest.fixture
def enemy(tmp_path, monkeypatch):
    monkeypatch.setattr(doctrines, "DOCTRINE_DIR", str(tmp_path / "doctrines"))
    monkeypatch.setattr(pilots, "PILOT_DIR", str(tmp_path / "pilots"))
    pilots._cache.clear()
    doctrines.save("enemy", [{"eft": NIGHTHAWK, "count": 3, "label": "Nighthawk"},
                             {"eft": BASI, "count": 1, "label": "Basilisk"},
                             {"eft": FALCON, "count": 1, "label": "Falcon"}])
    yield "enemy"
    pilots._cache.clear()


OPTIONS = [{"eft": NIGHTHAWK, "label": "Nighthawk", "cost": 300e6},
           {"eft": BASI, "label": "Basilisk", "cost": 250e6},
           {"eft": KITER, "label": "Hurricane", "cost": 80e6}]


def test_recommend_exhaustive_with_reasoning(enemy):
    r = comp.recommend(OPTIONS, enemy, pilots=5, distances_km=[10])
    assert r["method"] == "exhaustive" and r["comps_evaluated"] == 21  # C(5+3-1, 2)
    best = r["best"]
    assert sum(best["comp"].values()) == 5
    assert best["battle"]["10"]["winner"].startswith("us")
    assert best["cost_isk"] == sum(n * {"Nighthawk": 300e6, "Basilisk": 250e6, "Hurricane": 80e6}[k]
                                   for k, n in best["comp"].items())
    assert r["reasoning"] and r["marginal_value"]
    assert all(r["best"]["score"] >= x["score"] for x in r["runners_up"])


def test_role_minimum_and_budget(enemy):
    r = comp.recommend(OPTIONS, enemy, pilots=5, distances_km=[10], require={"logistics": 2})
    assert r["best"]["comp"].get("Basilisk", 0) >= 2
    r = comp.recommend(OPTIONS, enemy, pilots=5, distances_km=[10], budget=600e6)
    assert r["best"]["cost_isk"] <= 600e6


def test_named_pilots_and_infeasible_explained(enemy):
    pilots.save_custom("Rookie", base_level=3)
    r = comp.recommend(OPTIONS, enemy, pilots=["All 5", "All 5", "Rookie"], distances_km=[10], exact=False)
    assert r["who_can_fly"]["Rookie"]["Nighthawk"] is False
    assert sum(r["best"]["comp"].values()) <= 3
    with pytest.raises(FitError, match="Who can fly what"):
        comp.recommend(OPTIONS[:2], enemy, pilots=["Rookie", "Rookie"], distances_km=[10])
