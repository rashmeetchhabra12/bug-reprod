# Bug Repro

A small, honest prototype for turning a written web-app bug report into a
**replayable browser test with evidence**.

You give it a local app URL, a scenario (login as Alice, log out, login as
Bob), and a fixed human expectation (`Profile for Bob`). It drives Chromium
with Playwright, records every action, checks the expectation, and writes a
report plus screenshots into a unique run directory. After a manual fix, the
same recipe can be replayed against another local URL.

> **Phase 1 status: scripted foundation only.** There is no AI investigator,
> no automatic bug discovery and no automatic fixing. The included demo bug is
> deliberate, and the mechanics check is a human-written script.

## What is implemented

- Installable Python package with a `bugrepro` CLI.
- Demo app in two independent modes (`buggy` and `fixed`) on separate ports.
- Browser executor: observe, click, fill, screenshot, accessibility snapshots.
- Semantic, ARIA-role locators only; no model-generated Python or JavaScript.
- Strict JSON recipe validation and loopback-only target validation.
- Fixed expectation stored separately from the action sequence.
- Structured run records, Markdown reports, screenshots, and non-overwriting
  run directories.
- Replay of a saved recipe against a different local URL with no model calls.
- Explicit scripted mechanics check: buggy build fails, fixed build passes,
  execution errors stay distinct from expectation failures.

## What is deliberately not implemented

- An AI agent that chooses actions.
- Automatic bug discovery or automatic patches.
- A dashboard, auth system, database, multi-agent system or GitHub integration.
- Non-loopback targets.
- A provider integration for Alan or DeepSeek (see `docs/PROGRESS.md`).

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
  `fill`, `observe` and `screenshot`. Locators use an ARIA role plus an
  accessible name, e.g. `button "Log in"`.
- **The expectation is separate.** It lives in its own recipe field, is shown
  verbatim in every report, and is fingerprinted. Nothing in replay rewrites
  or weakens it.
- **Errors are separated.** `action_failures` are locator/action problems;
  `execution_errors` are browser/session problems. Either one makes the check
  `error`, not `failed`.
- **Every run is isolated.** A new Chromium context means no cookies or cache
  leak between runs, and a new output directory means old evidence survives.

## Agent-facing action interface (experimental)

Two single-shot commands expose the same validated action layer:

```powershell
.\.venv\Scripts\bugrepro.exe observe --base-url http://127.0.0.1:8101
.\.venv\Scripts\bugrepro.exe act --base-url http://127.0.0.1:8101 `
  --action-file examples\action-goto.json
```

PowerShell 5.1 strips inline JSON quotes when calling native executables, so
use `--action-file` here. Both commands print a JSON observation. Each call
starts a fresh browser, so cookies do not persist between calls. Multi-step
flows must use a recipe and `run`; a stateful agent session is Phase 2 work.

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
  cli.py            commands: serve, run, replay, act, observe
  safety.py         loopback-only URL and path validation
  recipe.py         JSON schema, action/locator validation, fingerprints
  browser.py        Playwright session, observations, screenshots
  expectations.py   immutable expectation and fingerprint
  check.py          fixed heading expectation -> passed/failed/error
  runner.py         execution, run directories, run.json
  report.py         Markdown report renderer
  sample_app/       demo app with buggy and fixed modes
recipes/            saved recipes
tests/              validation, safety, run-directory and mechanics tests
docs/PROGRESS.md    status, model-connection findings, next milestone
```

## Model connection

Phase 1 did not implement a provider integration. The confirmed facts,
proposed routes and blockers are recorded in `docs/PROGRESS.md`. In short: this
session runs `deepseek/deepseek-v4.1-flash` through Alan's `alan_code`
provider, and the agent can call the local CLI as a tool. A direct DeepSeek
API route would need a separate DeepSeek API key; Alan credits are not an API
key and no public Alan model endpoint is documented.