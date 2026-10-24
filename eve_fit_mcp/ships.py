"""Ship (hull) reference data: slots, hardpoints, base attributes, bonuses, and values with a pilot's skills."""
import html
import re

from .errors import FitError
from .fitting import FitHandle, get_item
from .pyfa_bridge import P, lock

# (section, label, attribute name)
KEY_ATTRS = [
    ("slots", "high", "hiSlots"), ("slots", "med", "medSlots"), ("slots", "low", "lowSlots"),
    ("slots", "rig", "rigSlots"), ("slots", "subsystem", "maxSubSystems"), ("slots", "service", "serviceSlots"),
    ("hardpoints", "turret", "turretSlotsLeft"), ("hardpoints", "launcher", "launcherSlotsLeft"),
    ("fitting", "cpu", "cpuOutput"), ("fitting", "powergrid", "powerOutput"),
    ("fitting", "calibration", "upgradeCapacity"), ("fitting", "rig_size", "rigSize"),
    ("defense", "shield_hp", "shieldCapacity"), ("defense", "armor_hp", "armorHP"), ("defense", "hull_hp", "hp"),
    ("defense", "shield_recharge_ms", "shieldRechargeRate"),
    ("capacitor", "capacity_gj", "capacitorCapacity"), ("capacitor", "recharge_ms", "rechargeRate"),
    ("navigation", "max_velocity", "maxVelocity"), ("navigation", "inertia_modifier", "agility"),
    ("navigation", "mass_kg", "mass"), ("navigation", "warp_speed_multiplier", "warpSpeedMultiplier"),
    ("navigation", "signature_radius", "signatureRadius"),
    ("targeting", "max_range_m", "maxTargetRange"), ("targeting", "scan_resolution", "scanResolution"),
    ("targeting", "max_locked_targets", "maxLockedTargets"),
    ("targeting", "radar", "scanRadarStrength"), ("targeting", "ladar", "scanLadarStrength"),
    ("targeting", "magnetometric", "scanMagnetometricStrength"),
    ("targeting", "gravimetric", "scanGravimetricStrength"),
    ("drones", "bandwidth", "droneBandwidth"), ("drones", "bay_m3", "droneCapacity"),
    ("cargo", "cargo_m3", "capacity"),
]
RIG_SIZES = {1: "small", 2: "medium", 3: "large", 4: "capital"}


def _text(markup):
    if not markup:
        return None
    s = re.sub(r"<br\s*/?>", "\n", markup)
    s = re.sub(r"<[^>]+>", "", s)
    return html.unescape(s).strip()


def _num(v):
    if v is None:
        return None
    v = float(v)
    return int(v) if v.is_integer() else round(v, 4)


def ship_info(name, pilot=None, all_attributes=False, subsystems=None):
    P.load()
    item = get_item(name)
    if item.category.name not in ("Ship", "Structure"):
        raise FitError("%s is a %s, not a ship" % (item.typeName, item.category.name))
    with lock():
        eft = "[%s, hull info]" % item.typeName
        if subsystems:
            for sub in subsystems:
                sub_item = get_item(sub)
                if sub_item.category.name != "Subsystem" or not sub_item.typeName.startswith(item.typeName):
                    raise FitError("%s is not a %s subsystem" % (sub_item.typeName, item.typeName))
            eft += "\n\n" + "\n".join(subsystems)
        h = FitHandle(eft, pilot, drones_active=False)
        ship = h.fit.ship
        base = {n: a.value for n, a in item.attributes.items()}

        def pair(attr):
            b = base.get(attr)
            m = ship.getModifiedItemAttr(attr)
            return _num(b), _num(m)

        sections = {}
        for section, label, attr in KEY_ATTRS:
            b, m = pair(attr)
            if b is None and not m:
                continue
            if label in ("radar", "ladar", "magnetometric", "gravimetric") and not b:
                continue
            entry = {"base": b, "with_skills": m} if b != m else b
            sections.setdefault(section, {})[label] = entry
        if "fitting" in sections and "rig_size" in sections["fitting"]:
            rs = base.get("rigSize")
            sections["fitting"]["rig_size"] = RIG_SIZES.get(int(rs or 0), rs)

        # Derived numbers straight from Pyfa's fit maths (empty hull, pilot's skills)
        from . import stats as stats_mod
        s = stats_mod.compute(h)
        sections["derived_with_skills"] = {
            "ehp_omni": round(s["ehp_total"]), "align_time_s": round(s["align_time"], 2),
            "warp_speed_au_s": round(s["warp_speed"], 2), "max_speed": round(s["max_speed"], 1),
            "cap_recharge_peak_gj_s": round(s["cap_delta"], 2) if s["cap_delta"] else None,
            "resists_pct": {l: [round(s["resist_%s_%s" % (l, d)] * 100, 1)
                                for d in ("em", "thermal", "kinetic", "explosive")]
                            for l in ("shield", "armor", "hull")},
        }

        mg = P.market.getMetaGroupByItem(item)
        out = {
            "name": item.typeName,
            "type_id": item.ID,
            "group": item.group.name,
            "meta_group": mg.name if mg is not None else "Tech I",
            "pilot": h.pilot,
            "required_skills": {sk.typeName: lvl for sk, lvl in item.requiredSkills.items()},
            "bonuses": _text(item.traits.display) if item.traits else None,
            **sections,
        }
        if (base.get("maxSubSystems") or 0) > 0:
            out["subsystems"] = list(subsystems or [])
            if not subsystems:
                out["note"] = ("Strategic cruiser: the bare hull has no slots. Pass subsystems=[core, defensive, "
                               "offensive, propulsion] or use list_subsystem_layouts to compare combinations.")
        if all_attributes:
            attrs = {}
            for n, a in sorted(item.attributes.items()):
                info = a.info
                b, m = pair(n)
                attrs[n] = {"display": info.displayName or None,
                            "unit": info.unit.displayName if info.unit is not None else None,
                            "base": b, "with_skills": m, "published": bool(info.published)}
            out["attributes"] = attrs
        h.close()
        return out


