"""Item database: a pre-computed, tagged index of every published fittable item, plus filtered search
and full attribute dumps.

The index is built from Pyfa's game data (market tree, groups, dogma effects and attributes) the first
time it's needed, and cached in data/item_index.json keyed by the game data build, so it is rebuilt
automatically when vendor/pyfa's static data changes.

Tags are hints about what an item is *for* (e.g. "damage", "turret-range", "speed", "tackle",
"tank:armor-buffer", "drawback:armor-hp", "ammo:long-range"). They're heuristics: confirm with
get_item_info / try_candidates before relying on them.
"""
import json
import os
import re

from .errors import FitError
from .pyfa_bridge import DATA_DIR, P, lock

INDEX_VERSION = 4  # bump when tagging logic changes
INDEX_PATH = os.path.join(DATA_DIR, "item_index.json")
CATEGORIES = ("Module", "Charge", "Drone", "Implant", "Subsystem", "Fighter", "Ship", "Structure Module")

SLOT_EFFECTS = {"loPower": "low", "medPower": "med", "hiPower": "high", "rigSlot": "rig",
                "subSystem": "subsystem", "serviceSlot": "service"}
SIZE_WORDS = {"small": "small", "medium": "medium", "large": "large", "extra large": "xl", "capital": "capital",
              "x-large": "xl", "micro": "micro"}
CHARGE_SIZES = {1: "small", 2: "medium", 3: "large", 4: "xl"}
RIG_SIZES = {1: "small", 2: "medium", 3: "large", 4: "capital"}

# (substring of group name, tags). Groups are CCP's stable item groupings.
GROUP_TAGS = [
    ("Gyrostabilizer", "damage turret-damage projectile"),
    ("Magnetic Field Stabilizer", "damage turret-damage hybrid"),
    ("Heat Sink", "damage turret-damage energy"),
    ("Entropic Radiation Sink", "damage turret-damage precursor"),
    ("Ballistic Control", "damage missile-damage"),
    ("Vorton Tuning", "damage vorton"),
    ("Drone Damage", "damage drone-damage"),
    ("Drone Tracking", "drone-application"),
    ("Drone Navigation", "drone-speed"),
    ("Drone Control Range", "drone-range"),
    ("Tracking Computer", "application turret-range turret-tracking"),
    ("Tracking Enhancer", "application turret-range turret-tracking"),
    ("Missile Guidance", "application missile-range missile-application"),
    ("Propulsion Module", "speed propulsion"),
    ("Overdrive Injector", "speed"),
    ("Nanofiber", "speed agility"),
    ("Inertial Stabilizer", "agility"),
    ("Warp Scrambler", "tackle point"),
    ("Warp Disrupt Field", "tackle bubble"),
    ("Stasis Web", "tackle web"),
    ("Stasis Grappler", "tackle web"),
    ("Shield Extender", "tank tank:shield-buffer"),
    ("Shield Hardener", "tank tank:shield-resist"),
    ("Shield Resistance Amplifier", "tank tank:shield-resist"),
    ("Ancillary Shield Booster", "tank tank:shield-active"),
    ("Shield Booster", "tank tank:shield-active"),
    ("Shield Recharger", "tank tank:shield-passive"),
    ("Shield Power Relay", "tank tank:shield-passive"),
    ("Shield Flux Coil", "tank tank:shield-passive"),
    ("Armor Plate", "tank tank:armor-buffer"),
    ("Armor Reinforcer", "tank tank:armor-buffer"),
    ("Armor Hardener", "tank tank:armor-resist"),
    ("Armor Coating", "tank tank:armor-resist"),
    ("Armor Resistance", "tank tank:armor-resist"),
    ("Energized Armor", "tank tank:armor-resist"),
    ("Armor Repair", "tank tank:armor-active"),
    ("Damage Control", "tank tank:resist tank:hull"),
    ("Hull Repair", "tank tank:hull"),
    ("Reinforced Bulkhead", "tank tank:hull-buffer"),
    ("Capacitor Booster", "cap cap-injection"),
    ("Capacitor Recharger", "cap"),
    ("Capacitor Power Relay", "cap"),
    ("Capacitor Battery", "cap"),
    ("Capacitor Flux", "cap"),
    ("Power Diagnostic", "fitting powergrid cap"),
    ("Reactor Control", "fitting powergrid"),
    ("Auxiliary Power Core", "fitting powergrid"),
    ("CPU Enhancer", "fitting cpu"),
    ("Sensor Booster", "targeting lock-range scan-res"),
    ("Signal Amplifier", "targeting lock-range"),
    ("Automated Targeting", "targeting"),
    ("ECM", "ewar ewar:jam"),
    ("Sensor Dampener", "ewar ewar:damp"),
    ("Weapon Disruptor", "ewar ewar:disrupt"),
    ("Target Painter", "ewar ewar:paint"),
    ("Energy Neutralizer", "cap-warfare neut"),
    ("Energy Nosferatu", "cap-warfare nos"),
    ("Remote Armor", "logistics logi:armor"),
    ("Remote Shield", "logistics logi:shield"),
    ("Remote Capacitor", "logistics logi:cap"),
    ("Remote Hull", "logistics logi:hull"),
    ("Remote Sensor Booster", "logistics logi:targeting"),
    ("Remote Tracking", "logistics logi:application"),
    ("Warp Core Stabilizer", "anti-tackle"),
    ("Interdiction Nullifier", "anti-tackle travel"),
    ("Cloaking", "cloak"),
    ("Mining", "mining"),
    ("Strip Miner", "mining"),
    ("Gas Cloud", "mining gas"),
    ("Salvager", "salvage"),
    ("Tractor Beam", "salvage"),
    ("Scan Probe Launcher", "exploration probing"),
    ("Data Miner", "exploration hacking"),
    ("Command Burst", "command"),
    ("Micro Jump", "mobility"),
    ("Smart Bomb", "damage area-damage"),
    ("Projectile Weapon", "weapon turret projectile"),
    ("Hybrid Weapon", "weapon turret hybrid"),
    ("Energy Weapon", "weapon turret energy"),
    ("Precursor Weapon", "weapon turret precursor"),
    ("Missile Launcher", "weapon launcher"),
    ("Vorton Projector", "weapon vorton"),
]

