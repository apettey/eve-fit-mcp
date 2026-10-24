"""Enemy (or friendly) doctrines: named comps of fits with counts, roles and a comp-level read-out.

A doctrine is stored in data/doctrines/<name>.json (private, gitignored):
  {"name", "description", "source", "created",
   "ships": [{"label", "ship", "count", "eft" | null, "pilot", "roles", "fit_known", "confidence", "samples"}]}

Ships come from pasted EFT, or from several killmail fits of the same hull merged into one consensus fit
(merge_fits: the most common modules per rack, with a confidence per module). Hulls seen only by type
(battle-report survivors) are kept with fit_known=false and evaluated as bare hulls, clearly flagged.
"""
import datetime
import json
import os
import re
from collections import Counter

from . import stats as stats_mod
from .errors import FitError
from .fitting import SLOT_ORDER, FitHandle, get_item
from .pyfa_bridge import DATA_DIR, P, lock

DOCTRINE_DIR = os.path.join(DATA_DIR, "doctrines")

ROLE_GROUPS = [
    ("logistics", ("Remote Shield Booster", "Remote Armor Repairer", "Remote Hull Repairer",
                   "Remote Capacitor Transmitter", "Mutadaptive Remote Armor Repairer")),
    ("ewar", ("ECM", "Burst Jammer", "Sensor Dampener", "Weapon Disruptor", "Target Painter")),
    ("cap_warfare", ("Energy Neutralizer", "Energy Nosferatu")),
    ("tackle", ("Warp Scrambler", "Stasis Web", "Stasis Grappler", "Warp Disrupt Field Generator",
                "Interdiction Sphere Launcher")),
    ("command", ("Command Burst",)),
]
DPS_ROLE_THRESHOLD = 150.0  # paper DPS above which a ship counts as a damage dealer
DMG = ("em", "thermal", "kinetic", "explosive")


def _path(name):
    safe = re.sub(r"[^\w\- ]+", "_", name).strip()
    if not safe:
        raise FitError("bad doctrine name %r" % name)
    return os.path.join(DOCTRINE_DIR, safe + ".json")


# ---- roles -------------------------------------------------------------------------------------

def detect_roles(handle, s=None):
    s = s or stats_mod.compute(handle)
    roles, details = [], {}
    for role, groups in ROLE_GROUPS:
        found = [m.item.typeName for m in handle.fit.modules
                 if not m.isEmpty and m.item.group.name in groups or
                 (not m.isEmpty and role == "logistics" and m.item.group.name.startswith("Ancillary Remote"))]
        if found:
            roles.append(role)
            details[role] = dict(Counter(found))
    if s["dps_total"] >= DPS_ROLE_THRESHOLD:
        roles.append("dps")
    order = ["logistics", "ewar", "dps", "cap_warfare", "command", "tackle"]
    primary = next((r for r in order if r in roles and not (r == "ewar" and "dps" in roles and
                                                              s["dps_total"] > 3 * DPS_ROLE_THRESHOLD)), None)
    return {"primary": primary or "support", "roles": roles, "modules": details}


# ---- merging killmail fits --------------------------------------------------------------------

