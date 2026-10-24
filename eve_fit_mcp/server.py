"""MCP server exposing Pyfa's fitting engine for iterative fit optimisation.

Run:  python -m eve_fit_mcp.server   (stdio transport)
"""
import contextlib
import functools
import os
import re
import sys

from mcp.server.mcpserver import MCPServer

from . import catalog, esi, evaluate, explore, items, optimizer, pilots, ships, stats as stats_mod
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
   find_items (filters: slot, size, meta, tags, fits_ship, attributes, sort) and get_item_info
   to discover modules/ammo; tags hint at roles (list_item_tags).
3. set_module / set_charge / set_drones for manual edits; clone_fit to branch ideas.
4. sweep_fit (exhaustive over small pools -> trade-off front), compare_charges (ammo side by side),
   make_variant (batch changes -> new fit, e.g. refit modes), or optimize_fit (weighted greedy search)
   with an objective (metric weights) and constraints for larger searches;
   the result is loaded as a new fit_id - inspect it, tweak, re-optimise with different pools.
5. save_fit writes EFT into the fits/ library (paste straight into EVE).
Slots are addressed as 'high:0', 'med:1', 'low:2', 'rig:0'. Metrics: see list_metrics.
Pilots: 'All 5' (default) .. 'All 0', custom skill sets (save_pilot) or real characters via
esi_login. With a pilot below All 5 the optimiser only picks modules/ammo the pilot can use.
Command boosts: generic links per fit (list_command_links, set_fit_options command_links).
EHP uses the fit's damage profile."""

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
    return stats_mod.summarize(stats_mod.compute(h, skills_check=True), metrics)


def _delta(before, after, keys):
    return {k: round(after[k] - before[k], 3) for k in keys
            if isinstance(before.get(k), (int, float)) and abs(after[k] - before[k]) > 1e-9}


DELTA_KEYS = ["dps_total", "volley", "ehp_total", "tank_sustained", "cap_stable", "cap_lasts_s", "max_speed",
              "align_time", "signature", "lock_range", "scan_res", "cpu_free", "pg_free", "calibration_free"]


# ---- fits ---------------------------------------------------------------------------------------

@_tool
def load_fit(eft: str, pilot: str = "All 5", damage_profile: str | list[float] | None = None,
             command_links: list[dict] | None = None, drones_active: bool = True) -> dict:
    """Load a fit from EFT text (Pyfa/EVE clipboard format). Returns fit_id, slot layout and stats.

    pilot: 'All 5'..'All 0', a custom pilot or an ESI character (list_pilots).
    damage_profile: incoming damage for EHP - preset (omni/em/thermal/kinetic/explosive) or [em, th, kin, exp].
    command_links: generic boosts, e.g. [{"type": "armor_all", "strength": 5, "mindlink": true}].
    drones_active: launch drones (heaviest first) within bandwidth (EFT import leaves them docked).
    """
    h = _register(FitHandle(eft, pilot, _profile(damage_profile) or DAMAGE_PRESETS["omni"], drones_active,
                            command_links))
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
    return {"fit_id": h.id, "ship": h.ship, "name": h.name, "pilot": h.pilot,
            "damage_profile": list(h.damage_profile), "command_links": h.command_links,
            "eft": h.eft(), "slots": h.slots()}


