"""Scripted recovery mechanics, with mocked endpoint/Codex decisions."""

import json
import threading
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from bugrepro.browser import BrowserSession, PlaywrightHost
from bugrepro.cli import build_parser
from bugrepro.codex_driver import CodexClient
from bugrepro.expectations import parse_expectation
from bugrepro.html_report import ReportError, load_report_run, write_report_html
from bugrepro.investigation import Investigation, InvestigationError
from bugrepro.model_driver import ModelClient, investigate
from bugrepro.recipe import load_recipe, parse_action, parse_recipe
from bugrepro.recovery import PreDispatchFailure
from bugrepro.runner import execute_recipe_with_host
from bugrepro.service import AgentService, ServiceConfig
from test_codex_driver import events, mocked_cli
from test_model_driver import endpoint, KEY


EXPECTED = {'kind': 'heading_text', 'text': 'Done', 'level': 1}
GOTO = {'type': 'goto', 'path': '/'}
MISSING = {'type': 'click', 'locator': {'role': 'button', 'name': 'Unavailable', 'exact': True}}
CORRECT = {'type': 'click', 'locator': {'role': 'button', 'name': 'Show result', 'exact': True}}


@pytest.fixture(scope='module')
def recovery_app():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            body = b'''<!doctype html><html><body style="min-height:2400px">
            <h1>Ready</h1><button onclick="window.inputs++;document.querySelector('h1').textContent='Done'">Show result</button>
            <a role="button" aria-label="Close" style="position:fixed;top:-720px;left:10px"
               onclick="window.inputs++">x</a>
            <button disabled>Disabled</button><button>Duplicate</button><button>Duplicate</button>
            <input aria-label="Locked" readonly value="private-value">
            <button style="position:absolute;top:1800px" onclick="window.inputs++">Below fold</button>
            <div style="position:absolute;top:2000px;transform:translateX(0)">
              <button style="position:fixed;top:0">Transformed fixed</button></div>
            <div id="shadow-host" style="position:absolute;top:2100px;translate:0px">
            </div>
            <script>document.getElementById('shadow-host').attachShadow({mode:'open'}).innerHTML=
            '<button style="position:fixed;top:0">Shadow fixed</button>';</script>
            <script>window.inputs=0;</script></body></html>'''
            if self.path == '/guard':
                body = b'<html><body><h1>Ready</h1><script>fetch("https://example.invalid/blocked?secret=not-recorded")</script></body></html>'
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            self.wfile.write(body)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture(scope='module')
def host():
    browser = PlaywrightHost().start()
    yield browser
    browser.close()


@pytest.fixture
def session_factory(recovery_app, tmp_path, host):
    sessions = []
    def create(limit=1, budget=10, expected=EXPECTED):
        session = Investigation.start(session_id=f'test-{len(sessions)}', base_url=recovery_app,
            bug_report='Scripted recovery mechanics, not an AI investigation.',
            expectation=parse_expectation(expected), driver='scripted-test', model={'id':'unknown','source':'unavailable'},
            budget_limit=budget, timeout_ms=1500, run_root=tmp_path, label='recovery-mechanics',
            headless=True, host=host, action_recovery_limit=limit)
        sessions.append(session)
        assert session.execute_action(parse_action(GOTO))['status'] == 'ok'
        return session
    yield create
    for session in sessions:
        session.close()


@pytest.mark.parametrize('action,code', [
    (MISSING, 'locator_missing'),
    ({'type':'click','locator':{'role':'button','name':'Duplicate','exact':True}}, 'locator_ambiguous'),
    ({'type':'click','locator':{'role':'button','name':'Disabled','exact':True}}, 'control_disabled'),
    ({'type':'click','locator':{'role':'button','name':'Close','exact':True}}, 'fixed_outside_viewport'),
    ({'type':'fill','locator':{'role':'textbox','name':'Locked','exact':True},'text':'redacted-input'}, 'control_readonly'),
])
def test_proven_pre_dispatch_failures_then_correct_action(session_factory, action, code):
    session = session_factory()
    page = session.session.page
    before = page.evaluate('() => [scrollX,scrollY,window.inputs,document.activeElement.tagName]')
    result = session.execute_action(parse_action(action))
    assert result['status'] == 'failed' and result['error'] == code
    assert result['recovery']['can_continue'] and result['observation']['headings'] == ['Ready']
    assert page.evaluate('() => [scrollX,scrollY,window.inputs,document.activeElement.tagName]') == before
    assert 'private-value' not in result['error'] and len(result['error']) < 100
    session.execute_action(parse_action(CORRECT))
    result = session.finish()
    assert result['check']['status'] == 'passed'
    assert result['action_failure_count'] == 1 and result['execution_error_count'] == 0
    assert result['recovery']['pending_attempt_indices'] == []
    assert session.action_failures[0]['resolved_by'] == 3
    session.export_recipe()
    record = json.loads((session.run_dir/'run.json').read_text(encoding='utf-8'))
    assert record['schema_version'] == 2
    assert [a['status'] for a in record['actions']] == ['ok','failed','ok']
    assert record['export']['source_attempt_indices'] == [1,3]
    assert record['export']['omitted_attempt_indices'] == [2]
    assert [a.to_dict() for a in load_recipe(session.run_dir/'recipe.json').actions] == [GOTO,CORRECT]


