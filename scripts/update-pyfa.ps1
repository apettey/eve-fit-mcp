# Pull the latest upstream Pyfa, rebuild the game database, and run the tests.
# Usage: powershell -File scripts\update-pyfa.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root ".venv\Scripts\python.exe"

Push-Location $root
try {
    $before = git -C vendor/pyfa rev-parse --short HEAD
    git submodule update --init --remote vendor/pyfa
    if ($LASTEXITCODE -ne 0) { throw "submodule update failed" }
    $after = git -C vendor/pyfa rev-parse --short HEAD
    Write-Host "Pyfa: $before -> $after"
    if ($before -ne $after) {
        git -C vendor/pyfa log --oneline "$before..$after"
    }

    # eve.db is rebuilt from vendor/pyfa/staticdata whenever the data version changes
    Push-Location vendor/pyfa
    & $py -c "from db_update import db_needs_update, update_db; update_db() if db_needs_update() is not False else print('eve.db up to date')"
    Pop-Location
    if ($LASTEXITCODE -ne 0) { throw "eve.db rebuild failed" }

    & $py -m pytest -q
    if ($LASTEXITCODE -ne 0) { throw "tests failed after update - check eve_fit_mcp/pyfa_bridge.py against upstream changes" }

    Write-Host "OK. Commit the bump with: git add vendor/pyfa; git commit -m 'Bump Pyfa to $after'"
}
finally {
    Pop-Location
}