# Name keywords, mostly for rigs where one market group holds several roles.
NAME_TAGS = [
    ("Collision Accelerator", "damage turret-damage"), ("Burst Aerator", "damage turret-rof"),
    ("Discharge Elutriation", "cap turret-cap"), ("Locus Coordinator", "turret-range"),
    ("Ambit Extension", "turret-range"), ("Metastasis Adjuster", "turret-tracking"),
    ("Warhead Calefaction", "damage missile-damage"), ("Bay Loading Accelerator", "damage missile-rof"),
    ("Warhead Rigor", "missile-application"), ("Warhead Flare", "missile-application"),
    ("Hydraulic Bay Thrusters", "missile-range"), ("Rocket Fuel Cache", "missile-range"),
    ("Polycarbon Engine Housing", "speed agility"), ("Auxiliary Thrusters", "speed"),
    ("Low Friction Nozzle", "agility"), ("Dynamic Fuel Valve", "propulsion cap"),
    ("Engine Thermal Shielding", "propulsion overheat"), ("Hyperspatial Velocity", "warp-speed"),
    ("Ancillary Current Router", "fitting powergrid"), ("Processor Overclocking", "fitting cpu"),
    ("Capacitor Control Circuit", "cap"), ("Semiconductor Memory Cell", "cap"),
    ("Egress Port Maximizer", "cap"), ("Powergrid Subroutine", "fitting powergrid"),
    ("Trimark Armor Pump", "tank tank:armor-buffer"), ("Auxiliary Nano Pump", "tank tank:armor-active"),
    ("Nanobot Accelerator", "tank tank:armor-active"), ("Anti-", "tank tank:resist"),
    ("Core Defense Field Extender", "tank tank:shield-buffer"), ("Core Defense Field Purger", "tank tank:shield-passive"),
    ("Core Defense Capacitor Safeguard", "tank tank:shield-active"), ("Core Defense Operational Solidifier", "tank tank:shield-active"),
    ("Screen Reinforcer", "tank tank:resist"), ("Transverse Bulkhead", "tank tank:hull-buffer"),
    ("Ionic Field Projector", "targeting lock-range"), ("Targeting System Subcontroller", "targeting scan-res"),
    ("Drone Durability", "drones"), ("Drone Mining", "drones mining"), ("Drone Repair", "drones"),
    ("Drone Scope Chip", "drone-range"), ("Drone Speed", "drone-speed"), ("Sentry Damage", "drone-damage"),
    ("Stasis Drone", "drones"), ("Cargohold Optimization", "hauling"), ("Signal Disruption", "ewar"),
    ("Particle Dispersion", "ewar"), ("Inverted Signal Field", "ewar"), ("Tracking Diagnostic", "ewar"),
    ("Gravity Capacitor", "probing"), ("Emission Scope", "probing"), ("Signal Focusing", "hacking"),
]

