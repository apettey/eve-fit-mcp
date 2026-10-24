"""Fit vs fit: who kills whom, and how fast.

For each side: damage applied to the other ship's real signature/speed/resists at a distance (Pyfa's graph
maths), the other side's EHP *against that exact damage mix*, minus its sustained repair, gives a
time-to-kill. ratio = their time-to-kill-us / our time-to-kill-them; > 1 means we win the trade.

Simplifications (stated in every result): both ships at full speed at the given distance with the target
moving perpendicular; no neuts/ECM/remote reps/boosts beyond what's in the fits; focus fire with `count`
ships per side; capacitor not simulated (cap-starved active tanks are optimistic).
"""
from . import application, stats as stats_mod
from .errors import FitError
from .pyfa_bridge import P, lock

INF = 1e9


def _raw_applied_split(attacker_fit, target, distance_km):
    """Applied damage per type BEFORE the target's resists (DmgTypes), using the target's sig/speed."""
    dmg = application.damage_map(attacker_fit)
    if not dmg:
        return None
    from eos.saveddata.targetProfile import TargetProfile
    from graphs.data.fitDamageStats.calc.application import getApplicationPerKey
    from graphs.data.fitDamageStats.getter import applyDamage
    from graphs.wrapper import SourceWrapper, TargetWrapper
    if "_handle" in target:
        tgt = TargetWrapper(target["_handle"].fit, 0, 0)  # for lock range / radius
    else:
        tgt = TargetWrapper(TargetProfile(0, 0, 0, 0, maxVelocity=target["speed"], signatureRadius=target["sig"],
                                          radius=target["radius"]), 0, 0)
    appl = getApplicationPerKey(src=SourceWrapper(attacker_fit, None), tgt=tgt, atkSpeed=0, atkAngle=0,
                                distance=float(distance_km) * 1000, tgtSpeed=target["speed"], tgtAngle=90.0,
                                tgtSigRadius=target["sig"])
    return applyDamage(dmg, {k: appl.get(k, 0) for k in dmg}, (0.0, 0.0, 0.0, 0.0), None)


def _side(attacker, defender_handle, defender_target, distance_km, attackers, defenders):
    split = _raw_applied_split(attacker.fit, defender_target, distance_km)
    raw = float(split.total) if split is not None else 0.0
    out = {"applied_dps_raw": round(raw, 1)}
    if raw <= 0:
        out.update({"ehp_vs_this_damage": None, "time_to_kill_s": INF, "applied_dps_after_resists": 0.0})
        return out
    profile = (split.em, split.thermal, split.kinetic, split.explosive)
    saved = defender_handle.damage_profile
    defender_handle.set_damage_profile(profile)  # EHP and reps measured against this exact damage mix
    try:
        s = stats_mod.compute(defender_handle)
    finally:
        defender_handle.set_damage_profile(saved)
    ehp = s["ehp_total"]
    # Passive shield regen peaks at ~25-35% shield; averaged over burning a full shield from 100% to 0 it is
    # 2/3 of that peak (mean of sqrt(s)-s on [0,1] is 1/6 vs a peak of 1/4). Active reps count in full.
    reps_one = s["tank_sustained"] - s["shield_regen"] / 3.0  # focus fire: one defender at a time
    total_raw = raw * attackers
    net = total_raw - reps_one
    ttk_one = ehp / net if net > 1e-6 else INF
    hp = s["hp_total"]
    out.update({
        "applied_dps_after_resists": round(raw * hp / ehp, 1) if ehp else 0.0,
        "damage_mix": {k: round(v / raw, 3) for k, v in zip(("em", "thermal", "kinetic", "explosive"), profile)},
        "ehp_vs_this_damage": round(ehp), "their_sustained_rep_ehp_s": round(reps_one, 1),
        "time_to_kill_one_s": round(ttk_one, 1) if ttk_one < INF else None,
        "time_to_kill_all_s": round(ttk_one * defenders, 1) if ttk_one < INF else None,
    })
    out["_ttk_all"] = ttk_one * defenders
    return out


def matchup(ours, enemy_spec, distance_km=10, our_count=1, their_count=1):
    """ours: FitHandle. enemy_spec: EFT text, {"eft":..., "pilot":...}, or {"ship": name, "prop": ...}."""
    P.load()
    if our_count < 1 or their_count < 1:
        raise FitError("counts must be >= 1")
    with lock():
        enemy = application.resolve_target(enemy_spec)
        if "_handle" not in enemy:
            raise FitError("matchup needs an enemy fit: EFT text, {'eft': ...} or {'ship': name, 'prop': 'mwd'}")
        own_target = application.resolve_target({"eft": ours.eft(), "pilot": ours.pilot, "name": "us"})
        try:
            # the clone used as "us"-as-target must carry our live state (drones, links, profile)
            us_handle = own_target["_handle"]
            us_handle.set_command_links(ours.command_links)
            attack = _side(ours, enemy["_handle"], enemy, distance_km, our_count, their_count)
            defend = _side(enemy["_handle"], us_handle, own_target, distance_km, their_count, our_count)
            t_them, t_us = attack.pop("_ttk_all", INF), defend.pop("_ttk_all", INF)
            if t_them >= INF and t_us >= INF:
                ratio, verdict = 1.0, "stalemate: neither side can break the other's tank at this range"
            elif t_them >= INF:
                ratio, verdict = 0.0, "we can't break their tank at this range"
            elif t_us >= INF:
                ratio, verdict = INF, "they can't break our tank at this range"
            else:
                ratio = t_us / t_them
                verdict = ("we win" if ratio > 1.05 else "they win" if ratio < 0.95 else "even") + \
                          " (%.2fx)" % ratio
            return {
                "us": {"fit": "%s, %s" % (ours.ship, ours.name), "count": our_count, "pilot": ours.pilot},
                "them": {"fit": enemy["name"], "count": their_count, "notes": enemy["notes"]},
                "distance_km": distance_km,
                "we_deal": attack, "they_deal": defend,
                "ratio": round(ratio, 3) if ratio < INF else None, "verdict": verdict,
                "assumptions": "both ships at max speed (prop as fitted), target moving perpendicular; focus "
                               "fire; no ewar/neuts/remote reps/boosts beyond the fits; capacitor not simulated "
                               "(active tanks assumed to run); passive shield regen counted at 2/3 of peak; "
                               "EHP and reps measured against the attacker's applied damage mix",
            }
        finally:
            application.close_targets([enemy, own_target])


def ratio(ours, enemy_spec, distance_km=10, our_count=1, their_count=1):
    """Scalar for sweeps/optimisers: their time to kill us / our time to kill them (capped at 100)."""
    r = matchup(ours, enemy_spec, distance_km, our_count, their_count)
    return 100.0 if r["ratio"] is None else min(float(r["ratio"]), 100.0)
