# Integrating with eve-fit (server-to-server)

Other MCP servers, notably [eve-esi-mcp](https://github.com/apettey/eve-esi-mcp), use eve-fit as a fitting
engine. They spawn it over stdio and call **`evaluate_fit`**, a stateless tool with a versioned contract.

## Responsibilities

| | eve-esi-mcp | eve-fit-mcp |
|---|---|---|
| EVE SSO, tokens, characters | **owner** | not used in integrated setups |
| Skills, implants, fittings, assets from ESI | **owner**, passes data in | consumes |
| Exact fit maths (Pyfa), optimisation, item roles | calls eve-fit | **owner** |
| Formatting for the user | **owner** | raw numbers only |

EVE SSO rotates refresh tokens, so only one server should hold a character's tokens. In integrated setups
that's eve-esi-mcp. eve-fit's own `esi_*` tools ([ESI.md](ESI.md)) are a **standalone fallback** for using
eve-fit on its own.

## Launching

```text
command: <eve-fit-mcp>/.venv/Scripts/python.exe      (Windows; .venv/bin/python elsewhere)
args:    -m eve_fit_mcp.server
```

The first start builds `vendor/pyfa/eve.db` (if missing) and the item index (`data/item_index.json`), which
takes about 30 s. Later starts take a few seconds. Keep one client alive and reuse it, because each call
is about 10–50 ms. Calls are serialised inside eve-fit, since Pyfa is single-threaded.

## `evaluate_fit`: contract `eve-fit/evaluate_fit@1`

### Request

| Param | Type | Notes |
|---|---|---|
| `eft` | string | EFT text, first line `[Ship, Name]` |
| `skills` | object or array, optional | `{type_id: level}` (active levels), or skill names as keys, or the ESI `/characters/{id}/skills/` response (or its `skills` array). Unlisted skills count as 0. |
| `pilot` | string, optional | a named pilot instead of `skills` (`All 5` … `All 0`, custom, imported). Neither given means `All 5`. Both given is an issue. |
| `implants` | int[], optional | implant type IDs (e.g. from ESI `/implants/`) applied to the fit |
| `boosters` | int[], optional | booster (drug) type IDs |
| `damage_profile` | string or number[4], optional | `omni` (default) / `em` / `thermal` / `kinetic` / `explosive` or `[em, th, kin, exp]`, used for EHP |
| `command_links` | object[], optional | generic boosts, e.g. `[{"type": "armor_all", "strength": 5, "mindlink": true}]` (see `list_command_links`) |
| `metrics` | string[], optional | return only these stat keys (`slots` is always included) |
| `drones_active` | bool, default true | launch drones (heaviest first) within bandwidth |

### Response

```jsonc
{
  "contract": "eve-fit/evaluate_fit@1",
  "valid": true,                       // fits CPU/PG/calibration/slots/hardpoints and every item resolved
  "issues": ["cpu over by 9.75 (172.25 / 162.50)"],   // human-readable, may be non-empty when valid (e.g. offline modules)
  "pilot_used": "custom skills",       // or the pilot name, e.g. "All 5"
  "engine": {"name": "eve-fit-mcp", "pyfa_version": "v2.69.3", "gamedata_build": "3624425", "gamedata_date": "..."},
  "ship": "Rifter",
  "stats": { "dps_total": 157.3, "max_speed": 3213.2, "resist_armor_em": 0.66, "slots": {"low": {"used": 4, "total": 4}}, ... },
  "units": { "dps_total": "HP/s", "max_speed": "m/s", "resist_<layer>_<type>": "fraction 0-1", ... },
  "missing_skills": [{"item": "200mm AutoCannon II", "skill": "Small Projectile Turret", "required": 5, "have": 0}],
  "weapon_groups": [{"weapon": "720mm Howitzer Artillery II", "charge": "Republic Fleet Depleted Uranium M",
                     "count": 6, "dps": 279.0, "hardpoint": "turret", "optimal": 41300, "falloff": 32800,
                     "tracking": 8.4}]   // most DPS first; launchers carry explosion_radius / explosion_velocity
}
```

- **Raw numbers only.** Resists are fractions from 0 to 1. `units` documents every key.
- **Never throws for bad fits.** Unparseable EFT, unknown items, unknown pilots and invalid params all come
  back as `valid: false` with `issues`. Unknown item names are reported, because Pyfa would otherwise drop
  them silently.
- **Weapon range keys** (`weapon_optimal`, `weapon_falloff`, `weapon_reach`, `weapon_tracking`) describe the
  **primary weapon group** (the one with the most DPS). `weapon_groups` (top level, added within `@1`) breaks
  every group down.
- **Stat keys** (all present unless `metrics` limits them):
  - Offence: `dps_total`, `dps_weapon`, `dps_drone`, `volley`, `weapon_optimal`, `weapon_falloff`, `weapon_reach`, `weapon_tracking`
  - Defence: `ehp_total`, `ehp_shield`, `ehp_armor`, `ehp_hull`, `hp_total`, `resist_<layer>_<em|thermal|kinetic|explosive>`, `resist_avg_<layer>`, `tank_sustained`, `tank_burst`, `shield_regen`, `remote_rep`
  - Capacitor: `cap_capacity`, `cap_stable`, `cap_stable_pct`, `cap_lasts_s`, `cap_delta`
  - Navigation: `max_speed`, `align_time`, `signature`, `warp_speed`
  - Targeting: `lock_range`, `scan_res`, `sensor_strength`, `max_targets`
  - Mining: `mining_yield`
  - Fitting resources: `cpu_*`, `pg_*`, `calibration_*`, `drone_bandwidth_*`, `drone_bay_*` (each `_used`, `_total` and `_free`)
  - Hardpoints and slots: `turret_hardpoints_free`, `launcher_hardpoints_free`, `slots`

### Versioning

- Within `@1`, changes are **additive only**: new optional params and new stat keys. Callers should ignore
  keys they don't know.
- Renaming or removing a param or stat key, or changing a unit, bumps the contract to `@2`.
  `tests/test_evaluate_contract.py` guards this.

## Beyond evaluation

For optimisation and exploration, the model can call eve-fit directly using EFT from eve-esi tools:
`load_fit` → `try_candidates` / `optimize_fit` → `get_fit` (EFT back) → eve-esi `fitting_save`.
For a named pilot built from eve-esi's skills, use `set_pilot_skills(name, skills)` once, then
`pilot=name` everywhere.
