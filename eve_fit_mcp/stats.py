"""Flatten a calculated eos Fit into a dict of plain numbers.

Every numeric key here is usable as an optimizer objective or constraint.
"""
from . import pilots
from .fitting import SLOT_NAMES
from .pyfa_bridge import P, lock

LAYERS = ("shield", "armor", "hull")
DMG = ("em", "thermal", "kinetic", "explosive")
_RESONANCE = {
    "shield": ["shield%sDamageResonance" % d.capitalize() for d in DMG],
    "armor": ["armor%sDamageResonance" % d.capitalize() for d in DMG],
    "hull": ["%sDamageResonance" % d for d in DMG],
}

# Short descriptions, surfaced through the list_metrics tool
METRICS = {
    "dps_total": "Total DPS (weapons + drones)",
    "dps_weapon": "Weapon DPS",
    "dps_drone": "Drone DPS",
    "volley": "Alpha strike damage",
    "ehp_total": "Effective HP against the fit's damage profile",
    "ehp_shield": "Shield EHP", "ehp_armor": "Armor EHP", "ehp_hull": "Hull EHP",
    "hp_total": "Raw HP",
    "tank_sustained": "Sustained EHP/s (local reps + passive shield regen), cap-limited",
    "tank_burst": "Burst EHP/s ignoring cap",
    "resist_avg_<layer>": "Mean resist of a layer (0-1)",
    "resist_<layer>_<type>": "Single resist (0-1)",
    "cap_stable": "1 if cap stable else 0",
    "cap_stable_pct": "Stable cap level % (0 if unstable)",
    "cap_lasts_s": "Seconds until cap empty (1e9 if stable)",
    "cap_delta": "Cap recharge minus usage, GJ/s",
    "max_speed": "Max velocity m/s (prop mod active)",
    "align_time": "Align time s (lower is better)",
    "signature": "Signature radius m (lower is better)",
    "warp_speed": "Warp speed AU/s",
    "lock_range": "Max targeting range m",
    "scan_res": "Scan resolution mm",
    "sensor_strength": "Sensor strength",
    "remote_rep": "Outgoing remote repair HP/s",
    "mining_yield": "Mining yield m3/s",
    "cpu_free": "Spare CPU tf", "pg_free": "Spare powergrid MW", "calibration_free": "Spare calibration",
}


def _a(fit, attr, default=0.0):
    v = fit.ship.getModifiedItemAttr(attr)
    return float(v) if v is not None else default


