"""Ammo choice by target: which charge applies the most damage to *this* enemy at *this* range.

Paper DPS favours the highest-damage ammo (e.g. Rage HAMs), but big explosion radius / slow explosion
velocity / short range can apply far worse to small or fast targets than a "weaker" faction or T1 charge.
recommend() ranks every loadable charge per target and distance; load_best() loads the winner into the
weapon group and records the intent in the fit's notes (exported as the EVE XML description).
"""
import re

from . import application, catalog
from .errors import FitError
from .pyfa_bridge import P, lock


def weapon_group(handle, slot=None):
    """Keys of identical weapons: the one in `slot`, or the primary (most DPS) group."""
    slots = handle.slots_by_key()
    if slot is None:
        best, best_dps = None, -1.0
        for key, pos_mod in ((k, handle._resolve(k)[1]) for k in slots):
            mod = pos_mod
            if mod.isEmpty or mod.hardpoint not in (P.FittingHardpoint.TURRET, P.FittingHardpoint.MISSILE):
                continue
            dps = sum(m.getDps().total for m in handle.fit.modules if not m.isEmpty and m.item.ID == mod.item.ID)
            if dps > best_dps:
                best, best_dps = key, dps
        if best is None:
            raise FitError("No damage-dealing turret or launcher on this fit")
        slot = best
    if slot not in slots or not slots[slot]["item"]:
        raise FitError("%s is empty or doesn't exist" % slot)
    item = slots[slot]["item"]
    return item, [k for k, s in slots.items() if s["item"] == item]


def _load(handle, keys, item, charge, state):
    for k in keys:
        handle.set_module(k, item, charge=charge, state=state, recalc=False, check=False)
    handle.recalc()


def recommend(handle, targets, distances_km=(10,), slot=None, charges=None,
              exclude_meta=catalog.DEFAULT_EXCLUDE_META, top=5, usable_only=True, tackle=True):
    P.load()
    with lock():
        work = handle.clone()
        resolved = [application.resolve_target(t) for t in targets]
        try:
            item, keys = weapon_group(work, slot)
            state = work.slots_by_key()[keys[0]]["state"]
            current = work.slots_by_key()[keys[0]]["charge"]
            names = charges or [c.typeName for c in catalog.charge_candidates(
                work, keys[0], exclude_meta, usable_only=usable_only)]
            if not names:
                raise FitError("%s can't load any charges" % item)
            per_charge = {}
            for name in names:
                try:
                    _load(work, keys, item, name, state)
                except FitError:
                    continue
                dmg = application.damage_map(work.fit)
                group = {m for m in dmg if getattr(m, "item", None) is not None and m.item.typeName == item
                         and not hasattr(m, "amountActive")}
                paper = sum(dmg[m].total for m in group)
                rng = max((float(m.maxRange or 0) for m in group), default=0.0)
                fo = max((float(m.falloff or 0) for m in group), default=0.0)
                applied = {}
                for t in resolved:
                    for d in distances_km:
                        applied[(t["name"], d)] = application.applied(work.fit, dmg, t, d, only=group,
                                                                      tackle=tackle)
                per_charge[name] = {"paper": paper, "range": rng, "falloff": fo, "applied": applied}
            if not per_charge:
                raise FitError("none of the charges could be loaded")
            max_paper = max(per_charge, key=lambda n: per_charge[n]["paper"])
            out_targets = []
            for t in resolved:
                for d in distances_km:
                    tk = application.tackle_effect(work.fit, t, d) if tackle else None
                    ranked = sorted(per_charge, key=lambda n: -per_charge[n]["applied"][(t["name"], d)])
                    best = ranked[0]
                    best_val = per_charge[best]["applied"][(t["name"], d)]
                    mp_val = per_charge[max_paper]["applied"][(t["name"], d)]
                    out_targets.append({
                        "target": t["name"], "distance_km": d,
                        "target_sig": round(t["sig"], 1), "target_speed": round(t["speed"], 1),
                        **({"tackled": {"speed": round(tk["speed"], 1), "sig": round(tk["sig"], 1),
                                        "scrammed": tk["scrammed"], "webs": tk["webs_in_range"]}} if tk else {}),
                        "best": best, "best_applied_dps": round(best_val, 1),
                        "max_paper_ammo": max_paper, "max_paper_ammo_applied_dps": round(mp_val, 1),
                        "loss_if_max_paper_ammo_pct": round(100 * (1 - mp_val / best_val), 1) if best_val else 0.0,
                        "ranking": [{"charge": n, "applied_dps": round(per_charge[n]["applied"][(t["name"], d)], 1),
                                     "paper_dps": round(per_charge[n]["paper"], 1),
                                     "applies_pct": round(100 * per_charge[n]["applied"][(t["name"], d)]
                                                          / per_charge[n]["paper"], 1) if per_charge[n]["paper"] else 0,
                                     "optimal_m": round(per_charge[n]["range"]), "falloff_m": round(per_charge[n]["falloff"])}
                                    for n in ranked[:top]],
                        **({"target_notes": t["notes"]} if t["notes"] else {})})
            return {"weapon": item, "slots": keys, "current": current, "max_paper_ammo": max_paper,
                    "max_paper_dps": round(per_charge[max_paper]["paper"], 1), "targets": out_targets,
                    "note": "weapon-group DPS only (drones excluded). Charges with equal applied DPS differ by "
                            "damage type: pick the target's weakest resist (resists are applied when known)."}
        finally:
            application.close_targets(resolved)
            work.close()


def _set_ammo_note(handle, item, text):
    notes = handle.fit.notes or ""
    lines = [l for l in notes.splitlines() if not l.startswith("Ammo (%s):" % item)]
    lines.append("Ammo (%s): %s" % (item, text))
    handle.fit.notes = "\n".join(l for l in lines if l.strip())


def load_best(handle, target=None, distance_km=10, slot=None, charges=None,
              exclude_meta=catalog.DEFAULT_EXCLUDE_META, usable_only=True, tackle=True):
    """Load the best charge into the weapon group and record why in the fit notes.

    target None = maximum paper DPS (e.g. structures, battleships); otherwise best applied vs that target."""
    with lock():
        if target is None:
            r = recommend(handle, ["structure"], [distance_km], slot, charges, exclude_meta, usable_only=usable_only)
            choice = r["max_paper_ammo"]
            reason = "maximum paper DPS (%s DPS) - best vs large/slow targets" % r["max_paper_dps"]
        else:
            r = recommend(handle, [target], [distance_km], slot, charges, exclude_meta, usable_only=usable_only,
                          tackle=tackle)
            t = r["targets"][0]
            choice = t["best"]
            tk = t.get("tackled")
            state = "" if not tk else (" scrammed+%d webs" % tk["webs"] if tk["scrammed"] else
                                       " %d webs" % tk["webs"] if tk["webs"] else " untackled")
            reason = "best applied vs %s at %g km%s (%s DPS; max-paper %s would apply %s)" % (
                t["target"], distance_km, state, t["best_applied_dps"], t["max_paper_ammo"],
                t["max_paper_ammo_applied_dps"])
        item, keys = weapon_group(handle, slot)
        state = handle.slots_by_key()[keys[0]]["state"]
        _load(handle, keys, item, choice, state)
        _set_ammo_note(handle, item, "%s - %s" % (choice, reason))
        return {"weapon": item, "slots": keys, "loaded": choice, "reason": reason, "notes": handle.fit.notes,
                "recommendation": r}
