"""Fleet-level effects layered on the fleet model: command bursts, capacitor warfare and smartbombs.

All numbers come from Pyfa:
  - bursts: a burst charge's warfare buff IDs map to Pyfa's generic link types; strength is the booster hull's
    "% bonus to ... burst strength" trait (0 for unbonused hulls); applied side-wide like a fleet booster.
  - neuts: enemy neut modules are added as cap drains (Fit.addDrain, exactly what Pyfa's neut effect does)
    and Pyfa's own capacitor simulation says when the target runs dry; a capped-out ship is re-calculated
    with every cap-using module switched off (hardeners, reps, remote reps, ECM, cap-using guns, prop).
  - smartbombs: missiles fly through the smartbomb radius around the defending fleet; each bomb pulse in that
    window pops them (chance = time inside the field / bomb cycle, per bomb). Drones orbiting the target die
    once enough pulses land to exceed their HP.
"""
import math
import re

from .pyfa_bridge import P

BUFF_ATTRS = ("warfareBuff1ID", "warfareBuff2ID", "warfareBuff3ID", "warfareBuff4ID")
CATEGORIES = ("shield", "armor", "skirmish", "information", "expedition", "mining")


def _attr(item_or_mod, name, charge=False):
    if charge:
        v = item_or_mod.getModifiedChargeAttr(name)
    else:
        v = item_or_mod.getModifiedItemAttr(name)
    return float(v or 0)


def burst_strength(handle):
    """{category: %/level} parsed from the hull's traits, e.g. Nighthawk -> shield 4, information 4."""
    traits = handle.fit.ship.item.traits
    text = traits.display if traits else ""
    text = re.sub(r"<[^>]+>", " ", text)
    out = {}
    for sentence in re.split(r"[\n.]", text):
        if "burst strength" not in sentence.lower() and "foreman burst" not in sentence.lower():
            continue
        m = re.search(r"(\d+(?:\.\d+)?)\s*%", sentence)
        if not m:
            continue
        for cat in CATEGORIES:
            if cat in sentence.lower():
                out[cat] = max(out.get(cat, 0), float(m.group(1)))
    return out


def burst_links(handle):
    """Generic command links this fit's loaded, active bursts provide."""
    defs = P.COMMAND_LINK_DEFS
    by_buffs = {frozenset(v[2]): k for k, v in defs.items() if v[1] and not k.endswith("_all")}
    strength = burst_strength(handle)
    out = {}
    for m in handle.fit.modules:
        if m.isEmpty or m.item.group.name != "Command Burst" or m.charge is None:
            continue
        if m.state < P.FittingModuleState.ACTIVE:
            continue
        buffs = frozenset(int(m.charge.attributes[a].value) for a in BUFF_ATTRS
                          if a in m.charge.attributes and m.charge.attributes[a].value)
        link = by_buffs.get(buffs)
        if not link:
            continue
        cat = defs[link][1]
        s = int(round(min(5, max(0, strength.get(cat, 0)))))  # generic links take 0-5 %/level
        out[link] = max(out.get(link, 0), s)
    return [{"type": k, "strength": v, "mindlink": False} for k, v in sorted(out.items())]


def merge_links(link_lists):
    best = {}
    for links in link_lists:
        for l in links:
            if l["type"] not in best or l["strength"] > best[l["type"]]["strength"]:
                best[l["type"]] = dict(l)
    return sorted(best.values(), key=lambda l: l["type"])


def neut_modules(handle):
    out = []
    for m in handle.fit.modules:
        if m.isEmpty or m.item.group.name != "Energy Neutralizer" or m.state < P.FittingModuleState.ACTIVE:
            continue
        amount = _attr(m, "energyNeutralizerAmount")
        cycle = _attr(m, "duration") + _attr(m, "moduleReactivationDelay")
        if amount and cycle:
            out.append({"mod": m, "amount": amount, "cycle": cycle, "optimal": float(m.maxRange or 0),
                        "falloff": float(m.falloff or 0)})
    return out


