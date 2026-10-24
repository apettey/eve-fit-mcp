---
name: eve-fit-design-fit
description: Use when designing, reviewing, optimising or comparing an EVE Online ship fit (e.g. "build me a Loki for wormhole sieges", "what's wrong with this killmail fit", "make this faster"), including refit modes, skill plans and handing fits to the user. Drives the eve-fit MCP tools (or the eve_fit_mcp Python library in a subagent) with the workflow and gotchas learned from real runs.
---

# Designing and optimising EVE fits with eve-fit

The goal is the fit that best does **the user's job** (siege, kite, solo, fleet role), with exact Pyfa
numbers, clear trade-offs and files they can import. Never guess numbers. Measure them.

## 1. Pin down the job (ask only if it's genuinely unclear)
- Role, expected targets/defenders, engagement range, fleet support (logi, links, scanner).
- What's fixed: hull, weapon system, must-haves (cloak, point, prop), budget/meta ceiling.
- Pilot: `list_pilots`. A real character comes from eve-esi (skills passed in), `esi_login`, or `set_pilot_skills`.
  Use the real pilot for numbers. Mention "All 5" only as a comparison.

## 2. Measure, don't assume
| Need | Tool |
|---|---|
| Hull facts | `get_ship_info` (T3C: `list_subsystem_layouts`, `get_ship_info(subsystems=…)`) |
| Candidates | `find_items` with `tags` (e.g. `speed`, `turret-damage`, `tank:shield-buffer`, `tackle`, `ammo:long-range`), `fits_ship` / `fits_fit`, `meta_groups`, attribute filters, `sort_by`; `get_item_info` |
| Stats | `load_fit` → `get_stats` (full stats incl. `*_noprop`, `ehp_vs_*`, `weapon_groups`), or `evaluate_fit` |
| Application | `applied_dps` (structure / cruiser_mwd / frigate… at distances); `compare_charges` for ammo |
| Ammo per enemy | `recommend_ammo` (best applied per target and range), `load_best_ammo` (loads it and writes the intent to the fit notes) |
| Beat a doctrine | `matchup_fits` (TTK both ways, win ratio, N vs M); `sweep_fit` goal `"matchup:<enemy fit_id>@<km>": "max"` |
| Small search | `sweep_fit` (exhaustive → Pareto front; goals can be `applied:structure@60`, `*_noprop`) |
| Big search | `optimize_fit` (weighted greedy), then polish with `sweep_fit` |
| What-if | `try_candidates`, `set_module` (deltas), `make_variant` (batch → new fit, e.g. refit modes) |
| Pilot fit-ness | `missing_skills` in stats; `skill_plan` (EVEMon export) |

## 3. Workflow
1. **Draft a valid fit first** (`get_stats` → `valid: true`). Search treats any valid fit as better than any
   invalid one, so an over-powergrid draft loses the very modules you care about.
2. **Make must-haves hard**: put them in `locked` slots or `constraints` (`max_speed: {min: …}`,
   `cap_lasts_s: {min: 180}`, `"applied:structure@60": {min: …}`). **Never** express them as objective weights.
   A linear range weight once traded 36% DPS for range nobody needed.
3. **Use and check every resource**: CPU, powergrid **and calibration** (a killmail fit left 56% of its
   calibration unused).
4. **Sweep small pools per rack** and present the **trade-off front** ("+30 DPS costs 45 m/s and 1 s align").
   Let the user pick, or recommend one and say why.
5. **Check application and range**: paper DPS lies. HAMs on an unwebbed MWD cruiser apply about 45%;
   arty against a structure is all about falloff (Tremor flat to about 75 km on a Hurricane Fleet Issue).
6. **Decide utility slots yourself** (cloak, probes, nos/neut, point) and lock them. Optimisers can't value them.
7. **Ammo and enemies**: if the user names an enemy or role, run `recommend_ammo` against those ships (real
   enemy EFTs beat bare-hull `{"ship": …}` targets) and `load_best_ammo` per purpose. Maximum paper DPS
   (`target=null`) only suits structures and battleships. Rage HAMs lose about a third vs Basilisks. State the
   loaded ammo and why, and carry the alternatives. For doctrine fights, load the enemy fit and check
   `matchup_fits`, then sweep with a `matchup:` goal.
8. **Refit modes** (cloaky vs fight, structure vs defenders): build each mode with `make_variant`. Carry the
   swap modules in cargo, list exact refit steps, and note the mechanic you're assuming (Mobile Depot, fitting
   service). Use `*_noprop` numbers for cloaky travel.
9. **Pilot check**: `missing_skills` must be empty for the user's character. If not, give a `skill_plan`.

## 4. Gotchas
- Speed, align and signature assume the prop mod is on. Use `max_speed_noprop` etc. for travel and cloaked comparisons.
- Range metrics describe the main weapon group. `weapon_groups` breaks every group down (utility launchers etc.).
- Faction EMP, Fusion and Phased Plasma (and HAM damage types without a ship damage-type bonus) have equal totals.
  Choose by the target's weak resist. Carry about 4 ammo types, not 13.
- Faction modules that gain under about 1% on every goal: keep T2.
- T3C: a covert cloak without the covert subsystem means a huge CPU penalty. The bare hull has 0 slots.
- Drones auto-launch heaviest-first and relaunch on bandwidth change (Interdiction Nullifier halves it).
- Combat boosters (drugs) can be evaluated with `evaluate_fit(boosters=[type_ids])`. Fleet boosts: `command_links`.

## 5. Deliver
- **Save every fit with `save_fit`**: it writes `.eft` (paste via Import from Clipboard) and `.xml` (Pyfa or EVE
  file import; Pyfa mangles `.eft` files). Send the `.xml` files to the user. Put the EFT in a code block.
- Give key numbers: DPS (paper and applied vs the real target), volley, range, EHP (omni plus a likely hostile
  profile), resists, cap, speed/align/signature, lock range, CPU/PG/calibration. Give deltas vs the starting fit.
- Explain the trade-offs taken and alternatives, and flag uncertain game mechanics plainly.
- Training: `skill_plan(…, save_as=…)` writes an EVEMon `.emp` (Plan → Import Plan from File).

## 6. Running as a subagent (Python library instead of MCP tools)
Run scripts from the eve-fit-mcp folder with `.venv\Scripts\python.exe`. Don't edit the repo. Wrap engine calls:

```python
from eve_fit_mcp.pyfa_bridge import on_engine
from eve_fit_mcp import evaluate, explore, items, ships, skillplan, application, stats
from eve_fit_mcp.fitting import FitHandle
print(on_engine(lambda: explore.sweep(FitHandle(eft, "Pilot Name"), {"low": [...]},
                                      goals={"dps_total": "max", "max_speed": "max"})))
```

Library names match the tools: `explore.sweep` / `compare_charges` / `make_variant`, `application.applied_dps`,
`items.find` (returns `{total, items}`) / `item_info`, `ships.ship_info` / `subsystem_layouts`,
`skillplan.build` / `save`, `evaluate.evaluate`, `stats.compute(h, skills_check=True, noprop=True)`.
To write files: `P.Port.exportXml([h.fit])` and `h.eft()`.
End your report with a short **tool feedback** section (what you had to hand-write, or what was awkward).
That's how eve-fit improves.
