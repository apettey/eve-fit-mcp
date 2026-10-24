import pytest

from eve_fit_mcp import ammo, explore, matchup
from eve_fit_mcp.fitting import FitError, FitHandle
from eve_fit_mcp.pyfa_bridge import P

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


KITER = """[Hurricane, kiter]
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

Medium Polycarbon Engine Housing II
Medium Polycarbon Engine Housing II
Medium Ancillary Current Router I
"""


@pytest.fixture(scope="module")
def nighthawk():
    return FitHandle(NIGHTHAWK)


def test_rage_is_max_paper_but_wrong_vs_small_fast_targets(nighthawk):
    # 15 km: outside the Nighthawk's own scram/web range, so the Basilisk keeps its MWD speed
    r = ammo.recommend(nighthawk, [NIGHTHAWK, {"ship": "Basilisk", "prop": "mwd"}, "battleship"], [15])
    assert r["max_paper_ammo"] == "Scourge Rage Heavy Assault Missile"
    by = {t["target"].split(" ")[0]: t for t in r["targets"]}
    assert by["Nighthawk"]["best"] == "Scourge Rage Heavy Assault Missile"          # big, slow-ish: Rage
    assert by["battleship"]["best"] == "Scourge Rage Heavy Assault Missile"
    basi = by["Basilisk"]
    assert "Rage" not in basi["best"]                                                 # small/fast: not Rage
    assert basi["loss_if_max_paper_ammo_pct"] > 20
    assert any("bare hull" in n for n in basi["target_notes"])
    with pytest.raises(FitError):
        ammo.recommend(nighthawk, ["moon"], [10])


def test_own_tackle_changes_application(nighthawk):
    basi = {"ship": "Basilisk", "prop": "mwd"}
    from eve_fit_mcp import application
    near = application.applied_dps(nighthawk, [basi], [5])["results"][0]
    assert near["tackle"]["scrammed"] and near["tackle"]["webs"] >= 1
    assert near["tackle"]["sig"] < 100            # scram switched the MWD off: no sig bloom
    far = application.applied_dps(nighthawk, [basi], [15])["results"][0]
    assert not far["tackle"]["scrammed"]
    assert near["applied_weapon_dps"] > far["applied_weapon_dps"] * 1.5
    off = application.applied_dps(nighthawk, [basi], [5], tackle=False)["results"][0]
    assert off["applied_weapon_dps"] == pytest.approx(far["applied_weapon_dps"], rel=0.01)
    stopped = application.applied_dps(nighthawk, [{"ship": "Basilisk", "prop": "mwd", "prop_state": "off"}],
                                      [15], tackle=False)["results"][0]
    assert stopped["target_sig"] < far["target_sig"]


def test_envelope_kiter_vs_brawler(nighthawk):
    from eve_fit_mcp.fitting import FitHandle
    kiter = FitHandle(KITER)
    env = matchup.envelope(kiter, NIGHTHAWK, [5, 30])
    assert 5 in env["they_win_at_km"] and 30 in env["we_win_at_km"]
    near = env["by_distance"][0]
    assert near["they_tackle_us"]["scrammed"]


def test_load_best_records_intent_in_notes_and_xml(nighthawk):
    h = nighthawk.clone()
    r = ammo.load_best(h, {"ship": "Basilisk", "prop": "mwd"}, 10)
    assert "Rage" not in r["loaded"]
    assert h.slots_by_key()["high:0"]["charge"] == r["loaded"]
    assert "best applied vs Basilisk" in h.fit.notes
    ammo.load_best(h, None)  # max paper DPS replaces the previous ammo note for this weapon
    assert h.fit.notes.count("Ammo (Heavy Assault Missile Launcher II)") == 1
    assert "maximum paper DPS" in h.fit.notes
    assert "maximum paper DPS" in h.clone().fit.notes                     # survives clone/derive
    assert "maximum paper DPS" in P.Port.exportXml([h.fit])                # becomes the EVE description


def test_matchup_mirror_and_counts(nighthawk):
    r = matchup.matchup(nighthawk, NIGHTHAWK, 10)
    assert 0.9 < r["ratio"] < 1.1  # same fit, same pilot (All 5) -> about even
    assert r["we_deal"]["ehp_vs_this_damage"] == pytest.approx(r["they_deal"]["ehp_vs_this_damage"], rel=0.02)
    two_v_one = matchup.matchup(nighthawk, NIGHTHAWK, 10, our_count=2, their_count=1)
    assert two_v_one["ratio"] > 2
    far = matchup.matchup(nighthawk, NIGHTHAWK, 100)
    assert "stalemate" in far["verdict"]
    with pytest.raises(FitError, match="enemy fit"):
        matchup.matchup(nighthawk, "battleship", 10)


def test_sweep_can_optimise_against_an_enemy(nighthawk, monkeypatch):
    enemy = FitHandle(NIGHTHAWK)
    from eve_fit_mcp import goals
    monkeypatch.setattr(goals, "ENEMY_RESOLVER", lambda fid: enemy)
    r = explore.sweep(nighthawk, {"low:3": ["Damage Control II", "Ballistic Control System II"]},
                      goals={"matchup:1@10": "max"}, include_eft=0)
    assert len(r["front"]) >= 1 and "matchup:1@10" in r["front"][0]["stats"]