@_tool
def get_stats(fit_id: int, metrics: list[str] | None = None, full: bool = False) -> dict:
    """Stats for a fit. full=True returns every metric (resists per type, resources, etc.)."""
    h = _get(fit_id)
    s = stats_mod.compute(h, skills_check=True)
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
def set_fit_options(fit_id: int, pilot: str | None = None, damage_profile: str | list[float] | None = None,
                    command_links: list[dict] | None = None) -> dict:
    """Change a fit's pilot, incoming damage profile and/or generic command links.

    command_links replaces the current set; pass [] to remove all boosts.
    Returns stats plus the deltas caused by the change."""
    h = _get(fit_id)
    before = stats_mod.compute(h)
    if pilot is not None:
        h.set_pilot(pilot)
    if damage_profile is not None:
        h.set_damage_profile(_profile(damage_profile))
    if command_links is not None:
        h.set_command_links(command_links)
    out = _summary(h)
    out["delta"] = _delta(before, stats_mod.compute(h), DELTA_KEYS)
    return out


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
def find_items(query: str | None = None, category: str | None = None, group: str | None = None,
               slot: str | None = None, size: str | None = None, hardpoint: str | None = None,
               meta_groups: list[str] | None = None, tags: list[str] | None = None,
               any_tags: list[str] | None = None, market: str | None = None, fits_ship: str | None = None,
               charge_for: str | None = None, attributes: dict | None = None, sort_by: str | None = None,
               descending: bool = True, include_attributes: list[str] | None = None,
               usable_by: str | None = None, limit: int = 30) -> dict:
    """Search the item database with filters. Text filters are case-insensitive substrings.

    query: words in the name. category: Module/Charge/Drone/Implant/Subsystem/Fighter/Ship.
    slot: high/med/low/rig/subsystem. size: small/medium/large/xl/capital. hardpoint: turret/launcher.
    meta_groups: e.g. ["Tech II", "Faction"]. market: substring of the market path (e.g. "Microwarpdrives").
    tags: role hints that must all match, e.g. ["speed"], ["tackle", "point"], ["ammo:long-range"];
        any_tags: at least one. See list_item_tags for the vocabulary.
    fits_ship: only items that ship can fit (slots, hardpoints, rig size, restrictions).
    charge_for: only charges/scripts that module can load.
    attributes: dogma filters by name or display name, e.g. {"cpu": {"max": 30}, "Damage Modifier": {"min": 1.1}}.
    sort_by: attribute (e.g. "damageMultiplier", "maxVelocity") or cpu/pg/meta_level/name.
    include_attributes: extra attribute values to return per item. usable_by: pilot - only items they can use.
    Returns {total, returned, items}; each item has slot/size/meta/cpu/pg/tags/market_path."""
    return items.find(query, category, group, slot, size, hardpoint, meta_groups, tags, any_tags, market,
                      fits_ship, charge_for, attributes, sort_by, descending, include_attributes, usable_by, limit)


@_tool
def get_item_info(item: str | None = None, fit_id: int | None = None, slot: str | None = None,
                  attributes: str = "published") -> dict:
    """Everything about an item: category/group/market path, slot, size, meta, tags, required skills,
    description, variations (T1/T2/faction...), loadable charge groups and its dogma attributes
    (display name, value, unit). attributes: published (default) | all | none.
    Or pass fit_id + slot (e.g. 'high:0') to inspect the fitted module: attributes then also show
    'fitted' values after skills, ship bonuses and other modules, plus the loaded charge."""
    module = None
    if fit_id is not None and slot:
        h = _get(fit_id)
        _, module = h._resolve(slot)
        if module.isEmpty:
            raise FitError("%s is empty" % slot)
        item = module.baseItem.typeName if module.isMutated else module.item.typeName
    if not item:
        raise FitError("give an item name, or fit_id + slot")
    return items.item_info(item, attributes, module)


@_tool
def list_item_tags(category: str | None = None) -> dict:
    """Tag vocabulary for find_items (with item counts), optionally for one category."""
    return {"tags": items.tag_counts(category)}


@_tool
def list_item_groups(category: str | None = None, query: str | None = None) -> dict:
    """Item groups with counts (e.g. category='Module', query='Shield'), for browsing."""
    return {"groups": items.list_groups(category, query)}


