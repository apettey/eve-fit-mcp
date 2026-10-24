"""Fleet vs fleet: focus fire, logistics, ECM, tackle, command bursts, capacitor warfare and smartbombs.

Each side is a list of ship types with counts. For a given engagement distance:
  - damage: every ship type's damage applied to every enemy type (Pyfa graph maths), with the best tackle
    the attacking fleet has in range (scram switches off MWD/MJD, webs slow, painters bloom sig);
  - EHP: each target's EHP against the attacking fleet's combined damage mix, plus its own sustained tank;
  - logistics: alive enemy logi put their remote reps on whoever is being shot (a logi can't rep itself),
    converted to EHP/s with the target's resists, reduced while the logi is jammed;
  - ECM: each jammer is assigned to the attacking side's most valuable ship (damage or reps) and jams it
    with EVE's chance formula (strength for the target's sensor type over its sensor strength, with
    range falloff); jammed ships lose that fraction of their output;
  - command bursts: loaded bursts on a side boost that whole side (fleet_effects);
  - neuts: assigned to logi first, then cap-dependent damage dealers; when Pyfa's capacitor simulation says a
    target runs dry it loses every cap-using module (active tank, remote reps, ECM, cap guns);
  - smartbombs: missiles crossing the defenders' smartbomb fields get popped; orbiting drones die;
  - focus fire: each side shoots one target at a time in a kill order; a time-stepped simulation gives
    the kill timeline, survivors and the winner. Our kill order is chosen by comparing strategies.
"""
import math

from . import application, doctrines, fleet_effects as fx, stats as stats_mod
from .errors import FitError
from .fitting import FitHandle
from .pyfa_bridge import P, lock

DT = 1.0
MAX_T = 1800.0
STRATEGIES = ("auto", "logi_first", "ewar_first", "dps_first", "squishiest_first")
SENSOR_ATTR = {"Gravimetric": "scanGravimetricStrengthBonus", "Ladar": "scanLadarStrengthBonus",
               "Magnetometric": "scanMagnetometricStrengthBonus", "Radar": "scanRadarStrengthBonus"}

ASSUMPTIONS = ("all ships at the same distance; each side's best tackle in range applies to its targets; logi "
               "assumed in range of the whole fleet and repping the current primary; ECM assigned to the most "
               "valuable enemy ships, jam chance from EVE's formula with range falloff; loaded command bursts "
               "boost their whole side (generic-link model: max skills, T2 bursts, hull bonus, no mindlink); "
               "neuts assigned to logi first, cap-out from Pyfa's cap simulation switches off cap-using modules; "
               "smartbomb fields cover the defending fleet (missiles popped per pulse, drones die); focus fire "
               "one target at a time; passive shield regen at 2/3 of peak")


class Member:
    def __init__(self, label, handle, count, fit_known=True):
        self.label, self.h, self.count, self.fit_known = label, handle, int(count), fit_known
        s = stats_mod.compute(handle)
        roles = doctrines.detect_roles(handle, s)
        self.roles, self.primary = roles["roles"], roles["primary"]
        self.dmg = application.damage_map(handle.fit)
        self.paper = sum(v.total for v in self.dmg.values())
        self.rr = float(s["remote_rep"])
        self.sensor_type = handle.fit.scanType
        self.sensor = float(handle.fit.scanStrength or 1)
        self.lock_range = float(s["lock_range"])
        self.ecm = []
        for m in handle.fit.modules:
            if not m.isEmpty and m.item.group.name in ("ECM", "Burst Jammer") and m.state >= P.FittingModuleState.ACTIVE:
                self.ecm.append({t: float(m.getModifiedItemAttr(a) or 0) for t, a in SENSOR_ATTR.items()} |
                                {"optimal": float(m.maxRange or 0), "falloff": float(m.falloff or 0)})
        self.bursts = fx.burst_links(handle)
        self.neuts = fx.neut_modules(handle)
        self.smartbombs = fx.smartbomb_modules(handle)
        self.missile_speed = fx.missile_speed(handle)
        self.drone_hp = fx.drone_hp(handle)
        self.cap_dependent = fx.is_cap_dependent(handle)
        self.missile_keys = {k for k in self.dmg if getattr(k, "hardpoint", None) == P.FittingHardpoint.MISSILE}
        self.drone_keys = {k for k in self.dmg if hasattr(k, "amountActive")}

    def refresh(self):
        self.__init__(self.label, self.h, self.count, self.fit_known)