DMG_ATTRS = (("emDamage", "em"), ("thermalDamage", "thermal"), ("kineticDamage", "kinetic"),
             ("explosiveDamage", "explosive"))

_index = None
_attr_names = None


def _split_camel(name):
    # ArmorHP -> armor-hp, CPUOutput -> cpu-output, SigRad -> sig-rad
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", "-", name).lower()


def _attr(item, name):
    a = item.attributes.get(name)
    return a.value if a is not None else None


def _market_path(item):
    out = []
    mg = P.market.getMarketGroupByItem(item)
    while mg is not None:
        out.append(mg.name)
        mg = mg.parent
    return list(reversed(out))


def _size(item, path):
    rs = _attr(item, "rigSize")
    if item.category.name in ("Module", "Structure Module") and rs and any("Rig" in p for p in path):
        return RIG_SIZES.get(int(rs))
    cs = _attr(item, "chargeSize")
    if cs:
        return CHARGE_SIZES.get(int(cs))
    for p in reversed(path):
        if p.lower() in SIZE_WORDS:
            return SIZE_WORDS[p.lower()]
    m = re.match(r"^(\d+)MN\b", item.typeName)
    if m:
        return {"1": "small", "5": "small", "10": "medium", "50": "medium", "100": "large",
                "500": "large", "1000": "capital", "10000": "capital"}.get(m.group(1))
    for word in ("Small", "Medium", "Large", "Capital", "Heavy", "Light"):
        if item.typeName.startswith(word + " ") or (" %s " % word) in item.typeName:
            return {"Heavy": "medium", "Light": "small"}.get(word, word.lower())
    return None


def _tags(item, path, slot):
    tags = set()
    cat, group = item.category.name, item.group.name
    for sub, t in GROUP_TAGS:
        if sub.lower() in group.lower():
            tags.update(t.split())
    for sub, t in NAME_TAGS:
        if sub.lower() in item.typeName.lower():
            tags.update(t.split())
    effects = set(item.effects)
    if "turretFitted" in effects:
        tags.update(("weapon", "turret"))
    if "launcherFitted" in effects:
        tags.update(("weapon", "launcher"))
    for eff in effects:
        if eff.startswith("drawback"):
            tags.add("drawback:" + _split_camel(eff[len("drawback"):]))
    if slot == "rig":
        tags.add("rig")
        for p in path:
            if p.endswith(" Rigs") and not p.startswith(("Small", "Medium", "Large", "Capital")):
                tags.add("rig:" + p[:-5].lower().replace(" ", "-"))
    if cat == "Charge":
        tags.add("ammo" if "Script" not in group else "script")
        dmg = {n: (_attr(item, a) or 0) for a, n in DMG_ATTRS}
        total = sum(dmg.values())
        if total > 0:
            top = max(dmg.values())
            tags.update("dmg:" + n for n, v in dmg.items() if v >= 0.3 * total)
            if top >= 0.75 * total:
                tags.add("dmg:mostly-" + max(dmg, key=dmg.get))
        rng = _attr(item, "weaponRangeMultiplier")
        if rng:
            tags.add("ammo:long-range" if rng > 1.2 else "ammo:short-range" if rng < 0.8 else "ammo:mid-range")
        if (_attr(item, "trackingSpeedMultiplier") or 1) > 1.05:
            tags.add("ammo:tracking-bonus")
        for p in path:
            if p in ("Advanced", "Faction Ammo", "Standard Ammo") or "Advanced" in p:
                tags.add("ammo:" + p.lower().replace(" ammo", "").replace(" ", "-"))
    if cat == "Drone":
        tags.add("drone")
        if len(path) > 1:
            tags.add("drone:" + path[1].lower().replace(" drones", "").replace(" ", "-"))
    if cat == "Implant":
        tags.add("booster" if group == "Booster" else "implant")
    if cat == "Ship":
        tags.add("ship")
    return sorted(tags)


