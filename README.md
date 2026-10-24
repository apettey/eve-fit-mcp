# eve-fit-mcp

An MCP server that wraps [Pyfa](https://github.com/pyfa-org/Pyfa)'s fitting engine so Claude (or any MCP client)
can load EVE Online fits, inspect stats, try what-ifs and run constrained optimisation, iterating until a fit is
as good as it gets.

Pyfa is **not forked**. It's a git submodule at `vendor/pyfa` pinned to upstream `pyfa-org/Pyfa`, and this repo
only wraps it. Every Pyfa import goes through `eve_fit_mcp/pyfa_bridge.py`, so upstream changes can only break
that one file.

## Layout

| Path | What |
|---|---|
| `vendor/pyfa/` | Upstream Pyfa (submodule). `eve.db` is built here from `staticdata/` |
| `eve_fit_mcp/pyfa_bridge.py` | Headless bootstrap of Pyfa, the only place that imports from it |
| `eve_fit_mcp/fitting.py` | `FitHandle`: load EFT, swap modules/charges/drones using Pyfa's own slot and state rules |
| `eve_fit_mcp/stats.py` | Flattens a fit into numeric metrics (DPS, EHP, tank, cap, speed, resources, ...) |
| `eve_fit_mcp/catalog.py` | Item search and candidate generation (meta variants, group, market group, pools) |
| `eve_fit_mcp/pilots.py` | Skill sets: built-in All 0-5, custom levels, ESI snapshots |
| `eve_fit_mcp/esi.py` | EVE SSO login, skill and in-game fitting import ([docs/ESI.md](docs/ESI.md)) |
| `eve_fit_mcp/ships.py` | Hull reference data (slots, attributes, bonuses) |
| `eve_fit_mcp/items.py` | Tagged item index, filtered search, full item attributes |
| `eve_fit_mcp/optimizer.py` | Weighted objective, hard constraints, greedy or exhaustive search |
| `eve_fit_mcp/server.py` | MCP tools |
| `fits/` | Your EFT library (`save_fit` / `load_saved_fit`) |

## Setup

```powershell
git clone --recurse-submodules <this repo>
cd eve-fit-mcp
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\python -m pytest      # first run builds vendor/pyfa/eve.db (~25s)
```

Register it with Claude Code (user scope = available in every project), pointing at this checkout's venv:

```powershell
claude mcp add --scope user eve-fit -- "<path to eve-fit-mcp>\.venv\Scripts\python.exe" -m eve_fit_mcp.server
```

(On macOS/Linux the interpreter is `.venv/bin/python`. Any MCP client works: the command is just
`python -m eve_fit_mcp.server` over stdio.)

## Keeping up with upstream Pyfa

```powershell
powershell -File scripts\update-pyfa.ps1
```

This pulls the latest Pyfa `master`, rebuilds `eve.db` if the game data changed, runs the tests, and prints the
commit command. If the tests fail after an update, the fix almost always goes in `pyfa_bridge.py`.

## Tools

- **Fits:** `load_fit`, `get_fit`, `get_stats`, `list_fits`, `clone_fit`, `close_fit`, `compare_fits`, `set_fit_options` (pilot, damage profile, command links)
- **Editing:** `set_module`, `set_charge`, `set_drones`. Each returns the stat changes.
- **Items:** `search_items`, `list_candidates`, `try_candidates` (what-if for every candidate in a slot, without changing the fit), `get_ship_info` (slots, hardpoints, attributes, bonuses; base and with skills)
- **Item database:** `find_items` (filter by name, category, group, slot, size, hardpoint, meta, market path, role tags, `fits_ship`, `charge_for`, attribute ranges; sort by any attribute), `get_item_info` (all attributes, variations, charges, skills; or a fitted module's modified values), `list_item_tags`, `list_item_groups`
- **Pilots:** `list_pilots`, `get_pilot`, `save_pilot` (base level + per-skill overrides), `delete_pilot`
- **Boosts:** `list_command_links`; apply with `command_links` on `load_fit` / `set_fit_options`
- **ESI:** `esi_app_info`, `esi_login`, `esi_login_status`, `esi_login_complete`, `esi_refresh_skills`, `esi_list_fittings`, `esi_import_fitting`, `esi_logout`
- **Optimisation:** `optimize_fit`, `list_metrics`
- **Library:** `save_fit`, `list_saved_fits`, `load_saved_fit`

### Optimiser

```json
{
  "objective":   {"dps_total": 1, "ehp_total": 0.6, "align_time": -0.2},
  "constraints": {"cap_stable": 1, "max_speed": {"min": 1800}},
  "slots":       ["low", "med", "rig"],
  "pools":       {"low": ["Damage Control II", "Magnetic Field Stabilizer II", "Energized Adaptive Nano Membrane II"]},
  "max_meta_level": 5
}
```

- Weights are normalised to the starting fit by default, so `1` vs `0.6` means "a 1% DPS gain is worth more
  than a 1% EHP gain". Use a negative weight to minimise a metric.
- CPU, PG, calibration, slots, hardpoints and max-group limits are always enforced. Any fittable result beats any
  unfittable one, so the search can start from an over-CPU fit.
- Candidates for each slot come from `mode` (meta `variants` of the current module by default) plus any
  `pools`. Pools are how empty slots get filled and how other module types get considered.
- Identical charge-using modules (e.g. a rack of turrets) change together, and ammo is optimised per weapon type.
- `strategy: "greedy"` is coordinate descent (fast, local). `"exhaustive"` tries every combination per rack
  within `max_evals`, then polishes the result with greedy.
- The original fit is never modified. The result is loaded as a new `fit_id` you can inspect, tweak and
  re-optimise.

## Item database and tags

On first start the server indexes every published module, rig, charge, drone, implant, booster and ship
(about 7,000 items) from Pyfa's data and caches it in `data/item_index.json`. It rebuilds automatically when
the game data changes. Each entry has a slot, size, meta group, fitting cost, market path and **role tags**
derived from CCP's groups, market tree and dogma effects, for example:

| Tag | Meaning |
|---|---|
| `damage`, `turret-damage`, `missile-damage`, `drone-damage` | damage modules and rigs |
| `turret-range`, `turret-tracking`, `application` | weapon application |
| `speed`, `agility`, `propulsion`, `warp-speed` | navigation |
| `tank:armor-buffer`, `tank:shield-active`, `tank:resist`, ... | tank style |
| `tackle`, `point`, `web`, `ewar:damp`, `cap-warfare`, `neut` | combat utility |
| `fitting`, `cpu`, `powergrid`, `cap` | resources |
| `drawback:armor-hp`, `drawback:sig-rad`, ... | rig penalties |
| `ammo:long-range`, `ammo:short-range`, `dmg:mostly-thermal`, ... | ammo profile |

So "low-slot speed modules a Hurricane can fit" is
`find_items(slot="low", tags=["speed"], fits_ship="Hurricane")`, and "720mm ammo by range" is
`find_items(charge_for="720mm Howitzer Artillery II", sort_by="weaponRangeMultiplier")`.
Tags are heuristics. Confirm with `get_item_info` or `try_candidates`.

## Pilots, skills and boosts

Every fit is calculated for a **pilot**:

- `All 5` (default) to `All 0`: every skill at that level.
- Custom: `save_pilot("Alt", base_level=4, skills={"Drones": 5, "Gallente Cruiser": 3})`.
- Real characters: log in with EVE SSO and the trained skills are imported. See **[docs/ESI.md](docs/ESI.md)**
  to register your own EVE application (Client ID, secret, callback `http://localhost:8765/callback`).

With a pilot below All 5, stats report `missing_skills`, and the optimiser only considers modules and ammo the
pilot can use (`usable_only`, on by default).

**Command boosts** use Pyfa's generic links: `command_links=[{"type": "armor_all", "strength": 5, "mindlink": true}]`.
Strength is the booster ship's bonus in %/level (0 to 5), and max skills with T2 bursts are assumed.
`list_command_links` lists the types. Boosts, pilot and damage profile stay with a fit through clones and
optimisation.

## Assumptions and limits

- EHP uses the fit's damage profile (default omni). DPS is raw, not applied against a target.
- Implants and drugs (boosters) in the EFT are respected but not optimised. ESI imports skills, not implants.
- Command boosts are the generic-link model, not a specific booster ship's fit.
- Each evaluation takes about 10 ms, so greedy runs over a few hundred variants finish in seconds.
