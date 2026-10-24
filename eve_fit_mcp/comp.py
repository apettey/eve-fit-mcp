"""Counter-comp recommender (phase 4): pick how many of each of our fits to bring against a saved doctrine.

Options are our candidate fits (loaded fit_ids, EFTs, or a saved friendly doctrine), each optionally with
min/max counts and an ISK cost. Constraints: number of pilots (or named pilots whose skills must cover the
hulls - checked by matching pilots to ships), role minimums (e.g. {"logistics": 2}), and a budget.

Search: exhaustive over all comps when few enough, otherwise local search (start from the best single-hull
and most-varied comps, move one ship at a time between options, keep improvements). Every comp is scored
with full fleet battles (all kill orders compared) at the given ranges.

Reasoning returned with the answer: the best comp's battle (winner, kill order, timeline), the runner-up
comps, the marginal value of one more / one fewer of each ship, and comp-vs-doctrine comparisons (reach,
tackle, logi throughput vs their damage, sensor strength vs their ECM).
"""
import itertools
import time

from . import doctrines, fleet, pilots as pilots_mod, stats as stats_mod
from .errors import FitError
from .fitting import FitHandle
from .pyfa_bridge import P, lock


class Option:
    def __init__(self, label, handle, cost=None, min_count=0, max_count=None, role=None):
        self.label, self.h = label, handle
        self.cost = float(cost) if cost is not None else None
        self.min, self.max = int(min_count or 0), (int(max_count) if max_count is not None else None)
        self.member = fleet.Member(label, handle, 1)
        self.role = role or self.member.primary
        self.stats = stats_mod.compute(handle)


def _options(spec, pilot):
    from . import server
    out = []
    if isinstance(spec, str):
        d = doctrines.load(spec)
        for e in d["ships"]:
            h, _ = doctrines.ship_handle(spec, e["label"])
            out.append(Option(e["label"], h, e.get("cost"), e.get("min", 0), e.get("max"), e.get("role_override")))
        return out
    for i, o in enumerate(spec):
        if "fit_id" in o:
            h = server._get(o["fit_id"]).clone()
        elif "eft" in o:
            h = FitHandle(o["eft"], o.get("pilot", pilot))
        else:
            raise FitError("each option needs fit_id or eft")
        out.append(Option(o.get("label") or h.ship, h, o.get("cost"), o.get("min", 0), o.get("max"), o.get("role")))
    labels = [o.label for o in out]
    if len(set(labels)) != len(labels):
        raise FitError("option labels must be unique")
    return out


def _flyable(options, pilot_names):
    """can[pilot][option] -> True if that pilot has every skill the option's fit needs."""
    can = {}
    for p in pilot_names:
        can[p] = {}
        for o in options:
            h = o.h.clone()
            h.set_pilot(p)
            can[p][o.label] = not pilots_mod.missing_skills(h.fit)
            h.close()
    return can


def _assignable(counts, options, pilot_names, can):
    """Bipartite matching: can every ship in `counts` get a distinct pilot who can fly it?"""
    ships = [o.label for o in options for _ in range(counts[o.label])]
    if len(ships) > len(pilot_names):
        return False
    match = {}

    def try_ship(i, seen):
        for p in pilot_names:
            if can[p][ships[i]] and p not in seen:
                seen.add(p)
                if p not in match or try_ship(match[p], seen):
                    match[p] = i
                    return True
        return False

    return all(try_ship(i, set()) for i in range(len(ships)))


