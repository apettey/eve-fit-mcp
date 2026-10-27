"""Counter-fit search: find the fit of a hull that does best in a fleet battle against a saved doctrine.

1. Calibrate: run every kill order with the starting fit, keep the winning orders per range.
2. Search: exhaustive sweep over rack pools (small spaces) or greedy optimisation (larger spaces), scoring
   each valid candidate as `count` copies (plus optional support ships) in fleet battles vs the doctrine.
3. Verify: full fleet battles (all kill orders compared) before and after, with the module changes, stat
   deltas and the battle insights - the reasoning behind the counter.
"""
import time

from . import explore, fleet, goals, optimizer, stats as stats_mod
from .errors import FitError
from .pyfa_bridge import P, lock

COMPARE_KEYS = ["dps_total", "volley", "ehp_total", "ehp_vs_em", "ehp_vs_thermal", "ehp_vs_kinetic",
                "ehp_vs_explosive", "tank_sustained", "weapon_optimal", "weapon_falloff", "max_speed",
                "signature", "lock_range", "scan_res", "sensor_strength", "cap_lasts_s"]


def _changes(before, after):
    from collections import Counter
    b, a = before.slots_by_key(), after.slots_by_key()

    def fitted(slots, k):
        return slots[k]["item"], slots[k]["charge"]

    # modules that only moved between slots of the same rack aren't changes
    removed, added = {}, {}
    for k in b:
        if k in a:
            rack = k.split(":")[0]
            removed.setdefault(rack, Counter())[fitted(b, k)] += 1
            added.setdefault(rack, Counter())[fitted(a, k)] += 1
    for rack in removed:
        removed[rack], added[rack] = removed[rack] - added[rack], added[rack] - removed[rack]
    out = []
    for k in b:
        rack = k.split(":")[0]
        if k in a and fitted(b, k) != fitted(a, k) and removed[rack][fitted(b, k)] and added[rack][fitted(a, k)]:
            removed[rack][fitted(b, k)] -= 1
            added[rack][fitted(a, k)] -= 1
            out.append({"slot": k, "from": b[k]["item"] + (", " + b[k]["charge"] if b[k]["charge"] else "")
                        if b[k]["item"] else None,
                        "to": a[k]["item"] + (", " + a[k]["charge"] if a[k]["charge"] else "") if a[k]["item"] else None})
    return out


def _battle_summary(r):
    out = []
    for d in r["by_distance"]:
        res = d["result"]
        out.append({"distance_km": d["distance_km"], "winner": res["winner"], "best_order": d["best_order"],
                    "duration_s": res["duration_s"], "survivors": res["survivors"],
                    "kill_order": res["our_kill_order"]})
    return out


def counter_fit(handle, doctrine, count=1, distances_km=(10,), racks=None, mode="sweep", locked=(),
                support=None, range_choice="best", constraints=None, max_evals=1500, time_limit_s=600,
                candidate_mode="variants", pools=None):
    P.load()
    if mode not in ("sweep", "greedy"):
        raise FitError("mode must be 'sweep' (exhaustive over racks pools) or 'greedy' (optimiser)")
    t0 = time.time()
    with lock():
        obj = fleet.FleetObjective(doctrine, count, distances_km, support, range_choice)
        orders = obj.calibrate(handle)
        key = goals.register_fleet_objective(obj)
        base_score = obj(handle)
        if mode == "sweep":
            if not racks:
                raise FitError("sweep mode needs racks, e.g. {'low': [...], 'med': [...]}")
            # score vs DPS vs EHP front: the best score plus the alternatives that trade one for another
            r = explore.sweep(handle, racks, goals={key: "max", "dps_total": "max", "ehp_total": "max"},
                              constraints=constraints, locked=locked, max_evals=max_evals, include_eft=50,
                              time_limit_s=time_limit_s, report=[key, "dps_total", "ehp_total", "max_speed"])
            if not r["front"]:
                raise FitError("no valid candidate met the constraints")
            ranked = sorted(r["front"], key=lambda f: -f["stats"][key])
            best_eft = ranked[0]["eft"]
            alternatives = [{"score": round(f["stats"][key], 4), "dps_total": round(f["stats"]["dps_total"]),
                             "ehp_total": round(f["stats"]["ehp_total"]), "modules": f["modules"]}
                            for f in ranked[:6]]
            search = {"mode": "sweep", "combinations": r["combinations"], "evaluated": r["evaluated"],
                      "valid": r["feasible"], "alternatives": alternatives,
                      "alternatives_note": "the score / DPS / EHP trade-off front, best score first"}
        else:
            r = optimizer.optimize(handle, {key: 1.0}, constraints, slots=list(racks) if racks else "all",
                                   mode=candidate_mode, pools=pools or racks, locked=list(locked),
                                   max_evals=max_evals, time_limit_s=time_limit_s, normalize=False)
            best_eft = r["eft"]
            search = {"mode": "greedy", "evaluations": r["evaluations"], "changes_made": r["changes"]}
        best = handle.derive(best_eft, name=(handle.name + " (counter %s)" % doctrine)[:60])
        best_score = obj(best)
        improved = best_score["score"] > base_score["score"] + 1e-4
        if not improved:  # nothing beat the starting fit: keep it rather than an arbitrary tie
            best.close()
            best = handle.clone(name=handle.name)
            best_score = base_score
        s0, s1 = stats_mod.compute(handle), stats_mod.compute(best, skills_check=True)
        ours = lambda h: [{"handle": h, "count": count, "label": "candidate"}] + list(support or [])
        before = fleet.battle(ours(handle), doctrine, distances_km)
        after = fleet.battle(ours(best), doctrine, distances_km)
        return {
            "handle": best,
            "doctrine": doctrine, "count": count, "distances_km": list(distances_km),
            "range_choice": range_choice, "calibrated_orders": {"%g" % d: list(o) for d, o in orders.items()},
            "score_before": base_score, "score_after": best_score, "improved": improved,
            "changes": _changes(handle, best),
            "stat_deltas": {k: round(s1[k] - s0[k], 2) for k in COMPARE_KEYS if abs(s1[k] - s0[k]) > 1e-6},
            "valid": s1["valid"], "issues": s1["issues"], "missing_skills": s1["missing_skills"],
            "battle_before": _battle_summary(before), "battle_after": _battle_summary(after),
            "insights_after": after["insights"],
            "eft": best.eft(), "search": search, "seconds": round(time.time() - t0, 1),
            "score_note": "fleet score: 1 for a win (+ up to 0.2 if fast) + share of enemy killed - share of "
                          "ours lost; range_choice 'best' = best engagement range, 'all' = average",
            "assumptions": fleet.ASSUMPTIONS,
        }
