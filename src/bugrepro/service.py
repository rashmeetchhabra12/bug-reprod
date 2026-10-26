"""Persistent local service that owns Playwright for agent-driven sessions.

Design rules:

* The service binds to 127.0.0.1 only and requires a locally generated
  bearer token. The token lives in a state file outside the repository and
  never appears in command arguments, console output, reports or Git.
* All Playwright work happens on one worker thread. Operations are therefore
  serialized, and the browser host is used from its owning thread.
* Every investigation gets a fresh browser context, so cookies and cache
  cannot leak between investigations.
* The existing loopback target restrictions are enforced on every request.
* There is no endpoint for arbitrary Python, JavaScript, shell commands or
  file paths. The only filesystem writes are inside the service-owned run
  directory.
"""

from __future__ import annotations

import json
import os
import queue
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from .browser import BrowserStartupError, PlaywrightHost
from .expectations import ExpectationError, parse_expectation
from .exploration import (
    Exploration,
    ExplorationError,
    parse_exploration_spec,
)
from .investigation import (
    INVESTIGATION_DRIVERS,
    MODEL_SOURCES,
    Investigation,
    InvestigationError,
)
from .recipe import RecipeError, parse_action
from .safety import TargetError, validate_base_url

SERVICE_NAME = "bugrepro-agent"
SERVICE_PROTOCOL = 1
MAX_BODY_BYTES = 256 * 1024
MAX_BUG_REPORT_CHARS = 20_000
MAX_RATIONALE_CHARS = 600
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
ALLOWED_START_FIELDS = frozenset(
    {
        "target",
        "bug_report",
        "expectation",
        "driver",
        "model",
        "label",
        "action_budget",
        "timeout_ms",
    }
)
ALLOWED_EXPLORATION_START_FIELDS = frozenset(
    {"spec", "driver", "model", "label", "target_override"}
)


class AgentServiceError(RuntimeError):
    """A service-level error that maps cleanly onto an HTTP status."""

    def __init__(
        self,
        message: str,
        *,
        http_status: int = 400,
        code: str = "invalid_request",
    ) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.code = code


