# Bug Repro

A small, honest prototype for turning a written web-app bug report into a
**replayable browser test with evidence**.

You give it a local app URL, a written bug report, and a fixed human
expectation. It drives Chromium with Playwright, records every action, checks
the expectation, and writes a report plus screenshots into a unique run
directory. After a manual fix, the same recipe can be replayed against another
local URL.

> **Phase 3 status: two fixtures, a persistent agent workflow, and a portable
> HTML evidence report.** The Alan session can still drive a browser one action
> at a time through local commands. There is no automatic bug discovery, no
> automatic fix and no direct model API integration.

## What is implemented

- Installable Python package with a `bugrepro` CLI.
- Two independent demo fixtures, each with `buggy` and `fixed` modes on
  separate ports:
  - account switching (cached profile after logout),
  - task list (a task disappears after reload).
- Browser executor: observe, click, fill, reload, screenshot, accessibility
  snapshots.
- Semantic, ARIA-role locators only; no model-generated Python or JavaScript.
- Strict JSON recipe validation and loopback-only target validation.
- Fixed expectations stored separately from the action sequence: heading text,
  or an exact task title inside an accessible region.
- Structured run records, Markdown reports, screenshots, and non-overwriting
  run directories.
- Replay of a saved recipe against a different local URL with no model calls.
- A persistent local service that keeps one browser context alive across
  separate CLI invocations, with token auth, an action budget, idle timeouts
  and crash handling.
- An agent-facing CLI (`agent start/observe/act/finish/export/status/close/
  stop`) returning machine-readable JSON, with optional recorded rationales.
- A self-contained HTML report with embedded screenshots, a three-way
  comparison and fingerprint-mismatch warnings.
- Explicit scripted mechanics checks: buggy build fails, fixed build passes,
  execution errors stay distinct from expectation failures.

The full operator workflow, including a reusable investigation prompt, is in
[`docs/ALAN_WORKFLOW.md`](docs/ALAN_WORKFLOW.md).

## What is deliberately not implemented

- Automatic bug discovery or automatic patches.
- A direct model/provider API integration (the Alan session itself is the
  decision maker; our local tools are the executor).
- A dashboard, database service, multi-agent system or GitHub integration.
- Non-loopback targets.
- Arbitrary code, shell or filesystem endpoints on the service.
- A general assertion language; only two explicit expectation kinds exist.

## Setup (Windows 11, PowerShell 5.1)

Verified on this machine with Python 3.13.14, Playwright 1.64.1 and Chromium
build 1248. Python 3.11+ is supported.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m playwright install chromium
```

This workspace already has `.venv` prepared.

## View the demo

Start the two builds in two terminals:

```powershell
# terminal 1 - buggy build
.\.venv\Scripts\bugrepro.exe serve --mode buggy --port 8101
```

```powershell
# terminal 2 - fixed build
.\.venv\Scripts\bugrepro.exe serve --mode fixed --port 8102
```

Open:

- Buggy: <http://127.0.0.1:8101>
- Fixed: <http://127.0.0.1:8102>

Reproduce by hand on the buggy build:

1. Type `alice`, click **Log in**.
2. Click **View profile** (this populates the cached profile).
3. Click **Log out**.
4. Type `bob`, click **Log in**.
5. Click **View profile**.

The header says `Signed in as: Bob`, but the heading says
`Profile for Alice`. The fixed build shows `Profile for Bob`.

The demo has two accounts, `alice` and `bob`, with no passwords and no real
credentials.

### Task-list fixture

The second fixture is independent of the account demo:

```powershell
.\.venv\Scripts\bugrepro.exe serve --app tasks --mode buggy --port 8103
.\.venv\Scripts\bugrepro.exe serve --app tasks --mode fixed --port 8104
```

Both builds expose the same labelled **Task title** input, **Add task** button,
and accessible region named **Tasks**. Adding `Prepare hackathon demo` shows it
in the list. On the buggy build it is gone after a reload; on the fixed build
it persists. Task storage is a browser cookie, so a fresh browser context per
investigation or replay starts empty.

## Run and replay

Run the recorded recipe against the buggy build:

```powershell
.\.venv\Scripts\bugrepro.exe run recipes\sample-alice-bob.json `
  --base-url http://127.0.0.1:8101 --label buggy-build
```

Expected: `Check: FAILED`, exit code `1`, a new directory under `runs\`.

Replay the **same saved recipe** against the fixed build:

```powershell
.\.venv\Scripts\bugrepro.exe replay runs\<buggy-run-dir> `
  --base-url http://127.0.0.1:8102 --label fixed-build
```

