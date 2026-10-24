"""MCP server exposing Pyfa's fitting engine for iterative fit optimisation.

Run:  python -m eve_fit_mcp.server   (stdio transport)
"""
import contextlib
import functools
import os
import re
import sys

from mcp.server.mcpserver import MCPServer

from . import catalog, optimizer, stats as stats_mod
from .fitting import FitError, FitHandle
from .pyfa_bridge import ROOT, boot, on_engine

FITS_DIR = os.path.join(ROOT, "fits")

DAMAGE_PRESETS = {
    "omni": (25, 25, 25, 25),
    "em": (100, 0, 0, 0),
    "thermal": (0, 100, 0, 0),
    "kinetic": (0, 0, 100, 0),
    "explosive": (0, 0, 0, 100),
}

INSTRUCTIONS = """\
EVE Online fitting engine (Pyfa). Typical loop:
1. load_fit with EFT text (or load_saved_fit) -> fit_id.
2. get_stats / get_fit to inspect; list_candidates + try_candidates for what-if comparisons.
3. set_module / set_charge / set_drones for manual edits; clone_fit to branch ideas.
4. optimize_fit with an objective (metric weights) and constraints for automated search;
   the result is loaded as a new fit_id - inspect it, tweak, re-optimise with different pools.
5. save_fit writes EFT into the fits/ library (paste straight into EVE).
Slots are addressed as 'high:0', 'med:1', 'low:2', 'rig:0'. Metrics: see list_metrics.
Stats assume all skills at skill_level (default 5) and the fit's damage profile for EHP."""

mcp = MCPServer("eve-fit", instructions=INSTRUCTIONS)
_fits = {}


def _quiet(fn):
    """Run on the Pyfa engine thread, keep Pyfa's stdout chatter off the stdio transport,
    and turn FitErrors into clean tool errors."""
    def call(*args, **kwargs):
        with contextlib.redirect_stdout(sys.stderr):
            return fn(*args, **kwargs)

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return on_engine(call, *args, **kwargs)
        except FitError as e:
            raise ValueError(str(e)) from None
    return wrapper


def _tool(fn):
    return mcp.tool()(_quiet(fn))


def _get(fit_id):
    h = _fits.get(int(fit_id))
    if h is None:
        raise FitError("No fit %s loaded; loaded: %s" % (fit_id, sorted(_fits) or "none"))
    return h


def _profile(p):
    if p is None:
        return None
    if isinstance(p, str):
        if p not in DAMAGE_PRESETS:
            raise FitError("damage_profile preset must be one of %s or [em, th, kin, exp]" % ", ".join(DAMAGE_PRESETS))
        return DAMAGE_PRESETS[p]
    return tuple(p)


def _register(h):
    _fits[h.id] = h
    return h


def _summary(h, metrics=None):
    return stats_mod.summarize(stats_mod.compute(h), metrics)


def _delta(before, after, keys):
    return {k: round(after[k] - before[k], 3) for k in keys
            if isinstance(before.get(k), (int, float)) and abs(after[k] - before[k]) > 1e-9}


DELTA_KEYS = ["dps_total", "volley", "ehp_total", "tank_sustained", "cap_stable", "cap_lasts_s", "max_speed",
              "align_time", "signature", "lock_range", "scan_res", "cpu_free", "pg_free", "calibration_free"]


# ---- fits ---------------------------------------------------------------------------------------

@_tool
def load_fit(eft: str, skill_level: int = 5, damage_profile: str | list[float] | None = None,
             drones_active: bool = True) -> dict:
    """Load a fit from EFT text (Pyfa/EVE clipboard format). Returns fit_id, slot layout and stats.

    damage_profile: incoming damage for EHP - preset (omni/em/thermal/kinetic/explosive) or [em, th, kin, exp].
    drones_active: launch drones in bay order up to bandwidth / max active (EFT import leaves them docked).
    """
    h = _register(FitHandle(eft, skill_level, _profile(damage_profile) or DAMAGE_PRESETS["omni"], drones_active))
    return {"fit_id": h.id, "slots": h.slots(), "stats": _summary(h)}


@_tool
def list_fits() -> dict:
    """Fits loaded in this session."""
    out = []
    for h in _fits.values():
        s = stats_mod.compute(h)
        out.append({"fit_id": h.id, "ship": h.ship, "name": h.name, "valid": s["valid"],
                    "dps_total": round(s["dps_total"], 1), "ehp_total": round(s["ehp_total"])})
    return {"fits": out}


@_tool
def get_fit(fit_id: int) -> dict:
    """EFT text and slot layout of a loaded fit."""
    h = _get(fit_id)
    return {"fit_id": h.id, "ship": h.ship, "name": h.name, "skill_level": h.skill_level,
            "damage_profile": list(h.damage_profile), "eft": h.eft(), "slots": h.slots()}


