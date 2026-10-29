"""Codex CLI decision adapter. The CLI alone manages its authentication.

Known tools are disabled and any reported tool event is rejected. This is not
a universal tool-isolation guarantee across CLI releases or managed policies.
"""

from __future__ import annotations

import json
import math
import os
import platform
import shutil
import signal
import subprocess
import tempfile
import sys
from pathlib import Path
from typing import Any

from .model_driver import ModelError, SYSTEM_PROMPT, parse_decision
from .recipe import FILLABLE_ROLES, ROLE_NAMES


def _object(properties: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": False}


def decision_schema() -> dict[str, Any]:
    """The final executor validator remains authoritative."""
    def locator(roles):
        return _object({"role": {"type": "string", "enum": sorted(roles)},
                        "name": {"type": "string", "minLength": 1, "maxLength": 200},
                        "exact": {"type": "boolean"}})

    actions = [
        _object({"type": {"const": "goto"}, "path": {"type": "string", "pattern": "^/", "maxLength": 2048}}),
        _object({"type": {"const": "click"}, "locator": locator(ROLE_NAMES)}),
        _object({"type": {"const": "fill"}, "locator": locator(FILLABLE_ROLES),
                 "text": {"type": "string", "maxLength": 500}}),
        _object({"type": {"const": "reload"}}),
    ]
    for kind in ("observe", "screenshot"):
        actions.append(_object({"type": {"const": kind}, "label": {
            "type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$"}}))
    return _object({"decision": {"anyOf": [
        _object({"action": {"anyOf": actions}}), _object({"finish": {"const": True}})
    ]}})


# All feature names below were present in `codex features list` on the tested
# CLI. Unknown/removed controls fail preflight instead of being silently ignored.
DISABLED_FEATURES = (
    "shell_tool", "shell_snapshot", "apps", "plugins", "remote_plugin",
    "multi_agent", "browser_use", "browser_use_external", "browser_use_full_cdp_access",
    "computer_use", "code_mode", "code_mode_host", "image_generation", "view_image",
    "hooks", "memories", "goals", "workspace_dependencies", "tool_suggest",
)
RESTRICTIONS = (
    "--ignore-user-config", "--ignore-rules", "--strict-config",
    "--sandbox", "read-only", "-c", 'approval_policy="never"',
    "-c", 'web_search="disabled"',
    "-c", "project_doc_max_bytes=0",
)
REQUIRED_OPTIONS = ("--output-schema", "--json", "--ephemeral",
                    "--ignore-user-config", "--ignore-rules", "--strict-config", "--sandbox")
MAX_OUTPUT_BYTES = 1024 * 1024
# The tested CLI emits this startup notice as item.error when our intentional
# host-disable restriction takes effect. It precedes inference, not a failed
# decision. Match the complete notice and its position; other errors stay fatal.
CODE_MODE_DISABLED_NOTICE = (
    "Code Mode is unavailable because code-mode host is disabled. "
    "Code mode will fail closed; enable `features.code_mode_host` and install "
    "`codex-code-mode-host`."
)


def _stop_process(process: subprocess.Popen) -> None:
    """Kill this decision's process tree and reap the CLI on cancellation."""
    if os.name == "nt":
        taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe"
        try:
            subprocess.run([str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=5, check=False, creationflags=subprocess.CREATE_NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        process.kill()
    process.communicate(timeout=5)


def _run_cli(arguments: list[str], *, cwd: str, timeout: float,
             prompt: str | None = None) -> tuple[int, str, str]:
    options = ({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt"
               else {"start_new_session": True})
    try:
        process = subprocess.Popen(arguments, cwd=cwd, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace", shell=False, **options)
    except FileNotFoundError:
        raise ModelError("Codex CLI is missing; install the official CLI and put it on PATH.") from None
    except OSError:
        raise ModelError("Could not start Codex CLI.") from None
    try:
        stdout, stderr = process.communicate(input=prompt, timeout=timeout)
    except subprocess.TimeoutExpired:
        _stop_process(process)
        raise ModelError("Codex CLI subprocess timed out.") from None
    except KeyboardInterrupt:
        _stop_process(process)
        raise ModelError("Codex CLI decision was cancelled.") from None
    if len(stdout.encode("utf-8")) + len(stderr.encode("utf-8")) > MAX_OUTPUT_BYTES:
        raise ModelError("Codex CLI output exceeded the size limit.")
    return process.returncode, stdout, stderr


def _failure(text: str) -> ModelError:
    """Classify CLI failures without returning raw diagnostics or credentials."""
    lowered = text.lower()
    if any(word in lowered for word in ("quota", "usage limit", "rate limit", "429", "credits")):
        return ModelError("Codex CLI quota or rate limit prevented the decision.")
    if any(word in lowered for word in ("not logged in", "authentication", "unauthorized", "401", "login")):
        return ModelError("Codex CLI authentication is unavailable; use 'codex login' locally.")
    if "configuration" in lowered and any(word in lowered for word in ("unknown", "invalid", "unsupported")):
        return ModelError("Codex CLI rejected required restriction configuration.")
    return ModelError("Codex CLI failed; configuration, restrictions, provider access or execution was unsuccessful.")


def _event_failure(text: str) -> ModelError:
    error = _failure(text)
    if str(error).startswith("Codex CLI failed;"):
        return ModelError("Codex CLI emitted an error event; decision rejected even if final output follows.")
    return error


def _parse_events(stdout: str, *, validator=parse_decision) -> Any:
    final = None
    completed = False
    turn_started = False
    disabled_notice_seen = False
    try:
        for line in stdout.splitlines():
            event = json.loads(line)
            kind = event["type"]
            if kind in {"error", "turn.failed"}:
                raise _event_failure(line)
            if kind == "turn.started":
                turn_started = True
                continue
            if kind == "thread.started":
                continue
            if kind == "turn.completed":
                completed = True
                continue
            if kind not in {"item.started", "item.updated", "item.completed"}:
                raise ModelError("Codex CLI returned an unsupported event; decision rejected.")
            item = event["item"]
            if item["type"] not in {"agent_message", "reasoning"}:
                if item["type"] == "error":
                    if (not turn_started and not disabled_notice_seen
                            and kind == "item.completed"
                            and item.get("message") == CODE_MODE_DISABLED_NOTICE):
                        disabled_notice_seen = True
                        continue
                    raise _event_failure(json.dumps(item))
                raise ModelError("Codex CLI reported a tool or other non-decision event; decision rejected.")
            if kind == "item.completed" and item["type"] == "agent_message":
                final = item["text"]
        if not completed or final is None:
            raise ValueError
        wrapped = json.loads(final)
        if not isinstance(wrapped, dict) or set(wrapped) != {"decision"}:
            raise ValueError
    except ModelError:
        raise
    except (ValueError, KeyError, TypeError):
        raise ModelError("Codex CLI returned malformed or incomplete structured output.") from None
    return validator(wrapped["decision"])


def _resolve_executable() -> str:
    executable = shutil.which("codex")
    if executable is None:
        raise ModelError("Codex CLI is missing; install the official CLI and put it on PATH.")
    entry = Path(executable)
    wrapper = entry.suffix.lower() in {".cmd", ".bat", ".ps1"}
    if sys.platform == "win32" and (wrapper or not entry.suffix):
        # npm shims sit beside node_modules. Inspect package data only; never
        # execute/interpret the shim or launch Node to discover its binary.
        package = entry.parent / "node_modules" / "@openai" / "codex"
        try:
            metadata = json.loads((package / "package.json").read_text(encoding="utf-8"))
            if metadata.get("name") != "@openai/codex":
                raise ValueError
        except (OSError, ValueError, AttributeError):
            raise ModelError("Codex CLI must resolve to its official native executable; no supported npm package was found beside the shell wrapper.") from None
        targets = {"amd64": ("x64", "x86_64"), "x86_64": ("x64", "x86_64"),
                   "arm64": ("arm64", "aarch64"), "aarch64": ("arm64", "aarch64")}
        target = targets.get(platform.machine().lower())
        if target is None:
            raise ModelError("Codex CLI npm discovery does not support this Windows architecture.")
        arch, triple_arch = target
        relative = Path("vendor") / f"{triple_arch}-pc-windows-msvc" / "bin" / "codex.exe"
        platform_package = f"codex-win32-{arch}"
        # Current nested dependency, npm-hoisted dependency, older bundled vendor.
        candidates = (
            package / "node_modules" / "@openai" / platform_package / relative,
            package.parent / platform_package / relative,
            package / relative,
        )
        for candidate in candidates:
            if candidate.is_file():
                return str(candidate.resolve())
        raise ModelError("Codex CLI npm package is missing its native Windows binary; repair the official npm installation locally.")
    if wrapper:
        raise ModelError("Codex CLI must resolve to its official native executable, not a shell wrapper.")
    return executable


class CodexClient:
    def __init__(self, *, model: str | None = None, timeout: float = 60) -> None:
        if not math.isfinite(timeout) or not 0 < timeout <= 120:
            raise ModelError("Codex CLI timeout must be finite and at most 120 seconds.")
        if model is not None and (not model.strip() or len(model) > 200):
            raise ModelError("Requested Codex model must contain 1-200 characters.")
        self.executable = _resolve_executable()
        self.model = model or "unknown"
        self.model_source = "session-reported" if model else "unavailable"
        self.timeout = timeout
        self._ready = False

    def reject_secret(self, value: Any) -> None:
        # No credential files or API-key environment variables are read by this adapter.
        # Credentials are managed inside the CLI; its raw diagnostics are never recorded.
        pass

    def _preflight(self, cwd: str) -> None:
        code, help_text, _ = _run_cli([self.executable, "exec", "--help"], cwd=cwd, timeout=10)
        if code or any(option not in help_text for option in REQUIRED_OPTIONS):
            raise ModelError("Installed Codex CLI lacks the required non-interactive restrictions or structured output options.")
        code, features, _ = _run_cli([self.executable, "features", "list"], cwd=cwd, timeout=10)
        available = {parts[0] for line in features.splitlines()
                     if len(parts := line.split()) >= 3 and parts[1] != "removed"}
        if code or not set(DISABLED_FEATURES) <= available:
            raise ModelError("Installed Codex CLI lacks the required tool-disable controls.")
        code, _, _ = _run_cli([self.executable, "login", "status"], cwd=cwd, timeout=10)
        if code:
            raise ModelError("Codex CLI authentication is unavailable; use 'codex login' locally.")
        self._ready = True

    def choose(self, context: dict[str, Any]) -> dict[str, Any]:
        return self._request(context, SYSTEM_PROMPT, decision_schema(), parse_decision,
            '{"decision":{"action":ACTION}} or {"decision":{"finish":true}}')

    def propose_expectation(self, context: dict[str, Any]):
        from .expectation_proposal import PROPOSAL_PROMPT, parse_proposal, proposal_schema

        return self._request(context, PROPOSAL_PROMPT,
            _object({"decision": proposal_schema()}), parse_proposal,
            '{"decision":{"proposal":EXPECTATION}} or {"decision":{"proposal":null}}')

    def _request(self, context, system_prompt, output_schema, validator, output_format):
        self.reject_secret(context)
        # A fresh empty cwd prevents this task, repository AGENTS, and project
        # configuration from being picked up by the decision-only invocation.
        with tempfile.TemporaryDirectory(prefix="bugrepro-codex-") as cwd:
            if not self._ready:
                self._preflight(cwd)
            schema = Path(cwd) / "decision-schema.json"
            schema.write_text(json.dumps(output_schema), encoding="utf-8")
            arguments = [self.executable, "exec", *RESTRICTIONS, "--skip-git-repo-check",
                         "--ephemeral", "--json", "--color", "never", "--output-schema", str(schema)]
            for feature in DISABLED_FEATURES:
                arguments.extend(["--disable", feature])
            if self.model != "unknown":
                arguments.extend(["--model", self.model])
            arguments.append("-")
            prompt = (system_prompt + "\nFor Codex structured output, wrap that object in a single "
                "top-level decision field: " + output_format + ". Do not use tools, commands, files, browsers, "
                "skills, subagents, or integrations. All context needed is below.\n"
                + json.dumps(context, ensure_ascii=False))
            code, stdout, stderr = _run_cli(arguments, cwd=cwd, timeout=self.timeout, prompt=prompt)
            if code:
                raise _failure(stderr + "\n" + stdout)
            return _parse_events(stdout, validator=validator)
