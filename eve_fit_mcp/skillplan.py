"""Minimum skill plans for one or more fits, with training times and EVEMon export.

What "minimum" means:
  1. every skill required by the ship, subsystems, fitted modules, loaded charges and drones
     (optionally cargo modules/charges too), with all prerequisites, at the required levels;
  2. plus the cheapest (in training time) extra levels of fitting skills that make every fit fit
     within CPU / powergrid. Found by greedy steps (best fitting-overrun reduction per minute of
     training), then pruned, then verified with the engine.
Plans can start from zero or from an existing pilot (only the missing levels are planned).
"""
import datetime
import gzip
import os
from xml.sax.saxutils import escape, quoteattr

from . import pilots
from .errors import FitError
from .fitting import FitHandle
from .pyfa_bridge import P, lock

SP_PER_LEVEL = {1: 250, 2: 1415, 3: 8000, 4: 45255, 5: 256000}  # cumulative, multiply by rank
ATTR_IDS = {164: "charisma", 165: "intelligence", 166: "memory", 167: "perception", 168: "willpower"}
DEFAULT_ATTRIBUTES = {"perception": 20, "willpower": 20, "intelligence": 20, "memory": 20, "charisma": 20}

# Skills that reduce module fitting costs or raise CPU/PG output: the fitting search space.
FITTING_SKILLS = ["CPU Management", "Power Grid Management", "Weapon Upgrades", "Advanced Weapon Upgrades",
                  "Electronics Upgrades", "Energy Grid Upgrades", "Shield Upgrades", "Capacitor Systems Operation",
                  "Jury Rigging"]


def _skill(name_or_id):
    item = P.market.getItem(name_or_id)
    if item is None or item.category.name != "Skill":
        raise FitError("Unknown skill %r" % (name_or_id,))
    return item


def rank(skill):
    return float(skill.attributes["skillTimeConstant"].value)


def sp_for(skill, from_level, to_level):
    r = rank(skill)
    return int(round(SP_PER_LEVEL.get(to_level, 0) * r - SP_PER_LEVEL.get(from_level, 0) * r))


def sp_per_minute(skill, attrs):
    pri = ATTR_IDS[int(skill.attributes["primaryAttribute"].value)]
    sec = ATTR_IDS[int(skill.attributes["secondaryAttribute"].value)]
    return attrs[pri] + attrs[sec] / 2.0


def minutes_for(skill, from_level, to_level, attrs):
    return sp_for(skill, from_level, to_level) / sp_per_minute(skill, attrs)


def _fmt_minutes(m):
    m = int(round(m))
    d, rem = divmod(m, 1440)
    h, mi = divmod(rem, 60)
    return ("%dd " % d if d else "") + ("%dh " % h if (h or d) else "") + "%dm" % mi


def _closure(levels):
    """Add prerequisites (recursively) to {skill_id: level}."""
    out = dict(levels)
    stack = list(levels.items())
    while stack:
        sid, lvl = stack.pop()
        if lvl <= 0:
            continue
        for req, need in _skill(sid).requiredSkills.items():
            if out.get(req.ID, 0) < need:
                out[req.ID] = need
                stack.append((req.ID, need))
    return out


def _requirements(handle, include_cargo):
    fit = handle.fit
    things = [fit.ship.item]
    for m in fit.modules:
        if not m.isEmpty:
            things.append(m.item)
            if m.charge is not None:
                things.append(m.charge)
    things += [d.item for d in fit.drones] + [f.item for f in fit.fighters]
    things += [i.item for i in fit.implants] + [b.item for b in fit.boosters]
    if include_cargo:
        for c in fit.cargo:
            if c.item.category.name in ("Module", "Charge", "Drone", "Subsystem"):
                things.append(c.item)
    need, why = {}, {}
    for item in things:
        for sk, lvl in item.requiredSkills.items():
            if need.get(sk.ID, 0) < lvl:
                need[sk.ID] = lvl
            why.setdefault(sk.ID, set()).add(item.typeName)
    return need, why


def _violation(handles, pilot_name):
    """Sum of relative CPU/PG/calibration overruns across fits (0 = all fit)."""
    total = 0.0
    for h in handles:
        h.set_pilot(pilot_name)
        f = h.fit
        for used, cap in ((f.cpuUsed, f.ship.getModifiedItemAttr("cpuOutput")),
                          (f.pgUsed, f.ship.getModifiedItemAttr("powerOutput")),
                          (f.calibrationUsed, f.ship.getModifiedItemAttr("upgradeCapacity"))):
            cap = float(cap or 0)
            over = float(used or 0) - cap
            if over > 1e-6:
                total += over / max(cap, 1.0)
    return total


