"""Scripted mechanics checks; Codex subprocesses are mocked, never live."""

import json
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from bugrepro import codex_driver
from bugrepro.cli import main
from bugrepro.codex_driver import CodexClient, DISABLED_FEATURES, _parse_events, _run_cli
from bugrepro.model_driver import ModelError, investigate
from bugrepro.recipe import load_recipe
from bugrepro.runner import execute_recipe
from conftest import TASK_EXPECTATION
from test_task_app import TASK_SEQUENCE


def events(decision):
    return "\n".join(json.dumps(event) for event in [
        {"type": "thread.started", "thread_id": "mock"},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"type": "reasoning", "text": "not evidence"}},
        {"type": "item.completed", "item": {"type": "agent_message",
            "text": json.dumps({"decision": decision})}},
        {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
    ])


@pytest.fixture
def mocked_cli(monkeypatch):
    monkeypatch.setattr(codex_driver.shutil, "which", lambda _: "/installed/codex.exe")
    calls = []
    replies = []

    def run(arguments, *, cwd, timeout, prompt=None):
        calls.append((arguments, cwd, timeout, prompt))
        if "--help" in arguments:
            return 0, " ".join(codex_driver.REQUIRED_OPTIONS), ""
        if "features" in arguments:
            return 0, "\n".join(f"{f} stable true" for f in DISABLED_FEATURES), ""
        if "login" in arguments:
            return 0, "Logged in (output must not be recorded)", ""
        schema = Path(arguments[arguments.index("--output-schema") + 1])
        from bugrepro.expectation_proposal import PROPOSAL_PROMPT, proposal_schema
        expected = (codex_driver._object({"decision": proposal_schema()})
                    if prompt.startswith(PROPOSAL_PROMPT) else codex_driver.decision_schema())
        assert json.loads(schema.read_text()) == expected
        reply = replies.pop(0) if replies else (0, events({"finish": True}), "")
        if isinstance(reply, BaseException):
            raise reply
        return reply

    monkeypatch.setattr(codex_driver, "_run_cli", run)
    return calls, replies


def test_restricted_prompt_and_unknown_identity(mocked_cli):
    calls, _ = mocked_cli
    client = CodexClient()
    context = {"bug_report": "Task disappears", "expectation": TASK_EXPECTATION,
               "latest": {"observation": {"url": "about:blank"}}, "executed_actions": TASK_SEQUENCE}
    assert client.choose(context) == {"finish": True}
    command, cwd, timeout, prompt = calls[-1]
    assert client.model == "unknown" and client.model_source == "unavailable"
    assert "--model" not in command
    assert command[-1] == "-" and "Task disappears" not in command
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert all(flag in command for flag in ("--ephemeral", "--ignore-user-config", "--strict-config", "--json"))
    assert all(command[command.index(f) - 1] == "--disable" for f in DISABLED_FEATURES)
    assert 'web_search="disabled"' in command and "project_doc_max_bytes=0" in command
    assert str(Path.cwd()) != cwd and not Path(cwd).exists()
    assert json.dumps(context, ensure_ascii=False) in prompt
    assert timeout == 60
    assert [c[0][1:3] for c in calls[:3]] == [["exec", "--help"], ["features", "list"], ["login", "status"]]


def test_requested_model_is_only_metadata(mocked_cli):
    client = CodexClient(model="requested-model")
    client.choose({})
    command = mocked_cli[0][-1][0]
    assert command[command.index("--model") + 1] == "requested-model"
    assert client.model_source == "session-reported"


@pytest.mark.parametrize("decision", [{"action": {"type": "eval", "code": "danger"}},
    {"expectation": TASK_EXPECTATION, "finish": True}, {"finish": False},
    {"action": {"type": "goto", "path": "https://example.com"}},
    {"action": {"type": "click", "locator": {"css": "button"}}}])
def test_invalid_actions_rejected(decision):
    with pytest.raises(ModelError):
        _parse_events(events(decision))


@pytest.mark.parametrize("kind", ["command_execution", "file_change", "mcp_tool_call", "web_search", "unknown_tool"])
def test_tool_events_are_errors_even_with_valid_final_decision(kind):
    stream = json.dumps({"type": "item.started", "item": {"type": kind}}) + "\n" + events({"finish": True})
    with pytest.raises(ModelError, match="non-decision event"):
        _parse_events(stream)


