import pytest

from eve_fit_mcp import doctrines, stats
from eve_fit_mcp.fitting import FitError, FitHandle

NIGHTHAWK = """[Nighthawk, HAM Brawl]
Ballistic Control System II
Ballistic Control System II
Ballistic Control System II
Damage Control II

10MN Afterburner II
Large Shield Extender II
Large Shield Extender II
Multispectrum Shield Hardener II
Warp Scrambler II
Stasis Webifier II

Heavy Assault Missile Launcher II, Scourge Rage Heavy Assault Missile
Heavy Assault Missile Launcher II, Scourge Rage Heavy Assault Missile
Heavy Assault Missile Launcher II, Scourge Rage Heavy Assault Missile
Heavy Assault Missile Launcher II, Scourge Rage Heavy Assault Missile
Heavy Assault Missile Launcher II, Scourge Rage Heavy Assault Missile
Shield Command Burst II
Information Command Burst II

Medium Core Defense Field Extender II
Medium Core Defense Field Extender II
"""
BASI = """[Basilisk, Logi]
Damage Control II
Power Diagnostic System II

10MN Afterburner II
Large Shield Extender II
Multispectrum Shield Hardener II
Multispectrum Shield Hardener II
Large Cap Battery II

Large Remote Shield Booster II
Large Remote Shield Booster II
Large Remote Shield Booster II
Large Remote Shield Booster II
Large Remote Capacitor Transmitter II

Medium Core Defense Field Extender II
Medium Core Defense Field Extender II
"""
FALCON = """[Falcon, ECM]
Signal Distortion Amplifier II
Signal Distortion Amplifier II
Damage Control II

10MN Afterburner II
ECM - Multispectral Jammer II
ECM - Multispectral Jammer II
ECM - Multispectral Jammer II
Large Shield Extender II

Covert Ops Cloaking Device II

Medium Particle Dispersion Augmentor II
Medium Particle Dispersion Augmentor II
"""


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(doctrines, "DOCTRINE_DIR", str(tmp_path / "doctrines"))


def test_dropped_modules_are_flagged():
    h = FitHandle(BASI.replace("Damage Control II", "Damage Control II\nPower Diagnostic System II"))
    s = stats.compute(h)
    assert h.import_dropped == {"Power Diagnostic System II": 1}
    assert not s["valid"] and any("was dropped" in i for i in s["issues"])


def test_merge_fits_consensus():
    other = BASI.replace("Large Cap Battery II", "Multispectrum Shield Hardener II")
    m = doctrines.merge_fits([BASI, BASI, other])
    assert m["samples"] == 3 and m["ship"] == "Basilisk"
    assert m["confidence"]["Large Remote Shield Booster II"] == 1.0
    assert m["confidence"]["Large Cap Battery II"] < 1.0          # only 2 of 3 fits
    assert m["eft"].count("Large Remote Shield Booster II") == 4
    with pytest.raises(FitError, match="one hull"):
        doctrines.merge_fits([BASI, FALCON])


def test_save_analyze_roles_and_bundle_shape():
    bundle = {"name": "HAM brawl", "description": "test", "source": {"type": "pasted"},
              "ships": [{"ship": "Nighthawk", "label": "Nighthawk", "count": 8, "efts": [NIGHTHAWK, NIGHTHAWK]},
                        {"eft": BASI, "count": 3, "label": "Basilisk"},
                        {"eft": FALCON, "count": 2},
                        {"ship": "Sabre", "count": 1, "efts": [], "weapons_seen": ["Light Neutron Blaster II"]}]}
    r = doctrines.save(**bundle)
    roles = {s["label"]: s for s in r["ships"]}
    assert roles["Nighthawk"]["primary_role"] == "dps" and "tackle" in roles["Nighthawk"]["roles"]
    assert roles["Basilisk"]["primary_role"] == "logistics" and roles["Basilisk"]["remote_rep_hp_s"] > 0
    assert roles["Falcon"]["primary_role"] == "ewar"
    assert "Signal Distortion Amplifier II" not in roles["Falcon"]["role_modules"]["ewar"]
    assert roles["Sabre"]["fit_known"] is False and r["unknown_fits"] == ["Sabre"]
    comp = r["comp"]
    assert comp["ships"] == 14 and comp["roles"]["logistics"] == 3
    assert comp["total_remote_rep_hp_s"] == roles["Basilisk"]["remote_rep_hp_s"] * 3
    assert comp["max_tackle_km"] >= 9
    assert any("Logistics" in o for o in r["observations"])
    assert doctrines.list_all()[0]["comp"]["Nighthawk"] == 8
    with pytest.raises(FitError, match="dropped"):
        doctrines.save("bad", [{"eft": BASI.replace("Damage Control II",
                                                    "Damage Control II\nPower Diagnostic System II")}])