@_tool
def get_ship_info(ship: str, pilot: str = "All 5", all_attributes: bool = False) -> dict:
    """Hull reference: slots, hardpoints, fitting resources, HP/resists, cap, navigation, targeting,
    drone/cargo capacity, bonuses text and required skills. Values show base and with the pilot's skills.
    all_attributes=true adds every dogma attribute (display name, unit, base, with_skills)."""
    return ships.ship_info(ship, pilot, all_attributes)


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
                 time_limit_s: float = 120, usable_only: bool = True) -> dict:
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
    usable_only: only consider modules/charges the fit's pilot has the skills for.
    The fit's pilot, damage profile and command links apply throughout.
    The original fit is untouched; the optimised fit is loaded as a new fit_id.
    """
    h = _get(fit_id)
    res = optimizer.optimize(
        h, objective, constraints, slots or "all", mode, pools, locked, allow_empty, optimize_charges,
        group_identical, strategy, max_evals=max_evals, normalize=normalize,
        exclude_meta=catalog.DEFAULT_EXCLUDE_META if exclude_meta is None else tuple(exclude_meta),
        max_meta_level=max_meta_level, time_limit_s=time_limit_s, usable_only=usable_only)
    new = _register(h.derive(res["eft"]))
    res["fit_id"] = new.id
    return res


@_tool
def sweep_fit(fit_id: int, racks: dict[str, list[str | None]], goals: dict[str, str] | None = None,
              constraints: dict | None = None, report: list[str] | None = None, locked: list[str] | None = None,
              max_evals: int = 20000, include_eft: int = 5) -> dict:
    """Exhaustive search: try every combination of the given pools and return the trade-off (Pareto) front.

    racks: {"low": ["Gyrostabilizer II", "Nanofiber Internal Structure II", ...], "rig": [...], "med:3": [...]}.
      A rack name fills all its unlocked slots (order-insensitive); a slot key fills just that slot.
      null in a pool = leave empty. Rig combos over calibration are skipped automatically.
    goals: e.g. {"dps_total": "max", "max_speed": "max", "align_time": "min"} (default dps_total max).
    constraints: {"weapon_reach": {"min": 40000}, "cap_lasts_s": {"min": 180}} - must hold.
    Returns the non-dominated fits (module counts per rack + stats; EFT for the first include_eft), and
    which index is best on each goal. ~100-150 combos/s; errors if combinations > max_evals.
    Prefer this over optimize_fit when pools are small: no weights to tune, and you see the trade-offs."""
    return explore.sweep(_get(fit_id), racks, goals, constraints, report, locked or (), max_evals, include_eft)


@_tool
def compare_charges(fit_id: int, slot: str, charges: list[str] | None = None,
                    metrics: list[str] | None = None, exclude_meta: list[str] | None = None) -> dict:
    """Side-by-side stats for every ammo/script the weapon in `slot` can load (applied to all identical
    weapons): DPS, volley, optimal/falloff (missile range), tracking, damage split. Flags charges with the
    same total damage (they differ only in damage type). The fit is not changed."""
    return explore.compare_charges(_get(fit_id), slot, charges,
                                   catalog.DEFAULT_EXCLUDE_META if exclude_meta is None else tuple(exclude_meta),
                                   metrics)


@_tool
def make_variant(fit_id: int, changes: list[dict], name: str | None = None) -> dict:
    """Copy a fit and apply several slot changes at once (variants, refits, cloaky vs fight modes).

    changes: [{"slot": "med:2", "item": "Covert Ops Cloaking Device II"},
              {"slot": "high:0", "charge": "Scourge Rage Heavy Assault Missile"},
              {"slot": "low:1", "item": null}, {"slot": "med:0", "state": "online"}]
    Returns the new fit_id, its stats, the deltas vs the original, and warnings. The original is unchanged."""
    base = _get(fit_id)
    new, warnings = explore.make_variant(base, changes, name)
    _register(new)
    before, after = stats_mod.compute(base), stats_mod.compute(new, skills_check=True)
    return {"fit_id": new.id, "warnings": warnings, "delta": _delta(before, after, DELTA_KEYS),
            "stats": stats_mod.summarize(after)}


# ---- library ------------------------------------------------------------------------------------

def _safe_name(name):
    name = re.sub(r"[^\w\- .]+", "_", name).strip(" .")
    if not name:
        raise FitError("bad file name")
    return name if name.endswith(".eft") else name + ".eft"


@_tool
def save_fit(fit_id: int, filename: str | None = None) -> dict:
    """Write the fit into the fits/ library as both EFT (.eft) and EVE XML (.xml).

    Use the .eft text with "Import from Clipboard" (EVE or Pyfa). Use the .xml for importing from a
    file: Pyfa treats any file starting with "[" as the legacy EFT-config format and mangles it,
    while XML imports intact (EVE's XML has no loaded-charge field, so ammo comes in as cargo)."""
    from .pyfa_bridge import P
    h = _get(fit_id)
    os.makedirs(FITS_DIR, exist_ok=True)
    path = os.path.join(FITS_DIR, _safe_name(filename or "%s - %s" % (h.ship, h.name)))
    with open(path, "w", encoding="utf-8") as f:
        f.write(h.eft() + "\n")
    xml_path = path[:-4] + ".xml"
    with open(xml_path, "w", encoding="utf-8") as f:
        f.write(P.Port.exportXml([h.fit]))
    return {"eft_path": path, "xml_path": xml_path,
            "note": "paste the .eft via Import from Clipboard; use the .xml for file import"}


@_tool
def list_saved_fits() -> dict:
    """EFT files in the fits/ library."""
    if not os.path.isdir(FITS_DIR):
        return {"fits": []}
    return {"fits": sorted(f for f in os.listdir(FITS_DIR) if f.endswith(".eft"))}


@_tool
def load_saved_fit(filename: str, pilot: str = "All 5", damage_profile: str | list[float] | None = None,
                   command_links: list[dict] | None = None) -> dict:
    """Load an EFT file from the fits/ library."""
    path = os.path.join(FITS_DIR, _safe_name(filename))
    if not os.path.isfile(path):
        raise FitError("No saved fit %r" % filename)
    with open(path, encoding="utf-8") as f:
        return load_fit(f.read(), pilot, damage_profile, command_links)


# ---- pilots & boosts -----------------------------------------------------------------------------

@_tool
def list_pilots() -> dict:
    """Available pilots: built-in All 0-5, custom skill sets and ESI characters."""
    return {"pilots": pilots.list_pilots(), "esi_logged_in": esi.characters()}


@_tool
def get_pilot(name: str, skills: list[str] | None = None) -> dict:
    """Pilot details; `skills` = names or substrings (e.g. ["Drone", "Gallente Cruiser"]) to show levels."""
    return pilots.describe(name, skills)


@_tool
def save_pilot(name: str, base_level: int = 4, skills: dict[str, int] | None = None,
               replace: bool = False) -> dict:
    """Create/update a custom pilot: every skill at base_level, then `skills` overrides,
    e.g. {"Drones": 5, "Gallente Cruiser": 4}. Overrides merge into an existing pilot unless replace=true.
    Fits using this pilot pick up the change on their next calculation."""
    return pilots.save_custom(name, base_level, skills, replace)


@_tool
def set_pilot_skills(name: str, skills: dict | list, character_id: int | None = None,
                     source: str | None = None) -> dict:
    """Create/replace a pilot from a full skill list supplied by another tool (e.g. eve-esi-mcp's
    character_skills), so no separate eve-fit ESI login is needed. Unlisted skills count as untrained.

    skills accepts: {"Drones": 5, "3436": 5} (names or type IDs), the ESI /skills/ response or its "skills"
    list ([{"skill_id": .., "active_skill_level": ..}]), or [{"skill": name, "level": n}]."""
    return pilots.save_imported(name, skills, character_id, source)


@mcp.tool()
def evaluate_fit(eft: str, skills: dict | list | None = None, pilot: str | None = None,
                 implants: list[int] | None = None, boosters: list[int] | None = None,
                 damage_profile: str | list[float] | None = None, command_links: list[dict] | None = None,
                 metrics: list[str] | None = None, drones_active: bool = True) -> dict:
    """Stateless exact stats for an EFT fit in one call: contract 'eve-fit/evaluate_fit@1' (docs/INTEGRATION.md).

    skills: inline {type_id: level} (active levels; also accepts names or the ESI /skills/ response), or
    pilot: a pilot name; neither = 'All 5'. implants / boosters: type IDs applied to the fit.
    damage_profile: preset (omni/em/thermal/kinetic/explosive) or [em, th, kin, exp] for EHP.
    command_links: generic boosts. metrics: limit stats to these keys.
    Returns {contract, valid, issues, pilot_used, engine, stats (raw numbers; resists are fractions 0-1),
    units, missing_skills: [{item, skill, required, have}], ship}. Never errors on bad fits: valid=false + issues."""
    def call():
        with contextlib.redirect_stdout(sys.stderr):
            return evaluate.evaluate(eft, skills, pilot, implants, boosters, damage_profile, command_links,
                                     metrics, drones_active)
    return on_engine(call)


@_tool
def delete_pilot(name: str) -> dict:
    """Delete a custom or ESI pilot (ESI login tokens are kept; see esi_logout)."""
    pilots.delete(name)
    return {"deleted": name}


@_tool
def list_command_links() -> dict:
    """Generic command link types usable in command_links.

    strength = booster ship's command bonus %/level (0 = no ship bonus, 5 = max); mindlink adds +25%.
    Max skills and T2 bursts are assumed (Pyfa's generic link model)."""
    from .pyfa_bridge import P
    P.load()
    return {"links": {k: {"label": v[0], "category": v[1]} for k, v in P.COMMAND_LINK_DEFS.items()},
            "strengths": list(P.LINK_STRENGTHS),
            "example": [{"type": "armor_all", "strength": 5, "mindlink": True},
                        {"type": "skirmish_all", "strength": 3}]}


# ---- ESI ------------------------------------------------------------------------------------------

@_tool
def esi_login(pilot_name: str | None = None) -> dict:
    """Start EVE SSO login. Give the returned URL to the user to open and log in; it redirects to a local
    callback and the character's skills are imported as a pilot (named pilot_name or the character name).
    Then poll esi_login_status. Uses your own EVE app if EVE_ESI_CLIENT_ID is set (docs/ESI.md), else
    Pyfa's public app. Scopes: read skills, read fittings."""
    uri = esi.login_start(pilot_name)
    return {"login_url": uri,
            "app": esi.app_info()["app"],
            "next": "Open login_url, log in, pick the character. Then call esi_login_status. If the browser "
                    "can't reach the local callback, pass the code it shows (or the full URL it ended on) "
                    "to esi_login_complete."}


@_tool
def esi_app_info() -> dict:
    """Which EVE application ESI logins use (own app from EVE_ESI_* env vars, or Pyfa's), callback URL
    and scopes. Never reveals the secret."""
    return esi.app_info()


@_tool
def esi_login_status() -> dict:
    """State of the pending ESI login: waiting / done (with pilot) / error."""
    st = esi.login_status()
    st.pop("uri", None)
    return st


@_tool
def esi_login_complete(code: str) -> dict:
    """Manual fallback: the code from Pyfa's callback page, or the full redirect URL the browser ended on."""
    return esi.login_complete(code)


@_tool
def esi_refresh_skills(pilot: str) -> dict:
    """Re-import current skills for an ESI pilot/character."""
    return esi.refresh_skills(pilot)


@_tool
def esi_list_fittings(pilot: str) -> dict:
    """Fittings saved in-game for an ESI character."""
    return {"fittings": esi.list_fittings(pilot)}


@_tool
def esi_import_fitting(pilot: str, fitting_id: int, use_pilot: bool = True,
                       command_links: list[dict] | None = None) -> dict:
    """Load an in-game fitting as a fit (calculated with that character's skills unless use_pilot=false)."""
    eft = esi.fitting_eft(pilot, fitting_id)
    return load_fit(eft, pilots.canonical(pilot) if use_pilot else "All 5", None, command_links)


@_tool
def esi_logout(pilot: str) -> dict:
    """Forget the stored ESI token for a character (its pilot skill snapshot is kept)."""
    return esi.logout(pilot)


def main():
    with contextlib.redirect_stdout(sys.stderr):
        on_engine(boot)  # pay the startup cost (and any DB rebuild) before serving
        on_engine(items.index)  # tagged item index: built once per game-data version, then cached
    mcp.run("stdio")


if __name__ == "__main__":
    main()
