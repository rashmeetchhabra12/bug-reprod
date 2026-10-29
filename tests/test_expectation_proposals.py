"""Scripted proposal/approval checks; endpoint and Codex responses are mocked."""

import json
import subprocess
import sys

import pytest

from bugrepro import cli, codex_driver
from bugrepro.codex_driver import CodexClient
from bugrepro.expectation_proposal import parse_proposal
from bugrepro.expectations import parse_expectation
from bugrepro.model_driver import ModelClient, ModelError
from bugrepro.recipe import load_recipe
from bugrepro.runner import execute_recipe
from conftest import TASK_EXPECTATION
from test_codex_driver import events, mocked_cli
from test_guided_cli import answers, explicit_json_args, fake_engine
from test_model_driver import KEY, endpoint
from test_task_app import TASK_SEQUENCE


HEADING = {'kind': 'heading_text', 'text': 'Task list', 'level': 1}
START = ['http://localhost:8110', 'Show the level-1 heading Task list.', '.']
COMMAND = ['guide', '--decision-backend', 'codex-cli']
NATURAL_LIST_REQUESTS = [
    ('Add a task named Prepare hackathon demo. After reloading, it should still appear in the Tasks list.',
     {'kind': 'region_text_exact', 'region': 'Tasks', 'text': 'Prepare hackathon demo'}),
    ('Add a book named The Left Hand of Darkness. After refreshing, it should remain in the Reading list.',
     {'kind': 'region_text_exact', 'region': 'Reading', 'text': 'The Left Hand of Darkness'}),
]


@pytest.fixture
def proposal_client(monkeypatch, fake_engine):
    state = {'proposal': parse_expectation(HEADING), 'requests': []}
    original = cli._decision_client
    def create(args):
        client, driver = original(args)
        def propose(context):
            state['requests'].append(context)
            if isinstance(state['proposal'], Exception):
                raise state['proposal']
            return state['proposal']
        client.propose_expectation = propose
        return client, driver
    monkeypatch.setattr(cli, '_decision_client', create)
    return state


@pytest.mark.parametrize('expected', [HEADING, TASK_EXPECTATION])
def test_accept_then_review_before_freeze(monkeypatch, fake_engine, proposal_client, capsys, expected):
    proposal_client['proposal'] = parse_expectation(expected)
    lines = iter(START + ['2', 'accept', 'yes'])
    count = 0
    def read():
        nonlocal count
        count += 1
        if count in {5, 6}:
            assert not fake_engine[0]
            output = capsys.readouterr()
            assert json.dumps(parse_expectation(expected).to_dict(), indent=2) in output.err
            assert ('Accept, edit, or reject' if count == 5 else 'Freeze this expectation') in output.err
        return next(lines)
    monkeypatch.setattr('builtins.input', read)
    assert cli.main(COMMAND) == 0
    assert fake_engine[0][0][2] == parse_expectation(expected).to_dict()
    assert proposal_client['requests'] == [{'target': START[0], 'bug_report': START[1]}]


def test_edit_returns_to_validated_manual_entry(monkeypatch, fake_engine, proposal_client):
    answers(monkeypatch, START + ['2', 'edit', '2', 'Tasks', 'Revised item', 'yes'])
    assert cli.main(COMMAND) == 0
    assert fake_engine[0][0][2] == {'kind': 'region_text_exact', 'text': 'Revised item', 'region': 'Tasks', 'level': 1}


@pytest.mark.parametrize('manual', [True, False])
def test_reject_offers_manual_entry_or_cancel(monkeypatch, fake_engine, proposal_client, capsys, manual):
    tail = ['yes', '1', 'Manual heading', '2', 'yes'] if manual else ['no']
    answers(monkeypatch, START + ['2', 'reject', *tail])
    assert cli.main(COMMAND) == (0 if manual else 2)
    assert 'Proposal rejected; no expectation is frozen.' in capsys.readouterr().err
    assert bool(fake_engine[0]) == manual
    if manual:
        assert fake_engine[0][0][2]['text'] == 'Manual heading'