def apply_side_bursts(members, explicit_links=None):
    """Explicit links, else the strongest of each burst any member carries, applied to the whole side."""
    links = explicit_links if explicit_links is not None else fx.merge_links(m.bursts for m in members)
    if links:
        for m in members:
            if m.h.command_links != links:
                m.h.set_command_links(links)
                m.refresh()
    return links


def _members(side, pilot, links):
    """side: doctrine name, or [{"fit_id"|"eft"|"handle", "count", "label", "pilot"}]."""
    from . import server  # fit_id lookup lives in the server's registry
    out = []
    if isinstance(side, str):
        d = doctrines.load(side)
        for e in d["ships"]:
            h, _ = doctrines.ship_handle(side, e["label"])
            out.append(Member(e["label"], h, e["count"], e["fit_known"]))
    else:
        for i, spec in enumerate(side):
            if "handle" in spec:
                h = spec["handle"].clone()
            elif "fit_id" in spec:
                h = server._get(spec["fit_id"]).clone()
            elif "eft" in spec:
                h = FitHandle(spec["eft"], spec.get("pilot", pilot))
            else:
                raise FitError("each fleet entry needs fit_id or eft")
            out.append(Member(spec.get("label") or "%s #%d" % (h.ship, i + 1), h, spec.get("count", 1)))
    apply_side_bursts(out, links)
    return out


def _target_of(member):
    """A member as a target for application maths (its own fit: sig/speed as fitted, prop running)."""
    ship = member.h.fit.ship
    return {"name": member.label, "_handle": member.h, "notes": [],
            "sig": float(ship.getModifiedItemAttr("signatureRadius")),
            "speed": float(ship.getModifiedItemAttr("maxVelocity")),
            "resists": [0, 0, 0, 0], "radius": float(ship.getModifiedItemAttr("radius") or 0)}


def _pair_tables(attackers, defenders, dist):
    """Damage of one `a` on one `d` (split into missile / drone / other), per-defender EHP/HP/tank vs the
    incoming mix, tackle, and each defender's capped-out numbers. dist in metres."""
    km = dist / 1000.0
    raw, parts, tackle = {}, {}, {}
    for d in defenders:
        tgt = _target_of(d)
        best_speed, best_sig, scrammed, webs = tgt["speed"], tgt["sig"], False, 0
        for a in attackers:
            t = application.tackle_effect(a.h.fit, tgt, km)
            best_speed, best_sig = min(best_speed, t["speed"]), max(best_sig, t["sig"])
            scrammed = scrammed or t["scrammed"]
            webs = max(webs, t["webs_in_range"])
        tackle[d.label] = {"speed": round(best_speed, 1), "sig": round(best_sig, 1), "scrammed": scrammed,
                           "webs": webs}
        for a in attackers:
            if not a.dmg:
                raw[(a.label, d.label)] = None
                continue
            r = application.applied(a.h.fit, a.dmg, tgt, km, raw=True, speed=best_speed, sig=best_sig)
            raw[(a.label, d.label)] = r
            mis = application.applied(a.h.fit, a.dmg, tgt, km, raw=True, speed=best_speed, sig=best_sig,
                                      only=a.missile_keys).total if a.missile_keys else 0.0
            dro = application.applied(a.h.fit, a.dmg, tgt, km, raw=True, speed=best_speed, sig=best_sig,
                                      only=a.drone_keys).total if a.drone_keys else 0.0
            parts[(a.label, d.label)] = {"missile": mis, "drone": dro, "other": max(0.0, r.total - mis - dro)}
    ehp, hp, tank, capless = {}, {}, {}, {}
    for d in defenders:
        mix = [0.0, 0.0, 0.0, 0.0]
        for a in attackers:
            r = raw[(a.label, d.label)]
            if r is not None:
                for i, v in enumerate((r.em, r.thermal, r.kinetic, r.explosive)):
                    mix[i] += v * a.count
        saved = d.h.damage_profile
        if sum(mix) > 0:
            d.h.set_damage_profile(mix)
        s = stats_mod.compute(d.h)
        d.h.set_damage_profile(saved)
        ehp[d.label], hp[d.label] = s["ehp_total"], max(s["hp_total"], 1.0)
        tank[d.label] = max(0.0, s["tank_sustained"] - s["shield_regen"] / 3.0)
        if d.cap_dependent:
            c = fx.capless_clone(d.h)
            try:
                if sum(mix) > 0:
                    c.set_damage_profile(mix)
                cs = stats_mod.compute(c)
                paper_c = sum(v.total for v in application.damage_map(c.fit).values())
                capless[d.label] = {"ehp": cs["ehp_total"], "hp": max(cs["hp_total"], 1.0),
                                    "tank": max(0.0, cs["tank_sustained"] - cs["shield_regen"] / 3.0),
                                    "rr": float(cs["remote_rep"]),
                                    "dmg_factor": paper_c / d.paper if d.paper else 1.0}
            finally:
                c.close()
    return raw, parts, ehp, hp, tank, tackle, capless


