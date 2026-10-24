"""Applied DPS against moving targets, using Pyfa's own damage-graph maths.

Paper DPS assumes a perfect target. This applies missile explosion radius/velocity, turret tracking and
optimal/falloff, drone application and lock range against a target's signature, speed and resists at a
given distance. The attacker is stationary and the target moves perpendicular (worst case for turrets)
unless angles are given.

Targets can be:
  - a preset name: "cruiser_mwd", "battleship", ... (rough generic numbers)
  - a real ship: {"ship": "Basilisk", "prop": "mwd"|"ab"|"none", "speed_factor": 0.4 (e.g. webbed)}
    -> the bare hull (All 5 pilot) with a T2 prop module of the right size; resists are the hull's own
  - an actual enemy fit: {"eft": "[Nighthawk, ...]\\n...", "pilot": "All 5", "speed_factor": ...} or the EFT
    string itself -> its real signature, speed (prop as fitted) and tank-layer resists (Pyfa's auto mode)
"""
from .errors import FitError
from .pyfa_bridge import P, lock

# Rough, typical numbers; override any field per call. sig in m, speed in m/s, resists as fractions.
TARGET_PRESETS = {
    "structure":       {"sig": 10000, "speed": 0},
    "battleship":      {"sig": 400, "speed": 120},
    "battlecruiser":   {"sig": 280, "speed": 170},
    "cruiser":         {"sig": 130, "speed": 220},
    "cruiser_mwd":     {"sig": 650, "speed": 1600},
    "cruiser_webbed":  {"sig": 650, "speed": 640},
    "destroyer":       {"sig": 70, "speed": 280},
    "frigate":         {"sig": 35, "speed": 400},
    "frigate_mwd":     {"sig": 180, "speed": 3000},
}
PROP_BY_SIZE = {  # rigSize -> (MWD, AB)
    1: ("5MN Microwarpdrive II", "1MN Afterburner II"),
    2: ("50MN Microwarpdrive II", "10MN Afterburner II"),
    3: ("500MN Microwarpdrive II", "100MN Afterburner II"),
}


def _default_t3c_subsystems(hull):
    """A plausible combat layout for a bare T3C target: the first combination with mid slots."""
    from . import ships
    layouts = ships.subsystem_layouts(hull.typeName, require={"med": 1})["layouts"]
    if not layouts:
        raise FitError("No usable subsystem layout for %s; give the target as an EFT fit" % hull.typeName)
    best = max(layouts, key=lambda l: (l["med"], l["max_speed"]))
    return ["%s %s" % (hull.typeName, s) for s in best["subsystems"]]


def _ship_target_eft(spec):
    from .fitting import get_item
    hull = get_item(spec["ship"])
    if hull.category.name != "Ship":
        raise FitError("%s is not a ship" % hull.typeName)
    prop = spec.get("prop", "mwd")
    if prop not in ("mwd", "ab", "none"):
        raise FitError("prop must be mwd, ab or none")
    lines = ["[%s, target %s]" % (hull.typeName, prop)]
    notes = []
    subs = []
    if (hull.attributes.get("maxSubSystems") and hull.attributes["maxSubSystems"].value):
        subs = spec.get("subsystems") or _default_t3c_subsystems(hull)
        notes.append("T3C subsystems assumed: %s" % ", ".join(s.split(" - ")[-1] for s in subs))
    if prop != "none":
        size = int(hull.attributes["rigSize"].value) if "rigSize" in hull.attributes else 2
        mod = PROP_BY_SIZE.get(size, PROP_BY_SIZE[3])[0 if prop == "mwd" else 1]
        lines += ["", mod]
    if subs:
        lines += [""] + subs
    notes.append("bare hull + %s: resists are the hull's own (no hardeners) - pass the enemy's EFT for "
                 "doctrine-accurate resists" % ("no prop" if prop == "none" else prop.upper()))
    return "\n".join(lines), notes


