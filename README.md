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

- **Fits:** `load_fit`, `get_fit`, `get_stats`, `list_fits`, `clone_fit`, `close_fit`, `compare_fits`, `set_fit_options` (skill level, damage profile)
- **Editing:** `set_module`, `set_charge`, `set_drones`. Each returns the stat changes.
- **Items:** `search_items`, `list_candidates`, `try_candidates` (what-if for every candidate in a slot, without changing the fit)
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

## Assumptions and limits

- Skills: every skill at one level (`skill_level`, default 5). Real characters aren't loaded yet.
- EHP uses the fit's damage profile (default omni). DPS is raw, not applied against a target.
- Implants, boosters and fleet boosts in the EFT are respected but not optimised.
- Each evaluation takes about 10 ms, so greedy runs over a few hundred variants finish in seconds.