def test_normal_below_fold_and_uncertain_containing_block_are_not_preflight_failures(session_factory):
    session = session_factory()
    # A transform establishes a different containing block: do not infer a
    # viewport-fixed failure from its offscreen rectangle.
    transformed = parse_action({'type':'click','locator':{'role':'button','name':'Transformed fixed','exact':True}})
    assert session.session._preflight(transformed) > 0
    shadow = parse_action({'type':'click','locator':{'role':'button','name':'Shadow fixed','exact':True}})
    assert session.session._preflight(shadow) > 0
    below = parse_action({'type':'click','locator':{'role':'button','name':'Below fold','exact':True}})
    assert session.execute_action(below)['status'] == 'ok'
    assert session.session.page.evaluate('window.inputs') == 1
    assert not session.action_failures


@pytest.mark.parametrize('kind', ['observe','screenshot'])
def test_diagnostics_do_not_resolve_pending_failure(session_factory, kind):
    session = session_factory()
    session.execute_action(parse_action(MISSING))
    session.execute_action(parse_action({'type':kind,'label':'inspect'}))
    result = session.finish()
    assert result['check']['status'] == 'error' and not result['export']['available']
    assert result['recovery']['pending_attempt_indices'] == [2]


@pytest.mark.parametrize('limit', [1,2])
def test_total_recovery_limit_is_not_reset_by_success(session_factory, limit):
    session = session_factory(limit=limit)
    for _ in range(limit):
        session.execute_action(parse_action(MISSING))
        session.execute_action(parse_action(CORRECT))
    result = session.execute_action(parse_action(MISSING))
    assert not result['recovery']['can_continue'] and result['recovery']['used'] == limit
    assert session.finish()['check']['status'] == 'error'
    with pytest.raises(InvestigationError):
        session.execute_action(parse_action(CORRECT))


def test_two_pending_failures_can_be_resolved_by_one_application_action(session_factory):
    session = session_factory(limit=2)
    session.execute_action(parse_action(MISSING))
    session.execute_action(parse_action(MISSING))
    session.execute_action(parse_action(CORRECT))
    assert session.finish()['check']['status'] == 'passed'
    session.export_recipe()
    assert session.export_summary()['source_attempt_indices'] == [1,4]
    assert [f['resolved_by'] for f in session.action_failures] == [4,4]


@pytest.mark.parametrize('fault', ['dispatch','guard','crash','evidence','observation'])
def test_uncertain_or_fatal_failures_never_recover(session_factory, monkeypatch, fault):
    session = session_factory()
    def fail(*args, **kwargs):
        raise PlaywrightTimeoutError('private provider/page detail must not be echoed')
    if fault == 'dispatch':
        original = session.session.perform
        def after_dispatch(action, **kwargs):
            original(action, **kwargs)
            fail()
        monkeypatch.setattr(session.session, 'perform', after_dispatch)
    elif fault == 'guard':
        session.session.guard_errors.append('Request interception failed: RuntimeError.')
    elif fault == 'crash':
        session.session._on_page_crash()
    elif fault == 'evidence':
        monkeypatch.setattr(session.session, 'screenshot', fail)
    else:
        monkeypatch.setattr(session.session, 'observe', fail)
    response = session.execute_action(parse_action(CORRECT if fault == 'dispatch' else MISSING))
    assert not response['recovery']['can_continue']
    assert session.execution_errors
    result = session.finish()
    assert result['check']['status'] == 'error' and not result['export']['available']
    document = json.dumps(session._run_document())
    assert 'private provider/page detail' not in document and 'private interception diagnostic' not in document
    if fault == 'observation':
        assert response['observation'] is None  # never reuse the previous page snapshot
    with pytest.raises(InvestigationError):
        session.execute_action(parse_action(CORRECT))