def _gamedata_build():
    import eos.config
    return str(getattr(eos.config, "gamedata_version", "") or "")


def _build():
    import eos.db
    from eos.gamedata import Category, Group, Item
    s = eos.db.get_gamedata_session()
    rows = s.query(Item).join(Item.group).join(Group.category).filter(Category.name.in_(CATEGORIES)).all()
    out = []
    for it in rows:
        if not P.market.getPublicityByItem(it):
            continue
        path = _market_path(it)
        slot = next((v for k, v in SLOT_EFFECTS.items() if k in it.effects), None)
        hp = "turret" if "turretFitted" in it.effects else "launcher" if "launcherFitted" in it.effects else None
        mg = P.market.getMetaGroupByItem(it)
        e = {
            "id": it.ID, "name": it.typeName, "category": it.category.name, "group": it.group.name,
            "market_path": " > ".join(path), "slot": slot, "hardpoint": hp,
            "meta_group": mg.name if mg is not None else "Tech I",
            "meta_level": _attr(it, "metaLevelOld"), "size": _size(it, path),
            "cpu": _attr(it, "cpu"), "pg": _attr(it, "power"), "calibration": _attr(it, "upgradeCost"),
            "tags": _tags(it, path, slot),
        }
        out.append({k: v for k, v in e.items() if v not in (None, [], "")})
    return out


def index():
    """The tagged item index (list of dicts)."""
    global _index
    if _index is not None:
        return _index
    P.load()
    with lock():
        key = {"version": INDEX_VERSION, "build": _gamedata_build()}
        if os.path.isfile(INDEX_PATH):
            try:
                with open(INDEX_PATH, encoding="utf-8") as f:
                    data = json.load(f)
                if data.get("key") == key:
                    _index = data["items"]
                    return _index
            except (OSError, ValueError):
                pass
        _index = _build()
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(INDEX_PATH, "w", encoding="utf-8") as f:
            json.dump({"key": key, "items": _index}, f)
        return _index


def attribute_names():
    """{lowercase attribute name or display name: attributeName} for resolving filters."""
    global _attr_names
    if _attr_names is None:
        import eos.db
        from eos.gamedata import AttributeInfo
        out = {}
        for info in eos.db.get_gamedata_session().query(AttributeInfo).all():
            out.setdefault(info.name.lower(), info.name)
            if info.displayName:
                out.setdefault(info.displayName.lower(), info.name)
        _attr_names = out
    return _attr_names


def resolve_attr(name):
    real = attribute_names().get(name.lower())
    if real is None:
        close = sorted(k for k in attribute_names() if name.lower() in k)[:10]
        raise FitError("Unknown attribute %r%s" % (name, (" (similar: %s)" % ", ".join(close)) if close else ""))
    return real


# ---- search --------------------------------------------------------------------------------

_OPS = {"min": lambda v, x: v >= x, "max": lambda v, x: v <= x, "eq": lambda v, x: v == x}


def slot_name_of(mod):
    from .fitting import slot_name
    return slot_name(mod.slot)


def _ship_fit(ship):
    from .fitting import FitHandle
    cache = _ship_fit.__dict__.setdefault("cache", {})
    if ship not in cache:
        cache[ship] = FitHandle("[%s, fits_ship probe]" % ship, drones_active=False)
    return cache[ship]


