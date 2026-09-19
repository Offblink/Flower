# One-shot quality gate: lint (autofix), format check, re-lint, test.
# The format step only CHECKS. The tree is aligned with the pinned ruff, so a
# disagreement means the caller's own edit is off-style — rewriting unrelated
# files behind their back is what a gate must not do.
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

ruff check --fix flower tests app.py
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
ruff format --check flower tests app.py
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
ruff check flower tests app.py
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$env:PYTHONIOENCODING = "utf-8"
python -m pytest -q
exit $LASTEXITCODE
