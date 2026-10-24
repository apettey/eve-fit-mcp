---
name: eve-fit-add-tool
description: Use when adding, changing or removing an eve-fit MCP tool or engine helper (anything in eve_fit_mcp/), including fixing a tool bug. Covers the project's founding principles (wrap Pyfa, never fork it; Pyfa is the source of truth; single engine thread; additive contracts), the file-by-file checklist, and the tests and docs that must change in the same commit.
---

# Adding or changing an eve-fit tool

eve-fit exists so an AI agent can **iterate on EVE Online fits with exact numbers**. It wraps Pyfa's engine
and exposes it as MCP tools. Every change must keep the spirit it was built with. Read the principles,
then follow the checklist.

## Principles (non-negotiable)

1. **Wrap, never fork.** `vendor/pyfa` is an unmodified upstream submodule. Never edit it. If Pyfa needs a
   workaround, put it in our code. Every import from Pyfa goes through `eve_fit_mcp/pyfa_bridge.py` (the
   `P` namespace or a local import inside a function), so an upstream change can only break that seam.
2. **Pyfa is the source of truth for game maths and data.** Never reimplement dogma, application, fitting
   rules or formats. Find the Pyfa function that already does it and call it:
   - `Port` for EFT, XML and ESI formats
   - `Module.fits`, `activeStateLimit` and `ModuleInfo` for fitting rules
   - `graphs/data/fitDamageStats` for applied damage
   - `DamagePattern.calculateEhp` for EHP
   - `Market` for variations and publicity

   If you catch yourself writing a formula, search `vendor/pyfa` first.
3. **One engine thread.** Pyfa keeps a gamedata DB session per thread. All engine work runs on the thread
   started by `pyfa_bridge.on_engine`. MCP tools get this through the `_tool` decorator (or call
   `on_engine` explicitly). Scripts and tests that touch the engine from other threads must wrap calls in
   `on_engine`.
4. **Working fits never enter Pyfa's SQLAlchemy session.** `FitHandle` wires `owner` links itself, and
   `recalc()` re-wires. Never call `eos.db.save`/`flush` on working fits. Thousands of throwaway swaps would
   leave orphans for SQLAlchemy to flush.
5. **Tools serve agent iteration.**
   - Return deltas and raw numbers.
   - Prefer stateless calls where a caller doesn't need a handle.
   - Show trade-offs (Pareto fronts) rather than one weighted answer.
   - Never silently drop anything (unknown items, skipped modules). Report it.
   - Raise `FitError` with an actionable message for bad input.
   - The original fit is never mutated by search or exploration tools; they work on clones.
6. **Contracts are additive.** `evaluate_fit` is versioned (`CONTRACT` in `evaluate.py`, currently
   `eve-fit/evaluate_fit@1`) and **eve-esi-mcp calls it server to server**.
   - Within a version: only new optional params and new keys. `stats` stays numbers-only (plus `slots`).
   - Structured extras go top-level (like `weapon_groups`).
   - Renaming or removing a key, changing a unit or changing a meaning means bumping to `@2`, and the
     eve-esi sessions must be told **before** you push.
   - Treat other tools' names and params the same way. Add, don't rename.
7. **Evidence-driven.** New tools come from observed friction: what agents hand-wrote, what they got wrong,
   what the user hit. See the `eve-fit-improve-from-agent-runs` skill. Verify with real fits through the
   engine, not by reasoning.
8. **User data stays private.** `fits/` (the user's library) and `data/` (pilots, ESI tokens, caches) are
   gitignored. Secrets come only from env vars (`EVE_ESI_*`). Never commit, print or log them. ESI tokens
   are encrypted with Pyfa's cipher. The repo is **public**.
9. **Output users can use.** Fits leave as `.eft` (clipboard import) **and** `.xml` (file import; Pyfa
   mangles `.eft` files). Training plans leave as EVEMon `.emp`/`.xml` plus Markdown.

## Checklist

1. **Find the Pyfa capability**: `grep -rn` in `vendor/pyfa/eos`, `service`, `graphs`, `gui/fitCommands`. Note
   any wx or GUI coupling; `service`/`gui` imports work headless because `boot()` pre-imports `gui.mainFrame`.
2. **Engine logic goes in a module, not in `server.py`.** Pick the right one:
   - `fitting.py`: `FitHandle` and mutation
   - `stats.py`: metrics (also add to `METRICS`, and to `UNITS` in `evaluate.py` if numeric)
   - `items.py`: item index and search (bump `INDEX_VERSION` if tagging changes)
   - `explore.py`: sweep, variants, charges
   - `optimizer.py`
   - `application.py`
   - `ships.py`
   - `skillplan.py`
   - `pilots.py`, `esi.py`
   - `evaluate.py`: contract
3. **The tool in `server.py`**: a thin `@_tool` wrapper with a docstring written for an LLM caller (what it
   does, every param's shape with an example, what comes back, when to prefer it over similar tools).
   Annotate types; return dicts.
4. **Performance**: an evaluation is about 10 ms. Keep per-candidate work out of Python loops where Pyfa can
   batch. Use `recalc=False, check=False` inside sweeps and recalc once per candidate.
5. **Tests** in `tests/` using real game data (no mocks of Pyfa). Cover the happy path, an error path, the
   "original untouched" guarantee for exploration tools, and a contract test if `evaluate_fit` changes. ESI
   tests mock the network only (see `test_esi_flow.py`). Run the whole suite:
   `.venv\Scripts\python.exe -m pytest -q`. It must pass before committing.
6. **Docs in the same commit**:
   - `README.md` tools list
   - `docs/AGENT_GUIDE.md` (tool-choice table and gotchas)
   - the `INSTRUCTIONS` string in `server.py` if the typical loop changes
   - `docs/INTEGRATION.md` for any `evaluate_fit` change
7. **Commit message**: explain why (which friction it removes). End with the Co-Authored-By line. Push to
   `main` only when the user wants it pushed. If `evaluate_fit` changed, message the eve-esi sessions
   (`ListAgents`; look for "eve-esi" or "Pyfa stats in eve-esi-mcp").

## Known pitfalls

- Files may have CRLF endings after git touches them, so `Edit` matching can fail. Re-read the file first.
- Bash heredocs with mixed quotes break. For larger patches, write a Python patch script to the scratchpad
  and run it.
- `slot` values on eos modules are raw ints. Pass `FittingSlot.X.value` to functions that compare with `is`.
- EFT import leaves drones docked. `FitHandle(drones_active=True)` launches them heaviest-first and relaunches
  them on bandwidth changes; set `auto_drones=False` before hand-setting drone states.
- T3C hulls have 0 slots until subsystems are fitted. Swapping a subsystem must `recalc()` then `fit.fill()`.