def build(efts, start_pilot=None, attributes=None, include_cargo=False, fitting_skills=None):
    P.load()
    attrs = dict(DEFAULT_ATTRIBUTES, **(attributes or {}))
    with lock():
        from .evaluate import unknown_eft_items
        for e in efts:
            unknown = unknown_eft_items(e)
            if unknown:
                raise FitError("Unknown item(s) in fit: %s" % ", ".join(unknown))
        handles = [FitHandle(e, "All 5", drones_active=True) for e in efts]
        try:
            # starting point
            start = {}
            if start_pilot:
                char = pilots.character(start_pilot)
                for sk in P.Character.getSkillIDMap().values():
                    lvl = char.getSkill(sk).level or 0
                    if lvl:
                        start[sk.ID] = int(lvl)
            need, why = {}, {}
            for h in handles:
                n, w = _requirements(h, include_cargo)
                for sid, lvl in n.items():
                    need[sid] = max(need.get(sid, 0), lvl)
                for sid, items in w.items():
                    why.setdefault(sid, set()).update(items)
            direct = set(need)
            target = _closure({sid: max(lvl, start.get(sid, 0)) for sid, lvl in need.items()})
            for sid, lvl in start.items():
                target[sid] = max(target.get(sid, 0), lvl)

            def as_pilot(levels):
                return pilots.inline({str(k): v for k, v in levels.items() if v > 0})

            # greedy fitting-skill search
            base_target = dict(target)
            fitting = [_skill(n) for n in (fitting_skills or FITTING_SKILLS)]
            added = {}
            v = _violation(handles, as_pilot(target))
            steps = 0
            while v > 1e-9 and steps < 60:
                steps += 1
                best = None
                for sk in fitting:
                    cur = target.get(sk.ID, 0)
                    if cur >= 5:
                        continue
                    trial = _closure({**target, sk.ID: cur + 1})
                    cost = sum(minutes_for(_skill(s), target.get(s, 0), l, attrs)
                               for s, l in trial.items() if l > target.get(s, 0))
                    nv = _violation(handles, as_pilot(trial))
                    gain = v - nv
                    if gain <= 1e-9:
                        continue
                    score = gain / max(cost, 1.0)
                    if best is None or score > best[0]:
                        best = (score, trial, nv, sk.ID)
                if best is None:
                    break
                _, target, v, sid = best
                added[sid] = target[sid]
            if v > 1e-9:
                raise FitError("Even with all fitting skills at V these fits don't fit CPU/PG/calibration")
            if added:
                target, added = _cheapest_core(handles, base_target, target, added, start, attrs, as_pilot)
            # prune: drop fitting levels that turned out unnecessary (largest first)
            for sid in sorted(added, key=lambda s: -minutes_for(_skill(s), 0, target[s], attrs)):
                while target[sid] > max(need.get(sid, 0), start.get(sid, 0), _prereq_floor(target, sid)):
                    trial = {**target, sid: target[sid] - 1}
                    if _violation(handles, as_pilot(trial)) > 1e-9:
                        break
                    target = trial
            target = {s: l for s, l in target.items() if l > 0}

            # verification
            final_pilot = as_pilot(target)
            checks = []
            from . import stats as stats_mod
            for h in handles:
                h.set_pilot(final_pilot)
                s = stats_mod.compute(h, skills_check=True)
                checks.append({"fit": "%s, %s" % (s["ship"], s["name"]), "valid": s["valid"], "issues": s["issues"],
                               "missing_skills": s["missing_skills"],
                               "cpu": [round(s["cpu_used"], 1), round(s["cpu_total"], 1)],
                               "pg": [round(s["pg_used"], 1), round(s["pg_total"], 1)],
                               "calibration": [s["calibration_used"], s["calibration_total"]],
                               "dps_total": round(s["dps_total"], 1), "max_speed": round(s["max_speed"], 1)})

            entries = _order(target, start, direct, set(added))
            rows, total_sp, total_min = [], 0, 0.0
            for sid, lvl, kind in entries:
                sk = _skill(sid)
                sp = sp_for(sk, lvl - 1, lvl)
                mins = sp / sp_per_minute(sk, attrs)
                total_sp += sp
                total_min += mins
                rows.append({"skill": sk.typeName, "skill_id": sid, "level": lvl, "type": kind,
                             "group": sk.group.name, "rank": rank(sk), "sp": sp, "minutes": round(mins, 1),
                             "time": _fmt_minutes(mins),
                             "for": sorted(why.get(sid, ()))[:4] if kind == "Planned" else []})
            planned = {}
            for e in rows:
                planned[e["skill"]] = max(planned.get(e["skill"], 0), e["level"])
            return {"entries": rows, "skills": dict(sorted(planned.items())),
                    "fitting_skills_added": {_skill(s).typeName: target.get(s, 0) for s in added if s in target},
                    "total_sp": total_sp, "total_time": _fmt_minutes(total_min),
                    "total_minutes": round(total_min), "attributes": attrs, "start_pilot": start_pilot,
                    "verification": checks}
        finally:
            for h in handles:
                h.close()


CORE_FITTING = ["CPU Management", "Power Grid Management", "Weapon Upgrades", "Advanced Weapon Upgrades"]