@pytest.mark.parametrize('backend', ['endpoint','codex'])
def test_both_decision_backends_receive_failure_and_successful_history(recovery_app, tmp_path, monkeypatch, mocked_cli, backend, host):
    decisions = [{'action':a} for a in [GOTO,MISSING,CORRECT]] + [{'finish':True}]
    contexts = []
    if backend == 'endpoint':
        monkeypatch.setenv('RECOVERY_TEST_KEY', KEY)
        with endpoint(decisions) as (url, requests):
            result = investigate(target=recovery_app, bug_report='Scripted mocked endpoint mechanics.', expectation=EXPECTED,
                model_client=ModelClient(url,'mock','RECOVERY_TEST_KEY',5), run_root=tmp_path,
                action_budget=3, model_call_limit=4, action_recovery_limit=1, timeout_ms=500)
        contexts = [json.loads(r[2]['messages'][1]['content']) for r in requests]
    else:
        mocked_cli[1].extend((0,events(d),'') for d in decisions)
        result = investigate(target=recovery_app, bug_report='Scripted mocked Codex mechanics.', expectation=EXPECTED,
            model_client=CodexClient(), run_root=tmp_path, action_budget=3, model_call_limit=4,
            action_recovery_limit=1, timeout_ms=500, driver='codex-cli')
        contexts = [json.loads(call[3].rsplit('\n',1)[1]) for call in mocked_cli[0] if call[3]]
    assert result['check']['status'] == 'passed' and result['action_count'] == 3
    assert result['model_calls']['used'] == len(contexts) == 4
    failure_context = contexts[2]
    assert failure_context['latest']['error'] == 'locator_missing'
    assert failure_context['latest']['observation']['headings'] == ['Ready']
    assert failure_context['executed_actions'] == [GOTO]
    assert [a['status'] for a in failure_context['attempted_actions']] == ['ok','failed']
    run_dir = Path(result['run_dir'])
    recipe = load_recipe(run_dir/'recipe.json')
    replay = execute_recipe_with_host(host=host, recipe=recipe, base_url=recovery_app, run_root=tmp_path, invocation='replay', timeout_ms=500)
    assert replay.check.status == 'passed' and len(replay.actions) == 2
    assert replay.expectation_fingerprint == result['expectation']['fingerprint']
    assert (run_dir/'recipe.json').read_bytes() == (replay.run_dir/'recipe.json').read_bytes()
    report = write_report_html(tmp_path/'comparison.html', {'attempt':load_report_run(run_dir,'attempt'),
                                                         'fixed':load_report_run(replay.run_dir,'fixed')})
    html = report.read_text(encoding='utf-8')
    assert 'successful path' in html and 'not its failed attempts' in html and 'recovered by attempt 3' in html
    assert 'Unchanged replay verified' in html
    assert len(contexts) == 4


@pytest.mark.parametrize('kwargs,expected_attempts,expected_calls', [
    ({'action_recovery_limit':0},2,2),
    ({'action_budget':2},2,2),
    ({'model_call_limit':2},2,2),
])
def test_default_strict_and_budgets(recovery_app, tmp_path, monkeypatch, kwargs, expected_attempts, expected_calls):
    monkeypatch.setenv('RECOVERY_TEST_KEY',KEY)
    options = dict(action_recovery_limit=1,action_budget=5,model_call_limit=6)
    options.update(kwargs)
    with endpoint([{'action':a} for a in [GOTO,MISSING,CORRECT]]+[{'finish':True}]) as (url, requests):
        result = investigate(target=recovery_app, bug_report='Scripted limit mechanics.', expectation=EXPECTED,
            model_client=ModelClient(url,'mock','RECOVERY_TEST_KEY',5),run_root=tmp_path,timeout_ms=500,**options)
    assert result['check']['status']=='error' and not result['export']['available']
    assert result['action_count']==expected_attempts and len(requests)==expected_calls


def test_recovered_failed_expectation_is_exportable_but_tampered_mapping_is_not_honest(session_factory, tmp_path):
    session = session_factory(expected={**EXPECTED,'text':'Other expected heading'})
    session.execute_action(parse_action(MISSING))
    session.execute_action(parse_action(CORRECT))
    assert session.finish()['check']['status']=='failed'
    session.export_recipe()
    load_report_run(session.run_dir,'attempt')
    path=session.run_dir/'run.json'
    data=json.loads(path.read_text(encoding='utf-8'))
    data['export']['source_attempt_indices']=[1,2,3]
    path.write_text(json.dumps(data),encoding='utf-8')
    with pytest.raises(ReportError,match='mapping'):
        load_report_run(session.run_dir,'attempt')


def test_scripted_and_replay_commands_remain_strict(recovery_app,tmp_path,host):
    recipe=parse_recipe({'schema_version':1,'name':'strict-mechanics','recorded_base_url':recovery_app,
                         'expectation':EXPECTED,'actions':[GOTO,MISSING,CORRECT]})
    for invocation in ['run','replay']:
        result=execute_recipe_with_host(host=host,recipe=recipe,base_url=recovery_app,run_root=tmp_path,invocation=invocation,timeout_ms=500)
        assert result.check.status=='error' and len(result.actions)==2
        assert 'recovery' not in json.loads(result.run_json_path.read_text(encoding='utf-8'))
    service=AgentService(ServiceConfig(state_file=tmp_path/'state.json',run_root=tmp_path))
    with pytest.raises(InvestigationError,match='Unknown field'):
        service.start_investigation({'action_recovery_limit':1})


