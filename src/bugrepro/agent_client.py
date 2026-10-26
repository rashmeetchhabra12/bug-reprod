"""Client for the persistent local agent service.

The service writes a locally generated bearer token to a state file outside
the repository. This module reads that file so CLI invocations can authenticate
without putting the token in command arguments, console output or Git.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .safety import is_loopback_url


class AgentClientError(RuntimeError):
    """A service request failed, or the local state file is unusable."""

    def __init__(
        self,
        message: str,
        *,
        status: int = 0,
        payload: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.payload = payload or {}


def default_state_path() -> Path:
    """Per-user state path outside the repository."""
    if os.name == "nt":
        base = Path(
            os.environ.get("LOCALAPPDATA")
            or (Path.home() / "AppData" / "Local")
        )
    else:
        base = Path(
            os.environ.get("XDG_STATE_HOME")
            or (Path.home() / ".local" / "state")
        )
    return base / "BugRepro" / "service.json"


def read_state_file(path: str | Path | None = None) -> dict[str, Any]:
    state_path = Path(path) if path is not None else default_state_path()
    try:
        data = json.loads(state_path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise AgentClientError(
            f"Agent service state file not found: {state_path}. "
            "Start the service with 'bugrepro agent serve'."
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise AgentClientError(f"Could not read the agent service state file: {exc}") from exc

    if not isinstance(data, dict):
        raise AgentClientError("Agent service state file is malformed.")
    url = data.get("url")
    token = data.get("token")
    if not isinstance(url, str) or not is_loopback_url(url):
        raise AgentClientError("Agent service URL is missing or not loopback.")
    if not isinstance(token, str) or len(token) < 20:
        raise AgentClientError("Agent service token is missing or malformed.")
    data["url"] = url.rstrip("/")
    data["state_file"] = str(state_path)
    return data


class AgentClient:
    """Small JSON-over-HTTP client; no model calls happen here."""

    def __init__(
        self,
        state_file: str | Path | None = None,
        *,
        timeout: float = 180.0,
    ) -> None:
        state = read_state_file(state_file)
        self.url: str = state["url"]
        self.state_file: str = state["state_file"]
        self._token: str = state["token"]
        self.timeout = timeout

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        data = (
            json.dumps(body, ensure_ascii=False).encode("utf-8")
            if body is not None
            else None
        )
        request = urllib.request.Request(
            self.url + path,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            payload: dict[str, Any] = {}
            try:
                parsed = json.loads(exc.read().decode("utf-8"))
                if isinstance(parsed, dict):
                    payload = parsed
            except Exception:  # noqa: BLE001 - status is enough for the message
                pass
            message = payload.get("error") or f"Agent service returned HTTP {exc.code}."
            raise AgentClientError(message, status=exc.code, payload=payload) from exc
        except urllib.error.URLError as exc:
            raise AgentClientError(
                f"Cannot reach the agent service: {exc.reason}. "
                "Start it with 'bugrepro agent serve'.",
                status=0,
            ) from exc
        except TimeoutError as exc:
            raise AgentClientError("Agent service request timed out.", status=0) from exc

        try:
            parsed = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise AgentClientError("Agent service returned malformed JSON.") from exc
        if not isinstance(parsed, dict):
            raise AgentClientError("Agent service returned an unexpected payload.")
        return parsed

    def start(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/sessions", payload)

    def observe(self, session_id: str) -> dict[str, Any]:
        return self._request("POST", f"/sessions/{session_id}/observe", {})

    def act(self, session_id: str, action: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", f"/sessions/{session_id}/action", action)

    def finish(self, session_id: str) -> dict[str, Any]:
        return self._request("POST", f"/sessions/{session_id}/finish", {})

    def export(self, session_id: str) -> dict[str, Any]:
        return self._request("POST", f"/sessions/{session_id}/export", {})

    def status(self, session_id: str) -> dict[str, Any]:
        return self._request("GET", f"/sessions/{session_id}")

    def close(self, session_id: str) -> dict[str, Any]:
        return self._request("POST", f"/sessions/{session_id}/close", {})

    def shutdown(self) -> dict[str, Any]:
        return self._request("POST", "/shutdown", {})

    def health(self) -> dict[str, Any]:
        """Unauthenticated liveness probe (no secrets in the response)."""
        try:
            with urllib.request.urlopen(self.url + "/health", timeout=3) as response:
                parsed = json.loads(response.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - caller wants a simple probe
            raise AgentClientError(f"Agent service is not healthy: {exc}") from exc
        if not isinstance(parsed, dict):
            raise AgentClientError("Agent service health response is malformed.")
        return parsed