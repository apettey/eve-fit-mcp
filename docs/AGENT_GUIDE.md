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
| Which ammo? | `compare_charges` (paper), **`recommend_ammo`** (applied, per enemy and range), `load_best_ammo` (loads it and records the intent in fit notes) |
| What is their comp and how does it work? | **`save_doctrine`** (pasted EFTs, killmail fits merged per hull, or an eve-esi doctrine bundle), `analyze_doctrine` (roles, reps, reach, tackle, resist holes, observations) |
| How does my fit do against their whole comp? | `matchup_vs_doctrine` (range envelope vs each ship), `load_doctrine_ship` → `matchup_fits` / sweep goal |
| What should we bring (how many of which fits)? | **`recommend_comp`** (pilots by count or name with skills, role minimums, budget; full fleet battles per comp; runners-up, swap values, reasoning) |
| What fit of our hull best counters their comp? | **`counter_fit`** (searches racks pools scoring fleet battles vs the doctrine, with your support ships and pilot skills; before/after battles, changes, alternatives) |
| Will our fleet beat their comp? Who do we kill first? | **`fleet_battle`** (logi reps on the primary, ECM jams, fleet tackle, command bursts, neut cap-outs, smartbombs vs missiles/drones, focus fire; compares kill orders; per range) |
| Will this beat their fit? | **`matchup_fits`** (both ways: applied DPS, EHP vs the other's damage mix, reps, time to kill, win ratio, N vs M) |
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

## Ammo and enemies

- **Ammo follows the target, not the paper.** Max-damage ammo (Rage, Fury, Hail…) has a big explosion radius,
  slow explosion velocity or short range. Example: a HAM Nighthawk on Scourge Rage applies about 89% to an
  enemy Nighthawk, but loses 33–35% against a Basilisk (MWD or AB) compared with Dread Guristas or Caldari Navy
  HAMs. Webbed, the gap shrinks to about 10%.
- When the user names who they're fighting, run `recommend_ammo` against those ships (`{"ship": "Basilisk",
  "prop": "mwd"}`, or better, their actual EFT, e.g. from killmails via eve-esi), and load per purpose:
  `load_best_ammo(target=None)` for maximum DPS (structures, battleships), or `load_best_ammo(target=…)` for a
  specific enemy. Always state the loaded ammo and its intent. It's written to the fit notes, which become
  the EVE fitting description in the `.xml`.
- Carry the alternatives in cargo and say when to swap ("Rage vs Nighthawks; Caldari Navy vs Basilisks and Tengus").
- **Tackle is modelled**: each side's own scram (switches off MWD/MJD), webs (stacked) and painters land
  within their real ranges. Check `matchup_envelope` for where you win: a kiter can win at 20–60 km and
  lose everything inside 12 km.
- **Enemy comps**: build a doctrine (`save_doctrine`; from eve-esi `doctrine_from_killmails` /
  `doctrine_from_battle_report` bundles, or pasted EFTs), read `analyze_doctrine` (logi rep totals, ewar,
  reach, tackle range, weakest damage type), then test candidates with `matchup_vs_doctrine`. Unknown
  fits are bare-hull placeholders; say so when they drive a conclusion.
- **Fleet fights**: `fleet_battle(our_fleet, "<doctrine>", distances_km=[8, 20, 35])`. Read the insights:
  which kill order wins (killing a Falcon first can decide the fight), which targets can't be broken through
  their logi (then you need more DPS, alpha, neuts or a different target), and how much their ECM jams you.
  Command bursts with charges loaded boost their whole side automatically (`command_bursts` in the result;
  pass `our_links`/`their_links` to override). Neuts cap out logi first: `capped_out` lists ships that lost
  their cap-using modules. `missiles_reaching` shows the share of missiles that get through smartbombs.
  Report the assumptions. It's a model, not a simulation of pilots.
- **Counter fits**: `counter_fit(fit_id, "<doctrine>", count=…, support=[…your logi…], racks={…small pools…})`.
  Build the pools from `analyze_doctrine` (e.g. their reach and tackle suggest range or web modules; ECM
  suggests sensor strength). Present its `changes`, `battle_before`/`battle_after` and `alternatives`, which
  show the trade-offs, e.g. "4th BCS wins twice as fast but loses 72% of your EHP". It keeps your fit if
  nothing beats it. The `fleet:<doctrine>@<km>x<count>` goal also works in `sweep_fit` and `optimize_fit`.
- **Counter comps**: `recommend_comp(options=[your doctrine fits with costs], enemy="<doctrine>",
  pilots=[names] or N, require={"logistics": 2}, budget_isk=…)`. Explain the pick with its battle
  (kill order, timeline), the runners-up and `marginal_value` (e.g. "swapping a Nighthawk for a Malediction
  turns the win into a stalemate"). If no comp is possible, `who_can_fly` shows why.
- **Renamed items**: Pyfa maps removed items to their replacements (old ECCM modules → Sensor Booster) and
  stats note it. Mention it to the user.
- Fit the counter, not just the fit: load the enemy doctrine as a fit, check `matchup_fits` at the expected
  range and numbers, then `sweep_fit` with goal `"matchup:<enemy fit_id>@10": "max"`. Ship-name targets use
  the bare hull (no hardeners). Use real enemy EFTs for decisions.

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
- **Smartbombs destroy missiles in flight** (and orbiting drones). Against a smartbomb fleet, missile and
  drone doctrines lose damage, more so up close: check `missiles_reaching` in `fleet_battle` before
  recommending HAMs or drones into a battleship brawl. Long range shrinks the share of the flight inside
  the field but never removes it, since the missiles must reach the target.
- **Bursts need charges**: a Command Burst without a charge does nothing in the fleet model or Pyfa.
- **How many bursts a fit can run**: most hulls, Command Destroyers included, run **1** Command Burst at a
  time. Command Ships and capitals get more from their hull. Each **Command Processor I** adds 1. It is a
  **rig** (Small/Medium/Large/Capital, 150 calibration), not a mid or high slot module. So a Stork with
  3 bursts needs both its rig slots for Command Processors, which leaves no rig for speed or tank.
  `command_bursts` and `command_bursts_max` report the count and the limit, and going over the limit makes
  the fit invalid. EFT import switches every burst on, so check this rather than trusting that all of them
  run. Find the rig with `find_items(tags=["command:extra-burst"])`.
- **Bursts cost a lot of CPU**: about 50–55 tf each (T2 55, T1 and faction 50). Three bursts on a destroyer
  use about half its CPU, so CPU, not slots, usually limits a link ship. Look for low-CPU module variants
  (compact damps, for example) before giving up tank for Co-Processors.
- **Own bursts boost the ship itself**: a fit's active bursts apply to that ship too. `max_speed` already
  includes its own Rapid Deployment, and lock range includes Sensor Optimization. When a user asks for a
  speed "cold" or unboosted, unload the charges (`set_charge(fit_id, ["high:1"], null)`) and quote both numbers. `issues`
  says this in a `note:`.
- **One burst per charge type**: two bursts loaded with the same charge don't stack, and only one counts.
  Each burst type has 3–4 charges (Information: Sensor Optimization, Electronic Superiority, Electronic
  Hardening). Pick charges per fleet role, e.g. Electronic Superiority boosts the fleet's ewar, including a
  damp ship's own damps.
- **Cap warfare**: a fit that isn't cap stable alone is assumed kept stable by its fleet until neuts land;
  only the neuts' drain decides the cap-out time.
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
