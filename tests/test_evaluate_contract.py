"""Contract tests for evaluate_fit ("eve-fit/evaluate_fit@1"), the API eve-esi-mcp depends on.

If one of these fails, the change is breaking for downstream callers: bump the contract version and
tell them, rather than editing the test.
"""
import json
import os
import sys

import anyio
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from eve_fit_mcp import evaluate, items, pilots

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
with open(os.path.join(ROOT, "tests", "fixtures", "rifter.eft"), encoding="utf-8") as f:
    RIFTER = f.read()
VALID_RIFTER = RIFTER.replace("Warp Scrambler II", "Initiated Compact Warp Scrambler")  # fits CPU

TOP_KEYS = {"contract", "valid", "issues", "pilot_used", "engine", "stats", "units", "missing_skills"}
STAT_KEYS = {"dps_total", "dps_weapon", "dps_drone", "volley", "weapon_optimal", "weapon_falloff",
             "weapon_tracking", "ehp_total", "ehp_shield", "ehp_armor", "ehp_hull", "tank_sustained",
             "cap_stable", "cap_lasts_s", "max_speed", "align_time", "signature", "lock_range", "scan_res",
             "cpu_used", "cpu_total", "pg_used", "pg_total", "calibration_used", "calibration_total", "slots"}


def test_shape_and_raw_numbers():
    r = evaluate.evaluate(VALID_RIFTER)
    assert TOP_KEYS <= set(r)
    assert r["contract"] == "eve-fit/evaluate_fit@1"
    assert r["valid"] is True, r["issues"]
    assert r["pilot_used"] == "All 5"
    assert {"pyfa_version", "gamedata_build"} <= set(r["engine"])
    assert STAT_KEYS <= set(r["stats"])
    for layer in ("shield", "armor", "hull"):
        for d in ("em", "thermal", "kinetic", "explosive"):
            v = r["stats"]["resist_%s_%s" % (layer, d)]
            assert 0 <= v < 1  # fractions, not percent
    assert all(isinstance(v, (int, float)) for k, v in r["stats"].items() if k != "slots")
    assert r["units"]["max_speed"] == "m/s"


def test_inline_skills_and_missing_list():
    r = evaluate.evaluate(VALID_RIFTER, skills={"3300": 1})  # Gunnery I only
    assert r["pilot_used"] == "custom skills"
    assert r["missing_skills"] and set(r["missing_skills"][0]) == {"item", "skill", "required", "have"}
    assert r["stats"]["dps_total"] < evaluate.evaluate(VALID_RIFTER)["stats"]["dps_total"]
    # ESI response shape is accepted as is
    esi = {"skills": [{"skill_id": 3300, "active_skill_level": 5, "trained_skill_level": 5}], "total_sp": 1}
    assert evaluate.evaluate(VALID_RIFTER, skills=esi)["pilot_used"] == "custom skills"
    assert pilots.normalize_skills(esi) == {3300: 5}


def test_implants_and_boosters_applied():
    nav = items.find(category="Implant", query="Rogue Navigation NN-605", limit=1)["items"][0]["id"]
    base = evaluate.evaluate(VALID_RIFTER)["stats"]["max_speed"]
    r = evaluate.evaluate(VALID_RIFTER, implants=[nav])
    assert r["stats"]["max_speed"] > base * 1.04
    drug = items.find(category="Implant", group="Booster", query="Rogue Agility", limit=1)["items"][0]["id"]
    r = evaluate.evaluate(VALID_RIFTER, implants=[drug], boosters=[123456789])
    assert any("is a booster, applied as one" in i for i in r["issues"])
    assert any("123456789" in i for i in r["issues"])


def test_mixed_weapons_range_is_primary_group():
    mixed = """[Hurricane Fleet Issue, mixed]
Gyrostabilizer II

Large Shield Extender II

Light Missile Launcher II, Caldari Navy Mjolnir Light Missile
Light Missile Launcher II, Caldari Navy Mjolnir Light Missile
720mm Howitzer Artillery II, Republic Fleet Depleted Uranium M
720mm Howitzer Artillery II, Republic Fleet Depleted Uranium M
720mm Howitzer Artillery II, Republic Fleet Depleted Uranium M
720mm Howitzer Artillery II, Republic Fleet Depleted Uranium M
"""
    r = evaluate.evaluate(mixed)
    groups = r["weapon_groups"]
    assert groups[0]["weapon"] == "720mm Howitzer Artillery II" and groups[0]["count"] == 4
    assert {g["weapon"] for g in groups} == {"720mm Howitzer Artillery II", "Light Missile Launcher II"}
    assert r["stats"]["weapon_optimal"] == pytest.approx(groups[0]["optimal"])
    assert r["stats"]["weapon_falloff"] > 10000  # artillery falloff, not the missiles' ~1 m
    assert "weapon_groups" not in r["stats"]


@pytest.mark.parametrize("eft, needle", [
    ("garbage", "header"),
    (RIFTER.replace("Gyrostabilizer II", "Gyrostabilizer Ultra"), "unknown item"),
    ("[NotAShip, x]\nGyrostabilizer II", ""),
])
def test_bad_input_never_raises(eft, needle):
    r = evaluate.evaluate(eft)
    assert r["valid"] is False and r["issues"]
    assert needle in " ".join(r["issues"])
    assert r["contract"] == "eve-fit/evaluate_fit@1"


def test_both_pilot_and_skills_is_an_issue_not_an_exception():
    r = evaluate.evaluate(VALID_RIFTER, skills={3300: 5}, pilot="All 5")
    assert r["valid"] is False and "either skills or pilot" in r["issues"][0]


async def _stdio():
    params = StdioServerParameters(command=sys.executable, args=["-m", "eve_fit_mcp.server"], cwd=ROOT)
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = await s.call_tool("evaluate_fit", {"eft": VALID_RIFTER, "skills": {"3300": 5, "3301": 5},
                                                     "metrics": ["dps_total", "max_speed"]})
            assert not res.is_error
            data = res.structured_content or json.loads(res.content[0].text)
            bad = await s.call_tool("evaluate_fit", {"eft": "nope"})
            assert not bad.is_error  # contract: bad fits are data, not tool errors
            bad_data = bad.structured_content or json.loads(bad.content[0].text)
            return data, bad_data


def test_stdio_like_eve_esi():
    data, bad = anyio.run(_stdio)
    assert data["contract"] == "eve-fit/evaluate_fit@1"
    assert set(data["stats"]) == {"dps_total", "max_speed", "slots"}
    assert bad["valid"] is False