def _range_factor(opt, falloff, dist):
    from eos.calc import calculateRangeFactor
    return calculateRangeFactor(srcOptimalRange=opt, srcFalloffRange=falloff, distance=dist)


def _jams(jammers, victims, alive, dist, capped=frozenset()):
    """Assign alive jammer modules to the most valuable alive victims; return {victim instance: jam prob}."""
    instances = sorted(((v, i) for v in victims for i in range(v.count) if alive[(v.label, i)]),
                       key=lambda vi: -(vi[0].paper + 2 * vi[0].rr))
    mods = [mod for j in jammers for i in range(j.count)
            if alive[(j.label, i)] and (j.label, i) not in capped for mod in j.ecm]
    keep = {}
    for k, mod in enumerate(mods):
        if not instances:
            break
        v, i = instances[k % len(instances)]
        strength = mod.get(v.sensor_type, max(mod[t] for t in SENSOR_ATTR)) * _range_factor(mod["optimal"],
                                                                                              mod["falloff"], dist)
        keep[(v.label, i)] = keep.get((v.label, i), 1.0) * (1 - min(1.0, strength / v.sensor))
    return {key: 1 - p for key, p in keep.items()}


def _neut_targets(neuters, victims, alive, capped):
    """Neut modules of alive, un-capped attackers assigned round-robin to the victims' logi first, then the
    most valuable cap-dependent ships. Returns {victim instance: [neut dicts]}."""
    instances = sorted(((v, i) for v in victims for i in range(v.count)
                        if alive[(v.label, i)] and (v.label, i) not in capped and v.cap_dependent),
                       key=lambda vi: (0 if vi[0].rr > 0 else 1, -vi[0].paper))
    mods = [n for a in neuters for i in range(a.count)
            if alive[(a.label, i)] and (a.label, i) not in capped for n in a.neuts]
    out = {}
    for k, n in enumerate(mods):
        if not instances:
            break
        v, i = instances[k % len(instances)]
        out.setdefault((v.label, i), []).append(n)
    return out