class CompSearch:
    def __init__(self, options, enemy, n_pilots, distances_km, pilot_names=None, require=None, budget=None,
                 exact=True, range_choice="best"):
        self.options, self.enemy, self.n = options, enemy, n_pilots
        self.distances = [float(d) for d in distances_km]
        self.pilot_names, self.require, self.budget = pilot_names, dict(require or {}), budget
        self.exact, self.range_choice = exact, range_choice
        self.can = _flyable(options, pilot_names) if pilot_names else None
        self.cache = {}

    def feasible(self, counts):
        total = sum(counts.values())
        if total == 0 or total > self.n or (self.exact and total != self.n):
            return False
        for o in self.options:
            c = counts[o.label]
            if c < o.min or (o.max is not None and c > o.max):
                return False
        for role, need in self.require.items():
            have = sum(counts[o.label] for o in self.options if o.role == role or role in o.member.roles)
            if have < need:
                return False
        if self.budget is not None:
            if any(o.cost is None for o in self.options if counts[o.label]):
                raise FitError("budget given but some options have no cost")
            if sum(counts[o.label] * o.cost for o in self.options) > self.budget:
                return False
        if self.can is not None and not _assignable(counts, self.options, self.pilot_names, self.can):
            return False
        return True

    def evaluate(self, counts):
        key = tuple(counts[o.label] for o in self.options)
        if key in self.cache:
            return self.cache[key]
        us = []
        for o in self.options:
            if counts[o.label]:
                o.member.count = counts[o.label]
                us.append(o.member)
        per = {}
        for d in self.distances:
            tb = fleet.tables(us, self.enemy, d * 1000)
            best = None
            for s in fleet.STRATEGIES:
                r = fleet.simulate(us, self.enemy, d * 1000, s, "auto", tb)
                sc = fleet.fleet_score(r, us, self.enemy)
                if best is None or sc > best[0]:
                    best = (sc, s, r)
            per[d] = best
        scores = [v[0] for v in per.values()]
        score = max(scores) if self.range_choice == "best" else sum(scores) / len(scores)
        best_d = max(per, key=lambda d: per[d][0])
        res = {"score": score, "counts": dict(counts), "best_distance_km": best_d,
               "by_distance": {"%g" % d: {"score": v[0], "order": v[1], "winner": v[2]["winner"],
                                          "duration_s": v[2]["duration_s"], "kill_order": v[2]["our_kill_order"],
                                          "survivors": v[2]["survivors"], "timeline": v[2]["timeline"],
                                          "ehp_destroyed": v[2]["ehp_destroyed"]} for d, v in per.items()}}
        self.cache[key] = res
        return res

    def all_comps(self):
        labels = [o.label for o in self.options]
        totals = [self.n] if self.exact else range(1, self.n + 1)
        for total in totals:
            for combo in itertools.combinations_with_replacement(labels, total):
                counts = {l: combo.count(l) for l in labels}
                if self.feasible(counts):
                    yield counts

    def count_comps(self, cap):
        n = 0
        for _ in self.all_comps():
            n += 1
            if n > cap:
                return n
        return n

    def local(self, max_evals, time_limit_s):
        t0 = time.time()
        labels = [o.label for o in self.options]
        starts = []
        for l in labels:  # single-hull comps (filled up to constraints by the repair step)
            starts.append({x: (self.n if x == l else 0) for x in labels})
        even = {l: self.n // len(labels) for l in labels}
        for i in range(self.n - sum(even.values())):
            even[labels[i % len(labels)]] += 1
        starts.append(even)
        best = None
        for start in starts:
            cur = self._repair(start)
            if cur is None:
                continue
            cur_r = self.evaluate(cur)
            improved = True
            while improved and len(self.cache) < max_evals and time.time() - t0 < time_limit_s:
                improved = False
                for a, b in itertools.permutations(labels, 2):  # move one ship from a to b
                    if cur[a] == 0:
                        continue
                    nxt = dict(cur)
                    nxt[a] -= 1
                    nxt[b] += 1
                    if not self.feasible(nxt):
                        continue
                    r = self.evaluate(nxt)
                    if r["score"] > cur_r["score"] + 1e-6:
                        cur, cur_r, improved = nxt, r, True
                        break
            if best is None or cur_r["score"] > best["score"]:
                best = cur_r
        return best

    def _repair(self, counts):
        """Nudge a start comp into feasibility (min counts, role minimums) by moving ships around."""
        if self.feasible(counts):
            return counts
        labels = [o.label for o in self.options]
        for combo in itertools.islice(self.all_comps(), 2000):
            dist = sum(abs(combo[l] - counts[l]) for l in labels)
            if dist <= 2 * self.n:
                return combo
        return None


def recommend(options_spec, enemy_doctrine, pilots=10, distances_km=(10,), require=None, budget=None,
              exact=True, range_choice="best", pilot="All 5", max_evals=400, time_limit_s=600, top=5):
    P.load()
    t0 = time.time()
    with lock():
        pilot_names = list(pilots) if isinstance(pilots, (list, tuple)) else None
        n = len(pilot_names) if pilot_names else int(pilots)
        if n < 1:
            raise FitError("need at least one pilot")
        options = _options(options_spec, pilot)
        enemy = fleet.enemy_members(enemy_doctrine)
        try:
            search = CompSearch(options, enemy, n, distances_km, pilot_names, require, budget, exact, range_choice)
            n_comps = search.count_comps(max_evals)
            if n_comps == 0:
                detail = ""
                if search.can is not None:
                    detail = " Who can fly what: " + "; ".join(
                        "%s: %s" % (p, ", ".join(l for l, ok in row.items() if ok) or "nothing")
                        for p, row in search.can.items())
                    if exact:
                        detail += ". exact=false allows fewer ships than pilots."
                raise FitError("no comp satisfies the constraints (pilots/skills, role minimums, budget, "
                               "min/max).%s" % detail)
            if n_comps <= max_evals:
                method = "exhaustive"
                for counts in search.all_comps():
                    search.evaluate(counts)
            else:
                method = "local search"
                search.local(max_evals, time_limit_s)
            ranked = sorted(search.cache.values(), key=lambda r: -r["score"])
            best = ranked[0]
            marginal = _marginal(search, best["counts"])
            reasoning = _reasoning(best, options, enemy_doctrine, search)
            return {
                "enemy": enemy_doctrine, "pilots": n, "method": method, "comps_evaluated": len(search.cache),
                "best": {"comp": {k: v for k, v in best["counts"].items() if v}, "score": best["score"],
                         "best_distance_km": best["best_distance_km"],
                         "cost_isk": _cost(best["counts"], options),
                         "battle": best["by_distance"]},
                "runners_up": [{"comp": {k: v for k, v in r["counts"].items() if v}, "score": r["score"],
                                "best_distance_km": r["best_distance_km"]} for r in ranked[1:top]],
                "marginal_value": marginal, "reasoning": reasoning,
                "who_can_fly": search.can,
                "options": [{"label": o.label, "ship": o.h.ship, "role": o.role, "paper_dps": round(o.member.paper),
                             "remote_rep_hp_s": round(o.member.rr), "ehp": round(o.stats["ehp_total"]),
                             "weapon_reach_km": round(o.stats["weapon_reach"] / 1000, 1), "cost": o.cost}
                            for o in options],
                "seconds": round(time.time() - t0, 1),
                "assumptions": fleet.ASSUMPTIONS + "; options evaluated with pilot '%s'%s" % (
                    pilot, "; named pilots only constrain who can fly what" if pilot_names else ""),
            }
        finally:
            for o in options:
                o.h.close()


def _cost(counts, options):
    if any(o.cost is None for o in options if counts[o.label]):
        return None
    return sum(counts[o.label] * o.cost for o in options)


def _marginal(search, counts):
    """Score change from swapping one ship of each option for one of each other (and +1/-1 if not exact)."""
    base = search.evaluate(counts)["score"]
    out = []
    for o in search.options:
        for p in search.options:
            if o is p or counts[o.label] == 0:
                continue
            nxt = dict(counts)
            nxt[o.label] -= 1
            nxt[p.label] += 1
            if not search.feasible(nxt):
                continue
            r = search.evaluate(nxt)
            out.append({"swap": "1 %s -> 1 %s" % (o.label, p.label), "score_change": round(r["score"] - base, 3),
                        "winner_after": r["by_distance"]["%g" % r["best_distance_km"]]["winner"]})
    out.sort(key=lambda x: x["score_change"])
    return out


def _reasoning(best, options, enemy_doctrine, search):
    a = doctrines.analyze(enemy_doctrine)["comp"]
    counts = best["counts"]
    chosen = [o for o in options if counts[o.label]]
    lines = []
    b = best["by_distance"]["%g" % best["best_distance_km"]]
    lines.append("Best result at %g km: %s in %ss, kill order %s." % (
        best["best_distance_km"], b["winner"], b["duration_s"], " -> ".join(b["kill_order"]) or "none"))
    reach = max((o.stats["weapon_reach"] for o in chosen), default=0) / 1000
    if reach and a["max_weapon_reach_km"]:
        if reach > a["max_weapon_reach_km"] * 1.3:
            lines.append("Our reach (%.0f km) outranges theirs (%.0f km): fight outside %.0f km." % (
                reach, a["max_weapon_reach_km"], a["max_weapon_reach_km"]))
        elif reach < a["max_weapon_reach_km"] * 0.8:
            lines.append("They outrange us (%.0f vs %.0f km): we must close in fast or brawl." % (
                a["max_weapon_reach_km"], reach))
    our_rr = sum(o.member.rr * counts[o.label] for o in chosen)
    if our_rr:
        lines.append("Our logi: %.0f HP/s of reps vs their %.0f paper DPS." % (our_rr, a["total_paper_dps"]))
    elif a["total_paper_dps"]:
        lines.append("No logi in the comp: every point of their %.0f DPS sticks." % a["total_paper_dps"])
    if a["roles"].get("ewar"):
        weakest = min((o.member.sensor for o in chosen), default=0)
        lines.append("They bring ECM; our weakest sensor strength is %.1f - expect jams on that hull." % weakest)
    if a["total_remote_rep_hp_s"]:
        lines.append("Their logi reps %.0f HP/s: our comp's paper DPS is %.0f." % (
            a["total_remote_rep_hp_s"], sum(o.member.paper * counts[o.label] for o in chosen)))
    return lines
