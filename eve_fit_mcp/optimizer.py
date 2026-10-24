"""Search over module/charge choices to maximise a weighted objective under constraints.

Scoring
-------
objective   {"dps_total": 1, "ehp_total": 1, "align_time": -0.5}
            With normalize=True (default) each metric is divided by its value on the starting fit, so
            weights mean "relative importance of a % change". Negative weight = minimise.
constraints {"cap_stable": {"min": 1}, "ehp_total": {"min": 8000}, "align_time": {"max": 6}}
            A bare number is shorthand for {"min": n}.
A fit must also be fittable (CPU/PG/calibration/slots/hardpoints) to count as feasible.
Any feasible fit beats any infeasible one; infeasible fits are ranked by how far off they are,
so the search can climb out of an over-CPU starting point.
"""
import itertools
import time

from . import catalog, stats as stats_mod
from .fitting import FitError, SLOT_ORDER
from .pyfa_bridge import P, lock

RESOURCE_KEYS = ("cpu", "pg", "calibration", "drone_bandwidth", "drone_bay")


def _norm_constraints(constraints):
    out = {}
    for k, v in (constraints or {}).items():
        if isinstance(v, (int, float)):
            v = {"min": float(v)}
        out[k] = {kk: float(vv) for kk, vv in v.items() if kk in ("min", "max")}
    return out


class Scorer:
    def __init__(self, objective, constraints, baseline, normalize=True):
        if not objective:
            raise FitError("objective must contain at least one metric weight")
        for k in list(objective) + list(constraints or {}):
            if k not in baseline or not isinstance(baseline[k], (int, float)):
                raise FitError("Unknown metric %r (see list_metrics)" % k)
        self.objective = {k: float(v) for k, v in objective.items()}
        self.constraints = _norm_constraints(constraints)
        self.scale = {}
        for k in self.objective:
            b = abs(float(baseline[k]))
            self.scale[k] = b if (normalize and b > 1e-9 and k != "cap_lasts_s") else 1.0

    def violation(self, s):
        v = 0.0
        for r in RESOURCE_KEYS:
            over = s[r + "_used"] - s[r + "_total"]
            if over > 1e-6:
                v += over / max(s[r + "_total"], 1.0)
        for issue in s["issues"]:
            if "cannot" in issue or "too many" in issue or "only" in issue:
                v += 1.0
        for k, c in self.constraints.items():
            val = s[k]
            if "min" in c and val < c["min"]:
                v += (c["min"] - val) / max(abs(c["min"]), 1.0)
            if "max" in c and val > c["max"]:
                v += (val - c["max"]) / max(abs(c["max"]), 1.0)
        return v

    def objective_value(self, s):
        return sum(w * s[k] / self.scale[k] for k, w in self.objective.items())

    def score(self, s):
        """Comparable tuple: (feasible, value)."""
        v = self.violation(s)
        if v > 0:
            return (0, -v)
        return (1, self.objective_value(s))


class Search:
    def __init__(self, handle, scorer, log, noprop=False):
        self.h = handle
        self.scorer = scorer
        self.noprop = noprop
        self.cache = {}
        self.evals = 0
        self.log = log

    def evaluate(self):
        sig = self.h.signature()
        hit = self.cache.get(sig)
        if hit is None:
            s = stats_mod.compute(self.h, noprop=self.noprop)
            hit = (self.scorer.score(s), s)
            self.cache[sig] = hit
            self.evals += 1
        return hit

    def apply(self, keys, item, charge="keep"):
        """Put `item` into every key; returns False (and reverts) if any placement is illegal."""
        saved = [(k, self.h.module_info(k)) for k in keys]
        try:
            for k in keys:
                self.h.set_module(k, item, charge=charge, recalc=False)
            self.h.recalc()
            return saved
        except FitError:
            self.restore(saved)
            return None

    def restore(self, saved):
        for k, info in saved:
            self.h.restore_module(k, info, recalc=False)
        self.h.recalc()


def _units(handle, keys, group_identical):
    """Group keys that should change together (identical weapons share one decision)."""
    slots = {s["key"]: s for s in handle.slots()}
    units, seen = [], set()
    for k in keys:
        if k in seen:
            continue
        unit = [k]
        s = slots[k]
        if group_identical and s["item"] and s["charge"]:
            unit = [k2 for k2 in keys if slots[k2]["item"] == s["item"] and k2.split(":")[0] == k.split(":")[0]]
        seen.update(unit)
        units.append(unit)
    return units


def _select_keys(handle, slots):
    all_keys = [s["key"] for s in handle.slots()]
    if not slots or slots == "all" or slots == ["all"]:
        return all_keys
    out = []
    for spec in slots:
        if ":" in spec:
            if spec not in all_keys:
                raise FitError("No slot %r" % spec)
            out.append(spec)
        elif spec in SLOT_ORDER:
            out.extend(k for k in all_keys if k.startswith(spec + ":"))
        else:
            raise FitError("slot spec %r must be a key like low:0 or a rack name (%s)" % (spec, ", ".join(SLOT_ORDER)))
    return list(dict.fromkeys(out))


def _pool_for(pools, key):
    if not pools:
        return None
    return pools.get(key) or pools.get(key.split(":")[0])


