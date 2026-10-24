"""Applied DPS against moving targets, using Pyfa's own damage-graph maths.

Paper DPS assumes a perfect target. This applies missile explosion radius/velocity, turret tracking and
optimal/falloff, drone application and lock range against a target's signature, speed and resists at a
given distance. The attacker is stationary and the target moves perpendicular (worst case for turrets)
unless angles are given.
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


def _target(spec):
    if isinstance(spec, str):
        if spec not in TARGET_PRESETS:
            raise FitError("unknown target preset %r; presets: %s" % (spec, ", ".join(TARGET_PRESETS)))
        spec = {"name": spec, **TARGET_PRESETS[spec]}
    elif isinstance(spec, dict) and spec.get("preset"):
        base = TARGET_PRESETS.get(spec["preset"])
        if base is None:
            raise FitError("unknown target preset %r" % spec["preset"])
        spec = {"name": spec["preset"], **base, **{k: v for k, v in spec.items() if k != "preset"}}
    res = spec.get("resists") or [0, 0, 0, 0]
    if len(res) != 4 or any(not 0 <= r < 1 for r in res):
        raise FitError("target resists must be 4 fractions 0-1 [em, th, kin, exp]")
    return {"name": spec.get("name", "custom"), "sig": float(spec["sig"]), "speed": float(spec.get("speed", 0)),
            "resists": [float(r) for r in res], "radius": float(spec.get("radius", 0) or 0)}


def applied_dps(handle, targets=("structure", "battleship", "cruiser", "cruiser_mwd", "frigate"),
                distances_km=(10,), target_angle=90.0):
    P.load()
    import eos.config
    from eos.saveddata.targetProfile import TargetProfile
    from graphs.data.fitDamageStats.calc.application import getApplicationPerKey
    from graphs.data.fitDamageStats.getter import applyDamage
    from graphs.wrapper import SourceWrapper, TargetWrapper
    from eos.utils.spoolSupport import SpoolOptions, SpoolType

    with lock():
        fit = handle.fit
        src = SourceWrapper(fit, None)
        spool = SpoolOptions(SpoolType.SPOOL_SCALE, eos.config.settings["globalDefaultSpoolupPercentage"], False)
        dmg = {}
        for mod in fit.activeModulesIter():
            if mod.isDealingDamage():
                dmg[mod] = mod.getDps(spoolOptions=spool)
        for drone in fit.activeDronesIter():
            if drone.isDealingDamage():
                dmg[drone] = drone.getDps()
        paper = sum(v.total for v in dmg.values())
        rows = []
        for spec in targets:
            t = _target(spec)
            prof = TargetProfile(*t["resists"], maxVelocity=t["speed"], signatureRadius=t["sig"],
                                 radius=t["radius"])
            tgt = TargetWrapper(prof, 0, 0)
            for d in distances_km:
                appl = getApplicationPerKey(src=src, tgt=tgt, atkSpeed=0, atkAngle=0, distance=float(d) * 1000,
                                            tgtSpeed=t["speed"], tgtAngle=target_angle, tgtSigRadius=t["sig"])
                total = applyDamage(dmgMap=dmg, applicationMap=appl, tgtResists=tuple(t["resists"]),
                                    tgtFullHp=None).total
                weapons = sum(applyDamage({k: v}, {k: appl.get(k, 0)}, tuple(t["resists"]), None).total
                              for k, v in dmg.items() if not hasattr(k, "amountActive"))
                rows.append({"target": t["name"], "distance_km": d, "applied_dps": round(total, 1),
                             "applied_weapon_dps": round(weapons, 1), "applied_drone_dps": round(total - weapons, 1),
                             "percent_of_paper": round(100 * total / paper, 1) if paper else 0.0,
                             "target_sig": t["sig"], "target_speed": t["speed"]})
        return {"paper_dps": round(paper, 1), "results": rows,
                "assumptions": "attacker stationary; target moving at angle %g deg to the line of fire; "
                               "target resists applied; no webs/painters unless reflected in the target's "
                               "speed/sig (e.g. preset cruiser_webbed)" % target_angle}
