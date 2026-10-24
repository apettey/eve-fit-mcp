---
name: eve-fit-update-pyfa
description: Use when updating eve-fit's vendored Pyfa to the latest upstream (new EVE patch, new ships or modules, "keep up with main"), or when something in eve-fit breaks after a Pyfa bump. Keeps Pyfa an unmodified submodule and confines fixes to pyfa_bridge.py and our own modules.
---

# Updating Pyfa (vendor/pyfa)

eve-fit tracks upstream `pyfa-org/Pyfa` as a git submodule and never modifies it. Updating means moving
the pointer, rebuilding game data and fixing **our** side if anything moved.

## Steps
1. Make sure the eve-fit working tree is clean (`git status`). Don't mix a Pyfa bump with feature work.
2. Run the update script (it pulls upstream, rebuilds `eve.db` if the game data changed, and runs the tests):
   ```powershell
   powershell -File scripts\update-pyfa.ps1
   ```
3. If the tests pass, check what changed upstream (`git -C vendor/pyfa log --oneline <old>..<new>`) for
   anything that affects us:
   - new or changed `service.port` formats, `Fit`/`Module` APIs, `graphs` calc signatures
   - new effects or modules that need tags
   - command-link definitions
4. **Rebuild derived caches**: the item index (`data/item_index.json`) rebuilds automatically when the game
   data build changes. If upstream changed groups or market trees in ways the tags rely on, bump
   `INDEX_VERSION` in `items.py`.
5. **Spot-check real fits** before committing: evaluate a few saved fits from `fits/` and compare their key
   numbers with the previous build. Large unexplained shifts are worth a note to the user (it's often a
   balance patch).
6. Commit only the submodule pointer (plus any of our fixes):
   `git add vendor/pyfa && git commit -m "Bump Pyfa to <hash>"`. Push when the user wants it pushed.

## If something breaks
- **Fix it in eve-fit, never in `vendor/pyfa`.**
  - Import or API moves: `pyfa_bridge.py` (the `P` namespace and `boot()`).
  - Behaviour changes: the module that uses it (`fitting.py`, `stats.py`, `application.py`…).
- `boot()` relies on: `config.defPaths(DATA_DIR)`, `db_update` and pre-importing `gui.mainFrame` (circular
  import workaround). Recheck these first if startup fails.
- If `evaluate_fit` numbers or keys change meaning, follow the contract rules in `eve-fit-add-tool` (tell
  eve-esi; `@2` if breaking).
- If a fix truly needs an upstream change, open an upstream issue or PR to Pyfa and work around it on our
  side meanwhile. Don't fork.