def resolve_target(spec):
    """Normalise a target spec. Returns dict with name, sig, speed, resists, radius, notes, (handle)."""
    if isinstance(spec, str) and spec.strip().startswith("["):
        spec = {"eft": spec}
    if isinstance(spec, str):
        if spec not in TARGET_PRESETS:
            raise FitError("unknown target %r; use a preset (%s), {'ship': name, 'prop': 'mwd'} or an "
                           "EFT fit" % (spec, ", ".join(TARGET_PRESETS)))
        spec = {"name": spec, **TARGET_PRESETS[spec]}
    elif isinstance(spec, dict) and spec.get("preset"):
        base = TARGET_PRESETS.get(spec["preset"])
        if base is None:
            raise FitError("unknown target preset %r" % spec["preset"])
        spec = {"name": spec["preset"], **base, **{k: v for k, v in spec.items() if k != "preset"}}
    if not isinstance(spec, dict):
        raise FitError("target must be a preset name, an EFT string or an object")

    speed_factor = float(spec.get("speed_factor", 1.0))
    sig_factor = float(spec.get("sig_factor", 1.0))
    if "ship" in spec or "eft" in spec:
        from graphs.wrapper import TargetWrapper
        from .fitting import FitHandle
        if "eft" in spec:
            eft, notes = spec["eft"], []
        else:
            eft, notes = _ship_target_eft(spec)
        h = FitHandle(eft, spec.get("pilot"), drones_active=False)
        w = TargetWrapper(h.fit, 0, 0)
        em, th, kin, ex, layer = w.getResists(includeLayer=True)
        resists = spec.get("resists") or [em, th, kin, ex]
        prop = spec.get("prop", "as fitted") if "ship" in spec else "as fitted"
        name = spec.get("name") or ("%s (%s)" % (h.ship, prop if prop != "as fitted" else h.fit.name))
        if speed_factor != 1.0:
            name += " x%.2g speed" % speed_factor
        return {"name": name, "sig": float(w.getSigRadius()) * sig_factor,
                "speed": float(w.getMaxVelocity()) * speed_factor, "resists": [float(r) for r in resists],
                "radius": float(w.getRadius() or 0), "notes": notes + (["resists from %s layer" % layer] if layer else []),
                "_handle": h}
    res = spec.get("resists") or [0, 0, 0, 0]
    if len(res) != 4 or any(not 0 <= r < 1 for r in res):
        raise FitError("target resists must be 4 fractions 0-1 [em, th, kin, exp]")
    return {"name": spec.get("name", "custom"), "sig": float(spec["sig"]) * sig_factor,
            "speed": float(spec.get("speed", 0)) * speed_factor, "resists": [float(r) for r in res],
            "radius": float(spec.get("radius", 0) or 0), "notes": []}


def damage_map(fit):
    """{module/drone: DmgTypes} paper DPS of everything that deals damage (Pyfa's spool defaults)."""
    import eos.config
    from eos.utils.spoolSupport import SpoolOptions, SpoolType
    spool = SpoolOptions(SpoolType.SPOOL_SCALE, eos.config.settings["globalDefaultSpoolupPercentage"], False)
    dmg = {}
    for mod in fit.activeModulesIter():
        if mod.isDealingDamage():
            dmg[mod] = mod.getDps(spoolOptions=spool)
    for drone in fit.activeDronesIter():
        if drone.isDealingDamage():
            dmg[drone] = drone.getDps()
    return dmg


def applied(fit, dmg, target, distance_km, target_angle=90.0, only=None):
    """Applied DPS of `dmg` (optionally only the keys in `only`) against a resolved target."""
    from eos.saveddata.targetProfile import TargetProfile
    from graphs.data.fitDamageStats.calc.application import getApplicationPerKey
    from graphs.data.fitDamageStats.getter import applyDamage
    from graphs.wrapper import SourceWrapper, TargetWrapper
    if "_handle" in target:
        tgt = TargetWrapper(target["_handle"].fit, 0, 0)
    else:
        tgt = TargetWrapper(TargetProfile(*target["resists"], maxVelocity=target["speed"],
                                          signatureRadius=target["sig"], radius=target["radius"]), 0, 0)
    appl = getApplicationPerKey(src=SourceWrapper(fit, None), tgt=tgt, atkSpeed=0, atkAngle=0,
                                distance=float(distance_km) * 1000, tgtSpeed=target["speed"],
                                tgtAngle=target_angle, tgtSigRadius=target["sig"])
    keys = [k for k in dmg if only is None or k in only]
    return applyDamage({k: dmg[k] for k in keys}, {k: appl.get(k, 0) for k in keys},
                       tuple(target["resists"]), None).total


def close_targets(targets):
    for t in targets:
        if "_handle" in t:
            t["_handle"].close()


def applied_dps(handle, targets=("structure", "battleship", "cruiser", "cruiser_mwd", "frigate"),
                distances_km=(10,), target_angle=90.0):
    P.load()
    with lock():
        fit = handle.fit
        dmg = damage_map(fit)
        weapons = {k for k in dmg if not hasattr(k, "amountActive")}
        paper = sum(v.total for v in dmg.values())
        resolved = [resolve_target(t) for t in targets]
        try:
            rows = []
            for t in resolved:
                for d in distances_km:
                    total = applied(fit, dmg, t, d, target_angle)
                    wpn = applied(fit, dmg, t, d, target_angle, only=weapons)
                    rows.append({"target": t["name"], "distance_km": d, "applied_dps": round(total, 1),
                                 "applied_weapon_dps": round(wpn, 1), "applied_drone_dps": round(total - wpn, 1),
                                 "percent_of_paper": round(100 * total / paper, 1) if paper else 0.0,
                                 "target_sig": round(t["sig"], 1), "target_speed": round(t["speed"], 1),
                                 "target_resists": [round(r, 3) for r in t["resists"]],
                                 **({"target_notes": t["notes"]} if t["notes"] else {})})
        finally:
            close_targets(resolved)
        return {"paper_dps": round(paper, 1), "results": rows,
                "assumptions": "attacker stationary; target moving at angle %g deg to the line of fire at its "
                               "max speed; target resists applied; use speed_factor/sig_factor for webs or "
                               "painters" % target_angle}
