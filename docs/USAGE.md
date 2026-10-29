# Usage

Bug Repro has three ways to drive a browser:

1. **Scripted recipes** - a validated JSON sequence run by the CLI. No model is
   involved.
2. **A persistent local service** - an external agent observes the page and
   submits one supported action at a time. The tool validates, executes,
   records, and checks each step.
3. **Standalone model investigation** - the CLI calls a user-configured
   OpenAI-compatible endpoint and submits its decisions to the same service
   worker, with no separately running service required.

All paths use the same browser engine, action schema, expectation checker, run
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
  `scripted-test`, `alan-agent`, `model-agent`, `codex-cli`, or `recorded-replay`.

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

## Standalone model investigation

For non-loopback providers, set `OPENAI_API_KEY` in your process environment
using your usual secret management. Do not put keys in command arguments,
files in this repository, bug reports or model names. `--api-key-env NAME`
selects another environment variable; it accepts the variable name, never
the key itself.

```powershell
.\.venv\Scripts\bugrepro.exe investigate `
  --target http://127.0.0.1:8101 `
  --report-file examples\live-bug-report.txt `
  --expectation-file examples\live-expectation.json `
  --model-endpoint https://api.openai.com/v1/chat/completions `
  --model "<your-configured-model>" `
  --action-budget 20 --model-call-limit 25 --model-timeout 60
```

For an already running local Ollama server with `qwen2.5:3b` installed, use
the following keyless example with `OPENAI_API_KEY` unset or empty in this
PowerShell process:

```powershell
.\.venv\Scripts\bugrepro.exe investigate `
  --target http://127.0.0.1:8103 `
  --report-file examples\task-bug-report.txt `
  --expectation-file examples\task-expectation.json `
  --model-endpoint http://127.0.0.1:11434/v1/chat/completions `
  --model qwen2.5:3b
```

Only validated loopback hosts (`127.0.0.1`, `localhost`, `[::1]`) may omit a
key. With no key, the client sends no `Authorization` header. If the selected
environment variable contains a key, it is sent even to a loopback endpoint,
so authenticated local providers continue to work. To keep a cloud key in
`OPENAI_API_KEY` while using a keyless local provider, select a different,
unset variable with `--api-key-env NAME`. Non-loopback HTTPS endpoints still
require a key; private LAN addresses and lookalike hostnames do not qualify
for keyless access. Redirects are rejected without forwarding credentials.

The endpoint is the full Chat Completions URL, using HTTPS (or HTTP on
loopback for a local provider). URL credentials, query strings, fragments and
redirects are rejected. Browser targets still must be literal loopback URLs;
the provider endpoint is configured separately. No Alan internals or credentials
are accessed. The command starts an isolated service worker and closes the
browser on completion, failure or interruption.

The provider receives the report, frozen expectation, latest text/accessibility
observation, remaining budget and executed actions. Screenshots are recorded
locally, not sent to the model. Use synthetic test data: page content and fill
values can appear in reports, screenshots and exported recipes.

Providers must support the [Chat Completions format](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create): non-streaming `messages` requests and
`choices[0].message.content` JSON text with `finish_reason: stop`. The model
returns either one supported action or a finish request; local validation
enforces the action schema. Tool calls, refusals, truncated responses,
malformed output and provider errors end the attempt as an error, with no
automatic retries. Provider response bodies and credentials are not logged.
The command does not require provider-specific structured-output extensions.

Limits: 1-50 browser actions (default 20), 1-100 model calls (default 25), and
a finite model I/O timeout of at most 120 seconds (default 60). Response bodies
are capped at 1 MiB and reading stops when the request deadline is reached
(a pending I/O can take up to one additional I/O timeout). A finish decision
uses a model call but no browser action. After the last allowed action the
model can request finish; requesting another action or needing another model
call past its limit makes the attempt an error. `--timeout-ms` controls browser
operations (500-60000); `--headed`, `--run-root` and `--label` are also available.

The expectation is frozen before the first model call. The model cannot set
the result: the existing checker evaluates it only on finish. Exit codes are
0 for passed, 1 for failed, and 2 for error/incomplete. A failed expectation
records an observation, not a verified root cause or replay-confirmed finding.
The model may stop too early or choose an irrelevant sequence; review evidence.

The command prints JSON with the result and paths to `report.md`, a
self-contained `report.html`, and `recipe.json` when existing export rules
allow it. Error-free completed passed and failed attempts can be exported;
provider errors and exhausted budgets cannot. `run.json` records model-call
usage, actions, observations, errors and the frozen fingerprint. Model identity
is user-configured metadata, not a verified provider identity. Missing keys for
non-loopback endpoints or invalid startup configuration fail before an
investigation can be recorded.

Replay an exported run using `bugrepro replay runs\<run-dir> --base-url
http://127.0.0.1:8102`; it never calls a model or needs a provider key.

### Codex CLI decisions

The endpoint backend remains the default. To use Codex instead, put the official
native `codex` executable on PATH and use its own login commands:

```powershell
codex --version
codex exec --help
codex login
codex login status

.\.venv\Scripts\bugrepro.exe investigate `
  --decision-backend codex-cli `
  --target http://127.0.0.1:8103 `
  --report-file examples\task-bug-report.txt `
  --expectation-file examples\task-expectation.json `
  --action-budget 8 --model-call-limit 10 --model-timeout 90 `
  --label codex-task-buggy
```

