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
    "scenario": "Exploration scenario",
    "confirmation": "Replay confirmation",
}
EXPLORATION_CLASSIFICATIONS = (
    "violation",
    "possible_issue",
    "no_violation",
    "execution_error",
    "not_evaluated",
)
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
    "model-agent": "Configured model endpoint decisions (user-reported identity).",
    "codex-cli": "Codex CLI decisions; model identity unverified. Tool-disable controls requested, not universal isolation.",
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
        role = self.role
        driver = self.driver
        if role == "attempt":
            return {
                "alan-agent": "Original model-driven attempt",
                "model-agent": "Original model-driven attempt",
                "codex-cli": "Original Codex CLI attempt",
                "scripted-test": "Scripted attempt",
                "recorded-replay": "Recorded replay",
            }.get(driver, f"Attempt (driver: {driver})")
        if role in {"buggy", "fixed"}:
            target = "buggy version" if role == "buggy" else "fixed version"
            if driver == "recorded-replay":
                return f"Replay against {target}"
            if driver == "alan-agent":
                return f"Alan-driven run against {target}"
            if driver == "model-agent":
                return f"Model-driven run against {target}"
            if driver == "codex-cli":
                return f"Codex CLI run against {target}"
            if driver == "scripted-test":
                return f"Scripted run against {target}"
            return f"Run against {target} (driver: {driver})"
        return ROLE_LABELS.get(role, role)

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
    """Return (is_unchanged_replay, reasons).

    An unchanged replay claim requires at least one replay run, all selected
    recipe and expectation fingerprints present, identical fingerprints across
    the selected runs, and the honest driver labels.
    """
    reasons: list[str] = []
    order = [role for role in ("attempt", "buggy", "fixed") if role in runs]
    replays = [role for role in ("buggy", "fixed") if role in runs]

    if not replays:
        reasons.append("No buggy or fixed replay run was selected.")

    for role in order:
        run = runs[role]
        if not run.recipe_fingerprint:
            reasons.append(f"{run.label}: recipe fingerprint is missing.")
        if not run.expectation_fingerprint:
            reasons.append(f"{run.label}: expectation fingerprint is missing.")

    known_recipes = {
        run.recipe_fingerprint for run in runs.values() if run.recipe_fingerprint
    }
    if len(known_recipes) > 1:
        reasons.append("Recipe fingerprints differ between the selected runs.")

    known_expectations = {
        run.expectation_fingerprint
        for run in runs.values()
        if run.expectation_fingerprint
    }
    if len(known_expectations) > 1:
        reasons.append("Expectation fingerprints differ between the selected runs.")

    for role in replays:
        run = runs[role]
        if run.driver != "recorded-replay":
            reasons.append(
                f'"{run.label}" is not a deterministic replay '
                f"(driver: {run.driver})."
            )

    attempt = runs.get("attempt")
    if attempt is not None and attempt.driver not in {"alan-agent", "model-agent", "codex-cli"}:
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
          <div><dt>Driver</dt><dd>{_esc(run.driver)}<span class="muted"> - {_esc(DRIVER_MEANINGS.get(run.driver, "Unrecognised driver."))}</span></dd></div>
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
            f"{run.label} - {STATUS_LABELS.get(run.status, run.status)} - "
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
            _image_block(run, shot, f"Action {index} - {action_type}")
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
    model_calls = run.data.get("model_calls")
    if isinstance(model_calls, dict):
        parts.append(
            f"<p>Model calls: {_esc(model_calls.get('used', 0))} used / "
            f"{_esc(model_calls.get('limit', 0))} allowed.</p>"
        )
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
.badge-violation { background:var(--bad-bg); color:var(--bad); border:1px solid #fecdca; }
.badge-possible_issue { background:var(--warn-bg); color:var(--warn); border:1px solid #fedf89; }
.badge-no_violation { background:var(--ok-bg); color:var(--ok); border:1px solid #abefc6; }
.badge-execution_error { background:#f2f4f7; color:#475467; border:1px solid #d0d5dd; }
.badge-not_evaluated { background:#f8fafc; color:var(--muted); border:1px solid var(--line); }
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
.req-table td:first-child { white-space:nowrap; }
.finding { border:1px solid var(--line); border-left:4px solid var(--bad);
           border-radius:10px; padding:16px 18px; margin:0 0 14px; background:#fff; }
.finding-possible { border-left-color:var(--warn); }
.finding h3 { margin:0 0 6px; font-size:1rem; }
.finding .req { margin:0 0 10px; color:var(--muted); }
.finding-grid { display:grid; grid-template-columns:1.15fr .85fr; gap:18px; }
@media (max-width:820px) { .finding-grid { grid-template-columns:1fr; } }
.chips { display:flex; flex-wrap:wrap; gap:8px; margin-top:6px; }
.chip { font-size:.78rem; padding:3px 9px; border-radius:999px;
        background:#f2f4f7; color:#475467; }
details.scenario-detail { border:1px solid var(--line); border-radius:10px;
  margin:0 0 12px; background:#fbfcfd; }
details.scenario-detail > summary { cursor:pointer; padding:12px 14px;
  display:flex; gap:12px; align-items:center; flex-wrap:wrap; list-style:none; }
details.scenario-detail > summary::-webkit-details-marker { display:none; }
details.scenario-detail[open] > summary { border-bottom:1px solid var(--line); }
.scenario-body { padding:16px; }
.note { margin:0 0 14px; padding:10px 12px; border-radius:8px;
        background:#f8fafc; border:1px solid var(--line); color:var(--muted);
        font-size:.88rem; }
"""


def _attempt_context_card(run: ReportRun) -> str:
    base_url = run.data.get("base_url") or "(unknown)"
    bug_report = run.data.get("bug_report")
    requirement = run.data.get("requirement") or {}
    scenario = run.data.get("scenario") or {}

    if bug_report:
        return f"""
    <section class="card">
      <h2>Bug report</h2>
      <blockquote class="bugreport">{_esc(bug_report)}</blockquote>
      <p class="muted small">Target: <code>{_esc(base_url)}</code></p>
    </section>
        """

    if requirement or scenario:
        test_data = scenario.get("test_data") or []
        requirement_id = requirement.get("id") or "(no requirement id)"
        requirement_text = requirement.get("text") or "(no requirement wording recorded)"
        scenario_title = scenario.get("title") or "(no scenario title)"
        scenario_description = scenario.get("description") or ""
        exploration_id = run.data.get("exploration_id") or ""
        exploration_note = (
            f'<p class="muted small">Exploration: <code>{_esc(exploration_id)}</code></p>'
            if exploration_id
            else ""
        )
        return f"""
    <section class="card">
      <h2>Requirement and scenario</h2>
      <p><strong>{_esc(requirement_id)}</strong> - {_esc(requirement_text)}</p>
      <p><strong>{_esc(scenario_title)}</strong></p>
      <p class="muted small">{_esc(scenario_description)}</p>
      <p class="muted small">Test data: {_esc(', '.join(str(x) for x in test_data) or 'none')}</p>
      <p class="muted small">Target: <code>{_esc(base_url)}</code></p>
      {exploration_note}
    </section>
        """

    return f"""
    <section class="card">
      <h2>Context</h2>
      <p class="muted">No bug report or exploration requirement was recorded for this run.</p>
      <p class="muted small">Target: <code>{_esc(base_url)}</code></p>
    </section>
    """


def render_report_html(runs: dict[str, ReportRun]) -> str:
    """Render the selected runs. ``runs`` must contain ``attempt``."""
    if "attempt" not in runs:
        raise ReportError("A report needs an 'attempt' run.")
    attempt = runs["attempt"]
    unchanged, reasons = _comparison(runs)

    final_states = _final_states(runs)
    comparison = _comparison_table(runs, unchanged, reasons)

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Bug Repro evidence report - {_esc(attempt.run_id)}</title>
  <style>{CSS}</style>
</head>
<body>
  <div class="wrap">
    <header class="page">
      <h1>Bug Repro evidence report</h1>
      <p class="muted">Generated {_esc(_utc_now())} | self-contained file | no scripts |
        source run <code>{_esc(attempt.run_id)}</code></p>
    </header>

    {_attempt_context_card(attempt)}

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
      <p class="muted small">{_esc(DRIVER_MEANINGS.get(attempt.driver, "Driver provenance is unavailable."))}
        Recorded rationales describe actions and are not independently verified.</p>
      {_action_details(attempt)}
    </section>

    <section class="card">
      <h2>Errors and budget</h2>
      {_errors_section(attempt)}
    </section>

    <footer class="page">
      <p><strong>Reading the result.</strong> A failed check means the expected
        result was not observed after this sequence. It is an assertion about the
        page, not proof of the underlying cause.</p>
      <p>Live agent decisions are labelled <code>alan-agent</code> or
        <code>model-agent</code> or <code>codex-cli</code>; scripted actions
        are labelled <code>scripted-test</code>; deterministic replays are labelled
        <code>recorded-replay</code> and make no model calls. Model identity is
        session-reported, not provider-verified.</p>
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
# ---------------------------------------------------------------- exploration

@dataclass
class ExplorationReport:
    run_dir: Path
    manifest: dict[str, Any]
    scenario_runs: dict[str, ReportRun | None]
    confirmation_runs: dict[str, ReportRun | None]
    integrity_warnings: list[str]

    @property
    def scenarios(self) -> list[dict[str, Any]]:
        value = self.manifest.get("scenarios")
        return value if isinstance(value, list) else []

    @property
    def requirements(self) -> list[dict[str, Any]]:
        value = self.manifest.get("requirements")
        return value if isinstance(value, list) else []

    @property
    def counts(self) -> dict[str, int]:
        counts = {name: 0 for name in EXPLORATION_CLASSIFICATIONS}
        for scenario in self.scenarios:
            classification = scenario.get("classification")
            if classification in counts:
                counts[classification] += 1
        return counts


def _classification_badge(value: Any) -> str:
    classification = str(value or "not_evaluated")
    if classification not in EXPLORATION_CLASSIFICATIONS:
        classification = "not_evaluated"
    label = classification.replace("_", " ").upper()
    return (
        f'<span class="badge badge-{_esc(classification)}">'
        f"{_esc(label)}</span>"
    )


def _exploration_status_badge(value: Any) -> str:
    status = str(value or "unknown")
    labels = {
        "finished": ("FINISHED", "no_violation"),
        "active": ("IN PROGRESS", "possible_issue"),
        "closed": ("CLOSED", "not_evaluated"),
    }
    label, css = labels.get(status, (status.upper(), "not_evaluated"))
    return f'<span class="badge badge-{css}">{_esc(label)}</span>'


def _replay_badge(value: Any) -> str:
    status = str(value or "not_run")
    labels = {
        "confirmed": "REPLAY CONFIRMED",
        "not_reproduced": "DID NOT RECUR",
        "error": "CONFIRMATION ERROR",
        "not_run": "NOT REPLAY-CONFIRMED",
    }
    classes = {
        "confirmed": "no_violation",
        "not_reproduced": "possible_issue",
        "error": "execution_error",
        "not_run": "not_evaluated",
    }
    label = labels.get(status, status.upper())
    css = classes.get(status, "not_evaluated")
    return f'<span class="badge badge-{_esc(css)}">{_esc(label)}</span>'


def _load_run_under(
    root: Path,
    relative: Any,
    role: str,
    warnings: list[str],
    *,
    required: bool,
) -> ReportRun | None:
    if not isinstance(relative, str) or not relative.strip():
        return None
    candidate = (root / relative).resolve()
    if candidate != root and root not in candidate.parents:
        warnings.append(f"Evidence path escapes the exploration directory: {relative}")
        return None
    try:
        return load_report_run(candidate, role)
    except ReportError as exc:
        if required:
            warnings.append(str(exc))
        return None


def load_exploration_report(exploration_dir: str | Path) -> ExplorationReport:
    """Load one explicitly selected exploration and its referenced evidence."""
    root = Path(exploration_dir).resolve()
    manifest_path = root / "exploration.json"
    if not manifest_path.is_file():
        raise ReportError(f"exploration.json not found in {root}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReportError(f"Could not read {manifest_path}: {exc}") from exc
    if not isinstance(manifest, dict):
        raise ReportError("exploration.json must contain a JSON object.")
    for field_name in ("exploration_id", "target", "requirements", "scenarios"):
        if field_name not in manifest:
            raise ReportError(f"exploration.json is missing {field_name!r}.")
    if not isinstance(manifest.get("requirements"), list):
        raise ReportError("exploration.json requirements must be an array.")
    if not isinstance(manifest.get("scenarios"), list):
        raise ReportError("exploration.json scenarios must be an array.")

    warnings: list[str] = []
    scenario_runs: dict[str, ReportRun | None] = {}
    confirmation_runs: dict[str, ReportRun | None] = {}

    for index, scenario in enumerate(manifest["scenarios"]):
        if not isinstance(scenario, dict):
            raise ReportError(f"exploration.json scenarios[{index}] is malformed.")
        scenario_id = scenario.get("scenario_id")
        if not isinstance(scenario_id, str):
            raise ReportError(
                f"exploration.json scenarios[{index}] has no scenario_id."
            )
        classification = scenario.get("classification")
        if classification not in EXPLORATION_CLASSIFICATIONS:
            warnings.append(
                f"Scenario {scenario_id} has unknown classification {classification!r}."
            )

        status = scenario.get("status")
        run = _load_run_under(
            root,
            scenario.get("run_dir"),
            "scenario",
            warnings,
            required=status in {"finished", "closed", "crashed"},
        )
        scenario_runs[scenario_id] = run
        if run is not None:
            scenario_expectation = scenario.get("expectation") or {}
            if (
                scenario_expectation.get("fingerprint")
                and run.expectation_fingerprint
                and scenario_expectation["fingerprint"] != run.expectation_fingerprint
            ):
                warnings.append(
                    f"Scenario {scenario_id}: expectation fingerprint differs "
                    "from its run record."
                )
            scenario_check = scenario.get("check") or {}
            if scenario_check.get("status") and run.status != scenario_check["status"]:
                warnings.append(
                    f"Scenario {scenario_id}: check status differs from its run record."
                )
            export = scenario.get("export") or {}
            if (
                export.get("sha256")
                and run.recipe_fingerprint
                and export["sha256"] != run.recipe_fingerprint
            ):
                warnings.append(
                    f"Scenario {scenario_id}: exported recipe fingerprint differs "
                    "from its run record."
                )

        confirmation = scenario.get("replay_confirmation") or {}
        confirmation_status = confirmation.get("status", "not_run")
        confirmation_run = _load_run_under(
            root,
            confirmation.get("run_dir"),
            "confirmation",
            warnings,
            required=confirmation_status != "not_run",
        )
        confirmation_runs[scenario_id] = confirmation_run
        if confirmation_run is not None:
            export = scenario.get("export") or {}
            if (
                export.get("sha256")
                and confirmation_run.recipe_fingerprint
                and export["sha256"] != confirmation_run.recipe_fingerprint
            ):
                warnings.append(
                    f"Scenario {scenario_id}: confirmation used a different recipe "
                    "fingerprint."
                )
            scenario_expectation = scenario.get("expectation") or {}
            if (
                scenario_expectation.get("fingerprint")
                and confirmation_run.expectation_fingerprint
                and scenario_expectation["fingerprint"]
                != confirmation_run.expectation_fingerprint
            ):
                warnings.append(
                    f"Scenario {scenario_id}: confirmation used a different "
                    "expectation fingerprint."
                )

    return ExplorationReport(
        run_dir=root,
        manifest=manifest,
        scenario_runs=scenario_runs,
        confirmation_runs=confirmation_runs,
        integrity_warnings=list(dict.fromkeys(warnings)),
    )


def _exploration_summary_card(report: ExplorationReport) -> str:
    manifest = report.manifest
    model = manifest.get("model") or {}
    budget = manifest.get("budget") or {}
    counts = report.counts
    target_override = manifest.get("target_override") or {}
    override_note = ""
    if target_override.get("used"):
        override_note = (
            f'<p class="note">Target override: spec target '
            f'{_esc(target_override.get("spec_target"))} was overridden with '
            f'{_esc(target_override.get("actual_target"))} for this exploration.</p>'
        )
    status = str(manifest.get("exploration_status") or "unknown")
    return f"""
      <div class="card summary">
<div class="summary-head">
          {_exploration_status_badge(status)}
          <div>
            <h2>Requirement-guided exploration - {_esc(status)}</h2>
            <p class="muted">{_esc(manifest.get("description", ""))}</p>
          </div>
        </div>
        <div class="chips">{''.join(
            f'<span class="chip">{_esc(name.replace("_", " "))}: {counts[name]}</span>'
            for name in EXPLORATION_CLASSIFICATIONS
        )}</div>
        <dl class="facts">
          <div><dt>Target</dt><dd><code>{_esc(manifest.get("target", ""))}</code></dd></div>
          <div><dt>Driver</dt><dd>{_esc(manifest.get("driver", "unknown"))}</dd></div>
          <div><dt>Model</dt><dd>{_esc(model.get("id", "unknown"))} <span class="muted">(source: {_esc(model.get("source", "unknown"))})</span></dd></div>
          <div><dt>Action budget</dt><dd>{_esc(budget.get("used", 0))} used / {_esc(budget.get("limit", 0))} allowed ({_esc(budget.get("remaining", 0))} remaining)</dd></div>
          <div><dt>Scenario actions</dt><dd>{_esc(budget.get("exploration_actions", 0))}</dd></div>
          <div><dt>Confirmation actions</dt><dd>{_esc(budget.get("confirmation_actions", 0))}</dd></div>
          <div><dt>Scenarios</dt><dd>{len(report.scenarios)} / {_esc(manifest.get("max_scenarios", "?"))}</dd></div>
          <div><dt>Spec fingerprint</dt><dd><code class="fp">{_esc(manifest.get("spec_fingerprint", "missing"))}</code></dd></div>
        </dl>
        {override_note}
      </div>
    """


def _exploration_requirements(report: ExplorationReport) -> str:
    manifest = report.manifest
    summary = manifest.get("requirement_summary")
    if not isinstance(summary, list) or not summary:
        summary = [
            {
                "id": item.get("id"),
                "text": item.get("text"),
                "scenario_ids": [],
                "classification": "not_evaluated",
            }
            for item in report.requirements
            if isinstance(item, dict)
        ]
    rows = []
    for item in summary:
        if not isinstance(item, dict):
            continue
        scenario_ids = item.get("scenario_ids") or []
        rows.append(
            "<tr>"
            f"<td><strong>{_esc(item.get('id', ''))}</strong></td>"
            f"<td>{_esc(item.get('text', ''))}</td>"
            f"<td>{_esc(', '.join(str(x) for x in scenario_ids) or 'none')}</td>"
            f"<td>{_classification_badge(item.get('classification'))}</td>"
            "</tr>"
        )
    allowed_actions = manifest.get("allowed_actions") or []
    excluded = manifest.get("excluded_interactions") or []
    test_data = manifest.get("allowed_test_data") or []
    constraints = f"""
      <div class="card">
        <h2>Exploration constraints</h2>
        <dl class="facts">
          <div><dt>Allowed actions</dt><dd>{_esc(', '.join(allowed_actions) or 'none')}</dd></div>
          <div><dt>Allowed test data</dt><dd>{_esc(', '.join(test_data) or 'none')}</dd></div>
          <div><dt>Excluded interactions</dt><dd>{_esc('; '.join(excluded) or 'none recorded')}</dd></div>
        </dl>
      </div>
    """
    return f"""
      <section class="card">
        <h2>Requirements supplied</h2>
        <p class="muted small">User-authored requirements are recorded unchanged.
          A passing scenario is not a claim that the application is bug-free.</p>
        <div class="table-wrap">
          <table class="compare req-table">
            <thead><tr><th scope="col">ID</th><th scope="col">Requirement</th>
              <th scope="col">Scenarios</th><th scope="col">Coverage</th></tr></thead>
            <tbody>{''.join(rows)}</tbody>
          </table>
        </div>
      </section>
      {constraints}
    """


def _exploration_findings(report: ExplorationReport) -> str:
    requirements = {
        item.get("id"): item
        for item in report.requirements
        if isinstance(item, dict)
    }
    findings = [
        scenario
        for scenario in report.scenarios
        if scenario.get("classification") in {"violation", "possible_issue"}
    ]
    if not findings:
        return """
      <section class="card">
        <h2>Findings</h2>
        <p class="muted">No requirement violation or possible issue was recorded.</p>
      </section>
        """
    cards = []
    for scenario in findings:
        scenario_id = scenario.get("scenario_id")
        requirement = requirements.get(scenario.get("requirement_id")) or {}
        check = scenario.get("check") or {}
        confirmation = scenario.get("replay_confirmation") or {}
        run = report.scenario_runs.get(scenario_id)
        confirmation_run = report.confirmation_runs.get(scenario_id)
        shot = _final_screenshot(run) if run else None
        shot_html = (
            _image_block(
                run,
                shot,
                f"{scenario_id} - {scenario.get('classification')} - observed: "
                f"{_observed_text(run)}",
            )
            if run
            else '<p class="muted small">Run record unavailable.</p>'
)
        observed = check.get("observed") or []
        possible = scenario.get("classification") == "possible_issue"
        confirmation_run_link = (
            f' <a href="#confirmation-{_esc(scenario_id)}">view replay evidence</a>'
            if confirmation_run is not None
            else ""
        )
        scenario_run_link = (
            f' <a href="#scenario-{_esc(scenario_id)}">view scenario evidence</a>'
            if run is not None
            else ""
        )
        cards.append(
            f"""
      <article class="finding{' finding-possible' if possible else ''}">
        <h3>{_classification_badge(scenario.get("classification"))}
          {_esc(scenario.get("title", ""))}</h3>
        <p class="req"><strong>{_esc(scenario.get("requirement_id", ""))}</strong>
          - {_esc(requirement.get("text", scenario.get("requirement_text", "")))}</p>
        <div class="finding-grid">
          <div>
            <h4>Frozen expectation</h4>
            <p><strong>{_esc((scenario.get("expectation") or {}).get("text", ""))}</strong></p>
            <p class="muted small">Fingerprint:
              <code class="fp">{_esc((scenario.get("expectation") or {}).get("fingerprint", "missing"))}</code></p>
            <h4>Observed result</h4>
            <p>{_esc(', '.join(str(x) for x in observed) or '(none)')}</p>
            <p class="muted small">{_esc(check.get("reason", ""))}</p>
            <p>{_replay_badge(confirmation.get("status"))}{confirmation_run_link}{scenario_run_link}</p>
            <p class="muted small">{_esc(confirmation.get("reason", ""))}</p>
            <p class="muted small">Classification: {_esc(scenario.get("classification_reason", ""))}</p>
          </div>
          <div>{shot_html}</div>
        </div>
      </article>
            """
        )
    return f"""
      <section class="card">
        <h2>Findings</h2>
        <p class="muted small">Only <code>violation</code> means the frozen
          expectation failed and the same observation recurred in a fresh replay.
          <code>possible_issue</code> still needs human confirmation.</p>
        {''.join(cards)}
      </section>
    """


def _exploration_scenario_table(report: ExplorationReport) -> str:
    rows = []
    for scenario in report.scenarios:
        scenario_id = scenario.get("scenario_id")
        confirmation = scenario.get("replay_confirmation") or {}
        rows.append(
            "<tr>"
            f'<td><a href="#scenario-{_esc(scenario_id)}">{_esc(scenario_id)}</a></td>'
            f"<td>{_esc(scenario.get('requirement_id', ''))}</td>"
            f"<td>{_esc(scenario.get('title', ''))}</td>"
            f"<td>{_classification_badge(scenario.get('classification'))}</td>"
            f"<td>{_replay_badge(confirmation.get('status'))}</td>"
            f"<td><code class=\"fp\">{_esc(scenario.get('run_dir', ''))}</code></td>"
            "</tr>"
        )
    if not rows:
        rows.append('<tr><td colspan="6">No scenarios were attempted.</td></tr>')
    return f"""
      <section class="card">
        <h2>Scenarios attempted</h2>
        <div class="table-wrap">
          <table class="compare">
            <thead><tr><th scope="col">Scenario</th><th scope="col">Requirement</th>
              <th scope="col">Title</th><th scope="col">Classification</th>
              <th scope="col">Replay</th><th scope="col">Run</th></tr></thead>
            <tbody>{''.join(rows)}</tbody>
          </table>
        </div>
      </section>
    """


def _exploration_scenario_details(report: ExplorationReport) -> str:
    details = []
    for scenario in report.scenarios:
        scenario_id = scenario.get("scenario_id")
        run = report.scenario_runs.get(scenario_id)
        expectation = scenario.get("expectation") or {}
        confirmation = scenario.get("replay_confirmation") or {}
        export = scenario.get("export") or {}
        check = scenario.get("check") or {}
        budget = scenario.get("budget") or {}
        errors = _errors_section(run) if run else ""
        actions = _action_details(run) if run else ""
        export_line = (
            f'<p class="small">Exported recipe: <code>{_esc(export.get("recipe_path"))}</code> '
            f'({_esc(export.get("sha256"))})</p>'
            if export
            else '<p class="muted small">No recipe was exported for this scenario.</p>'
        )
        details.append(
            f"""
      <details class="scenario-detail" id="scenario-{_esc(scenario_id)}">
        <summary>
          <span class="idx">{_esc(scenario_id)}</span>
          <strong>{_esc(scenario.get("requirement_id", ""))}</strong>
          <span>{_esc(scenario.get("title", ""))}</span>
          {_classification_badge(scenario.get("classification"))}
          {_replay_badge(confirmation.get("status"))}
          <span class="muted small">{_esc(scenario.get("action_count", 0))} actions</span>
        </summary>
        <div class="scenario-body">
          <dl class="facts">
            <div><dt>Requirement</dt><dd>{_esc(scenario.get("requirement_text", ""))}</dd></div>
            <div><dt>Intent</dt><dd>{_esc(scenario.get("description", ""))}</dd></div>
            <div><dt>Expected text</dt><dd>{_esc(expectation.get("text", ""))}</dd></div>
            <div><dt>Expectation fingerprint</dt><dd><code class="fp">{_esc(expectation.get("fingerprint", "missing"))}</code></dd></div>
            <div><dt>Frozen at</dt><dd>{_esc(expectation.get("frozen_at", ""))}</dd></div>
            <div><dt>Test data</dt><dd>{_esc(', '.join(scenario.get("test_data") or []) or 'none')}</dd></div>
            <div><dt>Budget</dt><dd>{_esc(budget.get("used", 0))} / {_esc(budget.get("limit", 0))}</dd></div>
            <div><dt>Check</dt><dd>{_status_badge(check.get("status", "error"))}</dd></div>
            <div><dt>Run</dt><dd><code class="fp">{_esc(scenario.get("run_dir", ""))}</code></dd></div>
          </dl>
          <p class="muted small">Classification reason: {_esc(scenario.get("classification_reason", ""))}</p>
          {export_line}
          {errors}
          {('<h4>Ordered actions</h4>' + actions) if run else '<p class="muted">Run evidence is not available.</p>'}
        </div>
      </details>
            """
        )
    if not details:
        details.append('<p class="muted">No scenario evidence exists.</p>')
    return f"""
      <section class="card">
        <h2>Scenario details and evidence</h2>
        <p class="muted small">Expand a scenario to inspect its validated inputs,
          rationales, observations, errors and screenshots.</p>
        {''.join(details)}
      </section>
    """


def _exploration_confirmations(report: ExplorationReport) -> str:
    blocks = []
    for scenario in report.scenarios:
        scenario_id = scenario.get("scenario_id")
        confirmation = scenario.get("replay_confirmation") or {}
        run = report.confirmation_runs.get(scenario_id)
        if run is None:
            continue
        check = confirmation.get("check") or {}
        shot = _final_screenshot(run)
        blocks.append(
            f"""
      <details class="scenario-detail" id="confirmation-{_esc(scenario_id)}">
        <summary>
          <span class="idx">{_esc(scenario_id)}</span>
          <strong>Replay confirmation</strong>
          {_replay_badge(confirmation.get("status"))}
          <span class="muted small">{_esc(confirmation.get("action_count", 0))} actions</span>
        </summary>
        <div class="scenario-body">
          <p>{_esc(confirmation.get("reason", ""))}</p>
          <dl class="facts">
            <div><dt>Replay check</dt><dd>{_status_badge(check.get("status", "error"))}</dd></div>
            <div><dt>Observed</dt><dd>{_esc(', '.join(str(x) for x in (confirmation.get("observed") or [])) or '(none)')}</dd></div>
            <div><dt>Recipe fingerprint</dt><dd><code class="fp">{_esc(confirmation.get("recipe_sha256", "missing"))}</code></dd></div>
            <div><dt>Run</dt><dd><code class="fp">{_esc(confirmation.get("run_dir", ""))}</code></dd></div>
          </dl>
          {_image_block(run, shot, f"{scenario_id} replay confirmation")}
          <h4>Ordered replay actions</h4>
          {_action_details(run)}
        </div>
      </details>
            """
        )
    if not blocks:
        blocks.append(
            '<p class="muted">No replay confirmation was recorded for this exploration.</p>'
        )
    return f"""
      <section class="card">
        <h2>Replay confirmation</h2>
        <p class="muted small">A violation is only replay-confirmed when the same
          frozen expectation fails with the same observed result in a fresh context.</p>
        {''.join(blocks)}
      </section>
    """


def _exploration_untested(report: ExplorationReport) -> str:
    untested = report.manifest.get("untested_requirements") or []
    summary = report.manifest.get("requirement_summary") or []
    not_evaluated = [
        item
        for item in summary
        if isinstance(item, dict)
        and item.get("classification") == "not_evaluated"
        and item.get("scenario_ids")
    ]
    parts = []
    if untested:
        parts.append("<h3>Requirements with no scenario</h3><ul>")
        for item in untested:
            parts.append(
                f"<li><strong>{_esc(item.get('id'))}</strong> - {_esc(item.get('text'))}</li>"
            )
        parts.append("</ul>")
    if not_evaluated:
        parts.append("<h3>Scenarios that did not evaluate their requirement</h3><ul>")
        for item in not_evaluated:
            parts.append(
                f"<li><strong>{_esc(item.get('id'))}</strong> - {_esc(item.get('text'))}</li>"
            )
        parts.append("</ul>")
    if not parts:
        parts.append(
            '<p class="muted">No requirement was left untested or unevaluated.</p>'
        )
    return f"""
      <section class="card">
        <h2>Untested and not evaluated</h2>
        {''.join(parts)}
        <p class="muted small">Not evaluated means the requirement was not checked
          with a supported expectation; it is not a pass.</p>
      </section>
    """


def render_exploration_html(report: ExplorationReport) -> str:
    manifest = report.manifest
    warning_banner = ""
    if report.integrity_warnings:
        items = "".join(
            f"<li>{_esc(item)}</li>" for item in report.integrity_warnings
        )
        warning_banner = (
            '<div class="card"><p class="banner banner-warn">Evidence integrity '
            "warnings: the record and its run files do not fully agree.</p>"
            f"<ul class=\"reasons\">{items}</ul></div>"
        )
    unfinished = ""
    if manifest.get("exploration_status") != "finished":
        unfinished = (
            '<p class="note">This exploration is not finished. Incomplete '
            "scenarios are classified not evaluated.</p>"
        )

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Bug Repro exploration - {_esc(manifest.get("exploration_id", ""))}</title>
  <style>{CSS}</style>
</head>
<body>
  <div class="wrap">
    <header class="page">
      <h1>Bug Repro exploration report</h1>
      <p class="muted">Generated {_esc(_utc_now())} | self-contained file | no scripts |
        exploration <code>{_esc(manifest.get("exploration_id", ""))}</code></p>
    </header>

    {_exploration_summary_card(report)}
    {unfinished}
    {warning_banner}
    {_exploration_requirements(report)}
    {_exploration_findings(report)}
    {_exploration_scenario_table(report)}
    {_exploration_confirmations(report)}
    {_exploration_scenario_details(report)}
    {_exploration_untested(report)}

    <footer class="page">
      <p><strong>Scope.</strong> This report checks recorded scenarios against
        supplied requirements. A passing scenario covers only the expectation
        tested; a failed check records observed behaviour, not a root cause.</p>
      <p>Live decisions are labelled <code>alan-agent</code>; scripted actions are
        labelled <code>scripted-test</code>; replays are labelled
        <code>recorded-replay</code> and make no model calls. Model identity is
        session-reported, not provider-verified.</p>
      <p>This file is static. It does not contact the browser service and includes
        only runs referenced by the selected exploration.</p>
    </footer>
  </div>
</body>
</html>
"""


def write_exploration_html(
    report: ExplorationReport,
    output: str | Path,
) -> Path:
    """Write an exploration report and return its path."""
    target = Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_exploration_html(report), encoding="utf-8")
    return target