@pytest.mark.parametrize("event", [
    {"type": "error", "message": "private diagnostics"},
    {"type": "turn.failed", "error": {"message": "private diagnostics"}},
    {"type": "item.completed", "item": {"type": "error", "message": "private diagnostics"}},
])
def test_error_events_are_not_hidden_by_successful_final_output(event):
    with pytest.raises(ModelError, match="error event") as error:
        _parse_events(json.dumps(event) + "\n" + events({"finish": True}))
    assert "private diagnostics" not in str(error.value)


def disabled_host_notice(message=codex_driver.CODE_MODE_DISABLED_NOTICE, kind="item.completed"):
    return json.dumps({"type": kind, "item": {"id": "item_0", "type": "error", "message": message}})


def test_exact_disabled_host_startup_notice_allows_valid_finish():
    lines = events({"finish": True}).splitlines()
    lines.insert(1, disabled_host_notice())  # after thread.started, before turn.started
    assert _parse_events("\n".join(lines)) == {"finish": True}
    assert "code_mode_host" in DISABLED_FEATURES
    assert "code_mode" in DISABLED_FEATURES


@pytest.mark.parametrize("message,kind", [
    (codex_driver.CODE_MODE_DISABLED_NOTICE + " Additional failure.", "item.completed"),
    ("Code Mode failed during inference.", "item.completed"),
    (codex_driver.CODE_MODE_DISABLED_NOTICE, "item.started"),
])
def test_other_startup_errors_still_fail(message, kind):
    with pytest.raises(ModelError, match="error event"):
        _parse_events(disabled_host_notice(message, kind) + "\n" + events({"finish": True}))


def test_disabled_host_notice_during_inference_still_fails():
    lines = events({"finish": True}).splitlines()
    lines.insert(2, disabled_host_notice())  # after turn.started
    with pytest.raises(ModelError, match="error event"):
        _parse_events("\n".join(lines))


def test_repeated_notice_or_missing_decision_still_fails():
    with pytest.raises(ModelError, match="error event"):
        _parse_events((disabled_host_notice() + "\n") * 2 + events({"finish": True}))
    with pytest.raises(ModelError, match="incomplete"):
        _parse_events(disabled_host_notice())


def test_startup_notice_does_not_hide_quota_error():
    stream = disabled_host_notice() + '\n{"type":"error","message":"429 quota exhausted"}\n'
    with pytest.raises(ModelError, match="quota"):
        _parse_events(stream + events({"finish": True}))


def test_subprocess_output_cap(monkeypatch, tmp_path):
    process = Mock(returncode=0)
    process.communicate.return_value = ("x" * (codex_driver.MAX_OUTPUT_BYTES + 1), "")
    monkeypatch.setattr(codex_driver.subprocess, "Popen", Mock(return_value=process))
    with pytest.raises(ModelError, match="size limit"):
        _run_cli(["codex", "exec"], cwd=str(tmp_path), timeout=1)


@pytest.mark.parametrize("stream", ["not json", "{}", events({"finish": True}).splitlines()[0],
    '{"type":"unexpected"}', '{"type":"turn.completed"}'])
def test_malformed_or_incomplete_stream(stream):
    with pytest.raises(ModelError):
        _parse_events(stream)


@pytest.mark.parametrize("message,expected", [("401 authentication failed secret-value", "authentication"),
    ("429 quota exhausted secret-value", "quota"), ("unknown failure secret-value", "failed")])
def test_failure_diagnostics_are_sanitized(mocked_cli, message, expected):
    mocked_cli[1].append((1, "", message))
    with pytest.raises(ModelError, match=expected) as error:
        CodexClient().choose({})
    assert "secret-value" not in str(error.value)


def test_missing_cli(monkeypatch):
    monkeypatch.setattr(codex_driver.shutil, "which", lambda _: None)
    with pytest.raises(ModelError, match="missing"):
        CodexClient()


