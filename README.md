# Bug Repro

A CLI toolkit for investigating web-app bugs, recording browser evidence, and replaying the same checks after a fix.

Bug Repro separates browser execution from agent decisions. An external agent chooses actions through the CLI; the tool validates and executes them, captures observations and screenshots, and checks a fixed expected result. Saved sequences can be replayed without further model calls.

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

## Agent-driven investigations

Start the persistent browser service:

```powershell
.\.venv\Scripts\bugrepro.exe agent serve
```

Give your agent the local target URL, a bug report or requirements, and supported expectations. The CLI exposes commands to start a session, observe the page, execute validated actions, finish, and export a recipe. The browser remains open between commands.

Alan can act as the decision-making agent through its local command tools. Bug Repro itself does not call a model provider. Agent-driven investigations and prewritten recipes use the same executor; recorded replay follows the saved actions without asking an agent to choose them again.

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

Bug Repro records and checks browser behaviour; it does not automatically repair application code. A passing scenario covers only the expectation that was tested.

## Development

```powershell
.\.venv\Scripts\python.exe -m pytest
```

## License

[MIT](LICENSE)