Expected: `Check: PASSED`, exit code `0`.

`replay` accepts either a run directory or a recipe JSON file. Add `--headed`
to watch the browser. `--run-root` changes where output goes, and `--timeout-ms`
sets a finite per-operation timeout.

Each run directory contains:

```text
runs/20261026-172605-buggy-build-run/
  recipe.json        # exact recipe that was executed
  run.json           # structured record (actions, observations, check, errors)
  report.md          # human-readable report
  evidence/01-goto.png ... 08-click.png
```

Run directories are never reused or overwritten; a numeric suffix is added if
the name already exists. `runs/` is gitignored.

Exit codes for `run` / `replay`:

| Code | Meaning |
| --- | --- |
| 0 | Expectation passed |
| 1 | Expectation failed (the reported bug was reproduced) |
| 2 | Execution error / inconclusive |

## Persistent agent service (Phase 2)

The service owns Playwright on a background worker thread and keeps one
browser context per investigation alive across separate CLI invocations. All
browser operations are serialized on that thread. The HTTP surface binds to
127.0.0.1 and requires a locally generated bearer token stored in a per-user
state file outside the repository (`%LOCALAPPDATA%\BugRepro\service.json` by
default on Windows). The token is never printed, passed as an argument or
written into a run.

Terminal 1:

```powershell
.\.venv\Scripts\bugrepro.exe agent serve
```

Terminal 2:

```powershell
.\.venv\Scripts\bugrepro.exe agent start `
  --target http://127.0.0.1:8101 `
  --report-file examples\live-bug-report.txt `
  --expectation-file examples\live-expectation.json `
  --driver alan-agent `
  --model deepseek/deepseek-v4.1-flash `
  --model-source session-reported `
  --label live-alan-agent
```

The start response contains the `session_id`, the frozen expectation
fingerprint and the action budget. Then, one step at a time:

```powershell
.\.venv\Scripts\bugrepro.exe agent observe --session <id>
.\.venv\Scripts\bugrepro.exe agent act --session <id> --action-file <action.json>
.\.venv\Scripts\bugrepro.exe agent status --session <id>
.\.venv\Scripts\bugrepro.exe agent finish --session <id>
.\.venv\Scripts\bugrepro.exe agent export --session <id>
.\.venv\Scripts\bugrepro.exe agent close --session <id>
.\.venv\Scripts\bugrepro.exe agent stop
```

Guarantees:

- Each `agent start` creates a fresh browser context, so investigations are
  isolated from each other and from scripted runs.
- The expectation is validated and fingerprinted at start. The action endpoint
  accepts no expectation field, and `export` preserves the original one.
- An action budget (default 20, maximum 50), a per-session idle timeout and a
  service idle timeout bound resource use.
- Every action is validated by `recipe.py`; there is no selector, script,
  shell or filesystem endpoint.
- Budget exhaustion, action errors and browser crashes produce check status
  `error`, never `failed`. Only a completed, error-free attempt can be
  exported, and the full attempt remains in `run.json`.
- `export` writes `recipe.json` inside the run directory. `replay` then runs
  it against any loopback URL with no model decisions.

## HTML evidence report (Phase 3)

Turn the selected runs into one portable file with embedded screenshots:

```powershell
.\.venv\Scripts\bugrepro.exe report `
  --attempt runs\<live-run-dir> `
  --buggy runs\<buggy-replay-dir> `
  --fixed runs\<fixed-replay-dir> `
  --output runs\evidence-report.html
```

Open the HTML file directly in a browser. It is self-contained: no scripts, no
network requests, no service needed. It shows the bug report, the frozen
expectation and its fingerprint, the outcome, driver and session-reported
model identity, ordered actions with recorded rationales, before/after
screenshots, errors, budget, and a three-way comparison.

The comparison explicitly warns when it is **not** an unchanged replay:
different recipe fingerprints, different expectation fingerprints, missing
fingerprints, or a replay not labelled `recorded-replay`. Only the runs passed
on the command line are included.

## Scripted verification

```powershell
.\.venv\Scripts\python.exe -m pytest
```

The mechanics test starts both real servers on free ports, runs the identical
recipe against both, and asserts:

- buggy build: check `failed`, observed heading `Profile for Alice`;
- fixed build: check `passed`, observed heading `Profile for Bob`;
- recipe text and expectation fingerprint are identical across both runs;
- an unreachable app produces check `error`, never `failed`.

