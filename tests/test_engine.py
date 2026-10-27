import os

import pytest

from eve_fit_mcp import catalog, optimizer, stats
from eve_fit_mcp.fitting import FitError, FitHandle

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


@pytest.fixture
def rifter():
    return FitHandle(fixture("rifter.eft"))


def test_load_and_stats(rifter):
    s = stats.compute(rifter)
    assert s["ship"] == "Rifter"
    assert s["dps_total"] > 100
    assert s["ehp_total"] > 1000
    assert s["slots"]["low"]["total"] >= 3
    assert s["slots"]["high"]["used"] == 3


def test_eft_round_trip(rifter):
    again = FitHandle(rifter.eft())
    assert again.slots() == rifter.slots()
    assert stats.compute(again)["dps_total"] == pytest.approx(stats.compute(rifter)["dps_total"])


def test_set_module_rules(rifter):
    with pytest.raises(FitError, match="med slot"):
        rifter.set_module("low:1", "Stasis Webifier II")
    with pytest.raises(FitError, match="does not fit"):
        rifter.set_module("low:3", "Damage Control II")  # max one per fit
    before = stats.compute(rifter)["dps_total"]
    rifter.set_module("low:1", None)
    assert stats.compute(rifter)["dps_total"] < before
    rifter.set_module("low:1", "Gyrostabilizer II")
    assert stats.compute(rifter)["dps_total"] == pytest.approx(before)


def test_charge_kept_and_swapped(rifter):
    rifter.set_module("high:0", "200mm AutoCannon I")
    assert rifter.slots_by_key()["high:0"]["charge"] == "EMP S"
    rifter.set_module("high:0", "200mm AutoCannon I", charge="Republic Fleet Phased Plasma S")
    assert rifter.slots_by_key()["high:0"]["charge"] == "Republic Fleet Phased Plasma S"


def test_candidates(rifter):
    names = [i.typeName for i in catalog.module_candidates(rifter, "low:1")]
    assert "Gyrostabilizer II" in names and "Gyrostabilizer I" in names
    assert not any("Officer" == catalog.meta_group(catalog.get_item(n)) for n in names)
    pooled = [i.typeName for i in catalog.module_candidates(rifter, "low:3", mode="pool",
                                                            pool=["Overdrive Injector System II", "Stasis Webifier II"])]
    assert pooled == ["Overdrive Injector System II"]  # web filtered out: wrong slot


def test_optimizer_improves_and_leaves_original(rifter):
    original = rifter.eft()
    res = optimizer.optimize(rifter, {"dps_total": 1.0}, pools={"low": ["Gyrostabilizer II"]},
                             max_meta_level=None, time_limit_s=60)
    assert rifter.eft() == original
    assert res["feasible"]
    assert res["stats"]["dps_total"] > stats.compute(rifter)["dps_total"]
    assert res["stats"]["cpu_used"] <= res["stats"]["cpu_total"]


VEXOR = """[Vexor, drones]
Drone Damage Amplifier II

Drone Navigation Computer II

Drone Link Augmentor I

Hobgoblin II x5
Hammerhead II x5
"""


def test_drones_launched_within_limits():
    h = FitHandle(VEXOR)
    s = stats.compute(h)
    assert s["dps_drone"] > 100
    assert s["drone_bandwidth_used"] <= s["drone_bandwidth_total"]
    active = {d.item.typeName: d.amountActive for d in h.fit.drones}
    assert active == {"Hammerhead II": 5, "Hobgoblin II": 0}
    again = FitHandle(h.eft())  # export re-sorts the bay; activation must not depend on order
    assert stats.compute(again)["dps_drone"] == pytest.approx(s["dps_drone"])
    assert stats.compute(h.clone())["dps_drone"] == pytest.approx(s["dps_drone"])
    # manual state survives cloning even when it differs from the default activation
    h.auto_drones = False  # manual drone states: stop automatic relaunching
    for d in h.fit.drones:
        d.amountActive = 5 if d.item.typeName == "Hobgoblin II" else 0
    h.recalc()
    light = stats.compute(h)["dps_drone"]
    assert light < s["dps_drone"]
    assert stats.compute(h.clone())["dps_drone"] == pytest.approx(light)
    docked = FitHandle(VEXOR, drones_active=False)
    assert stats.compute(docked)["dps_drone"] == 0


def test_optimizer_respects_constraints(rifter):
    res = optimizer.optimize(rifter, {"dps_total": 1.0}, constraints={"max_speed": {"min": 3500}},
                             pools={"low": ["Overdrive Injector System II", "Gyrostabilizer II"]}, time_limit_s=60)
    assert res["feasible"]
    assert res["stats"]["max_speed"] >= 3500


STORK_LINKS = """[Stork, links]
Federation Navy 400mm Steel Plates
Multispectrum Coating II

5MN Y-T8 Compact Microwarpdrive

Information Command Burst II, Electronic Superiority Charge
Skirmish Command Burst II, Rapid Deployment Charge
Skirmish Command Burst II, Rapid Deployment Charge

%s
"""


def test_command_burst_limit_needs_processor_rigs():
    bare = stats.compute(FitHandle(STORK_LINKS % ""))
    assert not bare["valid"]
    assert bare["command_bursts"] == 3 and bare["command_bursts_max"] == 1
    assert any("Command Burst modules online but this fit can only run 1" in i for i in bare["issues"])

    rigged = stats.compute(FitHandle(STORK_LINKS % "Small Command Processor I\nSmall Command Processor I"))
    assert rigged["valid"], rigged["issues"]
    assert rigged["command_bursts_max"] == 3
    notes = [i for i in rigged["issues"] if i.startswith("note: ")]
    assert any("Rapid Deployment Charge; the same buff doesn't stack" in n for n in notes)
    assert any("boost itself" in n for n in notes)


def test_own_rapid_deployment_boosts_own_speed():
    h = FitHandle(STORK_LINKS % "Small Command Processor I\nSmall Command Processor I")
    boosted = stats.compute(h)["max_speed"]
    for key in ("high:1", "high:2"):
        h.set_module(key, "Skirmish Command Burst II", charge=None)
    assert stats.compute(h)["max_speed"] < boosted * 0.9
