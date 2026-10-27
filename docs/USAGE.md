# Usage

Bug Repro has two ways to drive a browser:

1. **Scripted recipes** - a validated JSON sequence run by the CLI. No model is
   involved.
2. **A persistent local service** - an external agent observes the page and
   submits one supported action at a time. The tool validates, executes,
   records, and checks each step.

Both paths use the same browser engine, action schema, expectation checker, run
format, and report renderer.

## Install

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m playwright install chromium
```

All commands below run from the repository root. On Windows, use PowerShell
5.1 or later.

## Concepts

- **Recipe** - a JSON file containing a target URL, a frozen expectation, and
  an ordered list of actions.
- **Action** - one of `goto`, `click`, `fill`, `reload`, `observe`, or
  `screenshot`.
- **Expectation** - a supported check that is fixed before the actions run.
  It is never derived from the page or from a model response.
- **Run** - a new directory containing the executed recipe, `run.json`,
  `report.md`, and screenshots.
- **Driver** - a label for how the sequence was produced:
  `scripted-test`, `alan-agent`, or `recorded-replay`.

## Scripted reproduction

```powershell
.\.venv\Scripts\bugrepro.exe serve --mode buggy --port 8101
.\.venv\Scripts\bugrepro.exe run recipes\sample-alice-bob.json --base-url http://127.0.0.1:8101
```

A minimal recipe:

```json
{
  "schema_version": 1,
  "name": "alice-logout-bob-profile",
  "recorded_base_url": "http://127.0.0.1:8101",
  "expectation": {
    "kind": "heading_text",
    "text": "Profile for Bob",
    "level": 1
  },
  "actions": [
    {"type": "goto", "path": "/"},
    {"type": "fill", "locator": {"role": "textbox", "name": "Username"}, "text": "alice"},
    {"type": "click", "locator": {"role": "button", "name": "Log in"}},
    {"type": "click", "locator": {"role": "link", "name": "View profile"}},
    {"type": "click", "locator": {"role": "button", "name": "Log out"}},
    {"type": "fill", "locator": {"role": "textbox", "name": "Username"}, "text": "bob"},
    {"type": "click", "locator": {"role": "button", "name": "Log in"}},
    {"type": "click", "locator": {"role": "link", "name": "View profile"}}
  ]
}
```

`run` creates a unique directory under `runs\`, evaluates the expectation once,
and exits with code 0 when it passes, 1 when it fails, and 2 for an execution
error. A run directory contains:

```text
runs\<timestamp>-<label>-run\
  recipe.json
  run.json
  report.md
  evidence\01-goto.png ...
```

The Markdown report includes the expectation, ordered actions, observations,
check outcome, action failures, execution errors, and evidence links.

### Replay

```powershell
.\.venv\Scripts\bugrepro.exe replay runs\<run-dir> --base-url http://127.0.0.1:8102 --label fixed-replay
```

`replay` accepts a run directory or a recipe file. It uses a fresh browser
context, executes the same actions and expectation, and records driver
`recorded-replay`. It never calls a model. The base URL may change; the recipe,
actions, and expectation may not.

## Actions and locators

Locators use an ARIA role and accessible name. CSS and XPath locators are not
supported.

| Action | Fields | Notes |
| --- | --- | --- |
| `goto` | `path` | Same-origin path such as `/profile` |
| `click` | `locator` | ARIA `role`, accessible `name`, optional `exact` |
| `fill` | `locator`, `text` | Textbox, searchbox, or combobox |
| `reload` | none | Fresh GET of the current page |
| `observe` | `label` | Record current state |
| `screenshot` | `label` | Capture named evidence |

Accepted roles include `button`, `link`, `textbox`, `searchbox`, `combobox`,
`checkbox`, `radio`, `switch`, `heading`, `tab`, `menuitem`, and `option`.

## Checks

Two expectation kinds are supported:

| Kind | Fields | Meaning |
| --- | --- | --- |
| `heading_text` | `text`, `level` | A heading at that level has the exact text |
| `region_text_exact` | `text`, `region` | A named region contains a list item with the exact text |

Checks wait for the expected content up to the operation timeout. A missing
container is an execution error; a present container with wrong content is a
failed check.

## Persistent agent service

The service owns Chromium and keeps one browser context per investigation. It
binds to `127.0.0.1`, requires a locally generated bearer token, and writes the
token only to a per-user state file outside the repository.

```powershell
.\.venv\Scripts\bugrepro.exe agent serve
```

Start an investigation with a written report and a frozen expectation:

```powershell
.\.venv\Scripts\bugrepro.exe agent start `
  --target http://127.0.0.1:8101 `
  --report-file examples\live-bug-report.txt `
  --expectation-file examples\live-expectation.json `
  --driver alan-agent `
  --model "<your-configured-model>" `
  --model-source session-reported