def _cheapest_core(handles, base, greedy_target, greedy_added, start, attrs, as_pilot):
    """Greedy steps can overshoot (e.g. one rank-6 level where two rank-1 levels would do). Try every
    level combination of the core fitting skills (plus any others greedy used), cheapest first, and
    keep the first that fits. Bounded by the greedy result's cost."""
    import itertools

    def cost(levels):
        return sum(minutes_for(_skill(s), start.get(s, 0), l, attrs)
                   for s, l in levels.items() if l > start.get(s, 0))

    ids = list(dict.fromkeys([_skill(n).ID for n in CORE_FITTING] + list(greedy_added)))
    best_cost = cost(greedy_target)
    candidates = []
    for combo in itertools.product(*(range(base.get(s, 0), 6) for s in ids)):
        trial = dict(base)
        for s, l in zip(ids, combo):
            if l > trial.get(s, 0):
                trial[s] = l
        trial = _closure(trial)
        c = cost(trial)
        if c < best_cost - 1e-6:
            candidates.append((c, combo, trial))
    candidates.sort(key=lambda t: t[0])
    seen = set()
    for c, combo, trial in candidates:
        key = tuple(sorted(trial.items()))
        if key in seen:
            continue
        seen.add(key)
        if _violation(handles, as_pilot(trial)) <= 1e-9:
            return trial, {s: trial[s] for s in ids if trial.get(s, 0) > base.get(s, 0)}
    return greedy_target, greedy_added


def _prereq_floor(levels, sid):
    """Lowest level `sid` may have given other skills in `levels` that require it."""
    floor = 0
    for other, lvl in levels.items():
        if other == sid or lvl <= 0:
            continue
        for req, need in _skill(other).requiredSkills.items():
            if req.ID == sid:
                floor = max(floor, need)
    return floor


def _order(target, start, direct, fitting):
    """Per-level entries, prerequisites first, grouped by skill group then name."""
    have = dict(start)
    out = []

    def add(sid, lvl):
        if have.get(sid, 0) >= lvl:
            return
        for req, need in _skill(sid).requiredSkills.items():
            add(req.ID, need)
        for l in range(have.get(sid, 0) + 1, lvl + 1):
            kind = "Planned" if (sid in direct or sid in fitting) else "Prerequisite"
            out.append((sid, l, kind))
        have[sid] = lvl

    for sid in sorted(target, key=lambda s: (_skill(s).group.name, _skill(s).typeName)):
        add(sid, target[sid])
    return out


def to_evemon_xml(plan, name):
    lines = ['<?xml version="1.0" encoding="utf-8"?>',
             '<plan xmlns:xsd="http://www.w3.org/2001/XMLSchema" '
             'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" name=%s revision="4986">' % quoteattr(name),
             '  <sorting criteria="None" order="None" groupByPriority="false" />']
    for e in plan["entries"]:
        note = escape(name + (" - for " + ", ".join(e["for"]) if e["for"] else ""))
        lines.append('  <entry skillID="%d" skill=%s level="%d" priority="3" type="%s">'
                     % (e["skill_id"], quoteattr(e["skill"]), e["level"], e["type"]))
        lines.append("    <notes>%s</notes>" % note)
        lines.append("  </entry>")
    lines.append("</plan>")
    return "\n".join(lines) + "\n"


def to_markdown(plan, name):
    out = ["# %s - skill plan" % name, "",
           "Generated %s by eve-fit-mcp. Start: %s. Attributes: %s." % (
               datetime.date.today().isoformat(), plan["start_pilot"] or "no skills",
               ", ".join("%s %d" % (k, v) for k, v in plan["attributes"].items())),
           "", "**Total: %s SP, %s.**" % ("{:,}".format(plan["total_sp"]), plan["total_time"]), ""]
    if plan["fitting_skills_added"]:
        out.append("Fitting skills added so the fits fit CPU/PG: " + ", ".join(
            "%s %s" % (k, v) for k, v in plan["fitting_skills_added"].items()) + ".")
        out.append("")
    out += ["| # | Skill | Level | Type | SP | Time | Needed for |", "|---|---|---|---|---:|---:|---|"]
    for i, e in enumerate(plan["entries"], 1):
        out.append("| %d | %s | %d | %s | %s | %s | %s |" % (
            i, e["skill"], e["level"], e["type"], "{:,}".format(e["sp"]), e["time"], ", ".join(e["for"])))
    out += ["", "## Verification", ""]
    for c in plan["verification"]:
        out.append("- %s: valid=%s, CPU %s/%s, PG %s/%s, missing skills: %s" % (
            c["fit"], c["valid"], c["cpu"][0], c["cpu"][1], c["pg"][0], c["pg"][1], c["missing_skills"] or "none"))
    return "\n".join(out) + "\n"


def save(plan, name, directory):
    os.makedirs(directory, exist_ok=True)
    base = os.path.join(directory, name + " - skill plan")
    xml = to_evemon_xml(plan, name)
    with open(base + ".xml", "wb") as f:  # byte-identical to the .emp
        f.write(xml.encode("utf-8"))
    with gzip.open(base + ".emp", "wb") as f:
        f.write(xml.encode("utf-8"))
    with open(base + ".md", "w", encoding="utf-8") as f:
        f.write(to_markdown(plan, name))
    return {"emp": base + ".emp", "xml": base + ".xml", "markdown": base + ".md"}