Start the task fixture with `bugrepro serve --app tasks --mode buggy --port 8103`
in another terminal. No endpoint or Bug Repro API-key configuration is required
for the Codex backend. Bug Repro never opens Codex credential files, extracts
login tokens, or manages login. Shell wrappers such as `codex.cmd` are rejected;
the command must resolve to the official native executable. `--model` optionally
requests a model; when omitted, the CLI selects its default and the recorded
model identity is `unknown` / `unavailable`. A requested name is unverified
metadata, not evidence of which model served the request.

Each decision launches `codex exec` with the report, frozen expectation, latest
observation and executed action history on stdin. It requests `--output-schema`,
`--json`, `--ephemeral`, `--ignore-user-config`, `--ignore-rules`,
`--strict-config`, `--sandbox read-only`, `approval_policy="never"`,
`web_search="disabled"`, and `project_doc_max_bytes=0`.
The cwd is a fresh temporary directory containing only the output schema, so
the repository task is not supplied to Codex. No task resumes or forks are used.
The schema wraps one action or finish decision in `{"decision": ...}`; the
adapter unwraps it and applies the existing action validator.

The adapter disables the installed CLI's shell, shell snapshot, apps, plugins,
remote plugins, multi-agent, browser use (including external and full CDP),
computer use, code mode and its host, image generation/viewing, hooks, memories,
goals, workspace dependencies and tool suggestions. Preflight checks local help,
feature availability and login status, failing rather than silently dropping
missing controls. This was checked against `codex-cli 0.164.0-alpha.11.1`.

**Isolation limitation:** the CLI has no documented universal no-tools switch.
These supported restrictions are defense in depth, not proof that every current
or future native tool, managed integration or policy is disabled. Read-only mode
alone does not prohibit commands. Unknown events and any reported tool events
are investigation errors, even if followed by a valid decision; detecting an
event afterward cannot undo an independent action. Do not treat this backend
as a security boundary for hostile pages or run it where strict tool isolation
is required. The adapter never intentionally delegates browser operations,
filesystem edits or commands to Codex; all accepted browser actions still pass
through Bug Repro's executor.

`--model-timeout` bounds each decision subprocess (positive, at most 120 seconds); preflight
has three separate 10-second limits. `--model-call-limit` counts decision
requests, including failed requests, not preflight commands. Timeout or
cancellation kills the process tree and records an investigation error.
The tested CLI labels its known "Code Mode is unavailable because code-mode
host is disabled" startup notice as an error item. The adapter recognizes the
complete fail-closed notice only once, before inference starts; disabling the
host remains mandatory. Other error events, changed notices, and the same
notice during inference still fail the investigation.
Missing CLI, missing restrictions, authentication failures, quota/rate limits,
nonzero exit, malformed/incomplete output and tool events are errors, never
reproduced bugs. Raw CLI stdout/stderr and reasoning are not saved as evidence
or printed by Bug Repro. Only validated decisions and sanitized errors are
recorded. Codex itself manages its own diagnostics and login storage.

Completed error-free runs export their actual accepted actions automatically.
Use the same `replay` and comparison `report` commands as for endpoint runs;
replay never invokes Codex. Reports label this backend `codex-cli` and include
the tool-isolation limitation. Mocked subprocess tests are scripted mechanics
checks and do not establish live authentication or model quality.

See official [non-interactive execution](https://learn.chatgpt.com/docs/non-interactive-mode)
and [configuration controls](https://learn.chatgpt.com/docs/config-file/config-reference).

### Browser resource policies

The default `--resource-policy strict` blocks every external request and treats
any blocked resource as an execution error. An external image can therefore
make an otherwise rendered page inconclusive, including on a later action if
the image request arrived asynchronously.

The explicit `block-external-images` policy still aborts every external request
using CDP Fetch before network. Only requests Chromium classifies as `Image`
are recorded as nonfatal blocked resources. The decision is based on resource
type, never a `.gif`/`.png` suffix or an allowlist. External scripts, styles,
frames, top-level navigation, fetches and unknown resource types remain fatal;
interception failures, timeouts, locator failures and browser crashes are not
suppressed. Service workers remain disabled.

```powershell
.\.venv\Scripts\bugrepro.exe investigate `
  --decision-backend codex-cli `
  --target http://127.0.0.1:8110 `
  --report-file request.txt --expectation-file expectation.json `
  --resource-policy block-external-images

.\.venv\Scripts\bugrepro.exe replay runs\RUN_DIRECTORY `
  --base-url http://127.0.0.1:8110 `
  --resource-policy block-external-images
```

The same option is available on `run`. It is fixed at investigation start,
separate from the frozen expectation and action schema; actions cannot change
it. Recipes keep their original action/expectation format. Replay defaults to
`strict`, so pass the original policy explicitly when reproducing the same
restricted environment. The service start API accepts `resource_policy` with
the same two validated values; existing sessions and exploration stay strict
unless a supported start path explicitly selects another policy.

`run.json`, Markdown and HTML reports record `resource_policy`,
`blocked_resources` (sanitized URL, Chromium resource type, main-document flag,
timestamp, blocked disposition, fatal status), and any `request_guard_errors`.
URLs omit credentials, query strings and fragments. Comparison reports flag
differing policies even when recipe and expectation fingerprints match.

Results describe the app **under this restricted environment**, with external
images missing. A passing heading check does not show that the full page works
with network access. Blocked images remain visible in evidence and reports;
they are never silently allowed or treated as proof of an application bug.

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