SUBSYSTEM_GROUPS = ("Core Subsystem", "Defensive Subsystem", "Offensive Subsystem", "Propulsion Subsystem")


def subsystem_layouts(ship, pilot=None, require=None):
    """Every subsystem combination of a T3C with its slot layout and key numbers.

    require: optional filters, e.g. {"subsystem": "Covert Reconfiguration", "launcher": 5, "high": 7}
    (subsystem = substring that must appear in one of the chosen subsystems; numbers are minimums)."""
    import itertools
    from . import items, pilots, stats as stats_mod
    P.load()
    hull = get_item(ship)
    if not (hull.attributes.get("maxSubSystems") and hull.attributes["maxSubSystems"].value):
        raise FitError("%s has no subsystems" % hull.typeName)
    by_group = {g: [] for g in SUBSYSTEM_GROUPS}
    for e in items.index():
        if e["category"] == "Subsystem" and e["name"].startswith(hull.typeName + " ") and e["group"] in by_group:
            by_group[e["group"]].append(e["name"])
    require = dict(require or {})
    need_sub = require.pop("subsystem", None)
    rows = []
    with lock():
        for combo in itertools.product(*(sorted(by_group[g]) for g in SUBSYSTEM_GROUPS)):
            if need_sub and not any(need_sub.lower() in c.lower() for c in combo):
                continue
            h = FitHandle("[%s, layout]\n\n%s" % (hull.typeName, "\n".join(combo)), pilot, drones_active=False)
            s = stats_mod.compute(h)
            slots = s["slots"]
            row = {"subsystems": [c[len(hull.typeName) + 1:] for c in combo],
                   "high": slots.get("high", {}).get("total", 0), "med": slots.get("med", {}).get("total", 0),
                   "low": slots.get("low", {}).get("total", 0), "rig": slots.get("rig", {}).get("total", 0),
                   "turret": int(h.fit.ship.getModifiedItemAttr("turretSlotsLeft") or 0),
                   "launcher": int(h.fit.ship.getModifiedItemAttr("launcherSlotsLeft") or 0),
                   "cpu": round(s["cpu_total"], 1), "pg": round(s["pg_total"], 1),
                   "drone_bandwidth": s["drone_bandwidth_total"], "drone_bay": s["drone_bay_total"],
                   "ehp_hull_only": round(s["ehp_total"]), "max_speed": round(s["max_speed"], 1),
                   "align_time": round(s["align_time"], 2), "signature": round(s["signature"], 1),
                   "lock_range": round(s["lock_range"])}
            h.close()
            if all(row.get(k, 0) >= v for k, v in require.items()):
                rows.append(row)
    return {"ship": hull.typeName, "pilot": pilots.canonical(pilot), "count": len(rows), "layouts": rows,
            "note": "ehp/speed/align are for the bare hull with these subsystems and no modules"}
