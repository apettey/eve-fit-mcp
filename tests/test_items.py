import os

import pytest

from eve_fit_mcp import items, stats
from eve_fit_mcp.fitting import FitError, FitHandle

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def entry(name):
    return next(e for e in items.index() if e["name"] == name)


def test_index_has_tagged_items():
    idx = items.index()
    assert len(idx) > 5000
    gyro = entry("Gyrostabilizer II")
    assert gyro["slot"] == "low" and gyro["meta_group"] == "Tech II"
    assert {"damage", "turret-damage", "projectile"} <= set(gyro["tags"])
    rig = entry("Medium Polycarbon Engine Housing II")
    assert rig["slot"] == "rig" and rig["size"] == "medium"
    assert {"speed", "agility", "drawback:armor-hp"} <= set(rig["tags"])
    assert "ammo:long-range" in entry("Tremor M")["tags"]
    assert "ammo:short-range" in entry("Republic Fleet Phased Plasma M")["tags"]
    assert {"tackle", "point"} <= set(entry("Warp Disruptor II")["tags"])
    assert entry("720mm Howitzer Artillery II")["hardpoint"] == "turret"


def test_find_by_slot_tags_and_ship():
    r = items.find(slot="low", tags=["speed"], fits_ship="Hurricane", meta_groups=["Tech II"])
    names = {i["name"] for i in r["items"]}
    assert {"Overdrive Injector System II", "Nanofiber Internal Structure II"} <= names
    # rigs of the wrong size are excluded by fits_ship
    r = items.find(query="Polycarbon", fits_ship="Hurricane")
    assert r["items"] and all(i["size"] == "medium" for i in r["items"])


def test_find_attribute_filter_and_sort():
    r = items.find(slot="low", attributes={"Damage Modifier": {"min": 1.1}}, sort_by="damageMultiplier",
                   meta_groups=["Tech II"], limit=50)
    vals = [i["damageMultiplier"] for i in r["items"]]
    assert vals and all(v >= 1.1 for v in vals) and vals == sorted(vals, reverse=True)
    r = items.find(category="Module", slot="med", attributes={"cpu": {"max": 10}}, include_attributes=["cpu"])
    assert all(i["cpu"] <= 10 for i in r["items"])
    with pytest.raises(FitError, match="Unknown attribute"):
        items.find(attributes={"blorp": 1})


def test_charge_for_only_loadable():
    r = items.find(charge_for="720mm Howitzer Artillery II", sort_by="weaponRangeMultiplier", limit=200)
    names = [i["name"] for i in r["items"]]
    assert names[0] == "Tremor M"
    assert "Republic Fleet Phased Plasma M" in names
    assert not any(n.startswith(("Barrage", "Hail")) for n in names)  # autocannon-only ammo
    assert not any(n.endswith((" S", " L")) for n in names)  # wrong size


def test_item_info_and_fitted_values():
    info = items.item_info("720mm Howitzer Artillery II")
    assert info["slot"] == "high" and info["hardpoint"] == "turret"
    assert "Medium Artillery Specialization" in info["required_skills"]
    assert any(v["meta_group"] == "Tech I" for v in info["variations"])
    assert "Projectile Ammo" in info["charges"]["groups"]
    assert info["attributes"]["maxRange"]["display"]
    with open(os.path.join(FIXTURES, "rifter.eft"), encoding="utf-8") as f:
        h = FitHandle(f.read())
    _, mod = h._resolve("high:0")
    fitted = items.item_info("200mm AutoCannon II", module=mod)
    rof = fitted["attributes"]["speed"]
    assert rof.get("fitted") and rof["fitted"] < rof["value"]  # skills + gyro shorten cycle time
    assert fitted["loaded_charge"]["name"] == "EMP S"


def test_optimizer_result_has_full_stats():
    from eve_fit_mcp import optimizer
    with open(os.path.join(FIXTURES, "rifter.eft"), encoding="utf-8") as f:
        h = FitHandle(f.read())
    res = optimizer.optimize(h, {"dps_total": 1}, slots=["low"], time_limit_s=30)
    for k in ("volley", "signature", "weapon_optimal", "weapon_falloff", "max_speed"):
        assert res["stats"][k] is not None