@pytest.mark.parametrize('proposal,message', [
    (None, 'does not give enough information'),
    (ValueError('unsupported check ' + KEY), 'invalid output'),
    (ModelError('private provider error ' + KEY), 'backend failed'),
])
@pytest.mark.parametrize('manual', [True, False])
def test_unusable_proposals_require_explicit_manual_fallback(monkeypatch, fake_engine, proposal_client, capsys, proposal, message, manual):
    proposal_client['proposal'] = proposal
    tail = ['y', '1', 'Manual heading', '', 'yes'] if manual else ['n']
    answers(monkeypatch, START + ['2', *tail])
    assert cli.main(COMMAND) == (0 if manual else 2)
    output = capsys.readouterr()
    assert message in output.err and 'manually instead?' in output.err
    assert KEY not in output.out + output.err
    assert bool(fake_engine[0]) == manual


def test_unavailable_backend_offers_manual(monkeypatch, fake_engine, capsys):
    def unavailable(args):
        raise RuntimeError(KEY)
    monkeypatch.setattr(cli, '_decision_client', unavailable)
    answers(monkeypatch, START + ['2', 'n'])
    assert cli.main(COMMAND) == 2
    output = capsys.readouterr()
    assert 'backend is unavailable or not configured' in output.err
    assert 'manually instead?' in output.err and KEY not in output.err
    assert not fake_engine[0]


@pytest.mark.parametrize('response', ['n', '', None])
def test_accepting_proposal_without_freeze_starts_nothing(monkeypatch, fake_engine, proposal_client, response):
    answers(monkeypatch, START + ['2', 'a'] + ([response] if response is not None else []))
    assert cli.main(COMMAND) == 2 and not fake_engine[0]


@pytest.mark.parametrize('missing', ['--target', '--report-file', '--expectation-file', '--decision-backend'])
def test_json_requires_explicit_inputs_and_never_proposes(monkeypatch, fake_engine, proposal_client, capsys, tmp_path, missing):
    args = explicit_json_args(tmp_path)
    index = args.index(missing)
    del args[index:index+2]
    monkeypatch.setattr('builtins.input', lambda: pytest.fail('JSON must not prompt'))
    assert cli.main(args) == 2
    assert 'non-interactive' in json.loads(capsys.readouterr().out)['error']
    assert not fake_engine[0] and not proposal_client['requests']


def test_json_validates_explicit_expectation(monkeypatch, fake_engine, proposal_client, capsys, tmp_path):
    assert cli.main(explicit_json_args(tmp_path, {'kind': 'arbitrary'})) == 2
    assert json.loads(capsys.readouterr().out)['status'] == 'error'
    assert not fake_engine[0] and not proposal_client['requests']


def test_json_invalid_field_names_do_not_echo_credentials(fake_engine, capsys, tmp_path):
    assert cli.main(explicit_json_args(tmp_path, {**HEADING, KEY: 'unknown field'})) == 2
    output = capsys.readouterr()
    assert KEY not in output.out + output.err and not fake_engine[0]


@pytest.mark.parametrize('secret_in_proposal', [True, False])
def test_credentials_not_sent_or_displayed(monkeypatch, fake_engine, proposal_client, capsys, secret_in_proposal):
    if secret_in_proposal:
        proposal_client['proposal'] = parse_expectation({**HEADING, 'text': KEY})
        lines = START + ['2', 'n']
    else:
        lines = [START[0], KEY, '.', '2']
    answers(monkeypatch, lines)
    assert cli.main(COMMAND) == 2
    output = capsys.readouterr()
    assert KEY not in output.out + output.err and not fake_engine[0]
    assert bool(proposal_client['requests']) == secret_in_proposal