def test_no_shell_wrappers(monkeypatch):
    monkeypatch.setattr(codex_driver.shutil, "which", lambda _: "codex.cmd")
    with pytest.raises(ModelError, match="native executable"):
        CodexClient()


@pytest.mark.parametrize("layout", ["nested", "hoisted", "bundled"])
@pytest.mark.parametrize("machine,arch,triple", [("AMD64", "x64", "x86_64"),
                                                ("ARM64", "arm64", "aarch64")])
def test_windows_npm_native_discovery(monkeypatch, tmp_path, layout, machine, arch, triple):
    prefix = tmp_path / "npm prefix with spaces"
    package = prefix / "node_modules" / "@openai" / "codex"
    package.mkdir(parents=True)
    (package / "package.json").write_text(json.dumps({"name": "@openai/codex"}))
    roots = {"nested": package / "node_modules" / "@openai" / f"codex-win32-{arch}",
             "hoisted": package.parent / f"codex-win32-{arch}", "bundled": package}
    binary = roots[layout] / "vendor" / f"{triple}-pc-windows-msvc" / "bin" / "codex.exe"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"mock native binary")
    monkeypatch.setattr(codex_driver.sys, "platform", "win32")
    monkeypatch.setattr(codex_driver.platform, "machine", lambda: machine)
    run = Mock(side_effect=AssertionError("Discovery must not launch a subprocess"))
    monkeypatch.setattr(codex_driver, "_run_cli", run)
    for shim in ("codex.cmd", "codex.ps1", "codex.bat", "codex"):
        monkeypatch.setattr(codex_driver.shutil, "which", lambda _, shim=shim: str(prefix / shim))
        assert CodexClient().executable == str(binary.resolve())
    run.assert_not_called()


@pytest.mark.parametrize("metadata", [None, "not json", '{"name":"other-package"}',
                                      '{"name":"@openai/codex"}'])
