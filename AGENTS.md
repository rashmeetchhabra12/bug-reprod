# Agent guide - Bug Repro

This repository is a Phase 1 prototype. Read `docs/PROGRESS.md` before
extending it. The deliberately scripted mechanics check must stay green.

## Environment

- Windows 11, PowerShell 5.1 (do not assume bash).
- Python 3.13.14 in `.venv` (project supports 3.11+).
- Run commands from the repository root with `.\.venv\Scripts\python.exe`.
- Chromium for Playwright is installed in
  `%LOCALAPPDATA%\ms-playwright`.

## Commands

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\bugrepro.exe serve --mode buggy --port 8101
.\.venv\Scripts\bugrepro.exe serve --mode fixed --port 8102
.\.venv\Scripts\bugrepro.exe run recipes\sample-alice-bob.json --base-url http://127.0.0.1:8101
.\.venv\Scripts\bugrepro.exe replay runs\<run-dir> --base-url http://127.0.0.1:8102
```

`pytest` uses `.pytest-tmp/` as its basetemp because the machine's shared
pytest temp directory is not readable.

## Invariants - do not break these

1. **Expectations are immutable during replay.** The expectation comes from
   the recipe and is passed to `check.py` unchanged. Never infer it from the
   page, the actions, a model response, or a previous run. Never let a retry
   weaken it.
2. **Recipes are data, not code.** Only `goto`, `click`, `fill`, `observe` and
   `screenshot` are valid. Never add `eval`, `exec`, Python strings, raw
   JavaScript, or CSS/XPath locator fields. Extend `recipe.py` with explicit
   validation instead.
3. **Loopback only.** All targets pass through `safety.validate_base_url`.
   Do not add a bypass, redirect following to another origin, or non-loopback
   support without updating the threat model and tests.
4. **Fresh context per run.** Each `execute_recipe` call creates a new
   `BrowserSession` and a new Playwright context so cookies and cache cannot
   leak between runs.
5. **Finite timeouts and cleanup.** Every browser operation takes a timeout;
   context/browser/Playwright cleanup lives in `BrowserSession.close`.
6. **Never overwrite a run.** `allocate_run_dir` creates directories
   exclusively and adds a suffix on collision. Do not write into an existing
   run directory.
7. **Keep errors separate.** `action_failures` (locator/action) and
   `execution_errors` (browser/session) both force check status `error`. A
   timeout or missing element is never a reproduced bug.
8. **No secrets in the repo.** Do not read, copy, or commit Alan credentials,
   runtime tokens, API keys or `.env` files.

## Where to change things

- New action type: `recipe.py` validation + `browser.py` execution + a
  mechanics or integration test. Update `docs/PROGRESS.md`.
- New expectation kind: `expectations.py` schema + `check.py` evaluation, with
  explicit passed/failed/error semantics.
- Report output: `report.py` only; `run.json` remains the source of truth.
- Demo app: `sample_app/server.py`. Keep the buggy and fixed behaviour
  independently startable on different ports.
- Phase 2 agent loop: add a new module; do not put model calls inside
  `runner.py`, `browser.py`, or `check.py`.

## Definition of done

- `.\.venv\Scripts\python.exe -m pytest` passes.
- The buggy/fixed CLI demo still produces exit codes 1 and 0 respectively.
- Any new claim is marked implemented vs planned in `docs/PROGRESS.md`.
- No dashboard, database, auth system, auto-patching or GitHub integration is
  added in Phase 1.