def _order(strategy, our, their, raw, ehp, hp, tank, alive_their, our_dps_on):
    """Enemy ship types (alive) in this strategy's preferred kill order."""
    alive_types = [m for m in their if any(alive_their[(m.label, i)] for i in range(m.count))]
    if not alive_types:
        return []

    def ttk(m):
        dmg = our_dps_on(m)
        return ehp[m.label] / dmg if dmg > 1e-6 else math.inf

    if strategy == "logi_first":
        key = lambda m: (0 if m.rr > 0 else 1, ttk(m))
    elif strategy == "ewar_first":
        key = lambda m: (0 if m.ecm or m.primary == "ewar" else 1, ttk(m))
    elif strategy == "dps_first":
        key = lambda m: (-m.paper, ttk(m))
    elif strategy == "squishiest_first":
        key = ttk
    else:  # auto: most threat removed per second of shooting
        def key(m):
            threat = m.paper + 1.5 * m.rr * (ehp[m.label] / hp[m.label]) + 400 * len(m.ecm) + 300 * len(m.neuts)
            t = ttk(m)
            return -(threat / t) if t < math.inf else math.inf
    return sorted(alive_types, key=key)


def tables(our, their, dist):
    """Both directions' pair tables - reusable across kill orders."""
    return _pair_tables(our, their, dist), _pair_tables(their, our, dist)


