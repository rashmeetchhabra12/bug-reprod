# Bug Repro — recorded showcase

This folder is a small, self-contained demonstration package. Start with
[`index.html`](index.html).

It contains only selected evidence from the synthetic task-list fixture:

- `evidence/phase4-compare-r1.html` — R1 three-way comparison: original
  `alan-agent` attempt on the buggy app, replay confirmation on buggy, and the
  identical-recipe replay on fixed.
- `evidence/phase4-compare-r2.html` — the same three-way comparison for R2.
- `evidence/phase4-task-buggy-exploration.html` — requirements, scenarios,
  classifications, replay confirmation and full evidence.
- `evidence/phase4-task-fixed-exploration.html` — the same requirements on the
  fixed build.

The reports are static HTML with inline CSS and embedded screenshots. They
contain no scripts, make no network requests, and do not contact the browser
service. Open them directly from disk; no server is required.

## What the evidence shows

On the buggy task app, both requirements fail and are replay-confirmed. On the
fixed app, the same exported recipes and frozen expectations pass unchanged.
The fixed mode is a manual fixture change, not an automatic fix.

| Requirement | Buggy | Fixed |
| --- | --- | --- |
| R1: added task remains after reload | violation, replay confirmed | no violation |
| R2: second task preserves the first | violation, replay confirmed | no violation |

Recipe and expectation fingerprints match across the attempt, the buggy
confirmation and the fixed replay. See the comparison reports.

## Run the actual tool

The showcase is recorded output. To run the tool:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m playwright install chromium
.\scripts\demo.ps1 start
# follow docs\DEMO.md
.\scripts\demo.ps1 stop
```

## Privacy and scope

- Only the selected synthetic runs are copied here.
- Private `runs\` directories and the service state file are excluded from Git.
- No credentials, tokens or personal filesystem paths are included.
- These are known-app, requirement-guided demonstrations, not blind bug
  discovery or a hosted testing service.
- A failed expectation is an observation about the sequence, not proof of a
  root cause.