def optimize(handle, objective, constraints=None, slots="all", mode="variants", pools=None,
             locked=None, allow_empty=False, optimize_charges=True, group_identical=True,
             strategy="greedy", max_passes=4, max_evals=5000, normalize=True,
             exclude_meta=catalog.DEFAULT_EXCLUDE_META, max_meta_level=None, time_limit_s=120,
             usable_only=True):
    """Optimise a *clone* of `handle`; the original is untouched. Returns a result dict."""
    t0 = time.time()
    work = handle.clone(name=handle.name)
    try:
        noprop = any(k.endswith("_noprop") for k in list(objective) + list(constraints or {}))
        baseline = stats_mod.compute(work, noprop=noprop)
        scorer = Scorer(objective, constraints, baseline, normalize)
        log = []
        search = Search(work, scorer, log, noprop)
        start_score, _ = search.evaluate()

        keys = [k for k in _select_keys(work, slots) if k not in set(locked or ())]
        units = _units(work, keys, group_identical)

        def candidates(unit):
            k = unit[0]
            items = catalog.module_candidates(work, k, mode=mode, pool=_pool_for(pools, k),
                                              exclude_meta=exclude_meta, max_meta_level=max_meta_level,
                                              usable_only=usable_only)
            names = [i.typeName for i in items]
            if allow_empty:
                names.append(None)
            return names

        if strategy == "exhaustive":
            _exhaustive(search, units, candidates, max_evals, log)
        elif strategy != "greedy":
            raise FitError("strategy must be greedy|exhaustive")

        # Greedy coordinate descent (also polishes an exhaustive result)
        for p in range(max_passes):
            improved = False
            for unit in units:
                if search.evals >= max_evals or time.time() - t0 > time_limit_s:
                    break
                best_score, _ = search.evaluate()
                current = work.slots_by_key()[unit[0]]["item"]
                best_item = current
                for name in candidates(unit):
                    if name == current:
                        continue
                    saved = search.apply(unit, name)
                    if saved is None:
                        continue
                    sc, _ = search.evaluate()
                    if sc > best_score:
                        best_score, best_item = sc, name
                    search.restore(saved)
                if best_item != current:
                    search.apply(unit, best_item)
                    log.append({"pass": p + 1, "slots": unit, "from": current, "to": best_item,
                                "score": list(best_score)})
                    improved = True
            if optimize_charges:
                improved |= _charges(search, keys, exclude_meta, max_meta_level, log, p + 1, usable_only)
            if not improved:
                break

        final_score, final_stats = search.evaluate()
        return {
            "eft": work.eft(),
            "feasible": final_score[0] == 1,
            "score": final_score[1],
            "start_score": start_score[1],
            "start_feasible": start_score[0] == 1,
            "stats": stats_mod.summarize(final_stats, extra=list(scorer.objective) + list(scorer.constraints)),
            "baseline": stats_mod.summarize(baseline, list(dict.fromkeys(
                list(scorer.objective) + list(scorer.constraints)))),
            "changes": log,
            "evaluations": search.evals,
            "seconds": round(time.time() - t0, 2),
            "note": "Local search: try different start fits, pools or strategy='exhaustive' to escape local optima.",
        }
    finally:
        work.close()


def _charges(search, keys, exclude_meta, max_meta_level, log, p, usable_only=True):
    """Try every valid charge on each group of identical charge-using modules."""
    h = search.h
    slots = h.slots_by_key()
    groups = {}
    for k in keys:
        s = slots[k]
        if s["item"] and catalog.charge_candidates(h, k, exclude_meta, max_meta_level, usable_only):
            groups.setdefault(s["item"], []).append(k)
    improved = False
    for item, unit in groups.items():
        cands = [c.typeName for c in catalog.charge_candidates(h, unit[0], exclude_meta, max_meta_level, usable_only)]
        if not cands:
            continue
        best_score, _ = search.evaluate()
        current = h.slots_by_key()[unit[0]]["charge"]
        best = current
        for c in cands:
            if c == current:
                continue
            saved = search.apply(unit, item, charge=c)
            if saved is None:
                continue
            sc, _ = search.evaluate()
            if sc > best_score:
                best_score, best = sc, c
            search.restore(saved)
        if best != current:
            search.apply(unit, item, charge=best)
            log.append({"pass": p, "slots": unit, "charge_from": current, "charge_to": best,
                        "score": list(best_score)})
            improved = True
    return improved


def _exhaustive(search, units, candidates, max_evals, log):
    """Try every combination per rack (order-insensitive), keep the best. Falls back if too large."""
    h = search.h
    by_rack = {}
    for u in units:
        by_rack.setdefault(u[0].split(":")[0], []).append(u)
    for rack, rack_units in by_rack.items():
        # interchangeable singleton slots in a rack share one pool
        pool = sorted({n for u in rack_units for n in candidates(u)}, key=lambda n: (n is None, n or ""))
        n = len(rack_units)
        total = _ncr(len(pool) + n - 1, n)
        if total > max_evals - search.evals:
            log.append({"rack": rack, "exhaustive": "skipped, %d combos exceeds budget" % total})
            continue
        best_score, _ = search.evaluate()
        best_combo = [h.slots_by_key()[u[0]]["item"] for u in rack_units]
        for combo in itertools.combinations_with_replacement(pool, n):
            saved_all = []
            ok = True
            for u, name in zip(rack_units, combo):
                saved = search.apply(u, name)
                if saved is None:
                    ok = False
                    break
                saved_all.extend(saved)
            if ok:
                sc, _ = search.evaluate()
                if sc > best_score:
                    best_score, best_combo = sc, list(combo)
            search.restore(list(reversed(saved_all)))
        for u, name in zip(rack_units, best_combo):
            search.apply(u, name)
        log.append({"rack": rack, "exhaustive_best": best_combo, "score": list(best_score),
                    "combos": total})


def _ncr(n, r):
    from math import comb
    return comb(n, r) if n >= r >= 0 else 0