def simulate(our, their, dist, our_strategy="auto", their_strategy="auto", precomputed=None):
    (raw_us, parts_us, ehp_t, hp_t, tank_t, tackle_t, capless_t), \
        (raw_them, parts_them, ehp_u, hp_u, tank_u, tackle_u, capless_u) = precomputed or tables(our, their, dist)
    side_members = {"us": our, "them": their}
    capless = {**capless_t, **capless_u}
    alive = {(m.label, i): True for side in (our, their) for m in side for i in range(m.count)}
    left = {(m.label, i): ehp_t[m.label] for m in their for i in range(m.count)}
    left.update({(m.label, i): ehp_u[m.label] for m in our for i in range(m.count)})
    cap_left = {k: 1.0 for k in alive}
    capped = set()
    cap_cache = {}
    timeline, t = [], 0.0
    switches = {"us": [], "them": []}
    target = {"us": None, "them": None}

    def bombs_of(side):
        return [b for m in side_members[side] for i in range(m.count)
                if alive[(m.label, i)] and (m.label, i) not in capped for b in m.smartbombs]

    def fleet_dps_on(attackers, parts, defender, jams, defending_bombs):
        total = 0.0
        for a in attackers:
            p = parts.get((a.label, defender.label))
            if p is None:
                continue
            missile = p["missile"] * fx.missile_survival(dist, a.missile_speed, defending_bombs)
            drone = p["drone"] if t < fx.drone_lifetime(a.drone_hp, defending_bombs) else 0.0
            per_ship = missile + drone + p["other"]
            for i in range(a.count):
                if alive[(a.label, i)]:
                    factor = capless[a.label]["dmg_factor"] if (a.label, i) in capped else 1.0
                    total += per_ship * factor * (1 - jams.get((a.label, i), 0.0))
        return total

    def ehp_basis(d, idx, ehp, hp):
        if (d.label, idx) in capped:
            return capless[d.label]["ehp"], capless[d.label]["hp"]
        return ehp[d.label], hp[d.label]

    def reps_on(defenders, defender_type, idx, ehp, hp, jams):
        reps = 0.0
        for l in defenders:
            for i in range(l.count):
                if not alive[(l.label, i)] or (l is defender_type and i == idx):
                    continue
                rr = capless[l.label]["rr"] if (l.label, i) in capped else l.rr
                reps += rr * (1 - jams.get((l.label, i), 0.0))
        e, h = ehp_basis(defender_type, idx, ehp, hp)
        return reps * e / h

    def own_tank(d, idx, tank):
        return capless[d.label]["tank"] if (d.label, idx) in capped else tank[d.label]

    stall = {"us": False, "them": False}
    while t < MAX_T:
        jams_on_us = _jams(their, our, alive, dist, capped)
        jams_on_them = _jams(our, their, alive, dist, capped)
        # capacitor warfare: each side's neuts drain their assigned targets (Pyfa cap sim per assignment)
        for neuters, victims in ((our, their), (their, our)):
            for inst, neuts in _neut_targets(neuters, victims, alive, capped).items():
                key = (inst[0], tuple(sorted(id(n["mod"]) for n in neuts)))
                if key not in cap_cache:
                    vm = next(m for m in victims if m.label == inst[0])
                    cap_cache[key] = fx.cap_out_time(vm.h, neuts, dist)
                t_out = cap_cache[key]
                if t_out < math.inf:
                    cap_left[inst] -= DT / max(t_out, DT)
                    if cap_left[inst] <= 0 and inst not in capped:
                        vm = next(m for m in victims if m.label == inst[0])
                        side = "them" if victims is their else "us"
                        full_ehp = (ehp_t if side == "them" else ehp_u)[vm.label]
                        if vm.label in capless:
                            left[inst] *= capless[vm.label]["ehp"] / full_ehp if full_ehp else 1.0
                        capped.add(inst)
                        timeline.append({"t_s": round(t + DT), "side": "us" if side == "them" else "them",
                                         "capped_out": inst[0], "instance": inst[1] + 1})
        progressed = False
        for side, atk, dfn, parts, ehp, hp, tank, jams_atk, jams_dfn, strat, def_side in (
                ("us", our, their, parts_us, ehp_t, hp_t, tank_t, jams_on_us, jams_on_them, our_strategy, "them"),
                ("them", their, our, parts_them, ehp_u, hp_u, tank_u, jams_on_them, jams_on_us, their_strategy, "us")):
            bombs = bombs_of(def_side)

            def net_on(m, idx):
                return (fleet_dps_on(atk, parts, m, jams_atk, bombs) - own_tank(m, idx, tank)
                        - reps_on(dfn, m, idx, ehp, hp, jams_dfn))

            cur = target[side]
            cur_type = next((m for m in dfn if cur and m.label == cur[0]), None)
            if cur is None or not alive[cur] or net_on(cur_type, cur[1]) <= 1e-6:
                # pick the first target in kill order we can actually break (an FC switches off a target
                # whose tank + reps out-heal the fleet)
                chosen = None
                for m in _order(strat, atk, dfn, raw_us if side == "us" else raw_them, ehp, hp, tank, alive,
                                lambda m: fleet_dps_on(atk, parts, m, jams_atk, bombs)):
                    idx = next(i for i in range(m.count) if alive[(m.label, i)])
                    if net_on(m, idx) > 1e-6:
                        chosen = (m.label, idx)
                        break
                if chosen is None:
                    stall[side] = True
                    continue
                if cur is not None and alive[cur] and chosen != cur:
                    switches[side].append({"t_s": round(t), "from": cur[0], "to": chosen[0],
                                           "reason": "can't break %s's tank + reps" % cur[0]})
                target[side] = cur = chosen
                cur_type = next(m for m in dfn if m.label == cur[0])
            net = net_on(cur_type, cur[1])
            stall[side] = False
            progressed = True
            left[cur] -= net * DT
            if left[cur] <= 0:
                alive[cur] = False
                timeline.append({"t_s": round(t + DT), "side": side, "killed": cur[0], "instance": cur[1] + 1})
        t += DT
        us_alive = sum(alive[(m.label, i)] for m in our for i in range(m.count))
        them_alive = sum(alive[(m.label, i)] for m in their for i in range(m.count))
        if not us_alive or not them_alive or not progressed:
            break
    survivors = {"us": {m.label: sum(alive[(m.label, i)] for i in range(m.count)) for m in our},
                 "them": {m.label: sum(alive[(m.label, i)] for i in range(m.count)) for m in their}}

    def destroyed(side, ehp):
        total = sum(ehp[m.label] * m.count for m in side)
        remaining = sum(max(0.0, left[(m.label, i)]) for m in side for i in range(m.count) if alive[(m.label, i)])
        return round(1 - remaining / total, 4) if total else 0.0

    ehp_destroyed = {"us": destroyed(our, ehp_u), "them": destroyed(their, ehp_t)}
    us_left, them_left = sum(survivors["us"].values()), sum(survivors["them"].values())
    kills = [e for e in timeline if "killed" in e]
    if not them_left and us_left:
        winner = "us"
    elif not us_left and them_left:
        winner = "them"
    elif not kills or (stall["us"] and stall["them"]):
        winner = "stalemate"
    else:
        frac_us = us_left / sum(m.count for m in our)
        frac_them = them_left / sum(m.count for m in their)
        winner = "us (on points)" if frac_us > frac_them else "them (on points)" if frac_them > frac_us else "even"
    kill_order_used = []
    for ev in kills:
        if ev["side"] == "us" and ev["killed"] not in kill_order_used:
            kill_order_used.append(ev["killed"])
    no_caps = frozenset()
    all_alive = {k: True for k in alive}
    return {"winner": winner, "duration_s": round(t), "timeline": timeline, "survivors": survivors,
            "our_kill_order": kill_order_used, "target_switches": switches, "ehp_destroyed": ehp_destroyed,
            "stalled": [s for s, v in stall.items() if v],
            "tackle_on_them": tackle_t, "tackle_on_us": tackle_u,
            "jams_on_us_at_start": {"%s #%d" % (k[0], k[1] + 1): round(v, 2)
                                    for k, v in _jams(their, our, all_alive, dist, no_caps).items()},
            "capped_out": {"us": sorted({e["capped_out"] for e in timeline if e.get("capped_out") and e["side"] == "them"}),
                           "them": sorted({e["capped_out"] for e in timeline if e.get("capped_out") and e["side"] == "us"})},
            "missiles_reaching": {
                "ours": {a.label: round(fx.missile_survival(dist, a.missile_speed,
                                                            [b for m in their for b in m.smartbombs * m.count]), 3)
                         for a in our if a.missile_keys},
                "theirs": {a.label: round(fx.missile_survival(dist, a.missile_speed,
                                                              [b for m in our for b in m.smartbombs * m.count]), 3)
                           for a in their if a.missile_keys}},
            "ehp_vs_incoming": {"us": {k: round(v) for k, v in ehp_u.items()},
                                "them": {k: round(v) for k, v in ehp_t.items()}}}


