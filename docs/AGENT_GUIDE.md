# Fitting agent guide

How an AI agent should use eve-fit to design and optimise EVE Online fits. This is distilled from
reviewing real agent runs (a Hurricane arty kiter, a Loki HAM wormhole-siege fit with refits, a
Hurricane Fleet Issue killmail review): what they had to hand-write, and where they went wrong.

## Pick the right tool

| Question | Tool |
|---|---|
| What is this hull? Slots, bonuses, resources | `get_ship_info` (T3C: pass `subsystems`), `list_subsystem_layouts` |
| What modules or ammo could go here? | `find_items` (slot, size, meta, `tags`, `fits_ship` or `fits_fit`, `charge_for`, attribute filters, sort), `get_item_info` |
| Exact stats of a fit | `load_fit` → `get_stats`, or stateless `evaluate_fit` |
| Damage against real targets | `applied_dps` (structure, cruiser_mwd, frigate…, at distances) |
| Which ammo? | `compare_charges` |
| Small decision space (≤ ~20k combinations) | **`sweep_fit`**: exhaustive, returns the trade-off (Pareto) front |
| Large decision space | `optimize_fit` (weighted greedy), then polish with `sweep_fit` |
| One module or ammo swap | `try_candidates` (no change) or `set_module` (returns deltas) |
| Variants, refit modes (cloaky vs fight) | `make_variant` (batch changes → new fit with deltas) |
| Can my character fly it? Training plan | `get_stats` → `missing_skills`; `skill_plan` (EVEMon export) |
| Hand a fit to the user | `save_fit` (writes `.eft` for clipboard and `.xml` for file import) |

## Workflow

1. **Clarify the job**: role (siege, kite, solo, fleet), likely targets, what's fixed (hull, weapon system,
   budget, a cloak, a point) and the pilot (`list_pilots`; real characters are in eve-esi or `esi_login`).
2. **Read the hull** with `get_ship_info` (T3C: `list_subsystem_layouts` first).
3. **Draft a fit that's already valid.** `get_stats` should show `valid: true`. Any valid fit beats any
   invalid one in search, so an over-powergrid draft gets stripped of the very modules you care about.
4. **Fix the must-haves**: lock them (`locked`) or make them constraints (`max_speed: {min: 1800}`,
   `weapon_reach: {min: 40000}`, `cap_lasts_s: {min: 180}`). Don't turn them into objective weights. A
   linear reward on range traded away 36% DPS for range nobody needed.
5. **Explore with `sweep_fit`** over small pools per rack (use `find_items` with `tags` to build them).
   Present the trade-off front ("+30 DPS costs 45 m/s") rather than one weighted answer.
6. **Check application**: `applied_dps` against the targets you expect, and `compare_charges` across ranges.
   Paper DPS alone is misleading (HAMs on an unwebbed MWD cruiser apply about 45%). To make range or
   application count in a search, use `sweep_fit` goals like `"applied:structure@60": "max"` or
   `"applied:cruiser_mwd@20": "max"`. Without them, a Tracking Enhancer or range rig looks worthless.
   Range metrics (`weapon_optimal` and friends) describe the main weapon group; `weapon_groups` lists them all.
7. **Check the pilot**: `missing_skills`. With a real pilot, `optimize_fit` and `sweep_fit` only consider
   items the pilot can use (`usable_only`, on by default in `optimize_fit`). `skill_plan` gives the training.
8. **Deliver**: `save_fit` (both formats) plus the numbers that matter and the trade-offs taken.

## Gotchas

- **Prop mods are on in the main stats.** `max_speed`, `align_time` and `signature` assume the MWD is
  running. For cloaky travel or non-MWD comparisons, use `max_speed_noprop`, `align_time_noprop` and
  `signature_noprop` (also usable as sweep or optimiser goals).
- **Strategic cruisers**: the bare hull has 0 slots. Use `list_subsystem_layouts`, `get_ship_info(subsystems=…)`
  and `find_items(fits_fit=fit_id)`. Swapping a subsystem with `set_module` or `make_variant` re-lays out the
  slots, and anything that no longer fits shows up in `issues`. Compare subsystem choices by sweeping one
  subsystem slot, or by building one EFT per layout.
- **Covert Ops cloaks** cost a huge amount of CPU unless the hull or subsystem allows them. An unexpected
  "cpu over by 10000" means you lost the covert ability.
- **Drones** auto-launch heaviest first within bandwidth and relaunch when bandwidth changes (e.g. an
  Interdiction Nullifier halves it). `set_drones` gives exact control.
- **Ammo**: faction EMP, Fusion and Phased Plasma (and HAM damage types without a ship damage-type bonus)
  have identical totals. Pick by the target's weak resist, not by paper DPS.
- **EHP** uses the fit's damage profile (default omni). `ehp_vs_em`, `ehp_vs_thermal`, `ehp_vs_kinetic` and
  `ehp_vs_explosive` show each damage type without changing the profile.
- **Utility slots** (nos, neut, cloak, probes) have no direct stat value, so optimisers won't choose them.
  Decide them yourself and lock them.
- **Faction bling that adds nothing**: if a swap gains less than about 1% on every goal, keep the T2 module.
- **Pyfa file import** mangles `.eft` files (it parses them as the old EFT-config format). Give users the
  `.xml` from `save_fit`, or the EFT text to paste via "Import from Clipboard".
- **Units**: resists are fractions (0.6 = 60%). `weapon_tracking` is Pyfa's raw turret tracking attribute.
  See `list_metrics` and `docs/INTEGRATION.md`.

## Using eve-fit from Python (for subagents running scripts)

```python
from eve_fit_mcp.pyfa_bridge import on_engine          # Pyfa is single-threaded: wrap every call
from eve_fit_mcp import evaluate, explore, items, ships, skillplan, application, stats
from eve_fit_mcp.fitting import FitHandle

def run():
    h = FitHandle(eft_text, pilot="Commander Tyrael")
    front = explore.sweep(h, {"low": [...], "rig": [...]}, goals={"dps_total": "max", "max_speed": "max"})
    return front

print(on_engine(run))
```

Run scripts with the repo folder as the working directory, using `.venv\Scripts\python.exe`.