```

The response contains a `session_id`, the frozen expectation fingerprint, and
an action budget. The agent then uses:

```powershell
.\.venv\Scripts\bugrepro.exe agent observe --session <session-id>
.\.venv\Scripts\bugrepro.exe agent act --session <session-id> --action-file action.json
.\.venv\Scripts\bugrepro.exe agent status --session <session-id>
.\.venv\Scripts\bugrepro.exe agent finish --session <session-id>
.\.venv\Scripts\bugrepro.exe agent export --session <session-id>
.\.venv\Scripts\bugrepro.exe agent close --session <session-id>
```

`act` also accepts `--rationale-file` to record a short public reason for the
action. `finish` evaluates the frozen expectation; `export` writes a replayable
recipe only for a completed, error-free attempt. All commands print JSON.

Stop the service with:

```powershell
.\.venv\Scripts\bugrepro.exe agent stop
```

If the service uses a non-default state file, pass `--state-file` to each
client command. The collection of commands and action examples for an external
agent is in [`ALAN_INTEGRATION.md`](ALAN_INTEGRATION.md).

## Requirement-guided exploration

An exploration is a bounded set of scenarios linked to user-authored
requirements. Start with a validated specification:

```json
{
  "target": "http://127.0.0.1:8103",
  "description": "Task list with titles and a Tasks region.",
  "requirements": [
    {"id": "R1", "text": "An added task remains visible after reloading."}
  ],
  "allowed_actions": ["goto", "click", "fill", "reload"],
  "allowed_test_data": ["Prepare demo task"],
  "excluded_interactions": ["Deleting tasks"],
  "max_scenarios": 3,
  "max_actions": 60
}
```

```powershell
.\.venv\Scripts\bugrepro.exe explore start --spec-file exploration.json --driver alan-agent --model "<your-configured-model>"
```

A scenario freezes its expectation before its first action:

```json
{
  "requirement_id": "R1",
  "title": "Added task survives reload",
  "description": "Add a task, reload, and check the Tasks region.",
  "expectation": {"kind": "region_text_exact", "text": "Prepare demo task", "region": "Tasks"},
  "test_data": ["Prepare demo task"]
}
```

```powershell
.\.venv\Scripts\bugrepro.exe explore scenario start --exploration <id> --scenario-file scenario.json
.\.venv\Scripts\bugrepro.exe agent observe --session <session-id>
.\.venv\Scripts\bugrepro.exe agent act --session <session-id> --action-file action.json
.\.venv\Scripts\bugrepro.exe explore scenario finish --exploration <id> --scenario S1
.\.venv\Scripts\bugrepro.exe explore scenario export --exploration <id> --scenario S1
.\.venv\Scripts\bugrepro.exe explore scenario confirm --exploration <id> --scenario S1
.\.venv\Scripts\bugrepro.exe explore finish --exploration <id>
```

### Classifications

| Classification | Meaning |
| --- | --- |
| `violation` | An error-free failed expectation recurred in replay confirmation. |
| `possible_issue` | The expectation failed once but was not confirmed, or did not recur. |
| `no_violation` | The expectation held in that scenario. Not a bug-free claim. |
| `execution_error` | Action failure, timeout, or browser error. Not a product finding. |
| `not_evaluated` | Not finished, budget exhausted, or the check could not run. |

Limits: 3 scenarios, 60 browser actions total (including confirmation), and 20
actions per scenario. `allowed_actions` and allowed fill text are enforced by
the service.

## HTML reports

Comparison report:

```powershell
.\.venv\Scripts\bugrepro.exe report `
  --attempt runs\<attempt> `
  --buggy runs\<buggy-replay> `
  --fixed runs\<fixed-replay> `
  --output runs\evidence-report.html
```

Exploration report:

```powershell
.\.venv\Scripts\bugrepro.exe report `
  --exploration runs\<exploration-dir> `
  --output runs\exploration-report.html
```

Reports are single static HTML files with embedded screenshots. They contain no
scripts and make no network requests. Only the runs selected on the command
line are included. A comparison only claims an unchanged replay when a replay
is present, all fingerprints are available, and the recipe/expectation
fingerprints match.

## Safety and limits

- Targets must be literal loopback HTTP(S) hosts. External requests, including
  redirect destinations, are blocked before they reach the network.
- Chromium only. Service workers are blocked so they cannot bypass request
  interception.
- No model-generated Python, JavaScript, CSS, or XPath is executed.
- Browser operations have finite timeouts and recoverable sessions.
- A failed check is an observation about the sequence, not proof of a root
  cause. A passing scenario is not a claim that the app is bug-free.
- No automatic bug discovery, automatic repair, deployment, or hosted service.