def _validate_rationale(value: Any) -> str | None:
    """Validate an optional public rationale recorded with one action."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvestigationError("rationale must be a string when provided.")
    text = value.strip()
    if not text:
        return None
    if len(text) > MAX_RATIONALE_CHARS:
        raise InvestigationError(
            f"rationale must be at most {MAX_RATIONALE_CHARS} characters."
        )
    if "\x00" in text:
        raise InvestigationError("rationale must not contain a NUL character.")
    return text


def _parse_driver_and_model(payload: dict[str, Any]) -> tuple[str, dict[str, str]]:
    driver = payload.get("driver", "scripted-test")
    if driver not in INVESTIGATION_DRIVERS:
        raise InvestigationError(
            f"driver must be one of {sorted(INVESTIGATION_DRIVERS)}."
        )

    model_payload = payload.get("model", {})
    if not isinstance(model_payload, dict):
        raise InvestigationError("model must be an object.")
    unknown_model = set(model_payload) - {"id", "source"}
    if unknown_model:
        raise InvestigationError(
            f"Unknown model field(s): {sorted(unknown_model)}."
        )
    model_id = model_payload.get("id", "unknown")
    if not isinstance(model_id, str) or len(model_id) > 200:
        raise InvestigationError("model.id must be a string of at most 200 chars.")
    model_source = model_payload.get("source", "session-reported")
    if model_source not in MODEL_SOURCES:
        raise InvestigationError(
            f"model.source must be one of {sorted(MODEL_SOURCES)}."
        )
    if not model_id.strip() or model_id.strip().lower() == "unknown":
        model_id = "unknown"
        model_source = "unavailable"
    return driver, {"id": model_id, "source": model_source}


def _validate_label(value: Any, default: str = "investigation") -> str:
    label = value if value is not None else default
    if not isinstance(label, str) or not label.strip() or len(label) > 80:
        raise InvestigationError("label must be a string of 1-80 characters.")
    return label


@dataclass
class ServiceConfig:
    state_file: Path
    run_root: Path = Path("runs")
    port: int = 0
    idle_timeout: float = 900.0
    session_idle_timeout: float = 300.0
    headless: bool = True
    request_timeout: float = 180.0

    def validate(self) -> None:
        if not (0 <= self.port <= 65535):
            raise AgentServiceError("port must be between 0 and 65535.")
        if self.idle_timeout <= 0:
            raise AgentServiceError("idle timeout must be positive.")
        if self.session_idle_timeout <= 0:
            raise AgentServiceError("session idle timeout must be positive.")
        if self.request_timeout <= 0:
            raise AgentServiceError("request timeout must be positive.")


@dataclass
class _Task:
    fn: Callable[[], Any]
    done: threading.Event = field(default_factory=threading.Event)
    value: Any = None
    error: BaseException | None = None


class AgentService:
    """Owns the browser and the live investigations."""

    def __init__(self, config: ServiceConfig) -> None:
        config.validate()
        self.config = config
        self.token = secrets.token_urlsafe(32)
        self.sessions: dict[str, Investigation] = {}
        self.explorations: dict[str, Exploration] = {}
        self.host: PlaywrightHost | None = None
        self.last_activity = time.monotonic()
        self.stop_event = threading.Event()
        self._queue: "queue.Queue[_Task]" = queue.Queue()
        self._worker_stop = False
        self._worker = threading.Thread(
            target=self._worker_loop, name="bugrepro-agent-worker", daemon=True
        )

    # ------------------------------------------------------------ plumbing
    def start_worker(self) -> None:
        self._worker.start()

    def touch(self) -> None:
        self.last_activity = time.monotonic()

    def submit(self, fn: Callable[[], Any], *, timeout: float | None = None) -> Any:
        """Run a callable on the browser worker thread and wait for it."""
        if self._worker_stop or not self._worker.is_alive():
            raise AgentServiceError(
                "The agent service is shutting down.",
                http_status=503,
                code="service_stopping",
            )
        task = _Task(fn=fn)
        self._queue.put(task)
        if not task.done.wait(timeout or self.config.request_timeout):
            raise AgentServiceError(
                "The agent service did not answer in time.",
                http_status=504,
                code="service_timeout",
            )
        if task.error is not None:
            raise task.error
        return task.value

    def _worker_loop(self) -> None:
        while not self._worker_stop:
            try:
                task = self._queue.get(timeout=0.25)
            except queue.Empty:
                self._idle_check()
                continue
            try:
                task.value = task.fn()
            except BaseException as exc:  # noqa: BLE001 - reported to the caller
                task.error = exc
            finally:
                task.done.set()
            self._idle_check()

    def _idle_check(self) -> None:
        now = time.monotonic()
        for session_id, investigation in list(self.sessions.items()):
            if investigation.closed:
                self.sessions.pop(session_id, None)
                continue
            if now - investigation.last_used > self.config.session_idle_timeout:
                try:
                    investigation.close("Session idle timeout.")
                except Exception:  # noqa: BLE001 - cleanup is best effort
                    pass
                self.sessions.pop(session_id, None)
        if not self.sessions and now - self.last_activity > self.config.idle_timeout:
            self.begin_shutdown()

    def begin_shutdown(self) -> dict[str, Any]:
        """Close every session and the browser. Runs on the worker thread."""
        if self._worker_stop:
            return {"status": "ok", "stopping": True}
        for exploration in list(self.explorations.values()):
            try:
                exploration.close("Service shutdown.")
            except Exception:  # noqa: BLE001 - cleanup is best effort
                pass
        self.explorations.clear()
        for session_id, investigation in list(self.sessions.items()):
            try:
                investigation.close("Service shutdown.")
            except Exception:  # noqa: BLE001 - cleanup is best effort
                pass
            self.sessions.pop(session_id, None)
        if self.host is not None:
            self.host.close()
            self.host = None
        self._worker_stop = True
        self.stop_event.set()
        return {"status": "ok", "stopping": True}

    # ------------------------------------------------------------- browser
    def _ensure_host(self) -> PlaywrightHost:
        if self.host is not None:
            if self.host.is_connected():
                return self.host
            # The browser died: mark every live session as crashed, then
            # replace it with a fresh browser for future investigations.
            for investigation in list(self.sessions.values()):
                investigation.mark_crashed(
                    "The browser process disconnected."
                )
            self.host.close()
            self.host = None

        host = PlaywrightHost(headless=self.config.headless)
        try:
            host.start()
        except BrowserStartupError as exc:
            raise InvestigationError(
                str(exc), code="browser_startup", http_status=503
            ) from exc
        self.host = host
        return host

    # -------------------------------------------------------- investigations
    def _get(self, session_id: str) -> Investigation:
        investigation = self.sessions.get(session_id)
        if investigation is None:
            raise AgentServiceError(
                f"Unknown or closed session: {session_id}.",
                http_status=404,
                code="unknown_session",
            )
        return investigation

    def start_investigation(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.touch()
        if not isinstance(payload, dict):
            raise InvestigationError("The request body must be a JSON object.")
        unknown = set(payload) - ALLOWED_START_FIELDS
        if unknown:
            raise InvestigationError(
                f"Unknown field(s) in the start request: {sorted(unknown)}."
            )

        try:
            target = validate_base_url(payload.get("target"))
        except TargetError as exc:
            raise InvestigationError(str(exc)) from exc

        bug_report = payload.get("bug_report")
        if not isinstance(bug_report, str) or not bug_report.strip():
            raise InvestigationError("bug_report must be a non-empty string.")
        if len(bug_report) > MAX_BUG_REPORT_CHARS:
            raise InvestigationError(
                f"bug_report must be at most {MAX_BUG_REPORT_CHARS} characters."
            )

        try:
            expectation = parse_expectation(payload.get("expectation"))
        except ExpectationError as exc:
            raise InvestigationError(f"expectation: {exc}") from exc

        driver, model = _parse_driver_and_model(payload)
        label = _validate_label(payload.get("label"))

        budget_limit = payload.get("action_budget", 20)
        if isinstance(budget_limit, bool) or not isinstance(budget_limit, int):
            raise InvestigationError("action_budget must be an integer.")
        if not (1 <= budget_limit <= 50):
            raise InvestigationError("action_budget must be between 1 and 50.")

        timeout_ms = payload.get("timeout_ms", 10_000)
        if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int):
            raise InvestigationError("timeout_ms must be an integer.")
        if not (500 <= timeout_ms <= 60_000):
            raise InvestigationError("timeout_ms must be between 500 and 60000.")

        host = self._ensure_host()
        session_id = secrets.token_urlsafe(12)
        while session_id in self.sessions:  # pragma: no cover - collision is unlikely
            session_id = secrets.token_urlsafe(12)

        investigation = Investigation.start(
            session_id=session_id,
            base_url=target,
            bug_report=bug_report,
            expectation=expectation,
            driver=driver,
            model=model,
            budget_limit=budget_limit,
            timeout_ms=timeout_ms,
            run_root=self.config.run_root,
            label=label,
            headless=self.config.headless,
            host=host,
        )
        self.sessions[session_id] = investigation
        return {
            "status": "ok",
            "session_id": session_id,
            "run_id": investigation.run_dir.name,
            "run_dir": str(investigation.run_dir),
            "report": str(investigation.run_dir / "report.md"),
            "attempt_status": investigation.attempt_status,
            "driver": driver,
            "model": model,
            "base_url": target,
            "started_at": investigation.started_at,
            "expectation": {
                **expectation.to_dict(),
                "fingerprint": expectation.fingerprint(),
                "frozen_at": investigation.started_at,
            },
            "budget": investigation.budget(),
            "evidence": [],
        }

    def observe(self, session_id: str) -> dict[str, Any]:
        self.touch()
        return self._get(session_id).observe()

    def act(self, session_id: str, body: Any) -> dict[str, Any]:
        """Execute one action.

        Accepts either a bare action object (backwards compatible) or
        ``{"action": {...}, "rationale": "..."}``. A rationale is optional
        recorded evidence; it never influences validation or the check.
        """
        self.touch()
        investigation = self._get(session_id)
        if not isinstance(body, dict):
            raise InvestigationError("The action request must be a JSON object.")

        if "action" in body:
            unknown = set(body) - {"action", "rationale"}
            if unknown:
                raise InvestigationError(
                    f"Unknown field(s) in the action request: {sorted(unknown)}."
                )
            action_payload = body["action"]
            rationale = _validate_rationale(body.get("rationale"))
        else:
            action_payload = body
            rationale = None

        try:
            action = parse_action(action_payload)
        except RecipeError as exc:
            raise InvestigationError(str(exc)) from exc
        return investigation.execute_action(action, rationale)

    def finish(self, session_id: str) -> dict[str, Any]:
        self.touch()
        return self._get(session_id).finish()

    def export_recipe(self, session_id: str) -> dict[str, Any]:
        self.touch()
        return self._get(session_id).export_recipe()

    def status(self, session_id: str) -> dict[str, Any]:
        self.touch()
        return self._get(session_id).status()

    def close(self, session_id: str) -> dict[str, Any]:
        self.touch()
        investigation = self._get(session_id)
        result = investigation.close("Closed by client.")
        self.sessions.pop(session_id, None)
        return result

    # ---------------------------------------------------------- explorations
    def _get_exploration(self, exploration_id: str) -> Exploration:
        exploration = self.explorations.get(exploration_id)
        if exploration is None:
            raise AgentServiceError(
                f"Unknown exploration: {exploration_id}.",
                http_status=404,
                code="unknown_exploration",
            )
        return exploration

    def start_exploration(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.touch()
        if not isinstance(payload, dict):
            raise ExplorationError("The request body must be a JSON object.")
        unknown = set(payload) - ALLOWED_EXPLORATION_START_FIELDS
        if unknown:
            raise ExplorationError(
                f"Unknown field(s) in the exploration request: {sorted(unknown)}."
            )
        raw_spec = payload.get("spec")
        if not isinstance(raw_spec, dict):
            raise ExplorationError("spec must be a JSON object.")
        target_override = payload.get("target_override")
        if target_override is not None and not isinstance(target_override, str):
            raise ExplorationError("target_override must be a string when provided.")

        spec = parse_exploration_spec(raw_spec, target_override=target_override)
        driver, model = _parse_driver_and_model(payload)
        label = _validate_label(payload.get("label"), default="exploration")

        spec_target = raw_spec.get("target")
        override_meta = {
            "used": target_override is not None,
            "spec_target": spec_target if isinstance(spec_target, str) else None,
            "actual_target": spec.target,
        }

        exploration_id = secrets.token_urlsafe(10)
        while exploration_id in self.explorations:  # pragma: no cover
            exploration_id = secrets.token_urlsafe(10)

        exploration = Exploration.start(
            exploration_id=exploration_id,
            spec=spec,
            run_root=self.config.run_root,
            label=label,
            driver=driver,
            model=model,
            target_override=override_meta,
        )
        self.explorations[exploration_id] = exploration
        return exploration.status_payload()

    def exploration_status(self, exploration_id: str) -> dict[str, Any]:
        self.touch()
        return self._get_exploration(exploration_id).status_payload()

    def start_scenario(
        self, exploration_id: str, payload: Any
    ) -> dict[str, Any]:
        self.touch()
        exploration = self._get_exploration(exploration_id)
        host = self._ensure_host()
        session_id = secrets.token_urlsafe(12)
        while session_id in self.sessions:  # pragma: no cover
            session_id = secrets.token_urlsafe(12)

        scenario_payload, investigation, closed_sessions = exploration.start_scenario(
            payload,
            host=host,
            session_id=session_id,
            headless=self.config.headless,
            timeout_ms=10_000,
        )
        for closed_session_id in closed_sessions:
            self.sessions.pop(closed_session_id, None)
        self.sessions[session_id] = investigation
        return scenario_payload

    def finish_scenario(
        self, exploration_id: str, scenario_id: str
    ) -> dict[str, Any]:
        self.touch()
        return self._get_exploration(exploration_id).finish_scenario(scenario_id)

    def export_scenario(
        self, exploration_id: str, scenario_id: str
    ) -> dict[str, Any]:
        self.touch()
        return self._get_exploration(exploration_id).export_scenario(scenario_id)

    def confirm_scenario(
        self, exploration_id: str, scenario_id: str
    ) -> dict[str, Any]:
        self.touch()
        host = self._ensure_host()
        return self._get_exploration(exploration_id).confirm_scenario(
            scenario_id,
            host=host,
            timeout_ms=10_000,
        )

    def close_scenario(
        self, exploration_id: str, scenario_id: str
    ) -> dict[str, Any]:
        self.touch()
        result = self._get_exploration(exploration_id).close_scenario(scenario_id)
        self.sessions.pop(result.get("closed_session_id"), None)
        return result

    def finish_exploration(self, exploration_id: str) -> dict[str, Any]:
        self.touch()
        exploration = self._get_exploration(exploration_id)
        result = exploration.finish()
        for scenario in exploration.scenarios:
            self.sessions.pop(scenario.session_id, None)
        return result


class _ServiceHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    service: AgentService


class _Handler(BaseHTTPRequestHandler):
    server_version = "BugReproAgent/0.2"

    # ------------------------------------------------------------ helpers
    @property
    def service(self) -> AgentService:
        return self.server.service  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:
        """Stay quiet: the service prints structured output, not request logs."""

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def _error(self, status: int, message: str, code: str) -> None:
        self._send_json(status, {"status": "error", "error": message, "code": code})

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return False
        return secrets.compare_digest(header[7:], self.service.token)

    def _read_body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = 0
        if length < 0 or length > MAX_BODY_BYTES:
            raise AgentServiceError("Request body is too large.", http_status=413)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AgentServiceError(f"Request body is not valid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise AgentServiceError("Request body must be a JSON object.")
        return parsed

    def _require_empty_body(self, body: dict[str, Any]) -> None:
        if body:
            raise AgentServiceError(
                "This endpoint does not accept any fields.",
                http_status=400,
                code="unexpected_fields",
            )

    def _dispatch(self, method: str) -> None:
        path = urlsplit(self.path).path

        if method == "GET" and path == "/health":
            self._send_json(
                HTTPStatus.OK,
                {"status": "ok", "service": SERVICE_NAME, "protocol": SERVICE_PROTOCOL},
            )
            return

        if not self._authorized():
            self._error(HTTPStatus.UNAUTHORIZED, "Unauthorized.", "unauthorized")
            return

        try:
            body = self._read_body() if method == "POST" else {}
            if method == "GET":
                self._handle_get(path)
            else:
                self._handle_post(path, body)
        except InvestigationError as exc:
            self._error(exc.http_status, str(exc), exc.code)
        except AgentServiceError as exc:
            self._error(exc.http_status, str(exc), exc.code)
        except RecipeError as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc), "invalid_action")
        except Exception as exc:  # noqa: BLE001 - never leak a traceback
            self._error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                f"Internal agent service error: {type(exc).__name__}: {exc}",
                "internal_error",
            )

    def _handle_get(self, path: str) -> None:
        match = re.fullmatch(r"/sessions/([A-Za-z0-9_-]{1,64})", path)
        if match:
            self._send_json(
                HTTPStatus.OK,
                self.service.submit(lambda: self.service.status(match[1])),
            )
            return
        match = re.fullmatch(r"/explorations/([A-Za-z0-9_-]{1,64})", path)
        if match:
            self._send_json(
                HTTPStatus.OK,
                self.service.submit(
                    lambda: self.service.exploration_status(match[1])
                ),
            )
            return
        self._error(HTTPStatus.NOT_FOUND, "Unknown endpoint.", "not_found")

    def _handle_post(self, path: str, body: dict[str, Any]) -> None:
        if path == "/shutdown":
            self._require_empty_body(body)
            self._send_json(
                HTTPStatus.OK,
                self.service.submit(self.service.begin_shutdown),
            )
            return

        if path == "/sessions":
            self._send_json(
                HTTPStatus.CREATED,
                self.service.submit(lambda: self.service.start_investigation(body)),
            )
            return

        if path == "/explorations":
            self._send_json(
                HTTPStatus.CREATED,
                self.service.submit(lambda: self.service.start_exploration(body)),
            )
            return

        match = re.fullmatch(
            r"/explorations/([A-Za-z0-9_-]{1,64})/finish", path
        )
        if match:
            self._require_empty_body(body)
            self._send_json(
                HTTPStatus.OK,
                self.service.submit(
                    lambda: self.service.finish_exploration(match[1])
                ),
            )
            return

        match = re.fullmatch(
            r"/explorations/([A-Za-z0-9_-]{1,64})/scenarios", path
        )
        if match:
            self._send_json(
                HTTPStatus.CREATED,
                self.service.submit(
                    lambda: self.service.start_scenario(match[1], body)
                ),
            )
            return

        match = re.fullmatch(
            r"/explorations/([A-Za-z0-9_-]{1,64})/scenarios/"
            r"([A-Za-z0-9_-]{1,32})/(finish|export|confirm|close)",
            path,
        )
        if match:
            self._require_empty_body(body)
            exploration_id, scenario_id, action = match[1], match[2], match[3]
            if action == "finish":
                payload = self.service.submit(
                    lambda: self.service.finish_scenario(
                        exploration_id, scenario_id
                    )
                )
            elif action == "export":
                payload = self.service.submit(
                    lambda: self.service.export_scenario(
                        exploration_id, scenario_id
                    )
                )
            elif action == "confirm":
                payload = self.service.submit(
                    lambda: self.service.confirm_scenario(
                        exploration_id, scenario_id
                    )
                )
            else:
                payload = self.service.submit(
                    lambda: self.service.close_scenario(
                        exploration_id, scenario_id
                    )
                )
            self._send_json(HTTPStatus.OK, payload)
            return

        match = re.fullmatch(
            r"/sessions/([A-Za-z0-9_-]{1,64})/(observe|action|finish|export|close)",
            path,
        )
        if not match:
            self._error(HTTPStatus.NOT_FOUND, "Unknown endpoint.", "not_found")
            return

        session_id, action = match[1], match[2]
        if action == "action":
            self._send_json(
                HTTPStatus.OK,
                self.service.submit(
                    lambda: self.service.act(session_id, body)
                ),
            )
            return

        self._require_empty_body(body)
        if action == "observe":
            payload = self.service.submit(
                lambda: self.service.observe(session_id)
            )
        elif action == "finish":
            payload = self.service.submit(
                lambda: self.service.finish(session_id)
            )
        elif action == "export":
            payload = self.service.submit(
                lambda: self.service.export_recipe(session_id)
            )
        else:
            payload = self.service.submit(
                lambda: self.service.close(session_id)
            )
        self._send_json(HTTPStatus.OK, payload)

    # ------------------------------------------------------------- methods
    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._dispatch("POST")


def _write_state_file(path: Path, url: str, token: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "protocol": SERVICE_PROTOCOL,
        "service": SERVICE_NAME,
        "url": url,
        "token": token,
        "pid": os.getpid(),
        "started_at": time.time(),
    }
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    try:
        os.chmod(temporary, 0o600)
    except OSError:  # pragma: no cover - best effort on Windows
        pass
    os.replace(temporary, path)
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - best effort on Windows
        pass


def _remove_state_file(path: Path, token: str) -> None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if data.get("token") == token:
        try:
            path.unlink()
        except OSError:  # pragma: no cover - best effort
            pass


def _service_is_running(state_file: Path) -> bool:
    """True when a healthy service already owns this state file."""
    try:
        data = json.loads(state_file.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return False
    url = data.get("url")
    if not isinstance(url, str):
        return False
    import urllib.request

    try:
        with urllib.request.urlopen(url.rstrip("/") + "/health", timeout=1) as response:
            return response.status == 200
    except Exception:  # noqa: BLE001 - an unreachable URL means "not running"
        return False


def run_service(config: ServiceConfig) -> None:
    """Run the agent service in the foreground until it is stopped."""
    config.validate()
    state_file = Path(config.state_file)
    if _service_is_running(state_file):
        raise AgentServiceError(
            "An agent service is already running for this state file.",
            http_status=409,
            code="already_running",
        )

    service = AgentService(config)
    server = _ServiceHTTPServer(("127.0.0.1", config.port), _Handler)
    server.service = service
    host, port = server.server_address[0], server.server_address[1]
    url = f"http://{host}:{port}"

    service.start_worker()
    _write_state_file(state_file, url, service.token)

    def _watch_for_shutdown() -> None:
        service.stop_event.wait()
        server.shutdown()

    watcher = threading.Thread(
        target=_watch_for_shutdown, name="bugrepro-agent-watchdog", daemon=True
    )

    print(f"Bug Repro agent service listening on {url}", flush=True)
    print(f"State file: {state_file}", flush=True)

    watcher.start()
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        if not service.stop_event.is_set():
            try:
                service.submit(service.begin_shutdown, timeout=20)
            except Exception:  # noqa: BLE001 - shutting down anyway
                pass
        service.stop_event.wait(5)
        try:
            server.server_close()
        except Exception:  # noqa: BLE001 - best effort
            pass
        _remove_state_file(state_file, service.token)
        service._worker.join(timeout=5)
