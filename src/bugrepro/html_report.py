"""Self-contained HTML evidence report.

Renders existing run records (``run.json``) into one portable HTML file with
embedded screenshots. There is no framework, no network access and no script:
all dynamic text is escaped and images are inlined as ``data:`` URIs.

The report is read-only. It never talks to the browser service and only reads
run directories that the caller explicitly selects.
"""

from __future__ import annotations

import base64
import html
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

MAX_IMAGE_BYTES = 8 * 1024 * 1024
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg"})
ROLE_LABELS = {
    "attempt": "Original model-driven attempt",
    "buggy": "Replay against buggy version",
    "fixed": "Replay against fixed version",
}
STATUS_LABELS = {
    "passed": "PASSED",
    "failed": "FAILED",
    "error": "ERROR",
}
STATUS_MEANINGS = {
    "passed": "The expected result was observed.",
    "failed": "The expected result was not observed after this sequence.",
    "error": "The attempt was inconclusive; this is not a reproduction.",
}
DRIVER_MEANINGS = {
    "alan-agent": "Live model decisions (session-reported identity).",
    "scripted-test": "Pre-written actions. No model decided anything.",
    "recorded-replay": "Deterministic replay of a saved recipe. No model calls.",
}


class ReportError(ValueError):
    """Raised when a selected run cannot be rendered."""


@dataclass
class ReportRun:
    role: str
    run_dir: Path
    data: dict[str, Any]

    @property
    def label(self) -> str:
        return ROLE_LABELS.get(self.role, self.role)

    @property
    def run_id(self) -> str:
        return str(self.data.get("run_id") or self.run_dir.name)

    @property
    def check(self) -> dict[str, Any]:
        check = self.data.get("check")
        return check if isinstance(check, dict) else {}

    @property
    def status(self) -> str:
        return str(self.check.get("status") or "error")

    @property
    def driver(self) -> str:
        return str(self.data.get("driver") or "unknown")

    @property
    def expectation_fingerprint(self) -> str | None:
        expectation = self.data.get("expectation") or {}
        value = expectation.get("fingerprint")
        return value if isinstance(value, str) else None

    @property
    def recipe_fingerprint(self) -> str | None:
        recipe = self.data.get("recipe") or {}
        value = recipe.get("sha256")
        return value if isinstance(value, str) else None

    @property
    def actions(self) -> list[dict[str, Any]]:
        actions = self.data.get("actions")
        return actions if isinstance(actions, list) else []