def _fits_ship(entry, item, handle, layout_known=False):
    """Could this item be fitted to the ship (ignoring what's already fitted)?

    Bare T3C hulls have no slots until subsystems are fitted, so for them slot/hardpoint counts are
    skipped unless layout_known (a real fit with subsystems was given)."""
    fit = handle.fit
    if entry["category"] in ("Module", "Structure Module", "Subsystem"):
        try:
            mod = P.Module(item)
        except ValueError:
            return False
        t3c = (fit.ship.getModifiedItemAttr("maxSubSystems") or 0) > 0
        if not t3c and not layout_known:
            return bool(mod.fits(fit))
        if not fit.canFit(item):
            return False
        slot = slot_name_of(mod)
        if slot == "rig" and _attr(item, "rigSize") != fit.ship.getModifiedItemAttr("rigSize"):
            return False
        if slot == "subsystem":
            return True
        if (fit.ship.getModifiedItemAttr("isCapitalSize") or 0) != 1 and mod.isCapitalSize:
            return False
        if layout_known:
            if slot and not fit.getNumSlots(mod.slot):
                return False
            hp = entry.get("hardpoint")
            if hp == "turret" and not fit.ship.getModifiedItemAttr("turretSlotsLeft"):
                return False
            if hp == "launcher" and not fit.ship.getModifiedItemAttr("launcherSlotsLeft"):
                return False
        return True
    if entry["category"] == "Drone":
        bw = fit.ship.getModifiedItemAttr("droneBandwidth") or 0
        return bw >= (_attr(item, "droneBandwidthUsed") or 0) > 0 or bw > 0 and not _attr(item, "droneBandwidthUsed")
    if entry["category"] == "Charge":
        return any(not m.isEmpty and m.isValidCharge(item) for m in fit.modules)
    return True


def find(query=None, category=None, group=None, slot=None, size=None, hardpoint=None, meta_groups=None,
         tags=None, any_tags=None, market=None, fits_ship=None, charge_for=None, attributes=None,
         sort_by=None, descending=True, include_attributes=None, usable_by=None, limit=30, fit_handle=None):
    """Filter the item index. All text filters are case-insensitive substrings unless noted.

    tags: all must match; any_tags: at least one. attributes: {"cpu": {"max": 30}, "Damage Modifier": {"min": 1.1}}
    (dogma attribute names or display names). sort_by: attribute or "cpu"/"pg"/"meta_level"/"name".
    fits_ship: only items the ship can fit (slots, hardpoints, rig size, restrictions).
    charge_for: only charges loadable by this module. usable_by: pilot name - only items they can use.
    """
    P.load()
    from . import pilots
    with lock():
        rows = index()
        want_tags = set(tags or ())
        any_t = set(any_tags or ())
        metas = {m.lower() for m in (meta_groups or ())}
        q = (query or "").lower().split()

        def text_ok(e):
            name = e["name"].lower()
            return all(w in name for w in q)

        cand = []
        for e in rows:
            if q and not text_ok(e):
                continue
            if category and category.lower() not in e["category"].lower():
                continue
            if group and group.lower() not in e["group"].lower():
                continue
            if market and market.lower() not in e.get("market_path", "").lower():
                continue
            if slot and e.get("slot") != slot:
                continue
            if size and e.get("size") != size:
                continue
            if hardpoint and e.get("hardpoint") != hardpoint:
                continue
            if metas and e["meta_group"].lower() not in metas:
                continue
            et = set(e.get("tags", ()))
            if want_tags and not want_tags <= et:
                continue
            if any_t and not (any_t & et):
                continue
            cand.append(e)

        attr_filters = {resolve_attr(k): v for k, v in (attributes or {}).items()}
        sort_attr = None
        if sort_by and sort_by not in ("cpu", "pg", "calibration", "meta_level", "name"):
            sort_attr = resolve_attr(sort_by)
        shown = [resolve_attr(a) for a in (include_attributes or ())]
        handle = fit_handle or (_ship_fit(fits_ship) if fits_ship else None)
        layout_known = fit_handle is not None
        charge_mod = None
        if charge_for:
            from .fitting import get_item
            try:
                charge_mod = P.Module(get_item(charge_for))
            except ValueError:
                raise FitError("%s is not a module" % charge_for)
        char = pilots.character(usable_by) if usable_by else None

        out = []
        for e in cand:
            needs_item = attr_filters or sort_attr or shown or handle or charge_mod or char
            item = P.market.getItem(e["id"]) if needs_item else None
            if attr_filters:
                ok = True
                for a, cond in attr_filters.items():
                    v = _attr(item, a)
                    if isinstance(cond, (int, float)):
                        cond = {"min": cond}
                    if v is None or not all(_OPS[op](v, x) for op, x in cond.items()):
                        ok = False
                        break
                if not ok:
                    continue
            if charge_mod is not None and not charge_mod.isValidCharge(item):
                continue
            if handle is not None and not _fits_ship(e, item, handle, layout_known):
                continue
            if char is not None and not pilots.can_use(char, item):
                continue
            row = dict(e)
            if sort_attr:
                row[sort_attr] = _attr(item, sort_attr)
            for a in shown:
                row[a] = _attr(item, a)
            out.append(row)

        key = sort_attr or sort_by
        if key:
            if key == "name":
                out.sort(key=lambda r: r["name"], reverse=False)
            else:
                out.sort(key=lambda r: (r.get(key) is None, -(r.get(key) or 0) if descending else (r.get(key) or 0)))
        else:
            out.sort(key=lambda r: (len(r["name"]), r["name"]))
        total = len(out)
        return {"total": total, "returned": min(total, limit), "items": out[:limit]}


