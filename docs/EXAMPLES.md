# Examples

Two small fixtures ship with the project. Both run entirely on loopback and use
synthetic data.

For a standalone model investigation, start either fixture and use
`bugrepro investigate` with its loopback URL, a report file, a supported
expectation file, and your configured model endpoint. See the complete
[setup example](USAGE.md#standalone-model-investigation). The examples below
also demonstrate scripted replay and the external-agent interface; they do
not require a model provider key.

## Account switching

The account fixture has a deliberate stale-profile bug.

```powershell
# terminal 1
.\.venv\Scripts\bugrepro.exe serve --mode buggy --port 8101

# terminal 2
.\.venv\Scripts\bugrepro.exe serve --mode fixed --port 8102
```

Open <http://127.0.0.1:8101> and <http://127.0.0.1:8102>. Log in as `alice`,
open the profile, log out, log in as `bob`, and open the profile again. The
buggy header says `Signed in as: Bob` while the heading says
`Profile for Alice`. The fixed build shows `Profile for Bob`.

Run the bundled recipe and replay it:

```powershell
.\.venv\Scripts\bugrepro.exe run `
  recipes\sample-alice-bob.json `
  --base-url http://127.0.0.1:8101 `
  --label buggy-account

# use the run directory printed by the command
.\.venv\Scripts\bugrepro.exe replay runs\<run-dir> `
  --base-url http://127.0.0.1:8102 `
  --label fixed-account
```

The first command exits 1 with a failed heading check; the replay exits 0. Both
runs use the same recipe and expectation fingerprints.

## Task list exploration

The task fixture is a small list with a labelled **Task title** textbox, an
**Add task** button, and a region named **Tasks**. In buggy mode a task
disappears after a reload; in fixed mode it persists.

Start both builds and the agent service:

```powershell
.\scripts\demo.ps1 start
```

The script prints the buggy URL (`127.0.0.1:8103`), the fixed URL
(`127.0.0.1:8104`), and the service URL. It uses an isolated state file:

```powershell
$state = Join-Path $env:LOCALAPPDATA "BugRepro\demo\service.json"
```

The example requirements and scenario files are:

- `examples\task-exploration.json`
- `examples\task-scenario-r1.json`
- `examples\task-scenario-r2.json`

This manual walkthrough uses `scripted-test` because the action sequence is
supplied below. For an actual agent-driven investigation, use `alan-agent`
and let the agent choose actions from observations.

Start an exploration against the buggy build:

```powershell
$explore = .\.venv\Scripts\bugrepro.exe explore start `
  --spec-file examples\task-exploration.json `
  --driver scripted-test `
  --model "<your-configured-model>" `
  --label task-buggy `
  --state-file $state | ConvertFrom-Json
```

Start the first scenario. Its expectation is frozen before any browser action:

```powershell
$scenario = .\.venv\Scripts\bugrepro.exe explore scenario start `
  --exploration $explore.exploration_id `
  --scenario-file examples\task-scenario-r1.json `
  --state-file $state | ConvertFrom-Json
```

An external agent would now observe and submit one action at a time. For a
manual walkthrough, the equivalent action files are:

```powershell
'{"type":"goto","path":"/"}' | Set-Content goto.json
'{"type":"fill","locator":{"role":"textbox","name":"Task title"},"text":"Prepare demo task"}' | Set-Content fill.json
'{"type":"click","locator":{"role":"button","name":"Add task"}}' | Set-Content add.json
'{"type":"reload"}' | Set-Content reload.json
```

```powershell
.\.venv\Scripts\bugrepro.exe agent observe --session $scenario.session_id --state-file $state
.\.venv\Scripts\bugrepro.exe agent act --session $scenario.session_id --action-file goto.json --state-file $state
.\.venv\Scripts\bugrepro.exe agent act --session $scenario.session_id --action-file fill.json --state-file $state
.\.venv\Scripts\bugrepro.exe agent act --session $scenario.session_id --action-file add.json --state-file $state
.\.venv\Scripts\bugrepro.exe agent act --session $scenario.session_id --action-file reload.json --state-file $state
```

Finish, export, and replay-confirm the scenario:

```powershell
$finished = .\.venv\Scripts\bugrepro.exe explore scenario finish `
  --exploration $explore.exploration_id --scenario $scenario.scenario_id `
  --state-file $state | ConvertFrom-Json

$exported = .\.venv\Scripts\bugrepro.exe explore scenario export `
  --exploration $explore.exploration_id --scenario $scenario.scenario_id `
  --state-file $state | ConvertFrom-Json

$confirmed = .\.venv\Scripts\bugrepro.exe explore scenario confirm `
  --exploration $explore.exploration_id --scenario $scenario.scenario_id `
  --state-file $state | ConvertFrom-Json
```

`$finished.classification` is `possible_issue`; after confirmation,
`$confirmed.classification` is `violation` when the same observation recurs. Replay the exported recipe on the
fixed build:

```powershell
.\.venv\Scripts\bugrepro.exe replay $exported.recipe_path_absolute `
  --base-url http://127.0.0.1:8104 `
  --label task-fixed
```

The fixed replay should pass with `Prepare demo task` present. It uses the
same recipe bytes and expectation fingerprint, with no model decisions.

Finish the exploration, generate reports, and stop the demo:

```powershell
$done = .\.venv\Scripts\bugrepro.exe explore finish `
  --exploration $explore.exploration_id --state-file $state | ConvertFrom-Json

.\.venv\Scripts\bugrepro.exe report `
  --exploration $done.run_dir `
  --output runs\task-exploration.html

.\.venv\Scripts\bugrepro.exe report `
  --attempt (Join-Path $explore.run_dir $scenario.run_dir) `
  --buggy (Join-Path $explore.run_dir $confirmed.replay_confirmation.run_dir) `
  --fixed (Get-ChildItem runs -Directory -Filter "*task-fixed*" | Sort-Object LastWriteTime | Select-Object -Last 1).FullName `
  --output runs\task-comparison.html

.\scripts\demo.ps1 stop
```

The exploration report shows requirements, scenarios, classifications and
replay confirmation. The comparison report shows the original scripted attempt,
the buggy confirmation and the fixed replay side by side.

## Recorded evidence

[`../showcase/index.html`](../showcase/index.html) links to selected
self-contained reports from the bundled samples. They are recorded evidence,
not a live service.