def merge_fits(efts, pilot=None):
    """Consensus fit from several fits of the same hull (e.g. zKillboard losses).

    Per rack, modules are ranked by how often they appear (average count per fit) and slots filled in that
    order; each module's charge is the most common one. Returns {eft, confidence, samples, ship, issues}."""
    P.load()
    if not efts:
        raise FitError("merge_fits needs at least one EFT")
    with lock():
        handles = [FitHandle(e, pilot, drones_active=False) for e in efts]
        dropped = [h.import_dropped for h in handles if h.import_dropped]
        try:
            ships = {h.ship for h in handles}
            if len(ships) > 1:
                raise FitError("merge_fits needs fits of one hull, got: %s" % ", ".join(sorted(ships)))
            ship = ships.pop()
            n = len(handles)
            racks, charges, drones, slot_counts = {}, {}, Counter(), {}
            for h in handles:
                per_rack = {}
                for sl in h.slots():
                    rack = sl["key"].split(":")[0]
                    slot_counts.setdefault(rack, []).append(1)
                    if sl["item"]:
                        per_rack.setdefault(rack, Counter())[sl["item"]] += 1
                        if sl["charge"]:
                            charges.setdefault(sl["item"], Counter())[sl["charge"]] += 1
                for rack, c in per_rack.items():
                    racks.setdefault(rack, Counter()).update(c)
                for d in h.fit.drones:
                    drones[d.item.typeName] += d.amount
            lines, confidence = ["[%s, consensus of %d fits]" % (ship, n)], {}
            for rack in SLOT_ORDER[::-1] if False else ("low", "med", "high", "rig", "subsystem"):
                counts = racks.get(rack)
                if not counts:
                    continue
                n_slots = round(len(slot_counts.get(rack, [])) / n)
                chosen = []
                avg = {item: c / n for item, c in counts.items()}
                for item, a in sorted(avg.items(), key=lambda kv: -kv[1]):
                    take = min(max(1, round(a)), n_slots - len(chosen))
                    if take <= 0 or a < 0.25:
                        continue
                    chosen += [item] * take
                    confidence[item] = round(min(1.0, a / take), 2)
                lines.append("")
                for item in chosen:
                    ch = charges.get(item)
                    lines.append(item + (", " + ch.most_common(1)[0][0] if ch else ""))
            if drones:
                lines += ["", ""] + ["%s x%d" % (d, max(1, round(c / n))) for d, c in drones.most_common()]
            eft = "\n".join(lines)
        finally:
            for h in handles:
                h.close()
        merged = FitHandle(eft, pilot)
        s = stats_mod.compute(merged)
        merged.close()
        return {"ship": ship, "eft": eft, "samples": n, "confidence": confidence,
                "valid": s["valid"], "issues": s["issues"] + (
                    ["%d sample fit(s) had modules that don't fit the hull: %s" % (len(dropped), dropped)]
                    if dropped else []),
                "note": "modules with confidence < 1 were not on every sample fit"}


# ---- storage ----------------------------------------------------------------------------------

def _ship_entry(spec, pilot_default):
    """Normalise one ship spec: {eft} | {efts: [...]} (merged) | {ship} (fit unknown); count, label, pilot."""
    count = int(spec.get("count", 1))
    if count < 1:
        raise FitError("count must be >= 1")
    pilot = spec.get("pilot") or pilot_default
    entry = {"count": count, "pilot": pilot, "fit_known": True, "confidence": None, "samples": 1}
    if spec.get("efts"):
        merged = merge_fits(spec["efts"], pilot)
        entry.update(eft=merged["eft"], ship=merged["ship"], confidence=merged["confidence"],
                     samples=merged["samples"])
    elif spec.get("eft"):
        h = FitHandle(spec["eft"], pilot)
        if h.import_dropped:
            raise FitError("%s: %s don't fit the hull and would be dropped - fix the EFT first"
                           % (h.ship, ", ".join("%dx %s" % (n, k) for k, n in h.import_dropped.items())))
        entry.update(eft=h.eft(), ship=h.ship)
        h.close()
    elif spec.get("ship"):
        item = get_item(spec["ship"])
        if item.category.name != "Ship":
            raise FitError("%s is not a ship" % item.typeName)
        entry.update(eft=None, ship=item.typeName, fit_known=False)
    else:
        raise FitError("each ship needs eft, efts or ship")
    entry["label"] = spec.get("label") or entry["ship"]
    for extra in ("notes", "weapons_seen", "pilots_seen"):  # e.g. battle-report intel for unknown fits
        if spec.get(extra):
            entry[extra] = spec[extra]
    if spec.get("role"):
        entry["role_override"] = spec["role"]
    return entry