def fleet_score(r, our, their):
    """Scalar outcome for searches: 1 for a win (0.5 on points) + up to 0.2 for a fast win + share of the
    enemy fleet's EHP destroyed - share of ours destroyed (partial damage counts). About -1 .. 2.2."""
    # EHP-weighted, including partial damage, so a close loss outranks a rout (gives searches a gradient)
    killed = r["ehp_destroyed"]["them"]
    lost = r["ehp_destroyed"]["us"]
    win = 1.0 if r["winner"] == "us" else 0.5 if r["winner"].startswith("us") else 0.0
    fast = 0.2 * max(0.0, 1 - r["duration_s"] / MAX_T) if r["winner"] == "us" else 0.0
    return round(win + fast + killed - lost, 4)


_ENEMY_CACHE = {}


def enemy_members(doctrine, links=None):
    """Members for a saved doctrine, built once per doctrine content (FitHandles are reused)."""
    import json
    d = doctrines.load(doctrine)
    key = (json.dumps(d, sort_keys=True), json.dumps(links, sort_keys=True))
    if key not in _ENEMY_CACHE:
        _ENEMY_CACHE[key] = _members(doctrine, "All 5", links)
    return _ENEMY_CACHE[key]


class FleetObjective:
    """Score a candidate fit as `count` copies (plus optional support ships) vs a saved doctrine.

    calibrate() runs every kill-order strategy on a baseline fit and keeps the winners, so each candidate
    is scored with those orders only (fast): the best score across them and, per range_choice, the best
    range ("best": we pick the engagement range) or the average over ranges ("all": robust)."""

    def __init__(self, doctrine, count=1, distances_km=(10,), support=None, range_choice="best",
                 their_order="auto", links=None, their_links=None):
        if range_choice not in ("best", "all"):
            raise FitError("range_choice must be 'best' or 'all'")
        self.doctrine, self.count, self.distances = doctrine, int(count), [float(d) for d in distances_km]
        self.enemy = enemy_members(doctrine, their_links)
        self.support = _members(support, "All 5", links) if support else []
        for m in self.support:
            m.label = "support " + m.label
        self.range_choice, self.their_order, self.links = range_choice, their_order, links
        self.orders = {d: ("auto", "ewar_first", "logi_first") for d in self.distances}

    def calibrate(self, handle):
        with lock():
            us, restore = self._us(handle)
            try:
                for d in self.distances:
                    tb = tables(us, self.enemy, d * 1000)
                    runs = {s: simulate(us, self.enemy, d * 1000, s, self.their_order, tb) for s in STRATEGIES}
                    ranked = sorted(runs, key=lambda s: fleet_score(runs[s], us, self.enemy), reverse=True)
                    self.orders[d] = tuple(dict.fromkeys([ranked[0], ranked[1], "auto"]))
            finally:
                restore()
        return self.orders

    def _us(self, handle):
        """Our side for one evaluation: the candidate (with side bursts applied temporarily) + support."""
        saved = list(handle.command_links)
        cand = Member("candidate", handle, self.count)
        us = [cand] + self.support
        apply_side_bursts(us, self.links)

        def restore():
            if handle.command_links != saved:
                handle.set_command_links(saved)
        return us, restore

    def __call__(self, handle):
        with lock():
            us, restore = self._us(handle)
            try:
                per = {}
                for d in self.distances:
                    best = None
                    tb = tables(us, self.enemy, d * 1000)
                    for order in self.orders[d]:
                        r = simulate(us, self.enemy, d * 1000, order, self.their_order, tb)
                        sc = fleet_score(r, us, self.enemy)
                        if best is None or sc > best[0]:
                            best = (sc, order, r["winner"])
                    per[d] = best
            finally:
                restore()
            scores = [v[0] for v in per.values()]
            score = max(scores) if self.range_choice == "best" else sum(scores) / len(scores)
            return {"score": score, "by_distance": {"%g" % d: {"score": v[0], "order": v[1], "winner": v[2]}
                                                     for d, v in per.items()}}


