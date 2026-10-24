"""Fit vs fit: who kills whom, how fast, and at which ranges.

For each side: damage applied to the other ship at a distance (Pyfa's graph maths) after the attacker's own
tackle lands (warp scrams switch off MWDs/MJDs, webs slow - stacking penalised, painters bloom signature, all
within their real ranges), the other side's EHP *against that exact damage mix*, minus its sustained repair,
gives a time-to-kill. ratio = their time-to-kill-us / our time-to-kill-them; > 1 means we win the trade.

envelope() repeats this over several distances: a comp can win the kite and lose the brawl.

Simplifications (stated in every result): both ships at full speed with the target moving perpendicular;
no ECM/neuts/remote reps/boosts beyond what's in the fits; focus fire with `count` ships per side;
capacitor not simulated (active tanks assumed to run); passive shield regen counted at 2/3 of peak.
"""
from . import application, stats as stats_mod
from .errors import FitError
from .pyfa_bridge import P, lock

INF = 1e9
DEFAULT_ENVELOPE_KM = (2, 7, 12, 20, 30, 45, 60)


def _side(attacker, defender_handle, defender_target, distance_km, attackers, defenders, tackle=True):
    dmg = application.damage_map(attacker.fit)
    split = application.applied(attacker.fit, dmg, defender_target, distance_km, raw=True, tackle=tackle) \
        if dmg else None
    raw = float(split.total) if split is not None else 0.0
    tk = application.tackle_effect(attacker.fit, defender_target, distance_km) if tackle else None
    out = {"applied_dps_raw": round(raw, 1)}
    if tk:
        out["tackle_on_target"] = {"scrammed": tk["scrammed"], "webs": tk["webs_in_range"],
                                   "painters": tk["painters_in_range"], "target_speed": round(tk["speed"], 1),
                                   "target_sig": round(tk["sig"], 1)}
    if raw <= 0:
        out.update({"ehp_vs_this_damage": None, "time_to_kill_all_s": None})
        out["_ttk_all"] = INF
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
    net = raw * attackers - reps_one
    ttk_one = ehp / net if net > 1e-6 else INF
    hp = s["hp_total"]
    out.update({
        "applied_dps_after_resists": round(raw * hp / ehp, 1) if ehp else 0.0,
        "damage_mix": {k: round(v / raw, 3) for k, v in zip(("em", "thermal", "kinetic", "explosive"), profile)},
        "ehp_vs_this_damage": round(ehp), "their_sustained_rep_ehp_s": round(reps_one, 1),
        "time_to_kill_one_s": round(ttk_one, 1) if ttk_one < INF else None,
        "time_to_kill_all_s": round(ttk_one * defenders, 1) if ttk_one < INF else None,
    })
    out["_ttk_all"] = ttk_one * defenders if ttk_one < INF else INF
    return out


def _verdict(t_them, t_us):
    if t_them >= INF and t_us >= INF:
        return 1.0, "stalemate: neither side can break the other's tank at this range"
    if t_them >= INF:
        return 0.0, "they win: we can't break their tank at this range"
    if t_us >= INF:
        return INF, "we win: they can't break our tank at this range"
    ratio = t_us / t_them
    return ratio, ("we win" if ratio > 1.05 else "they win" if ratio < 0.95 else "even") + " (%.2fx)" % ratio


def _resolve_pair(ours, enemy_spec, our_prop, their_prop):
    if isinstance(enemy_spec, str) and not enemy_spec.strip().startswith("["):
        enemy_spec = {"preset": enemy_spec}  # a preset profile: rejected below with a clear message
    enemy_spec = dict(enemy_spec) if isinstance(enemy_spec, dict) else {"eft": enemy_spec}
    if their_prop and "preset" not in enemy_spec:
        enemy_spec["prop_state"] = their_prop
    enemy = application.resolve_target(enemy_spec)
    if "_handle" not in enemy:
        application.close_targets([enemy])
        raise FitError("matchup needs an enemy fit: EFT text, {'eft': ...} or {'ship': name, 'prop': 'mwd'}")
    us_spec = {"eft": ours.eft(), "pilot": ours.pilot, "name": "us"}
    if our_prop:
        us_spec["prop_state"] = our_prop
    own_target = application.resolve_target(us_spec)
    own_target["_handle"].set_command_links(ours.command_links)
    return enemy, own_target


def _attacker_for(target, original):
    """Which handle shoots: the original fit, or (when prop state changed) the resolved clone."""
    return target["_handle"] if "prop modules off" in target.get("notes", []) else original