def compute(handle, skills_check=False):
    fit = handle.fit
    with lock():
        s = {}
        s["ship"] = handle.ship
        s["name"] = handle.name
        s["pilot"] = handle.pilot
        s["command_links"] = list(handle.command_links)
        s["damage_profile"] = dict(zip(DMG, handle.damage_profile))

        # Offense
        s["dps_total"] = float(fit.getTotalDps().total)
        s["dps_weapon"] = float(fit.getWeaponDps().total)
        s["dps_drone"] = float(fit.getDroneDps().total)
        s["volley"] = float(fit.getTotalVolley().total)

        # Defense
        ehp = fit.ehp or {k: 0.0 for k in LAYERS}
        hp = fit.hp or {k: 0.0 for k in LAYERS}
        for layer in LAYERS:
            s["ehp_" + layer] = float(ehp.get(layer, 0))
            for dmg, attr in zip(DMG, _RESONANCE[layer]):
                s["resist_%s_%s" % (layer, dmg)] = 1 - _a(fit, attr, 1.0)
            s["resist_avg_" + layer] = sum(s["resist_%s_%s" % (layer, d)] for d in DMG) / 4
        s["ehp_total"] = sum(s["ehp_" + l] for l in LAYERS)
        s["hp_total"] = float(sum(hp.get(l, 0) for l in LAYERS))
        sus = fit.effectiveSustainableTank or {}
        burst = fit.effectiveTank or {}
        regen = float(sus.get("passiveShield", 0) or 0)
        s["tank_sustained"] = float(sum((sus.get(l + "Repair") or 0) for l in LAYERS)) + regen
        s["tank_burst"] = float(sum((burst.get(l + "Repair") or 0) for l in LAYERS)) + regen
        s["shield_regen"] = regen
        rr = fit.getRemoteReps()
        s["remote_rep"] = float(rr.shield + rr.armor + rr.hull)

        # Capacitor
        s["cap_capacity"] = _a(fit, "capacitorCapacity")
        stable = bool(fit.capStable)
        s["cap_stable"] = 1.0 if stable else 0.0
        s["cap_stable_pct"] = float(fit.capState) if stable else 0.0
        s["cap_lasts_s"] = 1e9 if stable else float(fit.capState)
        s["cap_delta"] = float(fit.capDelta or 0)

        # Navigation / targeting
        s["max_speed"] = float(fit.maxSpeed or 0)
        s["align_time"] = float(fit.alignTime or 0)
        s["signature"] = _a(fit, "signatureRadius")
        s["warp_speed"] = float(fit.warpSpeed or 0)
        s["lock_range"] = float(fit.maxTargetRange or 0)
        s["scan_res"] = _a(fit, "scanResolution")
        s["sensor_strength"] = float(fit.scanStrength or 0)
        s["max_targets"] = float(fit.maxTargets or 0)
        s["mining_yield"] = float(fit.totalYield or 0)

        # Fitting resources
        res = {
            "cpu": (fit.cpuUsed, _a(fit, "cpuOutput")),
            "pg": (fit.pgUsed, _a(fit, "powerOutput")),
            "calibration": (fit.calibrationUsed, _a(fit, "upgradeCapacity")),
            "drone_bandwidth": (fit.droneBandwidthUsed, _a(fit, "droneBandwidth")),
            "drone_bay": (fit.droneBayUsed, _a(fit, "droneCapacity")),
        }
        issues = []
        for k, (used, total) in res.items():
            used = float(used or 0)
            s[k + "_used"], s[k + "_total"], s[k + "_free"] = used, total, total - used
            if used > total + 1e-6:
                issues.append("%s over by %.2f (%.2f / %.2f)" % (k, used - total, used, total))

        slots = {}
        for name, enum_name in SLOT_NAMES.items():
            # eos compares slot types with `is` against the raw int, so pass .value
            st = getattr(P.FittingSlot, enum_name).value
            total = int(fit.getNumSlots(st) or 0)
            used = fit.getSlotsUsed(st)
            if total or used:
                slots[name] = {"used": used, "total": total}
                if used > total:
                    issues.append("%d %s modules but only %d slots" % (used, name, total))
        s["slots"] = slots
        for hp_name, hp_enum in (("turret", P.FittingHardpoint.TURRET), ("launcher", P.FittingHardpoint.MISSILE)):
            free = fit.getHardpointsFree(hp_enum)
            s[hp_name + "_hardpoints_free"] = free
            if free < 0:
                issues.append("%d too many %s modules" % (-free, hp_name))

        for mod in fit.modules:
            if mod.isEmpty:
                continue
            if not mod.fits(fit):
                issues.append("%s cannot be fitted here (restriction/max group)" % mod.item.typeName)
            if mod.charge is not None and not mod.isValidCharge(mod.charge):
                issues.append("%s cannot load %s" % (mod.item.typeName, mod.charge.typeName))
            if mod.state == P.FittingModuleState.OFFLINE:
                issues.append("%s is offline" % mod.item.typeName)
        s["issues"] = issues
        s["valid"] = not any(not i.endswith("is offline") for i in issues)
        if skills_check:
            s["missing_skills"] = pilots.missing_skills(fit)
        return s


ROUND_SKIP = {"cap_lasts_s"}


def summarize(stats, keys=None):
    """Compact view for chat: rounded numbers, the most useful metrics first."""
    default = ["dps_total", "volley", "ehp_total", "tank_sustained", "cap_stable", "cap_stable_pct", "cap_lasts_s",
               "max_speed", "align_time", "signature", "lock_range", "scan_res",
               "cpu_used", "cpu_total", "pg_used", "pg_total", "calibration_used", "calibration_total"]
    out = {"ship": stats["ship"], "name": stats["name"], "pilot": stats["pilot"], "valid": stats["valid"],
           "issues": stats["issues"]}
    if stats.get("command_links"):
        out["command_links"] = stats["command_links"]
    if "missing_skills" in stats:
        out["missing_skills"] = stats["missing_skills"]
    for k in keys or default:
        v = stats.get(k)
        if isinstance(v, float):
            v = round(v, 3 if abs(v) < 10 else 1)
        out[k] = v
    out["resists"] = {l: [round(stats["resist_%s_%s" % (l, d)] * 100, 1) for d in DMG] for l in LAYERS}
    out["slots"] = stats["slots"]
    return out