def save(name, ships, description="", source=None, pilot="All 5", replace=True):
    P.load()
    with lock():
        existing = load(name) if (not replace and os.path.isfile(_path(name))) else None
        entries = list(existing["ships"]) if existing else []
        for spec in ships:
            entry = _ship_entry(spec, pilot)
            entries = [e for e in entries if e["label"] != entry["label"]] + [entry]
        labels = [e["label"] for e in entries]
        if len(set(labels)) != len(labels):
            raise FitError("duplicate ship labels in doctrine: give each variant a label")
        doc = {"name": name, "description": description or (existing or {}).get("description", ""),
               "source": source or (existing or {}).get("source"),
               "created": (existing or {}).get("created") or datetime.datetime.now(
                   datetime.timezone.utc).isoformat(timespec="seconds"),
               "ships": entries}
        os.makedirs(DOCTRINE_DIR, exist_ok=True)
        with open(_path(name), "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=1)
        return analyze(name)


def load(name):
    p = _path(name)
    if not os.path.isfile(p):
        raise FitError("No doctrine %r; known: %s" % (name, ", ".join(d["name"] for d in list_all()) or "none"))
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def list_all():
    if not os.path.isdir(DOCTRINE_DIR):
        return []
    out = []
    for fn in sorted(os.listdir(DOCTRINE_DIR)):
        if fn.endswith(".json"):
            with open(os.path.join(DOCTRINE_DIR, fn), encoding="utf-8") as f:
                d = json.load(f)
            out.append({"name": d["name"], "ships": sum(s["count"] for s in d["ships"]),
                        "comp": {s["label"]: s["count"] for s in d["ships"]}, "source": d.get("source")})
    return out


def delete(name):
    os.remove(_path(load(name)["name"]))


def ship_handle(name, label):
    """FitHandle for one ship of a doctrine (bare hull if the fit is unknown)."""
    d = load(name)
    e = next((s for s in d["ships"] if s["label"].lower() == label.lower()), None)
    if e is None:
        raise FitError("No ship %r in %s; ships: %s" % (label, name, ", ".join(s["label"] for s in d["ships"])))
    eft = e["eft"] or "[%s, %s (fit unknown)]" % (e["ship"], e["label"])
    h = FitHandle(eft, e["pilot"])
    if not e["eft"]:
        h.fit.name = "%s (fit unknown - bare hull)" % e["label"]
    return h, e


# ---- analysis ---------------------------------------------------------------------------------

def analyze(name):
    """Per-ship read-out and comp-level picture: roles, damage, reps, reach, tackle, speed, resist holes."""
    P.load()
    d = load(name)
    with lock():
        ships, totals = [], {"ships": 0, "dps": 0.0, "volley": 0.0, "remote_rep": 0.0, "ehp_vs": Counter()}
        roles_count = Counter()
        reach, tackle_ranges, speeds, sigs, locks = [], [], [], [], []
        for e in d["ships"]:
            h, _ = ship_handle(name, e["label"])
            try:
                s = stats_mod.compute(h, noprop=True)
                r = detect_roles(h, s)
                if e.get("role_override"):
                    r["primary"] = e["role_override"]
                tk = _tackle_ranges(h)
            finally:
                h.close()
            c = e["count"]
            totals["ships"] += c
            totals["dps"] += s["dps_total"] * c
            totals["volley"] += s["volley"] * c
            totals["remote_rep"] += s["remote_rep"] * c
            for t in DMG:
                totals["ehp_vs"][t] += s["ehp_vs_" + t] * c
            roles_count[r["primary"]] += c
            if s["weapon_reach"]:
                reach.append(s["weapon_reach"])
            tackle_ranges += [v for v in tk.values() if v]
            speeds.append(s["max_speed"])
            sigs.append(s["signature_noprop"])
            locks.append(s["lock_range"])
            ships.append({
                "label": e["label"], "ship": e["ship"], "count": c, "fit_known": e["fit_known"],
                "samples": e.get("samples"), "primary_role": r["primary"], "roles": r["roles"],
                "role_modules": r["modules"],
                "dps": round(s["dps_total"]), "volley": round(s["volley"]), "remote_rep_hp_s": round(s["remote_rep"]),
                "ehp": round(s["ehp_total"]),
                "weakest_damage_type": min(DMG, key=lambda t: s["ehp_vs_" + t]),
                "weapon_reach_km": round(s["weapon_reach"] / 1000, 1),
                "tackle_km": {k: round(v / 1000, 1) for k, v in tk.items() if v},
                "speed": round(s["max_speed"]), "speed_noprop": round(s["max_speed_noprop"]),
                "sig": round(s["signature"]), "sig_noprop": round(s["signature_noprop"]),
                "lock_range_km": round(s["lock_range"] / 1000, 1), "issues": s["issues"],
            })
        weakest = min(DMG, key=lambda t: totals["ehp_vs"][t]) if totals["ehp_vs"] else None
        strongest = max(DMG, key=lambda t: totals["ehp_vs"][t]) if totals["ehp_vs"] else None
        comp = {
            "ships": totals["ships"], "roles": dict(roles_count),
            "total_paper_dps": round(totals["dps"]), "total_volley": round(totals["volley"]),
            "total_remote_rep_hp_s": round(totals["remote_rep"]),
            "fleet_weakest_damage_type": weakest, "fleet_strongest_damage_type": strongest,
            "ehp_by_damage_type": {t: round(v) for t, v in totals["ehp_vs"].items()},
            "max_weapon_reach_km": round(max(reach) / 1000, 1) if reach else 0,
            "min_weapon_reach_km": round(min(reach) / 1000, 1) if reach else 0,
            "max_tackle_km": round(max(tackle_ranges) / 1000, 1) if tackle_ranges else 0,
            "slowest_speed": round(min(speeds)) if speeds else 0, "fastest_speed": round(max(speeds)) if speeds else 0,
            "smallest_sig_noprop": round(min(sigs)) if sigs else 0,
            "shortest_lock_range_km": round(min(locks) / 1000, 1) if locks else 0,
        }
        return {"name": d["name"], "description": d.get("description", ""), "source": d.get("source"),
                "comp": comp, "ships": ships, "observations": _observations(comp, ships),
                "unknown_fits": [s["label"] for s in ships if not s["fit_known"]]}


def _tackle_ranges(handle):
    out = {}
    for m in handle.fit.modules:
        if m.isEmpty:
            continue
        g = m.item.group.name
        key = ("scram" if "Warp Scrambler" in g and "warpScrambleBlockMWDWithNPCEffect" in m.item.effects
               else "point" if "Warp Scrambler" in g else "web" if "Stasis" in g
               else "bubble" if "Warp Disrupt Field" in g else None)
        if key:
            out[key] = max(out.get(key, 0), float(m.maxRange or 0))
    return out


def _observations(comp, ships):
    """Plain-language read of the comp, each backed by the numbers above."""
    obs = []
    if comp["roles"].get("logistics"):
        obs.append("Logistics: %d ship(s), %s HP/s of remote reps in total - to kill anything you must out-damage "
                   "its own tank plus the reps that land on it (alpha, neuts on cap-chained logi, or kill the logi "
                   "first)." % (comp["roles"]["logistics"], comp["total_remote_rep_hp_s"]))
    else:
        obs.append("No logistics: damage sticks; sustained DPS wins.")
    if comp["roles"].get("ewar"):
        ew = [s for s in ships if s["primary_role"] == "ewar" or "ewar" in s["roles"]]
        kinds = sorted({m for s in ew for m in s["role_modules"].get("ewar", {})})
        obs.append("EWAR present (%s) - expect jams/damps; consider ECCM/sensor strength or primary them early."
                   % ", ".join(kinds))
    if comp["max_weapon_reach_km"]:
        obs.append("Their weapon reach is %.0f-%.0f km (optimal+falloff, primary groups): outside that, only drones "
                   "or long-range ships apply." % (comp["min_weapon_reach_km"], comp["max_weapon_reach_km"]))
    if comp["max_tackle_km"]:
        obs.append("Their tackle reaches %.0f km (scram/web/point): inside it, MWDs get switched off and ships get "
                   "slowed." % comp["max_tackle_km"])
    if comp["fleet_weakest_damage_type"]:
        obs.append("Fleet EHP is lowest against %s and highest against %s - bring %s damage if you can."
                   % (comp["fleet_weakest_damage_type"], comp["fleet_strongest_damage_type"],
                      comp["fleet_weakest_damage_type"]))
    unknown = [s["label"] for s in ships if not s["fit_known"]]
    if unknown:
        obs.append("Fits unknown for %s (bare hulls): their numbers are placeholders." % ", ".join(unknown))
    return obs
