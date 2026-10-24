"""Exploration tools distilled from what fitting agents kept hand-writing:

- sweep(): exhaustive search over racks with candidate pools, returning the trade-off (Pareto) front
  instead of one weighted answer.
- compare_charges(): every loadable ammo/script for a weapon group, side by side.
- make_variant(): apply a batch of slot changes to a copy of a fit (variants, refits, modes).
"""
import itertools
import time
from collections import Counter

from . import catalog, stats as stats_mod
from .errors import FitError
from .pyfa_bridge import P, lock

DEFAULT_REPORT = ["dps_total", "volley", "weapon_optimal", "weapon_falloff", "max_speed", "align_time",
                  "signature", "ehp_total", "tank_sustained", "cap_lasts_s", "cpu_used", "cpu_total",
                  "pg_used", "pg_total", "calibration_used", "calibration_total"]


def _rack_keys(handle, spec, locked):
    keys = [s["key"] for s in handle.slots()]
    if ":" in spec:
        if spec not in keys:
            raise FitError("No slot %r" % spec)
        out = [spec]
    else:
        out = [k for k in keys if k.startswith(spec + ":")]
        if not out:
            raise FitError("This fit has no %r slots" % spec)
    return [k for k in out if k not in locked]


def _calibration(name):
    a = catalog.get_item(name).attributes.get("upgradeCost")
    return float(a.value) if a is not None else 0.0


def _meets(s, constraints):
    for k, c in (constraints or {}).items():
        if isinstance(c, (int, float)):
            c = {"min": c}
        v = s.get(k)
        if v is None:
            raise FitError("Unknown metric %r" % k)
        if "min" in c and v < c["min"] - 1e-9:
            return False
        if "max" in c and v > c["max"] + 1e-9:
            return False
    return True


def _dominates(a, b, goals):
    better = False
    for k, d in goals.items():
        av, bv = a[k], b[k]
        if d == "max":
            if av < bv - 1e-9:
                return False
            better |= av > bv + 1e-9
        else:
            if av > bv + 1e-9:
                return False
            better |= av < bv - 1e-9
    return better


def sweep(handle, racks, goals=None, constraints=None, report=None, locked=(), max_evals=20000,
          include_eft=5, time_limit_s=300):
    """Try every combination of the given pools and return the non-dominated fits.

    racks: {"low": [names], "rig": [names], "med:2": [names], ...}. A rack name fills all its slots
      (order doesn't matter, so combinations-with-replacement); a slot key fills that one slot.
      Use null in a pool for "leave empty".
    goals: {"dps_total": "max", "max_speed": "max", "align_time": "min"} (default: dps_total max).
    constraints: {"metric": {"min"|"max": x}} - must hold (fitting validity is always required).
    Works on a clone; the original fit is untouched.
    """
    P.load()
    goals = goals or {"dps_total": "max"}
    for d in goals.values():
        if d not in ("max", "min"):
            raise FitError("goal directions must be 'max' or 'min'")
    report = list(dict.fromkeys(list(goals) + list(constraints or {}) + list(report or DEFAULT_REPORT)))
    t0 = time.time()
    with lock():
        work = handle.clone()
        try:
            groups = []  # (keys, list of combos)
            for spec, pool in racks.items():
                keys = _rack_keys(work, spec, set(locked))
                if not keys:
                    continue
                pool = list(dict.fromkeys(pool))
                for name in pool:
                    if name is not None:
                        catalog.get_item(name)
                combos = list(itertools.combinations_with_replacement(pool, len(keys)))
                if spec.split(":")[0] == "rig":
                    cal_total = work.fit.ship.getModifiedItemAttr("upgradeCapacity") or 0
                    fixed = sum(_calibration(s["item"]) for s in work.slots()
                                if s["key"].startswith("rig:") and s["key"] not in keys and s["item"])
                    combos = [c for c in combos
                              if fixed + sum(_calibration(n) for n in c if n) <= cal_total + 1e-9]
                groups.append((keys, combos))
            total = 1
            for _, combos in groups:
                total *= max(len(combos), 1)
            if total > max_evals:
                raise FitError("%d combinations exceeds max_evals=%d; shrink pools or raise max_evals (about "
                               "1 s per 100-150 combinations)" % (total, max_evals))

            rows, evaluated, invalid, failed = [], 0, 0, 0
            for product in itertools.product(*(combos for _, combos in groups)):
                if time.time() - t0 > time_limit_s:
                    break
                try:
                    for (keys, _), combo in zip(groups, product):
                        for k, name in zip(keys, combo):
                            work.set_module(k, name, charge="keep", recalc=False, check=False)
                    work.recalc()
                except FitError:
                    failed += 1
                    continue
                s = stats_mod.compute(work)
                evaluated += 1
                if not s["valid"]:
                    invalid += 1
                    continue
                if not _meets(s, constraints):
                    continue
                row = {k: s[k] for k in report}
                row["_choice"] = {",".join(keys) if len(keys) == 1 else keys[0].split(":")[0]:
                                  dict(Counter(n or "(empty)" for n in combo))
                                  for (keys, _), combo in zip(groups, product)}
                row["_product"] = product
                rows.append(row)

            front = [r for r in rows if not any(_dominates(o, r, goals) for o in rows if o is not r)]
            first = next(iter(goals))
            front.sort(key=lambda r: -r[first] if goals[first] == "max" else r[first])
            seen, uniq = set(), []
            for r in front:
                sig = tuple(round(r[k], 2) for k in goals)
                if sig in seen:
                    continue
                seen.add(sig)
                uniq.append(r)
            out_rows = []
            for i, r in enumerate(uniq):
                product = r.pop("_product")
                row = {"modules": r.pop("_choice"),
                       "stats": {k: (round(v, 3) if isinstance(v, float) else v) for k, v in r.items()}}
                if i < include_eft:
                    for (keys, _), combo in zip(groups, product):
                        for k, name in zip(keys, combo):
                            work.set_module(k, name, charge="keep", recalc=False, check=False)
                    work.recalc()
                    row["eft"] = work.eft()
                out_rows.append(row)
            best = {}
            for k, d in goals.items():
                if uniq:
                    pick = max(range(len(uniq)), key=lambda i: out_rows[i]["stats"][k] * (1 if d == "max" else -1))
                    best[k] = pick
            return {"front": out_rows, "best_index_per_goal": best, "combinations": total,
                    "evaluated": evaluated, "invalid": invalid, "failed": failed, "feasible": len(rows),
                    "seconds": round(time.time() - t0, 1),
                    "note": "front = fits no other fit beats on every goal; pick by trade-off. eft included "
                            "for the first %d." % include_eft}
        finally:
            work.close()