def smartbomb_modules(handle):
    out = []
    for m in handle.fit.modules:
        if m.isEmpty or m.item.group.name != "Smart Bomb" or m.state < P.FittingModuleState.ACTIVE:
            continue
        dmg = sum(_attr(m, a) for a in ("emDamage", "thermalDamage", "kineticDamage", "explosiveDamage"))
        cycle = _attr(m, "duration")
        if dmg and cycle:
            out.append({"range": float(m.maxRange or 0), "cycle": cycle, "damage": dmg})
    return out


def missile_speed(handle):
    """Average flight speed (m/s) of loaded missiles on damage-dealing launchers (0 if none)."""
    speeds = []
    for m in handle.fit.modules:
        if m.isEmpty or m.hardpoint != P.FittingHardpoint.MISSILE or m.charge is None:
            continue
        v = _attr(m, "maxVelocity", charge=True)
        if v:
            speeds.append(v)
    return sum(speeds) / len(speeds) if speeds else 0.0


def drone_hp(handle):
    """Raw HP of the frailest active combat drone (0 if none)."""
    hps = [_attr(d, "shieldCapacity") + _attr(d, "armorHP") + _attr(d, "hp")
           for d in handle.fit.drones if d.amountActive > 0]
    return min(hps) if hps else 0.0


def is_cap_dependent(handle):
    return any(not m.isEmpty and m.state >= P.FittingModuleState.ACTIVE and _attr(m, "capacitorNeed") > 0
               for m in handle.fit.modules)


def capless_clone(handle):
    """A copy of the fit with every cap-using module switched off (what's left after being capped out)."""
    h = handle.clone()
    for m in h.fit.modules:
        if not m.isEmpty and m.state >= P.FittingModuleState.ACTIVE and _attr(m, "capacitorNeed") > 0:
            m.state = P.FittingModuleState.ONLINE
    h.recalc()
    return h


def cap_out_time(handle, neuts, dist_m):
    """Seconds until `handle` runs out of cap with `neuts` on it (inf if it stays stable), via Pyfa's sim.

    A fit that isn't cap stable on its own (logi running every rep, cap-chained) is assumed kept stable by its
    fleet (chains, boosters) until neuts land: its own deficit is topped up, so only the neuts' drain counts."""
    from eos.calc import calculateRangeFactor
    handle.recalc()
    try:
        fit = handle.fit
        if not fit.capStable:
            deficit = (fit.capUsed or 0) - (fit.capRecharge or 0)  # GJ/s
            if deficit > 0:
                fit.addDrain(fit.ship, 1000, -deficit * 1.01, 0)
        rf_any = False
        for n in neuts:
            rf = calculateRangeFactor(srcOptimalRange=n["optimal"], srcFalloffRange=n["falloff"], distance=dist_m)
            if rf > 0.01:
                rf_any = True
                fit.addDrain(n["mod"], n["cycle"], n["amount"] * rf, 0)
        if not rf_any:
            return math.inf
        fit._Fit__capStable = fit._Fit__capState = None  # re-simulate with the drains (cached otherwise)
        if fit.capStable:
            return math.inf
        return float(fit.capState)
    finally:
        handle.recalc()


def missile_survival(dist_m, speed, bombs):
    """Share of missiles that reach the target through the defenders' smartbomb fields."""
    if not bombs or speed <= 0:
        return 1.0
    survive = 1.0
    for b in bombs:
        t_in = min(dist_m, b["range"]) / speed  # seconds the missile spends inside this bomb's field
        survive *= 1 - min(1.0, t_in * 1000.0 / b["cycle"])
    return survive


def drone_lifetime(hp, bombs):
    """Seconds attacking drones survive orbiting a target covered by these smartbombs (inf if none)."""
    if not bombs or hp <= 0:
        return math.inf
    per_pulse = sum(b["damage"] for b in bombs)
    pulses = math.ceil(hp / per_pulse) if per_pulse > 0 else math.inf
    return pulses * min(b["cycle"] for b in bombs) / 1000.0
