import os

import pytest

from eve_fit_mcp import explore, stats
from eve_fit_mcp.fitting import FitError, FitHandle

HURRICANE = """[Hurricane, test]
Gyrostabilizer II
Gyrostabilizer II
Gyrostabilizer II
Gyrostabilizer II
Nanofiber Internal Structure II
Overdrive Injector System II

50MN Microwarpdrive II
Warp Disruptor II
Tracking Computer II, Optimal Range Script
Tracking Computer II, Optimal Range Script

720mm Howitzer Artillery II, Republic Fleet Phased Plasma M
720mm Howitzer Artillery II, Republic Fleet Phased Plasma M
720mm Howitzer Artillery II, Republic Fleet Phased Plasma M
720mm Howitzer Artillery II, Republic Fleet Phased Plasma M
720mm Howitzer Artillery II, Republic Fleet Phased Plasma M
720mm Howitzer Artillery II, Republic Fleet Phased Plasma M
[Empty High slot]

Medium Polycarbon Engine Housing II
Medium Polycarbon Engine Housing II
Medium Ancillary Current Router I
"""


@pytest.fixture(scope="module")
def hurricane():
    return FitHandle(HURRICANE)


def test_sweep_front_is_non_dominated(hurricane):
    before = hurricane.eft()
    r = explore.sweep(hurricane, {"low": ["Gyrostabilizer II", "Nanofiber Internal Structure II",
                                          "Overdrive Injector System II"]},
                      goals={"dps_total": "max", "max_speed": "max"}, include_eft=2)
    assert hurricane.eft() == before  # original untouched
    assert r["combinations"] == 28 and r["feasible"] == 28
    front = r["front"]
    dps = [f["stats"]["dps_total"] for f in front]
    spd = [f["stats"]["max_speed"] for f in front]
    assert dps == sorted(dps, reverse=True)
    assert spd == sorted(spd)  # along a 2-goal front, more DPS means less speed
    assert front[0]["modules"]["low"] == {"Gyrostabilizer II": 6}
    assert "eft" in front[0] and "eft" not in front[-1]


def test_sweep_constraints_and_rig_calibration(hurricane):
    r = explore.sweep(hurricane, {"rig": ["Medium Polycarbon Engine Housing II", "Medium Ancillary Current Router I",
                                          "Medium Projectile Collision Accelerator II"]},
                      goals={"dps_total": "max"}, constraints={"max_speed": {"min": 1800}})
    assert r["combinations"] < 10  # 3x T2 rigs (600 calibration) can't fit 400
    assert all(f["stats"]["max_speed"] >= 1800 for f in r["front"])
    with pytest.raises(FitError, match="exceeds max_evals"):
        explore.sweep(hurricane, {"low": ["Gyrostabilizer II", "Nanofiber Internal Structure II"]}, max_evals=3)


def test_compare_charges(hurricane):
    r = explore.compare_charges(hurricane, "high:0")
    names = [x["charge"] for x in r["results"]]
    assert "Tremor M" in names and len(r["slots"]) == 6
    assert any({"Republic Fleet EMP M", "Republic Fleet Fusion M"} <= set(g) for g in r["same_total_damage"])
    tremor = next(x for x in r["results"] if x["charge"] == "Tremor M")
    emp = next(x for x in r["results"] if x["charge"] == "Republic Fleet EMP M")
    assert tremor["weapon_optimal"] > emp["weapon_optimal"] * 2 and tremor["dps_total"] < emp["dps_total"]


def test_make_variant(hurricane):
    v, warnings = explore.make_variant(hurricane, [{"slot": "med:1", "item": "Stasis Webifier II"},
                                                   {"slot": "high:0", "charge": "Tremor M"},
                                                   {"slot": "low:5", "item": None}], name="variant")
    s = v.slots_by_key()
    assert s["med:1"]["item"] == "Stasis Webifier II"
    assert s["high:0"]["charge"] == "Tremor M" and s["high:1"]["charge"] != "Tremor M"
    assert s["low:5"]["item"] is None
    assert hurricane.slots_by_key()["med:1"]["item"] == "Warp Disruptor II"
    with pytest.raises(FitError):
        explore.make_variant(hurricane, [{"slot": "low:0", "item": "Stasis Webifier II"}])


def test_save_fit_writes_importable_xml(tmp_path, monkeypatch):
    from eve_fit_mcp import server
    from eve_fit_mcp.pyfa_bridge import P
    monkeypatch.setattr(server, "FITS_DIR", str(tmp_path))
    h = server._register(FitHandle(HURRICANE))
    out = server.save_fit(h.id, "Hurri test")
    assert out["eft_path"].endswith(".eft") and out["xml_path"].endswith(".xml")
    text = open(out["xml_path"], encoding="utf-8").read()
    kind, _, data = P.Port.importAuto(text, out["xml_path"])  # Pyfa's file-import path
    assert kind == "XML"
    assert len([m for m in data[0].modules if not m.isEmpty]) == 19


def test_skill_plan_minimum_and_evemon(tmp_path):
    import gzip
    import xml.etree.ElementTree as ET
    from eve_fit_mcp import skillplan
    from eve_fit_mcp.pyfa_bridge import P
    plan = skillplan.build([HURRICANE])
    assert plan["fitting_skills_added"] == {"Power Grid Management": 5, "Advanced Weapon Upgrades": 4}
    assert plan["verification"][0]["valid"] and not plan["verification"][0]["missing_skills"]
    files = skillplan.save(plan, "Test", str(tmp_path))
    xml = gzip.open(files["emp"]).read()
    assert xml == open(files["xml"], "rb").read()
    entries = list(ET.fromstring(xml).iter("entry"))
    assert len(entries) == len(plan["entries"])
    have = {}
    for e in entries:  # every prerequisite level precedes the skill that needs it
        sid, lvl = int(e.get("skillID")), int(e.get("level"))
        assert lvl == have.get(sid, 0) + 1
        for req, need in P.market.getItem(sid).requiredSkills.items():
            assert have.get(req.ID, 0) >= need
        have[sid] = lvl
    with pytest.raises(FitError, match="Unknown item"):
        skillplan.build([HURRICANE.replace("Gyrostabilizer II", "Gyrostabilizer Ultra")])
