import pytest

from eve_fit_mcp import application, explore, items, ships, stats
from eve_fit_mcp.fitting import FitError, FitHandle

LOKI = """[Loki, test]
Damage Control II
1600mm Rolled Tungsten Compact Plates
Multispectrum Energized Membrane II
Multispectrum Energized Membrane II
Ballistic Control System II
Ballistic Control System II

50MN Microwarpdrive II
Warp Scrambler II
Stasis Webifier II

Heavy Assault Missile Launcher II, Scourge Rage Heavy Assault Missile
Heavy Assault Missile Launcher II, Scourge Rage Heavy Assault Missile
Heavy Assault Missile Launcher II, Scourge Rage Heavy Assault Missile
Heavy Assault Missile Launcher II, Scourge Rage Heavy Assault Missile
Heavy Assault Missile Launcher II, Scourge Rage Heavy Assault Missile
Covert Ops Cloaking Device II
Sisters Core Probe Launcher, Sisters Core Scanner Probe
Small Energy Nosferatu II

Medium Trimark Armor Pump II
Medium Trimark Armor Pump II
Medium Trimark Armor Pump II

Loki Core - Augmented Nuclear Reactor
Loki Defensive - Covert Reconfiguration
Loki Offensive - Launcher Efficiency Configuration
Loki Propulsion - Intercalated Nanofibers

Hobgoblin II x2
Hammerhead II x3
"""


@pytest.fixture(scope="module")
def loki():
    return FitHandle(LOKI)


def test_extra_stats(loki):
    s = stats.compute(loki, noprop=True)
    assert s["valid"], s["issues"]
    assert s["max_speed_noprop"] < s["max_speed"] / 3          # MWD off
    assert s["signature_noprop"] < s["signature"]
    assert s["align_time_noprop"] < s["align_time"]
    assert abs(s["volley_weapon"] + s["volley_drone"] - s["volley"]) < 1e-6
    assert s["ehp_vs_em"] > s["ehp_vs_explosive"]              # armor tank: strong EM, weak explosive
    assert s["missile_explosion_radius"] > 0 and s["missile_explosion_velocity"] > 0
    assert stats.compute(loki)["max_speed"] == pytest.approx(s["max_speed"])  # prop state restored


def test_drones_relaunch_with_bandwidth(loki):
    h = loki.clone()
    h.set_module("high:7", "Interdiction Nullifier II")  # halves drone bandwidth
    s = stats.compute(h)
    assert s["valid"], s["issues"]
    assert s["drone_bandwidth_used"] <= s["drone_bandwidth_total"]
    h.set_module("high:7", "Small Energy Nosferatu II")
    assert sum(d.amountActive for d in h.fit.drones) == 5


def test_applied_dps(loki):
    # tackle off: this checks raw application (the Loki's own web would otherwise slow the cruiser)
    r = application.applied_dps(loki, ["structure", "cruiser_mwd", {"name": "far", "sig": 400, "speed": 0}],
                                distances_km=[10, 60], tackle=False)
    by = {(x["target"], x["distance_km"]): x for x in r["results"]}
    assert by[("structure", 10)]["applied_weapon_dps"] == pytest.approx(stats.compute(loki)["dps_weapon"], rel=0.01)
    assert by[("cruiser_mwd", 10)]["applied_weapon_dps"] < by[("structure", 10)]["applied_weapon_dps"] * 0.7
    assert by[("structure", 60)]["applied_weapon_dps"] == 0   # beyond HAM range
    with pytest.raises(FitError):
        application.applied_dps(loki, ["moon"])


def test_subsystem_layouts_and_swaps(loki):
    r = ships.subsystem_layouts("Loki", require={"subsystem": "Covert Reconfiguration", "launcher": 5})
    assert r["count"] >= 3 and all(l["launcher"] >= 5 for l in r["layouts"])
    info = ships.ship_info("Loki", subsystems=["Loki Core - Augmented Nuclear Reactor",
                                               "Loki Defensive - Covert Reconfiguration",
                                               "Loki Offensive - Launcher Efficiency Configuration",
                                               "Loki Propulsion - Intercalated Nanofibers"])
    assert info["slots"]["high"]["with_skills"] == 8
    assert "note" in ships.ship_info("Loki")
    h = loki.clone()
    h.set_module("subsystem:1", "Loki Defensive - Augmented Durability")
    s = stats.compute(h)
    assert s["slots"]["low"]["total"] == 7 and s["slots"]["high"]["total"] == 7
    assert not s["valid"]  # 8 highs no longer fit, and the covert cloak's CPU penalty returns


def test_fits_ship_for_t3c(loki):
    names = {i["name"] for i in items.find(slot="med", tags=["point"], fits_ship="Loki")["items"]}
    assert "Warp Scrambler II" in names
    assert items.find(query="Polycarbon", fits_ship="Loki")["items"][0]["size"] == "medium"
    with_layout = items.find(slot="high", hardpoint="launcher", size="medium", fit_handle=loki, limit=200)
    assert with_layout["total"] > 0


def test_sweep_on_noprop_goal(loki):
    r = explore.sweep(loki, {"low:5": ["Ballistic Control System II", "Nanofiber Internal Structure II"]},
                      goals={"dps_total": "max", "align_time_noprop": "min"})
    assert "align_time_noprop" in r["front"][0]["stats"]
