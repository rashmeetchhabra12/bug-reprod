# Bug Repro - recorded showcase

This folder contains selected, self-contained evidence reports from the bundled
task-list sample. Start with [`index.html`](index.html).

## Reports

- `evidence/task-compare-r1.html` - R1 comparison: the original `alan-agent`
  attempt on the buggy build, the buggy replay confirmation, and the
  identical-recipe replay on the fixed build.
- `evidence/task-compare-r2.html` - the same comparison for R2.
- `evidence/task-buggy-exploration.html` - requirements, scenarios,
  classifications, replay confirmation and full action evidence.
- `evidence/task-fixed-exploration.html` - the same requirements tested against
  the fixed build.

The reports are static HTML with inline CSS and embedded screenshots. They
contain no scripts, make no network requests, and do not contact the browser
service. Open them directly from disk.

## What the evidence shows

On the buggy task fixture, both requirements fail and are replay-confirmed. On
the fixed fixture, the same exported recipes and frozen expectations pass
unchanged. The fixed mode is a manual fixture build, not an automatic fix.

| Requirement | Buggy | Fixed |
| --- | --- | --- |
| R1: added task remains after reload | violation, replay confirmed | no violation |
| R2: second task preserves the first | violation, replay confirmed | no violation |

Recipe and expectation fingerprints match across the attempt, the buggy
confirmation and the fixed replay.

## Run the tool locally

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m playwright install chromium
.\scripts\demo.ps1 start
# see docs\USAGE.md and docs\EXAMPLES.md
.\scripts\demo.ps1 stop
```

## Scope

- Only selected synthetic runs are copied here.
- Private `runs\` directories and the service state file are excluded from Git.
- No credentials, tokens or personal filesystem paths are included.
- Reports record observed behavior, not a root cause.
- These are sample-app runs, not a hosted testing service.