@_tool
def get_stats(fit_id: int, metrics: list[str] | None = None, full: bool = False) -> dict:
    """Stats for a fit. full=True returns every metric (resists per type, resources, etc.)."""
    h = _get(fit_id)
    s = stats_mod.compute(h)
    return s if full else stats_mod.summarize(s, metrics)


@_tool
def clone_fit(fit_id: int, name: str | None = None) -> dict:
    """Copy a fit to branch an idea without losing the original."""
    h = _register(_get(fit_id).clone(name))
    return {"fit_id": h.id, "name": h.name}


@_tool
def close_fit(fit_id: int) -> dict:
    """Unload a fit from the session."""
    _get(fit_id).close()
    del _fits[int(fit_id)]
    return {"closed": fit_id}


@_tool
def set_fit_options(fit_id: int, skill_level: int | None = None,
                    damage_profile: str | list[float] | None = None) -> dict:
    """Change all-skills level (0-5) and/or the incoming damage profile used for EHP."""
    h = _get(fit_id)
    if skill_level is not None:
        h.set_skill_level(skill_level)
    if damage_profile is not None:
        h.set_damage_profile(_profile(damage_profile))
    return _summary(h)


@_tool
def compare_fits(fit_ids: list[int], metrics: list[str] | None = None) -> dict:
    """Side-by-side stats for several fits."""
    keys = metrics or DELTA_KEYS
    rows = {}
    for fid in fit_ids:
        s = stats_mod.compute(_get(fid))
        rows[str(fid)] = {"name": s["name"], "valid": s["valid"],
                          **{k: round(s[k], 3) for k in keys if isinstance(s.get(k), (int, float))}}
    return {"fits": rows}


# ---- editing ------------------------------------------------------------------------------------

@_tool
def set_module(fit_id: int, slot: str, item: str | None, charge: str | None = "keep",
               state: str | None = None) -> dict:
    """Put `item` in `slot` (e.g. 'low:2'); item=null empties it.

    charge: 'keep' (default) keeps the current charge if compatible, null unloads, or a charge name.
    state: offline/online/active/overheated (default: active where possible).
    Returns the stat deltas so you can judge the change immediately.
    """
    h = _get(fit_id)
    before = stats_mod.compute(h)
    warnings = h.set_module(slot, item, charge=charge, state=state)
    after = stats_mod.compute(h)
    return {"warnings": warnings, "delta": _delta(before, after, DELTA_KEYS),
            "valid": after["valid"], "issues": after["issues"]}


@_tool
def set_charge(fit_id: int, slots: list[str], charge: str | None) -> dict:
    """Load `charge` into the modules at `slots` (e.g. all turrets). null unloads."""
    h = _get(fit_id)
    before = stats_mod.compute(h)
    current = h.slots_by_key()
    for k in slots:
        item = current[k]["item"]
        if item is None:
            raise FitError("%s is empty" % k)
        h.set_module(k, item, charge=charge, state=current[k]["state"], recalc=False)
    h.recalc()
    after = stats_mod.compute(h)
    return {"delta": _delta(before, after, DELTA_KEYS), "valid": after["valid"], "issues": after["issues"]}


@_tool
def set_drones(fit_id: int, drones: dict[str, int]) -> dict:
    """Replace the drone bay, e.g. {"Hobgoblin II": 5}. Heaviest drones launched first within bandwidth."""
    h = _get(fit_id)
    before = stats_mod.compute(h)
    h.set_drones(drones)
    after = stats_mod.compute(h)
    return {"delta": _delta(before, after, DELTA_KEYS), "valid": after["valid"], "issues": after["issues"]}


# ---- items --------------------------------------------------------------------------------------

@_tool
def search_items(query: str, kind: str | None = None, limit: int = 25) -> dict:
    """Search published items by name. kind: module/rig/charge/drone/fighter/implant/booster/subsystem/ship."""
    return {"items": catalog.search(query, kind, limit)}


@_tool
def list_candidates(fit_id: int, slot: str, mode: str = "variants", pool: list[str] | None = None,
                    max_meta_level: int | None = None, exclude_meta: list[str] | None = None) -> dict:
    """Modules that could go in `slot`. mode: variants (meta variants of current module) | group |
    market_group | pool (only `pool` names). exclude_meta defaults to ['Officer']."""
    h = _get(fit_id)
    items = catalog.module_candidates(h, slot, mode, pool,
                                      catalog.DEFAULT_EXCLUDE_META if exclude_meta is None else exclude_meta,
                                      max_meta_level)
    return {"slot": slot, "candidates": [catalog.describe(i) for i in items]}


