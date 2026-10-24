import math

import pytest

from eve_fit_mcp import doctrines, fleet, fleet_effects as fx
from eve_fit_mcp.fitting import FitHandle

from test_doctrines import BASI, NIGHTHAWK

NH_BURSTS = (NIGHTHAWK.replace("Shield Command Burst II", "Shield Command Burst II, Shield Harmonizing Charge")
             .replace("Information Command Burst II", "Information Command Burst II, Sensor Optimization Charge"))
BHAAL = """[Bhaalgorn, neut]
Damage Control II

Stasis Webifier II

Heavy Energy Neutralizer II
Heavy Energy Neutralizer II
Heavy Energy Neutralizer II
Heavy Energy Neutralizer II
"""
GEDDON = """[Armageddon, bomb]
Damage Control II

Large EMP Smartbomb II
Large EMP Smartbomb II
Large EMP Smartbomb II
Large EMP Smartbomb II
"""


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(doctrines, "DOCTRINE_DIR", str(tmp_path / "doctrines"))


def test_burst_links_from_loaded_charges():
    h = FitHandle(NH_BURSTS)
    assert fx.burst_strength(h) == {"shield": 4.0, "information": 4.0}
    assert {l["type"]: l["strength"] for l in fx.burst_links(h)} == {"shield_harmonizing": 4, "info_sensor": 4}
    assert fx.burst_links(FitHandle(NIGHTHAWK)) == []  # bursts without charges do nothing


def test_side_bursts_raise_resists(isolated):
    plain = fleet._members([{"eft": BASI, "label": "Basi"}], "All 5", None)[0]
    boosted = fleet._members([{"eft": BASI, "label": "Basi"}, {"eft": NH_BURSTS, "label": "NH"}], "All 5", None)
    basi = boosted[0]
    assert any(l["type"] == "shield_harmonizing" for l in basi.h.command_links)
    from eve_fit_mcp import stats
    assert stats.compute(basi.h)["ehp_total"] > stats.compute(plain.h)["ehp_total"]


def test_neuts_cap_out_logi(isolated):
    basi = FitHandle(BASI)
    neuts = fx.neut_modules(FitHandle(BHAAL))
    assert fx.cap_out_time(basi, neuts, 8000) < math.inf
    assert fx.cap_out_time(basi, neuts, 60000) == math.inf  # out of neut range
    doctrines.save("logi", [{"eft": NIGHTHAWK, "count": 2, "label": "Nighthawk"},
                            {"eft": BASI, "count": 2, "label": "Basilisk"}])
    r = fleet.battle([{"eft": BHAAL, "count": 2, "label": "Bhaal"}, {"eft": NIGHTHAWK, "count": 3, "label": "NH"}],
                     "logi", [8])
    res = r["by_distance"][0]["result"]
    assert "Basilisk" in res["capped_out"]["them"]
    assert any("cap out their" in i for i in r["insights"])


def test_smartbombs_pop_missiles(isolated):
    bombs = fx.smartbomb_modules(FitHandle(GEDDON))
    assert fx.missile_survival(5000, 2800, bombs) < fx.missile_survival(5000, 2800, bombs[:1]) < 1.0
    assert fx.missile_survival(5000, 2800, []) == 1.0
    assert fx.drone_lifetime(500, bombs) == pytest.approx(7.5)
    doctrines.save("hams", [{"eft": NIGHTHAWK, "count": 4, "label": "Nighthawk"}])
    r = fleet.battle([{"eft": GEDDON, "count": 3, "label": "Geddon"}], "hams", [5, 40])
    near, far = r["by_distance"]
    assert near["result"]["missiles_reaching"]["theirs"]["Nighthawk"] < 0.5
    assert far["result"]["missiles_reaching"]["theirs"]["Nighthawk"] < 1.0  # field covers the last 6 km
    assert any("smartbombs pop missiles" in i for i in r["insights"])