def test_cli_limit_is_opt_in_and_bounded():
    parser=build_parser()
    for command in ['guide','investigate']:
        args=[command]+(['--target','http://localhost:8103','--report-file','report.txt','--expectation-file','check.json'] if command=='investigate' else [])
        assert parser.parse_args(args).action_recovery_limit==0
        assert parser.parse_args(args+['--action-recovery-limit','2']).action_recovery_limit==2
        for value in ['-1','3']:
            with pytest.raises(SystemExit):
                parser.parse_args(args+['--action-recovery-limit',value])


def test_real_request_guard_stops_before_another_decision(recovery_app,tmp_path,monkeypatch):
    monkeypatch.setenv('RECOVERY_TEST_KEY',KEY)
    with endpoint([{'action':{'type':'goto','path':'/guard'}}, {'action':CORRECT}]) as (url,requests):
        result=investigate(target=recovery_app,bug_report='Scripted guard mechanics.',expectation=EXPECTED,
            model_client=ModelClient(url,'mock','RECOVERY_TEST_KEY',5),run_root=tmp_path,
            timeout_ms=1500,action_recovery_limit=2)
    assert len(requests)==1 and result['check']['status']=='error'
    assert not result['export']['available'] and result['blocked_resources']
    assert all(r['fatal'] for r in result['blocked_resources'])
    assert 'not-recorded' not in json.dumps(result['blocked_resources'])


def test_model_error_after_pending_recovery_is_fatal(recovery_app,tmp_path,monkeypatch):
    monkeypatch.setenv('RECOVERY_TEST_KEY',KEY)
    with endpoint([{'action':GOTO},{'action':MISSING},'malformed private output']) as (url,requests):
        result=investigate(target=recovery_app,bug_report='Scripted invalid-response mechanics.',expectation=EXPECTED,
            model_client=ModelClient(url,'mock','RECOVERY_TEST_KEY',5),run_root=tmp_path,
            timeout_ms=1500,action_recovery_limit=2)
    assert len(requests)==3 and result['action_count']==2 and result['check']['status']=='error'
    assert not result['export']['available'] and result['execution_error_count']>0
    assert result['recovery']['pending_attempt_indices']==[2]


def test_over_budget_after_success_stops_decisions_immediately(recovery_app,tmp_path,monkeypatch):
    monkeypatch.setenv('RECOVERY_TEST_KEY',KEY)
    with endpoint([{'action':a} for a in [GOTO,CORRECT,CORRECT,CORRECT]]) as (url,requests):
        result=investigate(target=recovery_app,bug_report='Scripted budget mechanics.',expectation=EXPECTED,
            model_client=ModelClient(url,'mock','RECOVERY_TEST_KEY',5),run_root=tmp_path,
            timeout_ms=1500,action_recovery_limit=2,action_budget=2)
    assert len(requests)==3 and result['action_count']==2 and result['budget']['used']==2
    assert result['check']['status']=='error' and not result['export']['available']


@pytest.mark.parametrize('already_exported', [False, True])
def test_guard_failure_after_finish_still_prevents_export(session_factory, already_exported):
    session=session_factory()
    session.execute_action(parse_action(CORRECT))
    assert session.finish()['check']['status']=='passed'
    if already_exported:
        session.export_recipe()
    session.session.guard_errors.append('Request interception failed: RuntimeError.')
    with pytest.raises(InvestigationError,match='guard'):
        session.export_recipe()
    assert session.check.status=='error' and not session.export_summary()['available']


def test_guide_forwards_recovery_limit_only_after_approval(monkeypatch):
    from bugrepro import cli, model_driver
    inputs=iter(['http://localhost:8103','Show the heading Done.','.','1','1','Done','1','yes'])
    calls=[]
    class Client:
        def reject_secret(self,value):
            pass
    monkeypatch.setattr('builtins.input',lambda:next(inputs))
    monkeypatch.setattr(cli,'_decision_client',lambda args:(Client(),'codex-cli'))
    def run(**kwargs):
        calls.append(kwargs)
        return {'check':{'status':'error'},'html_report':'report.html','export':{'available':False}}
    monkeypatch.setattr(model_driver,'investigate',run)
    assert cli.main(['guide','--decision-backend','codex-cli','--action-recovery-limit','2'])==2
    assert calls[0]['action_recovery_limit']==2 and calls[0]['expectation']==EXPECTED