@_tool
def try_candidates(fit_id: int, slot: str, items: list[str] | None = None, mode: str = "variants",
                   metrics: list[str] | None = None, max_meta_level: int | None = None) -> dict:
    """What-if: evaluate each candidate in `slot` and report stat deltas vs the current fit.
    The fit is left unchanged. Give `items`, or let `mode` generate candidates."""
    h = _get(fit_id)
    keys = metrics or DELTA_KEYS
    base = stats_mod.compute(h)
    if items is None:
        items = [i.typeName for i in catalog.module_candidates(h, slot, mode, None,
                                                               catalog.DEFAULT_EXCLUDE_META, max_meta_level)]
    saved = h.module_info(slot)
    rows = []
    try:
        for name in items:
            try:
                h.set_module(slot, name)
            except FitError as e:
                rows.append({"item": name, "error": str(e)})
                continue
            s = stats_mod.compute(h)
            rows.append({"item": name, "valid": s["valid"], "delta": _delta(base, s, keys)})
            h.restore_module(slot, saved)
    finally:
        h.restore_module(slot, saved)
    return {"slot": slot, "current": h.slots_by_key()[slot]["item"], "results": rows}


@_tool
def list_metrics() -> dict:
    """Metric names usable in objectives/constraints, plus damage profile presets."""
    return {"metrics": stats_mod.METRICS, "damage_presets": DAMAGE_PRESETS}


# ---- optimisation -------------------------------------------------------------------------------

@_tool
def optimize_fit(fit_id: int, objective: dict[str, float], constraints: dict | None = None,
                 slots: list[str] | None = None, mode: str = "variants",
                 pools: dict[str, list[str]] | None = None, locked: list[str] | None = None,
                 allow_empty: bool = False, optimize_charges: bool = True, group_identical: bool = True,
                 strategy: str = "greedy", max_evals: int = 5000, normalize: bool = True,
                 exclude_meta: list[str] | None = None, max_meta_level: int | None = None,
                 time_limit_s: float = 120) -> dict:
    """Search module/charge choices to maximise `objective` subject to `constraints`.

    objective: metric weights, e.g. {"dps_total": 1, "ehp_total": 0.5, "align_time": -0.3}.
        normalize=True (default) scales each metric by its starting value, so weights = importance of % change.
    constraints: {"cap_stable": 1, "ehp_total": {"min": 8000}, "align_time": {"max": 6}} (number = min).
        Fitting resources (CPU/PG/calibration/slots/hardpoints) are always enforced.
    slots: keys ('low:0') or racks ('low', 'med', 'high', 'rig'); default all. locked: keys to leave alone.
    mode: candidate source per slot - variants | group | market_group | pool.
    pools: extra candidates per rack or key, e.g. {"low": ["Damage Control II", "Gyrostabilizer II"]}.
        Pools are how empty slots get filled and how different module types are considered.
    strategy: greedy (coordinate descent) or exhaustive (all combos per rack, within max_evals).
    The original fit is untouched; the optimised fit is loaded as a new fit_id.
    """
    h = _get(fit_id)
    res = optimizer.optimize(
        h, objective, constraints, slots or "all", mode, pools, locked, allow_empty, optimize_charges,
        group_identical, strategy, max_evals=max_evals, normalize=normalize,
        exclude_meta=catalog.DEFAULT_EXCLUDE_META if exclude_meta is None else tuple(exclude_meta),
        max_meta_level=max_meta_level, time_limit_s=time_limit_s)
    new = _register(FitHandle(res["eft"], h.skill_level, h.damage_profile))
    res["fit_id"] = new.id
    return res


# ---- library ------------------------------------------------------------------------------------

def _safe_name(name):
    name = re.sub(r"[^\w\- .]+", "_", name).strip(" .")
    if not name:
        raise FitError("bad file name")
    return name if name.endswith(".eft") else name + ".eft"


@_tool
def save_fit(fit_id: int, filename: str | None = None) -> dict:
    """Write the fit as EFT into the fits/ library (default name: '<Ship> - <fit name>.eft')."""
    h = _get(fit_id)
    os.makedirs(FITS_DIR, exist_ok=True)
    path = os.path.join(FITS_DIR, _safe_name(filename or "%s - %s" % (h.ship, h.name)))
    with open(path, "w", encoding="utf-8") as f:
        f.write(h.eft() + "\n")
    return {"path": path}


@_tool
def list_saved_fits() -> dict:
    """EFT files in the fits/ library."""
    if not os.path.isdir(FITS_DIR):
        return {"fits": []}
    return {"fits": sorted(f for f in os.listdir(FITS_DIR) if f.endswith(".eft"))}


@_tool
def load_saved_fit(filename: str, skill_level: int = 5, damage_profile: str | list[float] | None = None) -> dict:
    """Load an EFT file from the fits/ library."""
    path = os.path.join(FITS_DIR, _safe_name(filename))
    if not os.path.isfile(path):
        raise FitError("No saved fit %r" % filename)
    with open(path, encoding="utf-8") as f:
        return load_fit(f.read(), skill_level, damage_profile)


def main():
    with contextlib.redirect_stdout(sys.stderr):
        on_engine(boot)  # pay the startup cost (and any DB rebuild) before serving
    mcp.run("stdio")


if __name__ == "__main__":
    main()