def tag_counts(category=None):
    from collections import Counter
    c = Counter()
    for e in index():
        if category and category.lower() not in e["category"].lower():
            continue
        c.update(e.get("tags", ()))
    return dict(sorted(c.items()))


def list_groups(category=None, query=None):
    from collections import Counter
    c = Counter()
    for e in index():
        if category and category.lower() not in e["category"].lower():
            continue
        if query and query.lower() not in e["group"].lower() and query.lower() not in e.get("market_path", "").lower():
            continue
        c[(e["category"], e["group"])] += 1
    return [{"category": k[0], "group": k[1], "items": n} for k, n in sorted(c.items())]


# ---- item info -----------------------------------------------------------------------------

def _clean(markup):
    if not markup:
        return None
    s = re.sub(r"<br\s*/?>", "\n", markup)
    s = re.sub(r"<[^>]+>", "", s)
    import html
    return html.unescape(s).strip()


def item_info(name, attributes="published", module=None):
    """Everything about one item. attributes: 'published' (default), 'all' or 'none'.
    module: a fitted eos Module to also report its modified (skills/ship/fit) values."""
    from .fitting import get_item
    P.load()
    with lock():
        item = get_item(name)
        entry = next((e for e in index() if e["id"] == item.ID), None) or {}
        out = {
            "name": item.typeName, "type_id": item.ID, "category": item.category.name, "group": item.group.name,
            "market_path": entry.get("market_path"), "meta_group": entry.get("meta_group"),
            "meta_level": entry.get("meta_level"), "slot": entry.get("slot"), "hardpoint": entry.get("hardpoint"),
            "size": entry.get("size"), "tags": entry.get("tags", []),
            "volume_m3": _attr(item, "volume") or getattr(item, "volume", None),
            "required_skills": {sk.typeName: lvl for sk, lvl in item.requiredSkills.items()},
            "description": _clean(item.description),
        }
        if item.traits:
            out["bonuses"] = _clean(item.traits.display)
        variations = P.market.getVariationsByItems([item])
        out["variations"] = sorted(
            ({"name": v.typeName, "meta_group": (P.market.getMetaGroupByItem(v).name
                                                 if P.market.getMetaGroupByItem(v) else "Tech I")}
             for v in variations if v.ID != item.ID and P.market.getPublicityByItem(v)),
            key=lambda d: d["name"])
        if item.isModule:
            try:
                charges = P.Module(item).getValidCharges()
            except ValueError:
                charges = []
            if charges:
                groups = sorted({c.group.name for c in charges if P.market.getPublicityByItem(c)})
                out["charges"] = {"groups": groups, "count": len(charges),
                                  "hint": "use find_items(charge_for=<this module>) for the list"}
        if attributes != "none":
            attrs = {}
            for n, a in sorted(item.attributes.items()):
                info = a.info
                if attributes == "published" and not info.published:
                    continue
                row = {"display": info.displayName or None, "value": _num(a.value),
                       "unit": info.unit.displayName if info.unit is not None else None}
                if module is not None:
                    mv = module.getModifiedItemAttr(n)
                    if mv is not None and _num(mv) != row["value"]:
                        row["fitted"] = _num(mv)
                attrs[n] = row
            out["attributes"] = attrs
        if module is not None and getattr(module, "charge", None) is not None:
            ch = module.charge
            out["loaded_charge"] = {"name": ch.typeName, "attributes": {
                n: {"display": a.info.displayName or None, "value": _num(a.value),
                    "fitted": _num(module.getModifiedChargeAttr(n))}
                for n, a in sorted(ch.attributes.items()) if a.info.published}}
        return out


def _num(v):
    if v is None:
        return None
    v = float(v)
    return int(v) if v.is_integer() else round(v, 5)