def _score(r, our):
    win = 2 if r["winner"] == "us" else 1 if r["winner"].startswith("us") else 0
    ours_left = sum(r["survivors"]["us"].values())
    theirs_left = sum(r["survivors"]["them"].values())
    return (win, ours_left, -theirs_left, -r["duration_s"])


def battle(our_side, their_side, distances_km=(10,), our_order="compare", their_order="auto",
           our_links=None, their_links=None, pilot="All 5"):
    P.load()
    if our_order != "compare" and our_order not in STRATEGIES:
        raise FitError("our_order must be 'compare' or one of %s" % ", ".join(STRATEGIES))
    if their_order not in STRATEGIES:
        raise FitError("their_order must be one of %s" % ", ".join(STRATEGIES))
    with lock():
        our = _members(our_side, pilot, our_links)
        their = _members(their_side, pilot, their_links)
        try:
            labels = [m.label for m in our + their]
            if len(set(labels)) != len(labels):
                for m in our:
                    m.label = "our " + m.label
            results = []
            for d in distances_km:
                dist = float(d) * 1000
                strategies = STRATEGIES if our_order == "compare" else (our_order,)
                tb = tables(our, their, dist)
                runs = {s: simulate(our, their, dist, s, their_order, tb) for s in strategies}
                best = max(runs, key=lambda s: _score(runs[s], our))
                summary = {s: {"winner": r["winner"], "duration_s": r["duration_s"],
                               "our_losses": sum(m.count for m in our) - sum(r["survivors"]["us"].values()),
                               "their_losses": sum(m.count for m in their) - sum(r["survivors"]["them"].values()),
                               "kill_order": r["our_kill_order"]} for s, r in runs.items()}
                results.append({"distance_km": d, "best_order": best, "result": runs[best],
                                "orders_compared": summary})

            def side_info(members, enemy_side):
                return [{"label": m.label, "count": m.count, "role": m.primary, "paper_dps": round(m.paper),
                         "remote_rep_hp_s": round(m.rr), "sensor": "%s %.1f" % (m.sensor_type, m.sensor),
                         "jammers": len(m.ecm), "neuts": len(m.neuts), "smartbombs": len(m.smartbombs),
                         "cap_dependent": m.cap_dependent, "fit_known": m.fit_known} for m in members]

            bursts = {"us": our[0].h.command_links if our else [], "them": their[0].h.command_links if their else []}
            return {"sides": {"us": side_info(our, their), "them": side_info(their, our)}, "command_bursts": bursts,
                    "by_distance": results, "insights": _insights(results, our, their, bursts),
                    "assumptions": ASSUMPTIONS}
        finally:
            for m in our + their:
                m.h.close()


