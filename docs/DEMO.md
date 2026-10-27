# One recommended demonstration

This is the shortest path that shows the real workflow: Alan observes a local
app, adds a task, reloads, records the violation, exports the sequence, and the
exact recipe then passes on the fixed build.

Everything is local. The app data is synthetic; no credentials are involved.

## 0. Start everything

From the repository root:

```powershell
.\scripts\demo.ps1 start
```

The script starts:

| What | URL / path |
| --- | --- |
| Buggy task app | <http://127.0.0.1:8103> |
| Fixed task app | <http://127.0.0.1:8104> |
| Agent service | printed by the script (random loopback port) |
| Runs and evidence | `runs\` |

If port 8103 or 8104 is occupied, the script stops with a clear message and
does **not** kill the other process. Stop everything later with:

```powershell
.\scripts\demo.ps1 stop
```

The demo service uses an isolated state file, so the exploration commands need
it explicitly:

```powershell
$state = Join-Path $env:LOCALAPPDATA "BugRepro\demo\service.json"
```

## 1. Show the requirement input

Open `examples\task-exploration.json`. It contains two user requirements:

- R1: an added task remains visible after reloading.
- R2: adding a second distinct task preserves the first task.

The allowed test data are `Prepare hackathon demo` and `Review submission`.
No bug report and no saved recipe are supplied.

For this path we will test R1 with `examples\task-scenario-r1.json` only.

## 2. Let Alan propose and run the scenario

Start the exploration against the buggy app:

```powershell
$explore = .\.venv\Scripts\bugrepro.exe explore start `
  --spec-file examples\task-exploration.json `
  --driver alan-agent `
  --model deepseek/deepseek-v4.1-flash `
  --model-source session-reported `
  --label demo-buggy `
  --state-file $state | ConvertFrom-Json

$explore.exploration_id
```

Freeze the scenario expectation before any action:

```powershell
$scenario = .\.venv\Scripts\bugrepro.exe explore scenario start `
  --exploration $explore.exploration_id `
  --scenario-file examples\task-scenario-r1.json `
  --state-file $state | ConvertFrom-Json

$scenario.session_id
```

Alan now chooses one action at a time from the latest observation. Create the
action files once:

```powershell
'{"type":"goto","path":"/"}' | Set-Content demo-01-goto.json
'{"type":"fill","locator":{"role":"textbox","name":"Task title"},"text":"Prepare hackathon demo"}' | Set-Content demo-02-fill.json
'{"type":"click","locator":{"role":"button","name":"Add task"}}' | Set-Content demo-03-add.json
'{"type":"reload"}' | Set-Content demo-04-reload.json
```

Observe, then execute each action:

```powershell
.\.venv\Scripts\bugrepro.exe agent observe --session $scenario.session_id --state-file $state
.\.venv\Scripts\bugrepro.exe agent act --session $scenario.session_id --action-file demo-01-goto.json --state-file $state
.\.venv\Scripts\bugrepro.exe agent act --session $scenario.session_id --action-file demo-02-fill.json --state-file $state
.\.venv\Scripts\bugrepro.exe agent act --session $scenario.session_id --action-file demo-03-add.json --state-file $state
.\.venv\Scripts\bugrepro.exe agent act --session $scenario.session_id --action-file demo-04-reload.json --state-file $state
```

After the add, the `Tasks` region contains `Prepare hackathon demo`. After the
reload it contains `No tasks yet.` That is the reported problem.

## 3. Finish, export and confirm

```powershell
$finished = .\.venv\Scripts\bugrepro.exe explore scenario finish `
  --exploration $explore.exploration_id --scenario $scenario.scenario_id `
  --state-file $state | ConvertFrom-Json

$finished.classification        # possible_issue: failed once, not yet confirmed
$finished.check.status          # failed
```

Export the exact executed sequence and replay it in a fresh context on the
same buggy target:

```powershell
$export = .\.venv\Scripts\bugrepro.exe explore scenario export `
  --exploration $explore.exploration_id --scenario $scenario.scenario_id `
  --state-file $state | ConvertFrom-Json

$confirm = .\.venv\Scripts\bugrepro.exe explore scenario confirm `
  --exploration $explore.exploration_id --scenario $scenario.scenario_id `
  --state-file $state | ConvertFrom-Json

$confirm.classification                 # violation
$confirm.replay_confirmation.status     # confirmed
```

## 4. Replay the unchanged recipe on the fixed app

```powershell
.\.venv\Scripts\bugrepro.exe replay $export.recipe_path_absolute `
  --base-url http://127.0.0.1:8104 `
  --label demo-fixed-replay
```

Expected: `Check: PASSED`, exit code `0`, observed
`Prepare hackathon demo`. The fixed replay uses the same recipe bytes and the
same expectation fingerprint; it makes no model decisions.

## 5. Generate the three-way report

The exported recipe is the same for the original attempt, the buggy
confirmation and the fixed replay. Build one report from those three selected
runs:

```powershell
$attemptDir = Join-Path $explore.run_dir $scenario.run_dir
$confirmDir = Join-Path $explore.run_dir $confirm.replay_confirmation.run_dir
$fixedDir = (Get-ChildItem runs -Directory -Filter "*demo-fixed-replay*" |
  Sort-Object LastWriteTime | Select-Object -Last 1).FullName

.\.venv\Scripts\bugrepro.exe report `
  --attempt $attemptDir `
  --buggy $confirmDir `
  --fixed $fixedDir `
  --output runs\demo-comparison.html
```

Open the report:

```powershell
Start-Process (Resolve-Path runs\demo-comparison.html)
```

It should say **Unchanged replay verified** and show:

- original model-driven attempt: `alan-agent`, failed;
- buggy confirmation: `recorded-replay`, failed;
- fixed replay: `recorded-replay`, passed.

The same recipe and expectation fingerprints appear in all three columns.

## 6. Finish the exploration and stop

```powershell
$done = .\.venv\Scripts\bugrepro.exe explore finish `
  --exploration $explore.exploration_id --state-file $state | ConvertFrom-Json
$done.classification_counts

.\.venv\Scripts\bugrepro.exe report `
  --exploration $done.run_dir `
  --output runs\demo-exploration.html

Start-Process (Resolve-Path runs\demo-exploration.html)
.\scripts\demo.ps1 stop
```

The exploration report shows the requirement, scenario, classification,
replay-confirmation status and every action/screenshot. The comparison report
shows the before/after result.

## If you only want to review recorded evidence

Open `showcase\index.html`. It links to selected self-contained reports from the
recorded demonstration runs. It is static evidence; it does not start the agent or
contact the service.

## Honest framing

- This is a **known-app, requirement-guided demonstration**. The fixture and
  its fixed mode were authored for the project.
- The fixed mode is a manual fixture change, not an automatic fix.
- The report is an observation about the page, not proof of the root cause.
- Replays are labelled `recorded-replay` and make no model calls. Only the
  original attempt is labelled `alan-agent`.
- Model identity is session-reported, not provider-verified.