# Bug Repro

A CLI toolkit for investigating web-app bugs, recording browser evidence, and replaying the same checks after a fix.

Bug Repro separates browser execution from agent decisions. A configured model or external agent chooses actions; the tool validates and executes them, captures observations and screenshots, and checks a fixed expected result. Saved sequences can be replayed without further model calls.

## Features

- **Explore:** test user-defined requirements through bounded scenarios and confirm candidate violations by replaying them.
- **Reproduce:** investigate a reported issue against an expected result supplied before the attempt.
- **Replay:** run the recorded actions against another local build while preserving the original expectation.
- **Inspect:** export HTML reports with screenshots, action details, outcomes, and recipe fingerprints.

Each investigation uses an isolated browser context. Action failures and browser errors are reported separately from failed expectations.

## Installation

Requires Python 3.11+ and Chromium installed through Playwright. The commands below use Windows PowerShell.

```powershell
git clone https://github.com/rashmeetchhabra12/bug-reprod.git
cd bug-reprod
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m playwright install chromium
```

## Quickstart

The repository includes an account-switching sample app with buggy and fixed versions. This quickstart runs a supplied recipe to demonstrate recording and replay.

Start the buggy version in one terminal:

```powershell
.\.venv\Scripts\bugrepro.exe serve --mode buggy --port 8101
```

Start the fixed version in another terminal:

```powershell
.\.venv\Scripts\bugrepro.exe serve --mode fixed --port 8102
```

From a third terminal, run the supplied check:

```powershell
.\.venv\Scripts\bugrepro.exe run recipes\sample-alice-bob.json --base-url http://127.0.0.1:8101
```

The check fails because the previous account's profile remains visible after switching users. The command prints a new run directory containing the recipe, structured results, report, and screenshots.

Replay that run against the fixed version, replacing `RUN_DIRECTORY` with the directory printed above:

```powershell
.\.venv\Scripts\bugrepro.exe replay runs\RUN_DIRECTORY --base-url http://127.0.0.1:8102
```

The expectation should now pass. Add `--headed` to watch the browser.

## Guided investigation

Start without preparing input files:

```powershell
.\.venv\Scripts\bugrepro.exe guide
```

Enter the local app URL, a bug report or task (end with a line containing only
`.`), a heading or region-item expectation, and your decision backend. Review
the complete expectation and confirm before the investigation starts. Codex
uses its existing CLI login; endpoint keys stay in environment variables.
The expectation is frozen before browser actions and cannot be changed by the
model. The command ends with an outcome, report path and a PowerShell replay
command when export is allowed. Use `--json` for the final machine-readable
result; prompts, progress and the summary then go to stderr.

Existing input files and scripts can continue using `investigate` below.
See [guided setup and limits](docs/USAGE.md#guided-investigation).

## Standalone model investigation

Supply your model and full OpenAI-compatible Chat Completions endpoint.
Non-loopback providers require an API key in `OPENAI_API_KEY` in your shell;
validated loopback endpoints can run without a key. A configured key is still
sent to loopback providers that require authentication.

```powershell
.\.venv\Scripts\bugrepro.exe investigate `
  --target http://127.0.0.1:8101 `
  --report-file examples\live-bug-report.txt `
  --expectation-file examples\live-expectation.json `
  --model-endpoint https://api.openai.com/v1/chat/completions `
  --model "<your-configured-model>"
```

This starts and closes its own browser service worker. The model chooses one
validated action per observation, then requests a check of the original frozen
expectation. Defaults: 20 actions, 25 model calls, 60-second model I/O timeout.
Results include Markdown/portable HTML evidence and a recipe whenever the
attempt is complete and error-free. Malformed responses, provider errors and
budget exhaustion are investigation errors (exit 2).

Investigation progress is printed to stderr; stdout remains the final JSON
result. Progress shows decision-call counts, action types and outcomes, and
the final check and report path, without prompts or page content.

The bug report, expectation, page observations and action history are sent to
your configured provider. Use synthetic local test data. Keys are read only
from the selected environment variable and never recorded. See
[setup and limitations](docs/USAGE.md#standalone-model-investigation).

You can also use an installed Codex CLI with its own login:

Windows npm installations are supported by resolving their packaged native
`codex.exe`; Bug Repro never runs the npm shell wrapper.

```powershell
.\.venv\Scripts\bugrepro.exe investigate `
  --decision-backend codex-cli `
  --target http://127.0.0.1:8103 `
  --report-file examples\task-bug-report.txt `
  --expectation-file examples\task-expectation.json
```

The adapter requests disabled tools and read-only execution, rejects reported
tool events, and uses a fresh temporary working directory. These controls are
not a universal no-tools isolation guarantee across CLI versions or managed
policies. See [Codex setup and limitations](docs/USAGE.md#codex-cli-decisions).

## External agent investigations

Start the persistent browser service:

```powershell
.\.venv\Scripts\bugrepro.exe agent serve
```

Give your agent the local target URL, a bug report or requirements, and supported expectations. The CLI exposes commands to start a session, observe the page, execute validated actions, finish, and export a recipe. The browser remains open between commands.

External agents can use their local command tools. This interface needs no provider key in Bug Repro. All investigation paths and prewritten recipes use the same executor; recorded replay follows the saved actions without asking an agent to choose them again.

See `bugrepro agent --help` and `bugrepro explore --help` for the available commands.

## Reports and results

Create a portable HTML report from a saved run:

```powershell
.\.venv\Scripts\bugrepro.exe report --attempt runs\RUN_DIRECTORY --output runs\report.html
```

Open the generated file in a browser. Reports can also compare an original attempt with buggy and fixed replays.

Exit codes: `0` means the expectation passed, `1` means it failed, and `2` means an execution error or inconclusive attempt.

## Supported scope

Currently supports local loopback URLs and Chromium. Actions include navigation, click, fill, reload, observation, and screenshots. Expectations check heading text or an exact list item within an accessible region. Service workers are disabled. Exploration is limited to three scenarios and sixty actions, including confirmations.

All external browser requests are blocked. The default resource policy is
`strict`, which makes every blocked resource an execution error. Explicitly
select `--resource-policy block-external-images` on `investigate`, `run`, or
`replay` to keep external images blocked and recorded without making only
those image blocks fatal. Scripts, frames, navigation, and unrelated errors
remain fatal. Reports show the policy and blocked resources; results describe
the app in that restricted environment. Use the same explicit policy for a replay.

Bug Repro records and checks browser behaviour; it does not automatically repair application code. A passing scenario covers only the expectation that was tested.

## Development

```powershell
.\.venv\Scripts\python.exe -m pytest
```

## License

[MIT](LICENSE)
