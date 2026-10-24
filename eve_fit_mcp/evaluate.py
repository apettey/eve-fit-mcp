"""Stateless fit evaluation: the contract eve-esi-mcp (and other servers) call.

Contract "eve-fit/evaluate_fit@1" (docs/INTEGRATION.md). Additive changes only within a major version:
  in:  eft, skills | pilot, implants, boosters, damage_profile, command_links, metrics
  out: {contract, valid, issues[], pilot_used, engine{}, stats{raw numbers}, units{}, missing_skills[]}
Never raises for bad input: problems come back as valid=false with issues.
"""
import re

from . import pilots, stats as stats_mod
from .errors import FitError
from .fitting import DEFAULT_DAMAGE_PROFILE, FitHandle
from .pyfa_bridge import P, lock

CONTRACT = "eve-fit/evaluate_fit@1"

DAMAGE_PRESETS = {
    "omni": (25, 25, 25, 25), "em": (100, 0, 0, 0), "thermal": (0, 100, 0, 0),
    "kinetic": (0, 0, 100, 0), "explosive": (0, 0, 0, 100),
}

# Keys of `stats` and their units. Resists are fractions 0-1 (0.5 = 50% resist).
UNITS = {
    "dps_total": "HP/s", "dps_weapon": "HP/s", "dps_drone": "HP/s", "volley": "HP",
    "weapon_optimal": "m", "weapon_falloff": "m", "weapon_reach": "m",
    "weapon_tracking": "Pyfa trackingSpeed attribute (turret tracking, higher = better)",
    "ehp_total": "HP", "ehp_shield": "HP", "ehp_armor": "HP", "ehp_hull": "HP", "hp_total": "HP",
    "resist_<layer>_<type>": "fraction 0-1", "resist_avg_<layer>": "fraction 0-1",
    "tank_sustained": "EHP/s", "tank_burst": "EHP/s", "shield_regen": "EHP/s", "remote_rep": "HP/s",
    "cap_capacity": "GJ", "cap_stable": "1 = stable, 0 = not", "cap_stable_pct": "% (0 when unstable)",
    "cap_lasts_s": "s (1e9 when stable)", "cap_delta": "GJ/s",
    "max_speed": "m/s", "align_time": "s", "signature": "m", "warp_speed": "AU/s",
    "lock_range": "m", "scan_res": "mm", "sensor_strength": "points", "max_targets": "count",
    "mining_yield": "m3/s",
    "cpu_used": "tf", "cpu_total": "tf", "cpu_free": "tf",
    "pg_used": "MW", "pg_total": "MW", "pg_free": "MW",
    "calibration_used": "points", "calibration_total": "points", "calibration_free": "points",
    "drone_bandwidth_used": "Mbit/s", "drone_bandwidth_total": "Mbit/s", "drone_bandwidth_free": "Mbit/s",
    "drone_bay_used": "m3", "drone_bay_total": "m3", "drone_bay_free": "m3",
    "turret_hardpoints_free": "count", "launcher_hardpoints_free": "count",
    "slots": "{rack: {used, total}} counts",
}
_NON_STATS = {"ship", "name", "pilot", "skill_level", "damage_profile", "command_links", "issues", "valid",
              "missing_skills"}


def engine_info():
    import config
    import eos.config
    return {"name": "eve-fit-mcp", "pyfa_version": getattr(config, "version", None),
            "gamedata_build": getattr(eos.config, "gamedata_version", None) or None,
            "gamedata_date": str(getattr(eos.config, "gamedata_date", "") or "") or None}


def _profile(p):
    if p is None:
        return DEFAULT_DAMAGE_PROFILE
    if isinstance(p, str):
        if p not in DAMAGE_PRESETS:
            raise FitError("damage_profile preset must be one of %s or [em, th, kin, exp]" % ", ".join(DAMAGE_PRESETS))
        return DAMAGE_PRESETS[p]
    return tuple(p)


_EFT_SKIP = re.compile(r"^\s*(\[.*\])?\s*$")


def unknown_eft_items(eft):
    """Names in the EFT that don't resolve to items (Pyfa silently drops those)."""
    P.load()
    lines = eft.strip().splitlines()
    unknown = []
    for line in lines[1:]:
        line = line.strip()
        if not line or _EFT_SKIP.match(line) or line.startswith("[Empty"):
            continue
        if re.match(r"^\[\d+\]", line) or re.match(r"^\s*\w[\w ]*:\s", line):  # mutation detail lines
            continue
        name = re.sub(r"\s*\[\d+\]\s*$", "", line)          # mutation reference
        name = re.sub(r"\s*/offline\s*$", "", name, flags=re.I)
        name = re.sub(r"\s+x\d+\s*$", "", name)              # drones/cargo amount
        parts = [p.strip() for p in name.split(",")]
        for part in parts[:2]:
            if part and P.market.getItem(part) is None:
                unknown.append(part)
    return unknown


def _add_implants(handle, implant_ids, booster_ids):
    fit = handle.fit
    issues = []
    # Applied by what the item really is, so a booster listed under implants still counts (with a note).
    for given, ids in (("implant", implant_ids), ("booster", booster_ids)):
        for tid in ids or ():
            item = P.market.getItem(int(tid))
            if item is None or not (item.isImplant or item.isBooster):
                issues.append("%s %s is not a known implant or booster (ignored)" % (given, tid))
                continue
            actual = "implant" if item.isImplant else "booster"
            if actual != given:
                issues.append("%s (%s) is a %s, applied as one" % (item.typeName, tid, actual))
            if item.isImplant:
                fit.implants.append(P.Implant(item))
            else:
                fit.boosters.append(P.Booster(item))
    if implant_ids or booster_ids:
        handle.recalc()
    return issues


def evaluate(eft, skills=None, pilot=None, implants=None, boosters=None, damage_profile=None,
             command_links=None, metrics=None, drones_active=True):
    out = {"contract": CONTRACT, "valid": False, "issues": [], "pilot_used": None, "engine": None,
           "stats": {}, "units": UNITS, "missing_skills": []}
    try:
        P.load()
        out["engine"] = engine_info()
        with lock():
            if skills is not None and pilot:
                raise FitError("give either skills or pilot, not both")
            if skills is not None:
                who, out["pilot_used"] = pilots.inline(skills), "custom skills"
            else:
                who = pilots.canonical(pilot)
                out["pilot_used"] = who
            if not eft or not eft.strip().startswith("["):
                raise FitError("EFT must start with a [Ship, Fit name] header line")
            unknown = unknown_eft_items(eft)
            h = FitHandle(eft, who, _profile(damage_profile), drones_active, command_links)
            try:
                extra = _add_implants(h, implants, boosters)
                s = stats_mod.compute(h, skills_check=True)
            finally:
                h.close()
        wanted = set(metrics) if metrics else None
        out["stats"] = {k: v for k, v in s.items()
                        if k not in _NON_STATS and (wanted is None or k in wanted or k == "slots")}
        out["missing_skills"] = [
            {"item": item, "skill": skill, "required": need, "have": have}
            for item, sk in sorted(s["missing_skills"].items()) for skill, (have, need) in sorted(sk.items())]
        out["issues"] = list(s["issues"]) + extra + ["unknown item %r (dropped from the fit)" % n for n in unknown]
        out["valid"] = bool(s["valid"]) and not unknown
        out["ship"] = s["ship"]
    except FitError as e:
        out["issues"].append(str(e))
    except Exception as e:  # contract: never throw at the caller
        out["issues"].append("internal error: %s: %s" % (type(e).__name__, e))
    return out