The last point matters: a timeout or missing element is an execution error,
**not** proof that the reported product bug was reproduced.

`tests/test_agent_service.py` adds scripted service checks: browser state
surviving separate CLI processes, isolation between investigations, frozen
expectation, budget and action-error handling, service cleanup, and replay of
an exported sequence. Those tests are labelled scripted; no model chooses
anything in them.

`tests/test_task_app.py` covers the second fixture: `reload` validation,
`region_text_exact` validation, the identical recipe failing on buggy mode and
passing on fixed mode, and fresh-context task isolation.
`tests/test_html_report.py` covers HTML escaping, embedded images, selected-run
filtering and fingerprint-mismatch warnings.

## How it works (beginner view)

```text
recipes/*.json  +  --base-url
        |
        v
  recipe.py  validate JSON, whitelist action types and ARIA roles
        |
        v
  browser.py  fresh Chromium context -> observe / click / fill / screenshot
        |
        v
  check.py  read the expectation -> passed / failed / error
        |
        v
  runner.py  write run.json + report.md + evidence/*.png into a unique run dir
```

- **The recipe is data, not code.** Supported actions are `goto`, `click`,
  `fill`, `reload`, `observe` and `screenshot`. Locators use an ARIA role plus
  an accessible name, e.g. `button "Log in"`. `reload` re-fetches the current
  page with GET.
- **The expectation is separate.** It lives in its own recipe field, is shown
  verbatim in every report, and is fingerprinted. Nothing in replay rewrites
  or weakens it. Two kinds exist: `heading_text` and `region_text_exact`
  (an exact list item inside an accessible region).
- **Errors are separated.** `action_failures` are locator/action problems;
  `execution_errors` are browser/session problems. Either one makes the check
  `error`, not `failed`.
- **Every run is isolated.** A new Chromium context means no cookies or cache
  leak between runs, and a new output directory means old evidence survives.

## Single-shot probing commands

Two one-shot commands expose the same validated action layer without starting
the service:

```powershell
.\.venv\Scripts\bugrepro.exe observe --base-url http://127.0.0.1:8101
.\.venv\Scripts\bugrepro.exe act --base-url http://127.0.0.1:8101 `
  --action-file examples\action-goto.json
```

PowerShell 5.1 strips inline JSON quotes when calling native executables, so
use `--action-file` here. Each call starts a fresh browser, so cookies do not
persist between calls; use the persistent service for multi-step sessions.

## Safety limits

- Only `http`/`https` and literal loopback hosts (`127.0.0.1`, `localhost`,
  `::1`) are accepted.
- Credentials in URLs, path traversal forms, `file:`, `javascript:` and
  non-loopback hosts are rejected.
- No `eval`, `exec`, inline Python, or page-side JavaScript is accepted from a
  recipe or from an agent.
- Browser operations have finite timeouts and resources are closed in a
  `finally`/context-manager path.

## Project layout

```text
src/bugrepro/
  cli.py             commands: serve, run, replay, act, observe, agent ..., report
  safety.py          loopback-only URL and path validation
  recipe.py          JSON schema, action/locator validation, fingerprints
  browser.py         Playwright host, session, observations, screenshots
  expectations.py    immutable heading / region expectations, fingerprint
  check.py           expectation evaluation -> passed/failed/error
  runner.py          scripted execution, run directories, run.json
  report.py          Markdown report renderer
  html_report.py     self-contained HTML evidence report + comparison
  agent_client.py    service state file + JSON-over-HTTP client
  service.py         persistent local service (worker thread, HTTP, auth)
  investigation.py   one investigation: freeze, record, budget, finish, export
  sample_app/        account-switching fixture
  task_app/          task-list fixture (reload persistence)
recipes/             saved recipes
examples/            live reports, expectations and action files
tests/               validation, safety, mechanics, service and report tests
docs/PROGRESS.md     status, model-connection findings, live-run results
docs/ALAN_WORKFLOW.md operator workflow and reusable investigation prompt
```

## Model connection

No provider integration was added. The current Alan session (`alan_code`
provider, session-reported model `deepseek/deepseek-v4.1-flash`) is the
decision maker, and the local service is the executor: the agent calls the CLI
as a tool, one action at a time, from its own observations. Alan credits are
not an API key and no public Alan model endpoint is documented, so nothing
here calls a model API. The confirmed facts, blockers and the live run are
recorded in `docs/PROGRESS.md`.