@pytest.mark.parametrize('bad', [
    {}, {'proposal': {'kind': 'url', 'text': 'anything'}},
    {'proposal': {**HEADING, 'level': 7}}, {'proposal': {'kind': 'heading_text', 'text': 'Missing level'}},
    {'proposal': {**TASK_EXPECTATION, 'region': ''}}, {'proposal': {**HEADING, 'css': 'h1'}},
    {'proposal': HEADING, 'action': {'type': 'goto', 'path': '/'}},
])
def test_invalid_endpoint_proposal_is_not_an_action(monkeypatch, bad):
    monkeypatch.setenv('PROPOSAL_TEST_KEY', KEY)
    with endpoint([bad]) as (url, requests):
        client = ModelClient(url, 'mock', 'PROPOSAL_TEST_KEY', 5)
        with pytest.raises((ValueError, ModelError)):
            client.propose_expectation({'bug_report': 'Vague description'})
    assert len(requests) == 1


def test_codex_proposal_uses_existing_restrictions(mocked_cli):
    calls, replies = mocked_cli
    replies.append((0, events({'proposal': HEADING}), ''))
    assert CodexClient().propose_expectation({'bug_report': START[1]}).to_dict() == HEADING
    command, cwd, timeout, prompt = calls[-1]
    assert 'read-only' in command and '--ignore-rules' in command
    assert all(command[command.index(f)-1] == '--disable' for f in codex_driver.DISABLED_FEATURES)
    assert 'Do not invent' in prompt and 'latest' not in json.loads(prompt.rsplit('\n', 1)[1])
    assert timeout == 60


def test_vague_description_can_be_declined_by_both_backends(monkeypatch, mocked_cli):
    assert parse_proposal({'proposal': None}) is None
    mocked_cli[1].append((0, events({'proposal': None}), ''))
    assert CodexClient().propose_expectation({'bug_report': 'It is broken.'}) is None
    monkeypatch.setenv('PROPOSAL_TEST_KEY', KEY)
    with endpoint([{'proposal': None}]) as (url, requests):
        assert ModelClient(url, 'mock', 'PROPOSAL_TEST_KEY', 5).propose_expectation({'bug_report': 'It is broken.'}) is None
    assert 'return null' in requests[0][2]['messages'][0]['content']


@pytest.mark.parametrize('raw', [{'proposal': {'kind': 'url', 'text': 'somewhere'}}, {'action': {'type': 'goto', 'path': '/'}}])
def test_codex_rejects_unsupported_proposals(mocked_cli, raw):
    mocked_cli[1].append((0, events(raw), ''))
    with pytest.raises(ValueError):
        CodexClient().propose_expectation({'bug_report': START[1]})


@pytest.mark.parametrize('report,expected', NATURAL_LIST_REQUESTS)
def test_natural_named_list_request_roundtrips_both_backends(monkeypatch, mocked_cli, report, expected):
    # These are mocked response/transport tests; live prompt behavior is checked
    # separately without starting an investigation. No natural-language regex
    # parser or application-specific fallback creates the expectation.
    context = {'target': START[0], 'bug_report': report}
    assert parse_proposal({'proposal': expected}).to_dict() == parse_expectation(expected).to_dict()
    mocked_cli[1].append((0, events({'proposal': expected}), ''))
    assert CodexClient().propose_expectation(context).to_dict() == parse_expectation(expected).to_dict()
    prompt = mocked_cli[0][-1][3]
    assert json.loads(prompt.rsplit('\n', 1)[1]) == context
    assert 'candidate accessible region' in prompt and 'same item named earlier' in prompt
    monkeypatch.setenv('PROPOSAL_TEST_KEY', KEY)
    with endpoint([{'proposal': expected}]) as (url, requests):
        result = ModelClient(url, 'mock', 'PROPOSAL_TEST_KEY', 5).propose_expectation(context)
    assert result.to_dict() == parse_expectation(expected).to_dict()
    assert json.loads(requests[0][2]['messages'][1]['content']) == context
    assert 'requires another kind of assertion, return null' in requests[0][2]['messages'][0]['content']


