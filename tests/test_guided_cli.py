"""Scripted guided-input mechanics; model responses are mocked."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from bugrepro import cli
from bugrepro.expectations import parse_expectation
from bugrepro.model_driver import ModelError
from bugrepro.recipe import load_recipe
from bugrepro.runner import execute_recipe
from conftest import TASK_EXPECTATION
from test_model_driver import endpoint, KEY
from test_task_app import TASK_SEQUENCE


def answers(monkeypatch, lines):
    iterator = iter(lines)
    def read():
        try:
            return next(iterator)
        except StopIteration:
            raise EOFError
    monkeypatch.setattr('builtins.input', read)


@pytest.fixture
def fake_engine(monkeypatch, tmp_path):
    calls = []
    class Client:
        def reject_secret(self, value):
            if KEY in json.dumps(value):
                raise ModelError('Credential appeared in investigation data; refusing to record it.')
    result = {'run_dir': str(tmp_path / "run with 'quotes'"),
              'html_report': str(tmp_path / 'report.html'), 'check': {'status': 'passed'},
              'export': {'available': True, 'recipe_path': 'recipe.json'}}
    monkeypatch.setattr(cli, '_decision_client', lambda args: (Client(), args.decision_backend))
    def run(args, report, expectation, client, driver):
        calls.append((args, report, expectation, driver))
        return result
    monkeypatch.setattr(cli, '_investigate_request', run)
    return calls, result


@pytest.mark.parametrize('kind,fields,expected', [
    ('1', ['Formatting Syntax', '1'], {'kind': 'heading_text', 'text': 'Formatting Syntax', 'level': 1}),
    ('2', ['Tasks', 'My task'], {'kind': 'region_text_exact', 'region': 'Tasks', 'text': 'My task', 'level': 1}),
])
def test_review_precedes_engine_and_uses_only_user_expectation(monkeypatch, fake_engine, capsys, kind, fields, expected):
    calls, result = fake_engine
    inputs = ['http://127.0.0.1:8110', 'Search and reload.', '.', kind, *fields, '1']
    iterator = iter(inputs)
    def read():
        try:
            return next(iterator)
        except StopIteration:
            output = capsys.readouterr()
            assert not calls  # no investigation/browser/model action before approval
            assert json.dumps(parse_expectation(expected).to_dict(), indent=2) in output.err
            assert parse_expectation(expected).fingerprint() in output.err
            assert 'Freeze this expectation and start?' in output.err
            return 'yes'
    monkeypatch.setattr('builtins.input', read)
    assert cli.main(['guide', '--resource-policy', 'block-external-images']) == 0
    args, report, frozen, driver = calls[0]
    assert frozen == expected and report == 'Search and reload.' and driver == 'codex-cli'
    assert args.action_budget == 20 and args.model_call_limit == 25
    output = capsys.readouterr()
    assert 'Outcome: expectation passed.' in output.out
    assert result['html_report'] in output.out
    assert "''quotes''" in output.out  # PowerShell single quotes escaped
    assert "--resource-policy 'block-external-images'" in output.out


@pytest.mark.parametrize('response', ['n', '', 'anything', None])
def test_decline_or_eof_starts_nothing(monkeypatch, fake_engine, capsys, response):
    lines = ['http://localhost:8110', 'Search.', '.', '1', 'Formatting Syntax', '', '1']
    answers(monkeypatch, lines + ([response] if response is not None else []))
    assert cli.main(['guide', '--json']) == 2
    assert not fake_engine[0]
    assert json.loads(capsys.readouterr().out)['status'] == 'cancelled'


@pytest.mark.parametrize('lines', [
    ['https://example.com'],
    ['http://localhost:8110', '.', '1'],
    ['http://localhost:8110', 'Task', '.', 'unsupported'],
    ['http://localhost:8110', 'Task', '.', '1', 'Heading', '7'],
    ['http://localhost:8110', 'Task', '.', '1', 'Heading', 'not-a-number'],
    ['http://localhost:8110', 'Task', '.', '2', '', 'Item'],
    ['http://localhost:8110', 'Task', '.', '1', 'Heading', '1', 'unsupported'],
])
def test_invalid_input_starts_nothing(monkeypatch, fake_engine, capsys, lines):
    answers(monkeypatch, lines)
    assert cli.main(['guide', '--json']) == 2
    assert not fake_engine[0]
    assert json.loads(capsys.readouterr().out)['status'] == 'error'


def test_keyboard_interrupt_is_clean_cancellation(monkeypatch, fake_engine, capsys):
    def interrupt():
        raise KeyboardInterrupt
    monkeypatch.setattr('builtins.input', interrupt)
    assert cli.main(['guide', '--json']) == 2
    assert not fake_engine[0]
    assert json.loads(capsys.readouterr().out)['status'] == 'cancelled'


@pytest.mark.parametrize('status,code,exported', [('passed', 0, True), ('failed', 1, True), ('error', 2, False)])
def test_json_stdout_and_honest_summary(monkeypatch, fake_engine, capsys, status, code, exported):
    calls, result = fake_engine
    result['check']['status'] = status
    result['export']['available'] = exported
    answers(monkeypatch, ['http://localhost:8110', 'Task', '.', '1', 'Heading', '', '1', 'y'])
    assert cli.main(['guide', '--json']) == code
    output = capsys.readouterr()
    assert json.loads(output.out) == result
    assert 'Outcome:' in output.err and 'Report:' in output.err
    assert ('Replay (PowerShell' in output.err) == exported
    if status == 'failed':
        assert 'not a confirmed bug' in output.err


def test_credentials_not_echoed_during_review(monkeypatch, fake_engine, capsys):
    answers(monkeypatch, ['http://localhost:8110', KEY, '.', '1', KEY, '', '1', 'y'])
    assert cli.main(['guide', '--json']) == 2
    output = capsys.readouterr()
    assert KEY not in output.out + output.err
    assert not fake_engine[0]


@pytest.mark.parametrize('kind', ['heading', 'region'])
def test_guided_subprocess_mock_endpoint_export_and_replay(task_apps, tmp_path, monkeypatch, kind):
    monkeypatch.setenv('GUIDE_TEST_KEY', KEY)
    expected = TASK_EXPECTATION if kind == 'region' else {'kind': 'heading_text', 'text': 'Task list', 'level': 1}
    fields = ['2', 'Tasks', TASK_EXPECTATION['text']] if kind == 'region' else ['1', 'Task list', '1']
    replies = [{'action': a} for a in TASK_SEQUENCE] + [{'finish': True}]
    with endpoint(replies) as (url, requests):
        # Select configured endpoint through the actual prompts, not a new provider.
        input_lines = [task_apps['fixed'], 'Add a task then reload.', '.', *fields, '2', url, 'mock', 'yes']
        process = subprocess.run([sys.executable, '-m', 'bugrepro', 'guide', '--json',
            '--api-key-env', 'GUIDE_TEST_KEY', '--run-root', str(tmp_path),
            '--action-budget', '4', '--model-call-limit', '5', '--timeout-ms', '500'],
            input='\n'.join(input_lines)+'\n', capture_output=True, text=True, timeout=45)
    assert process.returncode == 0, process.stderr
    result = json.loads(process.stdout)
    assert result['check']['status'] == 'passed'
    assert KEY not in process.stdout + process.stderr
    frozen = parse_expectation(expected).fingerprint()
    assert frozen in process.stderr
    contexts = [json.loads(req[2]['messages'][1]['content']) for req in requests]
    assert all(parse_expectation(context['expectation']).fingerprint() == frozen for context in contexts)
    assert result['model_calls']['used'] == len(requests) == 5
    assert result['action_count'] == 4
    recipe = load_recipe(Path(result['run_dir']) / 'recipe.json')
    assert recipe.expectation.fingerprint() == frozen
    replay = execute_recipe(recipe=recipe, base_url=task_apps['fixed'], run_root=tmp_path,
                            invocation='replay', timeout_ms=500)
    assert replay.check.status == 'passed'
    assert len(requests) == 5  # replay has no decision calls