def load_report_run(run_dir: str | Path, role: str) -> ReportRun:
    """Load one explicitly selected run directory."""
    if role not in ROLE_LABELS:
        raise ReportError(f"Unknown report role: {role!r}.")
    path = Path(run_dir)
    if not path.is_dir():
        raise ReportError(f"Run directory not found: {path}")
    run_json = path / "run.json"
    try:
        data = json.loads(run_json.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise ReportError(f"run.json not found in {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ReportError(f"Could not read {run_json}: {exc}") from exc
    if not isinstance(data, dict):
        raise ReportError(f"run.json in {path} is not a JSON object.")
    if not isinstance(data.get("check"), dict):
        raise ReportError(f"run.json in {path} has no check result.")
    return ReportRun(role=role, run_dir=path, data=data)


def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _safe_image_path(run_dir: Path, relative: Any) -> Path | None:
    """Resolve a screenshot reference without escaping the run directory."""
    if not isinstance(relative, str) or not relative.strip():
        return None
    candidate = (run_dir / relative).resolve()
    root = run_dir.resolve()
    if candidate != root and root not in candidate.parents:
        return None
    if candidate.suffix.lower() not in IMAGE_SUFFIXES:
        return None
    try:
        if not candidate.is_file() or candidate.stat().st_size > MAX_IMAGE_BYTES:
            return None
    except OSError:
        return None
    return candidate


def _image_data_uri(path: Path) -> str | None:
    try:
        payload = path.read_bytes()
    except OSError:
        return None
    mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    encoded = base64.b64encode(payload).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _final_screenshot(run: ReportRun) -> str | None:
    candidates: list[str] = []
    for action in reversed(run.actions):
        shot = action.get("screenshot")
        if isinstance(shot, str):
            candidates.append(shot)
    candidates.append("evidence/final.png")
    for relative in candidates:
        if _safe_image_path(run.run_dir, relative):
            return relative
    return None


def _image_block(run: ReportRun, relative: str | None, caption: str) -> str:
    if relative is None:
        return (
            '<figure class="shot"><div class="shot-empty">No screenshot recorded</div>'
            f"<figcaption>{_esc(caption)}</figcaption></figure>"
        )
    path = _safe_image_path(run.run_dir, relative)
    uri = _image_data_uri(path) if path else None
    if uri is None:
        return (
            '<figure class="shot"><div class="shot-empty">Screenshot unavailable</div>'
            f"<figcaption>{_esc(caption)}</figcaption></figure>"
        )
    return (
        '<figure class="shot">'
        f'<img src="{uri}" alt="{_esc(caption)}">'
        f"<figcaption>{_esc(caption)}</figcaption></figure>"
    )


def _observed_text(run: ReportRun) -> str:
    observed = run.check.get("observed") or []
    if not isinstance(observed, list) or not observed:
        return "(none)"
    return ", ".join(str(item) for item in observed)


def _comparison(runs: dict[str, ReportRun]) -> tuple[bool, list[str]]:
    """Return (is_unchanged_replay, reasons)."""
    reasons: list[str] = []
    fingerprints: dict[str, str | None] = {
        role: run.recipe_fingerprint for role, run in runs.items()
    }
    expectations: dict[str, str | None] = {
        role: run.expectation_fingerprint for role, run in runs.items()
    }

    known_recipes = {value for value in fingerprints.values() if value}
    if len(known_recipes) > 1:
        reasons.append("Recipe fingerprints differ between the selected runs.")
    if len(fingerprints) > 1 and any(value is None for value in fingerprints.values()):
        reasons.append("At least one selected run has no exported recipe fingerprint.")

    known_expectations = {value for value in expectations.values() if value}
    if len(known_expectations) > 1:
        reasons.append("Expectation fingerprints differ between the selected runs.")

    for role in ("buggy", "fixed"):
        run = runs.get(role)
        if run is not None and run.driver != "recorded-replay":
            reasons.append(
                f'The "{ROLE_LABELS[role]}" run is not a deterministic replay '
                f"(driver: {run.driver})."
            )

    attempt = runs.get("attempt")
    if attempt is not None and attempt.driver != "alan-agent":
        reasons.append(
            "The original attempt is not labelled as a live agent run "
            f"(driver: {attempt.driver})."
        )

    return (not reasons), reasons


def _status_badge(status: str) -> str:
    label = STATUS_LABELS.get(status, status.upper())
    return f'<span class="badge badge-{_esc(status)}">{_esc(label)}</span>'


def _attempt_status(run: ReportRun) -> str:
    recorded = run.data.get("attempt_status")
    if recorded:
        return str(recorded)
    if run.driver == "recorded-replay":
        return "n/a (replay)"
    return "unknown"


def _summary_card(run: ReportRun) -> str:
    model = run.data.get("model") or {}
    model_id = model.get("id") or "unknown"
    model_source = model.get("source") or "unknown"
    budget = run.data.get("budget") or {}
    used = budget.get("used", 0)
    limit = budget.get("limit", 0)
    remaining = budget.get("remaining", 0)
    attempt_status = _attempt_status(run)
    errors = run.data.get("execution_errors") or []
    failures = run.data.get("action_failures") or []
    return f"""
      <div class="card summary">
        <div class="summary-head">
          {_status_badge(run.status)}
          <div>
            <h2>{_esc(STATUS_MEANINGS.get(run.status, "Unknown outcome."))}</h2>
            <p class="muted">{_esc(run.label)}</p>
          </div>
        </div>
        <dl class="facts">
          <div><dt>Driver</dt><dd>{_esc(run.driver)}<span class="muted"> — {_esc(DRIVER_MEANINGS.get(run.driver, "Unrecognised driver."))}</span></dd></div>
          <div><dt>Model</dt><dd>{_esc(model_id)} <span class="muted">(source: {_esc(model_source)})</span></dd></div>
          <div><dt>Attempt status</dt><dd>{_esc(attempt_status)}</dd></div>
          <div><dt>Action budget</dt><dd>{_esc(used)} used / {_esc(limit)} allowed ({_esc(remaining)} remaining)</dd></div>
          <div><dt>Action failures</dt><dd>{_esc(len(failures))}</dd></div>
          <div><dt>Execution errors</dt><dd>{_esc(len(errors))}</dd></div>
        </dl>
        <div class="compare-line">
          <span class="muted">Expected:</span> <strong>{_esc(run.check.get("expected", ""))}</strong>
          <span class="muted">Observed:</span> <strong>{_esc(_observed_text(run))}</strong>
        </div>
      </div>
    """


def _comparison_table(
    runs: dict[str, ReportRun],
    unchanged: bool,
    reasons: list[str],
) -> str:
    order = [role for role in ("attempt", "buggy", "fixed") if role in runs]
    header = "".join(f"<th>{_esc(runs[role].label)}</th>" for role in order)

    def row(label: str, getter: Any) -> str:
        cells = "".join(f"<td>{getter(runs[role])}</td>" for role in order)
        return f"<tr><th scope=\"row\">{_esc(label)}</th>{cells}</tr>"

    recipe_values = {runs[role].recipe_fingerprint for role in order}
    expectation_values = {runs[role].expectation_fingerprint for role in order}

    def fingerprint_cell(value: str | None, values: set[str | None]) -> str:
        if not value:
            return '<span class="warn">missing</span>'
        marker = "" if len({v for v in values if v}) <= 1 else ' <span class="warn">differs</span>'
        return f"<code class=\"fp\">{_esc(value)}</code>{marker}"

    rows = [
        row("Outcome", lambda run: _status_badge(run.status)),
        row("Driver", lambda run: _esc(run.driver)),
        row("Attempt status", lambda run: _esc(_attempt_status(run))),
        row("Expected", lambda run: _esc(run.check.get("expected", ""))),
        row("Observed", lambda run: _esc(_observed_text(run))),
        row(
            "Recipe fingerprint",
            lambda run: fingerprint_cell(run.recipe_fingerprint, recipe_values),
        ),
        row(
            "Expectation fingerprint",
            lambda run: fingerprint_cell(
                run.expectation_fingerprint, expectation_values
            ),
        ),
        row("Run", lambda run: f'<code class="fp">{_esc(run.run_id)}</code>'),
    ]
    banner = (
        '<p class="banner banner-ok">Unchanged replay verified: all selected runs '
        "used the same recipe and the same frozen expectation, and the replay runs "
        "are deterministic.</p>"
        if unchanged
        else '<p class="banner banner-warn">Warning: this comparison does not '
        "represent an unchanged replay. See the reasons below before treating the "
        "difference as a product result.</p>"
    )
    reasons_html = ""
    if reasons:
        reasons_html = (
            '<ul class="reasons">'
            + "".join(f"<li>{_esc(reason)}</li>" for reason in reasons)
            + "</ul>"
        )
    return f"""
      <div class="card">
        <h2>Comparison</h2>
        {banner}
        {reasons_html}
        <div class="table-wrap">
          <table class="compare">
            <thead><tr><th scope="col">Field</th>{header}</tr></thead>
            <tbody>{''.join(rows)}</tbody>
          </table>
        </div>
      </div>
    """


def _final_states(runs: dict[str, ReportRun]) -> str:
    order = [role for role in ("attempt", "buggy", "fixed") if role in runs]
    cards = []
    for role in order:
        run = runs[role]
        shot = _final_screenshot(run)
        caption = (
            f"{run.label} — {STATUS_LABELS.get(run.status, run.status)} — "
            f"observed: {_observed_text(run)}"
        )
        cards.append(_image_block(run, shot, caption))
    return f"""
      <section class="card">
        <h2>Final observed states</h2>
        <p class="muted">The last recorded frame of each selected run, side by side.</p>
        <div class="shots">{''.join(cards)}</div>
      </section>
    """


def _action_details(run: ReportRun) -> str:
    if not run.actions:
        return '<p class="muted">No actions were executed.</p>'

    blocks = []
    for action in run.actions:
        index = action.get("index", "?")
        action_type = action.get("type", "unknown")
        status = action.get("status", "unknown")
        duration = action.get("duration_ms", 0)
        observation = action.get("observation") or {}
        rationale = action.get("rationale")
        error = action.get("error")
        shot = action.get("screenshot")

        rationale_html = (
            f'<p class="rationale"><span class="label">Rationale</span> {_esc(rationale)}</p>'
            if rationale
            else '<p class="muted small">No rationale recorded for this action.</p>'
        )
        error_html = (
            f'<p class="error"><span class="label">Error</span> {_esc(error)}</p>'
            if error
            else ""
        )
        headings = observation.get("headings") or []
        headings_text = ", ".join(str(item) for item in headings) if headings else "(none)"
        url = observation.get("url") or "(none)"
        aria = observation.get("aria_excerpt") or ""
        aria_block = (
            f'<details class="aria"><summary>Accessibility snapshot</summary>'
            f"<pre>{_esc(aria)}</pre></details>"
            if aria
            else ""
        )
        input_json = json.dumps(action.get("input") or {}, indent=2, ensure_ascii=False)
        shot_html = (
            _image_block(run, shot, f"Action {index} — {action_type}")
            if shot
            else ""
        )

        blocks.append(
            f"""
        <details class="action">
          <summary>
            <span class="idx">#{_esc(index)}</span>
            <span class="atype">{_esc(action_type)}</span>
            <span class="astatus astatus-{_esc(status)}">{_esc(status)}</span>
            <span class="muted small">{_esc(duration)} ms</span>
          </summary>
          <div class="action-body">
            {rationale_html}
            <div class="action-grid">
              <div>
                <h4>Validated input</h4>
                <pre>{_esc(input_json)}</pre>
              </div>
              <div>
                <h4>Page after action</h4>
                <p class="mono small">{_esc(url)}</p>
                <p class="small">Headings: {_esc(headings_text)}</p>
                {error_html}
                {aria_block}
              </div>
            </div>
            {shot_html}
          </div>
        </details>
            """
        )
    return "".join(blocks)


def _errors_section(run: ReportRun) -> str:
    failures = run.data.get("action_failures") or []
    errors = run.data.get("execution_errors") or []
    parts: list[str] = []
    if failures:
        items = "".join(
            f"<li>Action {_esc(item.get('index'))} ({_esc(item.get('type'))}): "
            f"{_esc(item.get('error'))}</li>"
            for item in failures
            if isinstance(item, dict)
        )
        parts.append(f"<h3>Action failures</h3><ul>{items}</ul>")
    if errors:
        items = "".join(
            f"<li>{_esc(item.get('stage', 'run'))}: {_esc(item.get('error'))}</li>"
            for item in errors
            if isinstance(item, dict)
        )
        parts.append(f"<h3>Execution errors</h3><ul>{items}</ul>")
    if not parts:
        parts.append('<p class="muted">No action failures or execution errors.</p>')
    return "".join(parts)


def _fingerprint_block(run: ReportRun) -> str:
    expectation = run.data.get("expectation") or {}
    recipe = run.data.get("recipe") or {}
    frozen_at = expectation.get("frozen_at") or "(not recorded)"
    return f"""
        <dl class="facts">
          <div><dt>Expectation kind</dt><dd>{_esc(expectation.get("kind", ""))}</dd></div>
          <div><dt>Expected text</dt><dd>{_esc(expectation.get("text", ""))}</dd></div>
          <div><dt>Region</dt><dd>{_esc(expectation.get("region") or "(not used)")}</dd></div>
          <div><dt>Frozen at</dt><dd>{_esc(frozen_at)}</dd></div>
          <div><dt>Expectation fingerprint</dt><dd><code class="fp">{_esc(run.expectation_fingerprint or "missing")}</code></dd></div>
          <div><dt>Recipe fingerprint</dt><dd><code class="fp">{_esc(run.recipe_fingerprint or "not exported")}</code></dd></div>
        </dl>
    """


CSS = """
:root {
  --ink:#172026; --muted:#5b6b76; --line:#d9e1e6; --bg:#f4f6f8; --card:#ffffff;
  --ok:#067647; --ok-bg:#ecfdf3; --bad:#b42318; --bad-bg:#fef3f2;
  --warn:#b54708; --warn-bg:#fffaeb; --accent:#1d4ed8;
}
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--ink);
       font:15px/1.55 "Segoe UI", system-ui, sans-serif; }
.wrap { max-width:1080px; margin:0 auto; padding:28px 22px 56px; }
header.page { margin-bottom:22px; }
header.page h1 { margin:0 0 6px; font-size:1.65rem; letter-spacing:-0.01em; }
.muted { color:var(--muted); }
.small { font-size:.86rem; }
.mono, code.fp { font-family:Consolas, "Cascadia Mono", monospace; }
code { background:#eef2f4; padding:1px 5px; border-radius:4px; font-size:.88em; }
code.fp { font-size:.78rem; word-break:break-all; }
.card { background:var(--card); border:1px solid var(--line); border-radius:12px;
        padding:22px 24px; margin:0 0 18px; }
.card h2 { margin:0 0 12px; font-size:1.15rem; }
.card h3 { margin:18px 0 8px; font-size:1rem; }
.card h4 { margin:0 0 8px; font-size:.9rem; text-transform:uppercase;
           letter-spacing:.04em; color:var(--muted); }
.badge { display:inline-block; padding:5px 11px; border-radius:999px;
         font-weight:700; font-size:.78rem; letter-spacing:.05em; }
.badge-passed { background:var(--ok-bg); color:var(--ok); border:1px solid #abefc6; }
.badge-failed { background:var(--bad-bg); color:var(--bad); border:1px solid #fecdca; }
.badge-error  { background:var(--warn-bg); color:var(--warn); border:1px solid #fedf89; }
.summary-head { display:flex; gap:16px; align-items:flex-start; }
.summary-head h2 { margin:2px 0 2px; font-size:1.12rem; }
.summary-head p { margin:0; }
.facts { display:grid; grid-template-columns:repeat(auto-fit, minmax(250px, 1fr));
         gap:10px 22px; margin:18px 0 0; }
.facts div { min-width:0; }
.facts dt { font-size:.76rem; text-transform:uppercase; letter-spacing:.05em;
            color:var(--muted); font-weight:600; }
.facts dd { margin:2px 0 0; }
.compare-line { margin-top:16px; padding-top:14px; border-top:1px solid var(--line); }
.compare-line span { margin-right:6px; }
.compare-line strong { margin-right:18px; }
.banner { margin:0 0 14px; padding:11px 14px; border-radius:8px; font-size:.92rem; }
.banner-ok { background:var(--ok-bg); color:var(--ok); border:1px solid #abefc6; }
.banner-warn { background:var(--warn-bg); color:var(--warn); border:1px solid #fedf89; }
.reasons { margin:0 0 14px; padding:10px 14px 10px 30px; border-radius:8px;
           background:var(--warn-bg); color:var(--warn); font-size:.88rem; }
.bugreport { margin:0 0 10px; padding:11px 15px; border-left:3px solid var(--line);
             background:#f8fafb; border-radius:0 8px 8px 0; }
.table-wrap { overflow-x:auto; }
table.compare { width:100%; border-collapse:collapse; font-size:.9rem; }
table.compare th, table.compare td { text-align:left; vertical-align:top;
  padding:9px 10px; border-bottom:1px solid var(--line); }
table.compare thead th { background:#f8fafb; font-size:.8rem; text-transform:uppercase;
  letter-spacing:.04em; color:var(--muted); }
table.compare tbody th { width:190px; font-weight:600; }
.warn { color:var(--warn); font-weight:700; font-size:.8rem; }
.shots { display:grid; grid-template-columns:repeat(auto-fit, minmax(260px, 1fr)); gap:16px; }
.shot { margin:0; border:1px solid var(--line); border-radius:10px; overflow:hidden;
        background:#fbfcfd; }
.shot img { display:block; width:100%; height:auto; }
.shot figcaption { padding:9px 11px; font-size:.8rem; color:var(--muted);
                   border-top:1px solid var(--line); }
.shot-empty { padding:38px 12px; text-align:center; color:var(--muted); font-size:.85rem; }
details.action { border:1px solid var(--line); border-radius:10px; margin:0 0 10px;
                 background:#fbfcfd; }
details.action > summary { cursor:pointer; padding:11px 14px; display:flex; gap:12px;
  align-items:center; list-style:none; }
details.action > summary::-webkit-details-marker { display:none; }
details.action[open] > summary { border-bottom:1px solid var(--line); }
.idx { font-weight:700; color:var(--muted); min-width:34px; }
.atype { font-weight:600; min-width:74px; }
.astatus { font-size:.72rem; font-weight:700; text-transform:uppercase;
           padding:2px 8px; border-radius:999px; }
.astatus-ok { background:var(--ok-bg); color:var(--ok); }
.astatus-failed { background:var(--bad-bg); color:var(--bad); }
.action-body { padding:16px 16px 18px; }
.action-grid { display:grid; grid-template-columns:1fr 1fr; gap:18px; }
@media (max-width:760px) { .action-grid { grid-template-columns:1fr; } }
pre { margin:0; padding:11px 12px; background:#0f172a; color:#e2e8f0;
      border-radius:8px; overflow:auto; font-size:.78rem; line-height:1.45;
      font-family:Consolas, "Cascadia Mono", monospace; white-space:pre-wrap;
      word-break:break-word; }
.rationale { margin:0 0 14px; padding:10px 12px; background:#eff6ff;
             border-left:3px solid var(--accent); border-radius:6px; }
.rationale .label, .error .label { display:block; font-size:.72rem;
  text-transform:uppercase; letter-spacing:.05em; color:var(--muted);
  font-weight:700; margin-bottom:2px; }
.error { margin:10px 0; padding:10px 12px; background:var(--bad-bg);
         border-left:3px solid var(--bad); border-radius:6px; }
details.aria { margin-top:10px; }
details.aria > summary { cursor:pointer; font-size:.84rem; color:var(--accent); }
details.aria pre { margin-top:8px; max-height:320px; }
.action-body .shot { margin-top:16px; }
footer.page { color:var(--muted); font-size:.82rem; margin-top:26px; }
footer.page p { margin:6px 0; }
"""


def render_report_html(runs: dict[str, ReportRun]) -> str:
    """Render the selected runs. ``runs`` must contain ``attempt``."""
    if "attempt" not in runs:
        raise ReportError("A report needs an 'attempt' run.")
    attempt = runs["attempt"]
    unchanged, reasons = _comparison(runs)

    bug_report = str(attempt.data.get("bug_report") or "(not recorded)")
    base_url = attempt.data.get("base_url") or "(unknown)"

    final_states = _final_states(runs)
    comparison = _comparison_table(runs, unchanged, reasons)

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Bug Repro evidence report — {_esc(attempt.run_id)}</title>
  <style>{CSS}</style>
</head>
<body>
  <div class="wrap">
    <header class="page">
      <h1>Bug Repro evidence report</h1>
      <p class="muted">Generated {_esc(_utc_now())} · self-contained file · no scripts ·
        source run <code>{_esc(attempt.run_id)}</code></p>
    </header>

    <section class="card">
      <h2>Bug report</h2>
      <blockquote class="bugreport">{_esc(bug_report)}</blockquote>
      <p class="muted small">Target: <code>{_esc(base_url)}</code></p>
    </section>

    {_summary_card(attempt)}

    <section class="card">
      <h2>Frozen expected result</h2>
      <p>The expectation below was supplied before the attempt and is not derived
        from the page or the actions.</p>
      {_fingerprint_block(attempt)}
    </section>

    {final_states}
    {comparison}

    <section class="card">
      <h2>Ordered actions ({len(attempt.actions)})</h2>
      <p class="muted small">Chosen by the live agent through the local service.
        Rationales are the agent's recorded reasons, not verified claims.</p>
      {_action_details(attempt)}
    </section>

    <section class="card">
      <h2>Errors and budget</h2>
      {_errors_section(attempt)}
    </section>

    <footer class="page">
      <p><strong>Reading this honestly.</strong> A failed check means the expected
        result was not observed after this sequence. It is an assertion about the
        page, not proof of the underlying cause.</p>
      <p>Live agent decisions are labelled <code>alan-agent</code>; deterministic
        replays are labelled <code>recorded-replay</code> and make no model calls.
        Model identity is session-reported, not provider-verified.</p>
      <p>This file is static. It does not contact the browser service.</p>
    </footer>
  </div>
</body>
</html>
"""


def write_report_html(
    output: str | Path,
    runs: dict[str, ReportRun],
) -> Path:
    """Write the rendered report and return its path."""
    target = Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_report_html(runs), encoding="utf-8")
    return target


def compare_fingerprints(runs: Iterable[ReportRun]) -> tuple[bool, list[str]]:
    """Public helper used by tests and the CLI summary."""
    mapping = {run.role: run for run in runs}
    return _comparison(mapping)