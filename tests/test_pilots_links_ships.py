import os

import pytest

from eve_fit_mcp import catalog, optimizer, pilots, ships, stats
from eve_fit_mcp.fitting import FitError, FitHandle

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


@pytest.fixture(autouse=True)
def isolated_pilots(tmp_path, monkeypatch):
    monkeypatch.setattr(pilots, "PILOT_DIR", str(tmp_path / "pilots"))
    pilots._cache.clear()
    yield
    pilots._cache.clear()


@pytest.fixture
def rifter_eft():
    with open(os.path.join(FIXTURES, "rifter.eft"), encoding="utf-8") as f:
        return f.read()


def test_builtin_pilot_names():
    assert pilots.canonical(None) == "All 5"
    assert pilots.canonical("all3") == "All 3"
    assert pilots.canonical(4) == "All 4"
    with pytest.raises(FitError, match="Unknown pilot"):
        pilots.canonical("Nobody")


def test_skill_levels_change_stats(rifter_eft):
    s5 = stats.compute(FitHandle(rifter_eft, "All 5"))
    s0 = stats.compute(FitHandle(rifter_eft, "All 0"))
    assert s0["dps_total"] < s5["dps_total"]
    assert s0["cpu_total"] < s5["cpu_total"]


def test_custom_pilot_overrides_and_live_update(rifter_eft):
    pilots.save_custom("Gunner", base_level=5, skills={"Small Projectile Turret": 1})
    info = pilots.describe("Gunner", ["Small Projectile Turret"])
    assert info["levels"]["Small Projectile Turret"] == 1
    h = FitHandle(rifter_eft, "Gunner")
    weak = stats.compute(h)["dps_total"]
    assert weak < stats.compute(FitHandle(rifter_eft, "All 5"))["dps_total"]
    pilots.save_custom("Gunner", base_level=5, skills={"Small Projectile Turret": 5})
    h.recalc()  # fits pick up pilot edits on recalc
    assert stats.compute(h)["dps_total"] > weak
    with pytest.raises(FitError, match="Unknown skill"):
        pilots.save_custom("Gunner", skills={"Underwater Basket Weaving": 5})


def test_missing_skills_and_usable_only(rifter_eft):
    pilots.save_custom("NoT2", base_level=3)  # T2 guns need the turret skill at V
    h = FitHandle(rifter_eft, "NoT2")
    missing = stats.compute(h, skills_check=True)["missing_skills"]
    assert "200mm AutoCannon II" in missing
    names = [i.typeName for i in catalog.module_candidates(h, "high:0", usable_only=True)]
    assert "200mm AutoCannon I" in names and "200mm AutoCannon II" not in names
    res = optimizer.optimize(h, {"dps_total": 1}, slots=["high"], time_limit_s=60)
    assert "AutoCannon II" not in res["eft"]


def test_command_links_boost_and_survive_clone(rifter_eft):
    plain = FitHandle(rifter_eft)
    boosted = FitHandle(rifter_eft, command_links=[{"type": "armor_all", "strength": 5, "mindlink": True},
                                                   {"type": "skirmish_all", "strength": 5}])
    a, b = stats.compute(plain), stats.compute(boosted)
    assert b["ehp_armor"] > a["ehp_armor"] * 1.05
    assert b["signature"] < a["signature"]
    assert stats.compute(boosted.clone())["ehp_armor"] == pytest.approx(b["ehp_armor"])
    derived = boosted.derive(boosted.eft())
    assert derived.command_links == boosted.command_links
    boosted.set_command_links([])
    assert stats.compute(boosted)["ehp_armor"] == pytest.approx(a["ehp_armor"])
    with pytest.raises(FitError, match="Unknown command link"):
        plain.set_command_links([{"type": "laser_pointer"}])
    with pytest.raises(FitError, match="strength"):
        plain.set_command_links([{"type": "armor_all", "strength": 9}])


def test_ship_info():
    info = ships.ship_info("Vexor")
    assert info["group"] == "Cruiser"
    assert info["slots"]["high"] and info["slots"]["low"]
    assert info["fitting"]["powergrid"]["with_skills"] > info["fitting"]["powergrid"]["base"]
    assert info["fitting"]["rig_size"] == "medium"
    assert "Drone" in info["bonuses"]
    assert "Gallente Cruiser" in info["required_skills"]
    full = ships.ship_info("Vexor", "All 0", all_attributes=True)
    assert full["attributes"]["powerOutput"]["display"]
    assert full["attributes"]["powerOutput"]["with_skills"] == full["attributes"]["powerOutput"]["base"]
    with pytest.raises(FitError, match="not a ship"):
        ships.ship_info("Gyrostabilizer II")


def test_saving_a_pilot_recalculates_loaded_fits(rifter_eft, monkeypatch):
    from eve_fit_mcp import server
    monkeypatch.setattr(server, "_fits", {})
    pilots.save_custom("Rookie", base_level=0)
    h = FitHandle(rifter_eft, "Rookie")
    server._fits[1] = h
    server._fits[2] = FitHandle(rifter_eft, "All 5")
    before = stats.compute(h)["cpu_total"]
    r = server._refresh_pilot_fits("rookie", pilots.save_custom("Rookie", skills={"CPU Management": 5}))
    assert r["refreshed_fits"] == [1]
    assert stats.compute(h)["cpu_total"] > before  # no explicit recalc needed
