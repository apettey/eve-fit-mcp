"""Item lookup and candidate generation, using Pyfa's market service for variations/publicity."""
from .fitting import FitError, get_item, slot_name
from .pyfa_bridge import P, lock

DEFAULT_EXCLUDE_META = ("Officer",)

KINDS = {
    "module": lambda i: i.isModule,
    "rig": lambda i: i.isModule and i.group.category.name == "Module" and "Rig" in i.group.name,
    "charge": lambda i: i.isCharge,
    "drone": lambda i: i.isDrone,
    "fighter": lambda i: i.isFighter,
    "implant": lambda i: i.isImplant,
    "booster": lambda i: i.isBooster,
    "subsystem": lambda i: i.isSubsystem,
    "ship": lambda i: i.category.name == "Ship",
}


def meta_group(item):
    mg = P.market.getMetaGroupByItem(item)
    return mg.name if mg is not None else "Tech I"


def _attr(item, name):
    a = item.attributes.get(name)
    return a.value if a is not None else None


def describe(item):
    d = {
        "name": item.typeName,
        "group": item.group.name,
        "category": item.category.name,
        "meta_group": meta_group(item),
        "meta_level": _attr(item, "metaLevel"),
    }
    if item.isModule or item.isSubsystem:
        try:
            d["slot"] = slot_name(P.Module(item).slot)
        except ValueError:
            pass
        d["cpu"] = _attr(item, "cpu")
        d["pg"] = _attr(item, "power")
        if _attr(item, "upgradeCost"):
            d["calibration"] = _attr(item, "upgradeCost")
    return d


def search(query, kind=None, limit=25):
    P.load()
    if kind is not None and kind not in KINDS:
        raise FitError("kind must be one of: %s" % ", ".join(KINDS))
    with lock():
        items = P.db.searchItems(query)
        out = []
        for it in items:
            if not P.market.getPublicityByItem(it):
                continue
            if kind and not KINDS[kind](it):
                continue
            out.append(describe(it))
        out.sort(key=lambda d: (len(d["name"]), d["name"]))
        return out[:limit]


def _meta_ok(item, exclude_meta, max_meta_level):
    if exclude_meta and meta_group(item) in exclude_meta:
        return False
    if max_meta_level is not None:
        lvl = _attr(item, "metaLevel") or 0
        if lvl > max_meta_level:
            return False
    return True


def module_candidates(handle, key, mode="variants", pool=None, exclude_meta=DEFAULT_EXCLUDE_META,
                      max_meta_level=None):
    """Items that could replace whatever sits in `key`.

    mode: "variants"     - meta variants of the current module (T1/T2/faction/deadspace...)
          "group"        - every published item in the current module's group
          "market_group" - siblings in the market tree (e.g. all 'Armor Hardeners')
          "pool"         - only names given in `pool`
    `pool` names are always added (and filtered to the right slot) in every mode.
    """
    P.load()
    with lock():
        pos, cur = handle._resolve(key)
        want_slot = cur.slot
        fit = handle.fit
        items = set()
        base = None if cur.isEmpty else (cur.baseItem if cur.isMutated else cur.item)
        if base is not None and mode != "pool":
            if mode == "variants":
                items.update(P.market.getVariationsByItems([base]))
            elif mode == "group":
                items.update(P.market.getItemsByGroup(base.group))
            elif mode == "market_group":
                mg = P.market.getMarketGroupByItem(base)
                if mg is not None:
                    items.update(P.market.getItemsByMarketGroup(mg))
            else:
                raise FitError("mode must be variants|group|market_group|pool")
            items.add(base)
        for name in pool or ():
            items.add(get_item(name))

        out = []
        for it in items:
            if not it.isModule and not it.isSubsystem:
                continue
            if not P.market.getPublicityByItem(it):
                continue
            if not _meta_ok(it, exclude_meta, max_meta_level):
                continue
            try:
                if P.Module(it).slot != want_slot:
                    continue
            except ValueError:
                continue
            if not fit.canFit(it):
                continue
            out.append(it)
        out.sort(key=lambda i: i.typeName)
        return out


def charge_candidates(handle, key, exclude_meta=DEFAULT_EXCLUDE_META, max_meta_level=None):
    P.load()
    with lock():
        _, mod = handle._resolve(key)
        if mod.isEmpty:
            return []
        out = [c for c in mod.getValidCharges()
               if P.market.getPublicityByItem(c) and _meta_ok(c, exclude_meta, max_meta_level)]
        out.sort(key=lambda i: i.typeName)
        return out