@pytest.mark.parametrize('report,expected', NATURAL_LIST_REQUESTS)
@pytest.mark.parametrize('approve', [True, False])
def test_natural_list_proposal_still_requires_approval(monkeypatch, fake_engine, proposal_client, capsys, report, expected, approve):
    proposal_client['proposal'] = parse_expectation(expected)
    lines = iter([START[0], report, '.', '2', 'accept', 'yes' if approve else 'no'])
    count = 0
    def read():
        nonlocal count
        count += 1
        if count in {5, 6}:
            assert not fake_engine[0]
            output = capsys.readouterr()
            assert json.dumps(parse_expectation(expected).to_dict(), indent=2) in output.err
            assert ('Accept, edit, or reject' if count == 5 else 'Freeze this expectation') in output.err
        return next(lines)
    monkeypatch.setattr('builtins.input', read)
    assert cli.main(COMMAND) == (0 if approve else 2)
    assert bool(fake_engine[0]) == approve
    if approve:
        assert fake_engine[0][0][2] == parse_expectation(expected).to_dict()


@pytest.mark.parametrize('report', [
    'After reloading, it should still be there.',  # missing item and list
    'Add an item named Notes. It should still appear after reload.',  # missing list name
    'Loading the Records list should take less than 100 milliseconds.',  # unsupported timing assertion
])
def test_vague_and_unsupported_descriptions_remain_rejectable(monkeypatch, fake_engine, proposal_client, capsys, report):
    proposal_client['proposal'] = None
    answers(monkeypatch, [START[0], report, '.', '2', 'n'])
    assert cli.main(COMMAND) == 2
    output = capsys.readouterr()
    assert 'manually instead?' in output.err and not fake_engine[0]


@pytest.mark.parametrize('expected', [HEADING, TASK_EXPECTATION])
def test_approved_mock_endpoint_proposal_stays_frozen_through_replay(task_apps, tmp_path, monkeypatch, expected):
    monkeypatch.setenv('GUIDE_TEST_KEY', KEY)
    replies = [{'proposal': expected}] + [{'action': a} for a in TASK_SEQUENCE] + [{'finish': True}]
    with endpoint(replies) as (url, requests):
        lines = [task_apps['fixed'], 'Add a task and reload. Expected check: ' + json.dumps(expected),
                 '.', '2', '2', url, 'mock', 'accept', 'yes']
        process = subprocess.run([sys.executable, '-m', 'bugrepro', 'guide',
            '--api-key-env', 'GUIDE_TEST_KEY', '--run-root', str(tmp_path), '--action-budget', '4',
            '--model-call-limit', '5', '--timeout-ms', '500'],
            input='\n'.join(lines)+'\n', capture_output=True, text=True, timeout=50)
    assert process.returncode == 0, process.stderr
    assert KEY not in process.stdout + process.stderr
    run_file, = tmp_path.glob('*/run.json')
    record = json.loads(run_file.read_text(encoding='utf-8'))
    fingerprint = parse_expectation(expected).fingerprint()
    assert record['expectation']['fingerprint'] == fingerprint
    contexts = [json.loads(req[2]['messages'][1]['content']) for req in requests]
    assert set(contexts[0]) == {'target', 'bug_report'}
    assert all(parse_expectation(c['expectation']).fingerprint() == fingerprint for c in contexts[1:])
    assert record['model_calls']['used'] == 5 and len(requests) == 6  # one preparation request
    recipe = load_recipe(run_file.parent/'recipe.json')
    assert recipe.expectation.fingerprint() == fingerprint
    replay = execute_recipe(recipe=recipe, base_url=task_apps['fixed'], run_root=tmp_path, invocation='replay', timeout_ms=500)
    assert replay.check.status == 'passed' and replay.expectation_fingerprint == fingerprint
    assert len(requests) == 6
    assert KEY not in ''.join(p.read_text(encoding='utf-8') for p in run_file.parent.glob('*.json'))