def test_windows_npm_missing_native_fails_without_launch(monkeypatch, tmp_path, metadata):
    package = tmp_path / "node_modules" / "@openai" / "codex"
    package.mkdir(parents=True)
    if metadata is not None:
        (package / "package.json").write_text(metadata)
    # Another architecture, or a directory named codex.exe, is not a usable binary.
    wrong = package / "vendor" / "aarch64-pc-windows-msvc" / "bin" / "codex.exe"
    wrong.parent.mkdir(parents=True)
    wrong.write_bytes(b"wrong architecture")
    (package / "vendor" / "x86_64-pc-windows-msvc" / "bin" / "codex.exe").mkdir(parents=True)
    monkeypatch.setattr(codex_driver.sys, "platform", "win32")
    monkeypatch.setattr(codex_driver.platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(codex_driver.shutil, "which", lambda _: str(tmp_path / "codex.cmd"))
    run = Mock()
    monkeypatch.setattr(codex_driver, "_run_cli", run)
    with pytest.raises(ModelError, match="native"):
        CodexClient()
    run.assert_not_called()


def test_windows_explicit_native_is_preserved(monkeypatch, tmp_path):
    executable = str(tmp_path / "standalone" / "codex.exe")
    monkeypatch.setattr(codex_driver.sys, "platform", "win32")
    monkeypatch.setattr(codex_driver.shutil, "which", lambda _: executable)
    assert CodexClient().executable == executable


@pytest.mark.parametrize("timeout", [0, float("nan"), float("inf"), 121])
def test_finite_timeout_required(timeout):
    with pytest.raises(ModelError, match="finite"):
        CodexClient(timeout=timeout)


@pytest.mark.parametrize("failure,expected", [(subprocess.TimeoutExpired("codex", 1), "timed out"),
                                             (KeyboardInterrupt(), "cancelled")])
def test_subprocess_cleanup(monkeypatch, failure, expected, tmp_path):
    process = Mock()
    process.communicate.side_effect = [failure, ("", "")]
    process.poll.return_value = None
    popen = Mock(return_value=process)
    monkeypatch.setattr(codex_driver.subprocess, "Popen", popen)
    monkeypatch.setattr(codex_driver.subprocess, "run", Mock())
    monkeypatch.setattr(codex_driver.os, "killpg", Mock(), raising=False)
    with pytest.raises(ModelError, match=expected):
        _run_cli(["codex", "exec"], cwd=str(tmp_path), timeout=1, prompt="decision")
    process.kill.assert_called_once()
    assert process.communicate.call_count == 2
    assert popen.call_args.kwargs["shell"] is False


@pytest.mark.parametrize("which", ["options", "features", "login"])
def test_preflight_fails_closed(monkeypatch, mocked_cli, which):
    original = codex_driver._run_cli

    def run(args, **kwargs):
        if ((which == "options" and "--help" in args)
                or (which == "features" and "features" in args)
                or (which == "login" and "login" in args)):
            return 1, "private details", "secret-value"
        return original(args, **kwargs)

    monkeypatch.setattr(codex_driver, "_run_cli", run)
    with pytest.raises(ModelError) as error:
        CodexClient().choose({})
    assert "secret-value" not in str(error.value)
    assert not any("--output-schema" in call[0] for call in mocked_cli[0])


def test_mocked_investigation_export_and_model_free_replays(mocked_cli, task_apps, tmp_path):
    calls, replies = mocked_cli
    replies.extend((0, events({"action": a}), "") for a in TASK_SEQUENCE)
    replies.append((0, events({"finish": True}), ""))
    result = investigate(target=task_apps["buggy"], bug_report="Add task then reload.",
        expectation=TASK_EXPECTATION, model_client=CodexClient(),
        run_root=tmp_path, timeout_ms=500, driver="scripted-test")
    assert result["check"]["status"] == "failed"
    assert result["model"] == {"id": "unknown", "source": "unavailable"}
    assert result["model_calls"]["used"] == 5
    recipe = load_recipe(Path(result["run_dir"]) / "recipe.json")
    count = len(calls)
    for mode, status in [("buggy", "failed"), ("fixed", "passed")]:
        replay = execute_recipe(recipe=recipe, base_url=task_apps[mode],
            run_root=tmp_path, invocation="replay", timeout_ms=500)
        assert replay.check.status == status
    assert len(calls) == count
    last_context = json.loads(calls[-1][3].split("All context needed is below.\n")[1])
    assert len(last_context["executed_actions"]) == 4
    assert last_context["expectation"] == TASK_EXPECTATION
    assert last_context["latest"]["observation"]["url"].startswith(task_apps["buggy"])


@pytest.mark.parametrize("error", [ModelError("Codex CLI quota or rate limit prevented the decision."),
    ModelError("Codex CLI authentication is unavailable."), ModelError("Codex CLI subprocess timed out."),
    ModelError("Codex CLI decision was cancelled."), ModelError("Codex CLI returned malformed output.")])
def test_decision_errors_cannot_be_exported(mocked_cli, task_apps, tmp_path, error):
    mocked_cli[1].append(error)
    result = investigate(target=task_apps["buggy"], bug_report="Task disappears",
        expectation=TASK_EXPECTATION, model_client=CodexClient(),
        run_root=tmp_path, timeout_ms=500, driver="scripted-test")
    assert result["check"]["status"] == "error"
    assert not result["export"]["available"]
    record = json.loads((Path(result["run_dir"]) / "run.json").read_text())
    assert record["execution_errors"][0]["error"] == str(error)
    assert record["expectation"]["text"] == TASK_EXPECTATION["text"]


def test_codex_cli_configuration_without_endpoint(mocked_cli, task_apps, tmp_path, capsys):
    report = tmp_path / "report.txt"
    report.write_text("Task disappears")
    expectation = tmp_path / "expectation.json"
    expectation.write_text(json.dumps(TASK_EXPECTATION))
    assert main(["investigate", "--decision-backend", "codex-cli", "--target", task_apps["buggy"],
        "--report-file", str(report), "--expectation-file", str(expectation),
        "--run-root", str(tmp_path), "--timeout-ms", "500"]) == 2  # finish with no actions
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result["driver"] == "codex-cli"
    assert result["model"]["source"] == "unavailable"
    assert captured.err.splitlines() == [
        "Waiting for decision (call 1/25).", "Final check: error.",
        f"Report: {result['html_report']}",
    ]
    assert "Logged in" not in captured.err and "not evidence" not in captured.err