def compare_charges(handle, slot, charges=None, exclude_meta=catalog.DEFAULT_EXCLUDE_META,
                    metrics=None):
    """Load each candidate charge into every module identical to the one in `slot` and report stats."""
    P.load()
    metrics = metrics or ["dps_total", "dps_weapon", "volley", "weapon_optimal", "weapon_falloff",
                          "weapon_tracking", "cap_lasts_s"]
    with lock():
        work = handle.clone()
        try:
            cur = work.slots_by_key()[slot]
            if not cur["item"]:
                raise FitError("%s is empty" % slot)
            group = [k for k, s in work.slots_by_key().items() if s["item"] == cur["item"]]
            names = charges or [c.typeName for c in catalog.charge_candidates(work, slot, exclude_meta)]
            rows = []
            for name in names:
                try:
                    for k in group:
                        work.set_module(k, cur["item"], charge=name, state=cur["state"], recalc=False, check=False)
                    work.recalc()
                except FitError as e:
                    rows.append({"charge": name, "error": str(e)})
                    continue
                s = stats_mod.compute(work)
                item = catalog.get_item(name)
                dmg = {d: float(item.attributes[a].value) for a, d in (("emDamage", "em"), ("thermalDamage", "thermal"),
                                                                      ("kineticDamage", "kinetic"),
                                                                      ("explosiveDamage", "explosive"))
                       if a in item.attributes and item.attributes[a].value}
                rows.append({"charge": name, "damage_split": dmg,
                             **{k: round(s[k], 3) if isinstance(s[k], float) else s[k] for k in metrics}})
            rows.sort(key=lambda r: -(r.get("dps_total") or 0))
            # flag charges whose total damage equals another's (they differ only in damage type)
            by_dps = {}
            for r in rows:
                if "dps_total" in r:
                    by_dps.setdefault(round(r["dps_total"], 1), []).append(r["charge"])
            same = [v for v in by_dps.values() if len(v) > 1]
            return {"weapon": cur["item"], "slots": group, "current": cur["charge"], "results": rows,
                    "same_total_damage": same,
                    "note": "paper numbers vs a stationary target; consider range (optimal/falloff) and "
                            "the target's resists when choosing."}
        finally:
            work.close()


def make_variant(handle, changes, name=None):
    """Copy the fit and apply changes: [{"slot": "med:2", "item": "Covert Ops Cloaking Device II"},
    {"slot": "high:0", "charge": "Scourge Rage Heavy Assault Missile"}, {"slot": "low:1", "item": null}].
    Returns (new_handle, warnings)."""
    new = handle.clone(name=name)
    warnings = []
    try:
        for ch in changes:
            key = ch.get("slot")
            if not key:
                raise FitError("each change needs a 'slot'")
            cur = new.slots_by_key().get(key)
            if cur is None:
                raise FitError("No slot %r" % key)
            if "item" in ch:
                warnings += new.set_module(key, ch["item"], charge=ch.get("charge", "keep"),
                                           state=ch.get("state"), recalc=False)
            elif "charge" in ch or "state" in ch:
                if not cur["item"]:
                    raise FitError("%s is empty" % key)
                warnings += new.set_module(key, cur["item"], charge=ch.get("charge", "keep"),
                                           state=ch.get("state", cur["state"]), recalc=False)
        new.recalc()
    except Exception:
        new.close()
        raise
    return new, warnings