def _one(ours, enemy, own_target, distance_km, our_count, their_count, tackle):
    us_attacker = _attacker_for(own_target, ours)
    attack = _side(us_attacker, enemy["_handle"], enemy, distance_km, our_count, their_count, tackle)
    defend = _side(enemy["_handle"], own_target["_handle"], own_target, distance_km, their_count, our_count, tackle)
    t_them, t_us = attack.pop("_ttk_all"), defend.pop("_ttk_all")
    ratio, verdict = _verdict(t_them, t_us)
    return {"distance_km": distance_km, "we_deal": attack, "they_deal": defend,
            "ratio": round(ratio, 3) if ratio < INF else None, "verdict": verdict}


ASSUMPTIONS = ("both ships at max speed, target moving perpendicular; each side's own scram/webs/painters "
               "applied to the other within range (scram switches off MWD/MJD); focus fire; no ECM/neuts/remote "
               "reps/boosts beyond the fits; capacitor not simulated (active tanks assumed to run); passive "
               "shield regen at 2/3 of peak; EHP and reps measured against the attacker's applied damage mix")


def matchup(ours, enemy_spec, distance_km=10, our_count=1, their_count=1, our_prop=None, their_prop=None,
            tackle=True):
    """ours: FitHandle. enemy_spec: EFT text, {"eft":..., "pilot":...}, or {"ship": name, "prop": ...}.
    our_prop / their_prop: None (as fitted) | "on" | "off"."""
    P.load()
    if our_count < 1 or their_count < 1:
        raise FitError("counts must be >= 1")
    with lock():
        enemy, own_target = _resolve_pair(ours, enemy_spec, our_prop, their_prop)
        try:
            r = _one(ours, enemy, own_target, distance_km, our_count, their_count, tackle)
            return {"us": {"fit": "%s, %s" % (ours.ship, ours.name), "count": our_count, "pilot": ours.pilot,
                           "prop": our_prop or "as fitted"},
                    "them": {"fit": enemy["name"], "count": their_count, "notes": enemy["notes"],
                             "prop": their_prop or "as fitted"},
                    **r, "assumptions": ASSUMPTIONS}
        finally:
            application.close_targets([enemy, own_target])


def envelope(ours, enemy_spec, distances_km=DEFAULT_ENVELOPE_KM, our_count=1, their_count=1, our_prop=None,
             their_prop=None, tackle=True):
    """Verdict at each distance: where each side wins (e.g. our kite band vs their brawl band)."""
    P.load()
    with lock():
        enemy, own_target = _resolve_pair(ours, enemy_spec, our_prop, their_prop)
        try:
            rows = []
            for d in distances_km:
                r = _one(ours, enemy, own_target, d, our_count, their_count, tackle)
                w, t = r["we_deal"], r["they_deal"]
                rows.append({"distance_km": d, "verdict": r["verdict"], "ratio": r["ratio"],
                             "our_ttk_s": w.get("time_to_kill_all_s"), "their_ttk_s": t.get("time_to_kill_all_s"),
                             "our_raw_dps": w["applied_dps_raw"], "their_raw_dps": t["applied_dps_raw"],
                             "we_tackle_them": w.get("tackle_on_target"),
                             "they_tackle_us": t.get("tackle_on_target")})
            wins = [r["distance_km"] for r in rows if r["ratio"] is None or r["ratio"] > 1.05]
            losses = [r["distance_km"] for r in rows if r["ratio"] is not None and r["ratio"] < 0.95]
            return {"us": "%s, %s" % (ours.ship, ours.name), "them": enemy["name"],
                    "counts": [our_count, their_count], "by_distance": rows,
                    "we_win_at_km": wins, "they_win_at_km": losses,
                    "advice": _advice(wins, losses), "assumptions": ASSUMPTIONS}
        finally:
            application.close_targets([enemy, own_target])


def _advice(wins, losses):
    if wins and not losses:
        return "we win at every tested range"
    if losses and not wins:
        return "we lose at every tested range: change the fit, the numbers or the target"
    if wins and losses:
        return ("fight at %s km (we win there); avoid %s km" %
                (", ".join("%g" % d for d in wins), ", ".join("%g" % d for d in losses)))
    return "no decisive range: stalemate or even everywhere tested"


def ratio(ours, enemy_spec, distance_km=10, our_count=1, their_count=1):
    """Scalar for sweeps/optimisers: their time to kill us / our time to kill them (capped at 100)."""
    r = matchup(ours, enemy_spec, distance_km, our_count, their_count)
    return 100.0 if r["ratio"] is None else min(float(r["ratio"]), 100.0)
