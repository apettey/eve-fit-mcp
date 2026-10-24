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
    "weapon_optimal": "Optimal range m (turrets) / max range (missiles) of the primary weapon group",
    "weapon_falloff": "Falloff m (turrets) of the primary weapon group",
    "weapon_reach": "Optimal + falloff m - a good kiting-range proxy",
    "weapon_tracking": "Turret tracking (Pyfa trackingSpeed attribute) of the primary weapon group",
    "applied:<target>@<km>": "sweep_fit only: applied DPS vs a target preset at a distance, e.g. applied:structure@60",
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
    "max_speed_noprop": "Max velocity m/s with prop mods off (cloaky travel, kiting w/o MWD)",
    "align_time_noprop": "Align time s with prop mods off", "signature_noprop": "Signature m with prop mods off",
    "volley_weapon": "Weapon-only alpha", "volley_drone": "Drone-only alpha",
    "ehp_vs_<type>": "EHP against pure em/thermal/kinetic/explosive damage",
    "missile_explosion_radius": "Largest missile explosion radius m (lower applies better)",
    "missile_explosion_velocity": "Slowest missile explosion velocity m/s (higher applies better)",
    "cargo_used": "Cargo m3 used", "cargo_total": "Cargo capacity m3",
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


def compute(handle, skills_check=False, noprop=False):
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
        s["volley_weapon"] = float(fit.getWeaponVolley().total)
        s["volley_drone"] = float(fit.getDroneVolley().total)

        # Weapon reach: worst (shortest) across fitted damage-dealing turrets/launchers
        # Weapon groups: identical weapon + charge. Range/tracking metrics describe the PRIMARY group
        # (most DPS), so a couple of utility launchers don't mask the main guns' range.
        groups = {}
        for mod in fit.modules:
            if mod.isEmpty or mod.hardpoint not in (P.FittingHardpoint.TURRET, P.FittingHardpoint.MISSILE):
                continue
            dps = mod.getDps().total
            if dps <= 0:
                continue
            key = (mod.item.typeName, mod.charge.typeName if mod.charge is not None else None)
            g = groups.setdefault(key, {"weapon": key[0], "charge": key[1], "count": 0, "dps": 0.0,
                                        "hardpoint": "turret" if mod.hardpoint == P.FittingHardpoint.TURRET
                                        else "launcher",
                                        "optimal": float(mod.maxRange or 0), "falloff": float(mod.falloff or 0)})
            g["count"] += 1
            g["dps"] += float(dps)
            if g["hardpoint"] == "turret":
                g["tracking"] = float(mod.getModifiedItemAttr("trackingSpeed") or 0)
            elif mod.charge is not None:
                g["explosion_radius"] = float(mod.getModifiedChargeAttr("aoeCloudSize") or 0)
                g["explosion_velocity"] = float(mod.getModifiedChargeAttr("aoeVelocity") or 0)
        ordered = sorted(groups.values(), key=lambda g: -g["dps"])
        s["weapon_groups"] = ordered
        main = ordered[0] if ordered else {}
        s["weapon_optimal"] = main.get("optimal", 0.0)
        s["weapon_falloff"] = main.get("falloff", 0.0)
        s["weapon_reach"] = s["weapon_optimal"] + s["weapon_falloff"]
        s["weapon_tracking"] = main.get("tracking", 0.0)
        launchers = [g for g in ordered if g["hardpoint"] == "launcher" and "explosion_radius" in g]
        s["missile_explosion_radius"] = launchers[0]["explosion_radius"] if launchers else 0.0
        s["missile_explosion_velocity"] = launchers[0]["explosion_velocity"] if launchers else 0.0

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
        for dmg in DMG:  # EHP against a single damage type, without changing the fit's profile
            s["ehp_vs_" + dmg] = float(sum(P.DamagePattern.oneType(dmg).calculateEhp(fit.ship).values()))
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
            "cargo": (fit.cargoBayUsed, _a(fit, "capacity")),
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
        for name, n in sorted(getattr(handle, "import_dropped", {}).items()):
            issues.append("%d x %s in the EFT could not be fitted (no free slot/hardpoint) and was dropped" % (n, name))
        for old, new in sorted(getattr(handle, "import_renamed", {}).items()):
            issues.append("note: %s no longer exists in game data; Pyfa uses %s instead" % (old, new))
        s["issues"] = issues
        s["valid"] = not any(not (i.endswith("is offline") or i.startswith("note: ")) for i in issues)
        if skills_check:
            s["missing_skills"] = pilots.missing_skills(fit)
        if noprop:
            s.update(_noprop(handle))
        return s


def _noprop(handle):
    """Speed/align/signature with prop modules (MWD/AB/MJD...) switched off, e.g. cloaky travel."""
    fit = handle.fit
    props = [m for m in fit.modules if not m.isEmpty and m.item.group.name == "Propulsion Module"
             and m.state >= P.FittingModuleState.ACTIVE]
    if not props:
        return {"max_speed_noprop": float(fit.maxSpeed or 0), "align_time_noprop": float(fit.alignTime or 0),
                "signature_noprop": _a(fit, "signatureRadius")}
    states = [m.state for m in props]
    try:
        for m in props:
            m.state = P.FittingModuleState.ONLINE
        handle.recalc()
        return {"max_speed_noprop": float(fit.maxSpeed or 0), "align_time_noprop": float(fit.alignTime or 0),
                "signature_noprop": _a(fit, "signatureRadius")}
    finally:
        for m, st in zip(props, states):
            m.state = st
        handle.recalc()


ROUND_SKIP = {"cap_lasts_s"}


DEFAULT_KEYS = [
    "dps_total", "volley", "weapon_optimal", "weapon_falloff", "ehp_total", "tank_sustained",
    "cap_stable", "cap_stable_pct", "cap_lasts_s", "max_speed", "align_time", "signature", "lock_range",
    "scan_res", "cpu_used", "cpu_total", "pg_used", "pg_total", "calibration_used", "calibration_total"]


def summarize(stats, keys=None, extra=None):
    """Compact view for chat: rounded numbers, the most useful metrics first.
    keys replaces the default metric list; extra appends to it."""
    default = list(dict.fromkeys(list(keys or DEFAULT_KEYS) + list(extra or ())))
    out = {"ship": stats["ship"], "name": stats["name"], "pilot": stats["pilot"], "valid": stats["valid"],
           "issues": stats["issues"]}
    if stats.get("command_links"):
        out["command_links"] = stats["command_links"]
    if "missing_skills" in stats:
        out["missing_skills"] = stats["missing_skills"]
    for k in default:
        v = stats.get(k)
        if isinstance(v, float):
            v = round(v, 3 if abs(v) < 10 else 1)
        out[k] = v
    out["resists"] = {l: [round(stats["resist_%s_%s" % (l, d)] * 100, 1) for d in DMG] for l in LAYERS}
    out["slots"] = stats["slots"]
    return out