def _insights(results, our, their, bursts=None):
    out = []
    wins = [r["distance_km"] for r in results if r["result"]["winner"].startswith("us")]
    losses = [r["distance_km"] for r in results if r["result"]["winner"].startswith("them")]
    if wins:
        out.append("We win at %s km." % ", ".join("%g" % d for d in wins))
    if losses:
        out.append("We lose at %s km." % ", ".join("%g" % d for d in losses))
    if bursts:
        for side, name in (("us", "Our"), ("them", "Their")):
            if bursts.get(side):
                out.append("%s command bursts: %s." % (name, ", ".join(
                    "%s (%d%%/lvl)" % (l["type"], l["strength"]) for l in bursts[side])))
    for r in results:
        res = r["result"]
        d = r["distance_km"]
        if "us" in res["stalled"]:
            out.append("At %g km we can't break some targets' tank + reps: more DPS, alpha, neuts on logi, "
                       "or kill the logi first." % d)
        jams = res["jams_on_us_at_start"]
        if jams:
            worst = max(jams.items(), key=lambda kv: kv[1])
            out.append("At %g km their ECM jams up to %d%% of the time (%s): ECCM/sensor strength or primary "
                       "the ECM ships." % (d, round(100 * worst[1]), worst[0]))
        if res["capped_out"]["them"]:
            out.append("At %g km our neuts cap out their %s (active tank/reps/ECM switch off)."
                       % (d, ", ".join(res["capped_out"]["them"])))
        if res["capped_out"]["us"]:
            out.append("At %g km their neuts cap out our %s - bring cap boosters or kill the neut ships."
                       % (d, ", ".join(res["capped_out"]["us"])))
        for who, label in (("ours", "Our"), ("theirs", "Their")):
            popped = {k: v for k, v in res["missiles_reaching"][who].items() if v < 0.95}
            if popped:
                out.append("At %g km smartbombs pop missiles: %s missiles reaching the target - %s." % (
                    d, label.lower(), ", ".join("%s %d%%" % (k, round(100 * v)) for k, v in popped.items())))
        out.append("At %g km the best kill order is '%s': %s." % (
            d, r["best_order"], " -> ".join(res["our_kill_order"]) or "no kills"))
        unbreakable = sorted({sw["from"] for sw in res["target_switches"]["us"]})
        if unbreakable:
            out.append("At %g km we had to switch off %s (their tank + logi reps out-healed our damage)."
                       % (d, ", ".join(unbreakable)))
        worst = [s for s, v in r["orders_compared"].items() if not v["winner"].startswith("us")]
        if worst and r["result"]["winner"].startswith("us"):
            out.append("At %g km the kill order matters: %s lose(s)." % (d, ", ".join(worst)))
    unknown = [m.label for m in their if not m.fit_known]
    if unknown:
        out.append("Enemy fits unknown for %s (bare hulls) - treat their part of the result as a guess."
                   % ", ".join(unknown))
    return out
