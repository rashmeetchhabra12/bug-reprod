# Agent guide - Bug Repro

This repository is a Phase 4 prototype. Read `docs/PROGRESS.md` and
`docs/ALAN_WORKFLOW.md` before extending it. The deliberately scripted
mechanics checks must stay green, and they must stay labelled as scripted.

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
.\.venv\Scripts\bugrepro.exe serve --app tasks --mode buggy --port 8103
.\.venv\Scripts\bugrepro.exe serve --app tasks --mode fixed --port 8104
.\.venv\Scripts\bugrepro.exe run recipes\sample-alice-bob.json --base-url http://127.0.0.1:8101
.\.venv\Scripts\bugrepro.exe replay runs\<run-dir> --base-url http://127.0.0.1:8102
.\.venv\Scripts\bugrepro.exe agent serve
.\.venv\Scripts\bugrepro.exe agent start --target http://127.0.0.1:8101 --report-file examples\live-bug-report.txt --expectation-file examples\live-expectation.json
.\.venv\Scripts\bugrepro.exe agent act --session <id> --action-file <action.json> --rationale-file <rationale.txt>
.\.venv\Scripts\bugrepro.exe agent finish --session <id>
.\.venv\Scripts\bugrepro.exe agent export --session <id>
.\.venv\Scripts\bugrepro.exe report --attempt runs\<live> --buggy runs\<replay> --fixed runs\<replay> --output runs\evidence-report.html
.\.venv\Scripts\bugrepro.exe explore start --spec-file examples\task-exploration.json --driver alan-agent --label live-task-buggy
.\.venv\Scripts\bugrepro.exe explore scenario start --exploration <id> --scenario-file examples\task-scenario-r1.json
.\.venv\Scripts\bugrepro.exe explore scenario finish --exploration <id> --scenario S1
.\.venv\Scripts\bugrepro.exe explore scenario export --exploration <id> --scenario S1
.\.venv\Scripts\bugrepro.exe explore scenario confirm --exploration <id> --scenario S1
.\.venv\Scripts\bugrepro.exe explore finish --exploration <id>
.\.venv\Scripts\bugrepro.exe report --exploration runs\<exploration-dir> --output runs\exploration-report.html
```

`pytest` uses `.pytest-tmp/` as its basetemp because the machine's shared
pytest temp directory is not readable.

## Invariants - do not break these

1. **Expectations are immutable once frozen.** For scripted runs the
   expectation comes from the recipe; for investigations it is frozen at
   start and fingerprinted. The action interface must never accept an
   expectation. Never infer or weaken it from the page, the actions, a model
   response, or a previous run.
2. **Recipes are data, not code.** Only `goto`, `click`, `fill`, `reload`,
   `observe` and `screenshot` are valid. Never add `eval`, `exec`, Python
   strings, raw JavaScript, or CSS/XPath locator fields. Extend `recipe.py`
   with explicit validation instead.
3. **Loopback only.** All targets pass through `safety.validate_base_url`.
   Do not add a bypass or non-loopback support without updating the threat
   model and tests. Request interception uses CDP `Fetch` and blocks service
   workers; do not replace it with `context.route`, which does not reliably
   intercept redirect destinations.
4. **One execution engine.** `BrowserSession` in `browser.py` is the only
   place Playwright drives a page. Scripted runs and the service both use it.
   Do not add a second browser engine.
5. **Fresh context per run/investigation.** Each `execute_recipe` call and
   each new investigation creates a new context, so cookies and cache cannot
   leak between runs or between sessions.
6. **Finite timeouts and cleanup.** Every browser operation takes a timeout.
   The service also has an action budget, a per-session idle timeout and a
   service idle timeout.
7. **Never overwrite a run.** `allocate_run_dir` creates directories
   exclusively and adds a suffix on collision. Do not write into an existing
   run directory.
8. **Keep errors separate.** `action_failures` (locator/action) and
   `execution_errors` (browser/session) both force check status `error`. A
   timeout, missing element, budget exhaustion or browser crash is never a
   reproduced bug.
9. **Wait for content, not just the container.** `check.py` waits for the
   expected heading/region content up to the timeout. A present container with
   wrong content is `failed`; a missing container is `error`.
10. **No secrets in the repo or logs.** The service token lives only in a
    per-user state file outside the repository. Never print it, accept it as a
    command argument, or write it into a run directory. Never read or copy Alan
    credentials, `local-api.json`, provider auth files, or API keys.
11. **Driver labels must stay honest.** `alan-agent` only when a model chose
    the actions; `scripted-test` for pre-written sequences;
    `recorded-replay` for replays. Never describe a scripted run as AI.
12. **Exploration requirements are user-owned and frozen.** Requirements and
    allowed test data come only from the validated exploration spec. A scenario
    must reference an existing requirement; it can never add, reword or replace
    one, and it freezes its expectation before the first action.
13. **Exploration stays bounded.** At most 3 scenarios, at most 60 browser
    actions in total (scenario plus replay-confirmation actions), and at most 20
    actions per scenario. Existing per-session timeouts still apply.
14. **Exploration findings are conservative.** `violation` requires an
    error-free failed expectation whose exported recipe reproduces the same
    observation in a fresh context on the same target. Action failures,
    execution errors, crashes and budget exhaustion are never product findings;
    a passing scenario is never a bug-free claim.

## Where to change things

- New action type: `recipe.py` validation + `browser.py` execution + a
  mechanics or integration test. Update `docs/PROGRESS.md`.
- New expectation kind: `expectations.py` schema + `check.py` evaluation, with
  explicit passed/failed/error semantics. Only `heading_text` and
  `region_text_exact` exist; do not turn this into a general assertion language.
- Report output: `report.py` (Markdown) and `html_report.py` (portable HTML,
  including exploration mode); `run.json` and `exploration.json` remain the
  source of truth. Escape every dynamic value in HTML and embed only images
  inside the selected run or exploration directory.
- Exploration: `exploration.py` (spec/scenario validation, budget,
  classification, confirmation). It must not call a model and must not weaken
  the rules above. Replay confirmation goes through `execute_recipe_with_host`
  so there is still one Playwright host per worker thread.
- Demo apps: `sample_app/server.py` (accounts) and `task_app/server.py`
  (tasks). Keep buggy and fixed independently startable, with identical
  accessible controls in both modes.
- Service transport or lifecycle: `service.py` (worker thread, HTTP surface,
  timeouts). Do not put model calls there.
- Investigation state machine: `investigation.py`. It must not call a model.
- Agent CLI commands: `cli.py` + `agent_client.py`.
- Workflow documentation: `docs/ALAN_WORKFLOW.md`; keep it in sync with the
  actual CLI.

## Definition of done

- `.\.venv\Scripts\python.exe -m pytest` passes.
- Both buggy/fixed CLI demos (accounts and tasks) still produce exit codes 1
  and 0 respectively.
- Any new claim is marked implemented vs planned in `docs/PROGRESS.md`.
- The HTML report is still self-contained: no scripts, no network requests,
  only explicitly selected runs or the selected exploration.
- No dashboard, database, auth system, auto-patching or GitHub integration is
  added.