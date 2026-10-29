"""Scripted observation mechanics on a small local page; no model calls."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from bugrepro.browser import BrowserSession, BUTTON_HINT_CHAR_LIMIT, BUTTON_HINT_LIMIT, Observation
from bugrepro.recipe import parse_action, parse_recipe, recipe_fingerprint
from bugrepro.runner import execute_recipe


@pytest.fixture(scope="module")
def button_app():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            buttons = '''
              <button title="Show details" aria-describedby="help">&#9670;</button>
              <span id="help" role="tooltip" hidden>Open the details panel</span>
              <a role="button" aria-label="Dismiss" style="position:fixed;top:-1500px">x</a>
              <button disabled>Unavailable</button>
              <div style="position:absolute;top:150px;left:20px;width:120px;height:40px">
                <button style="width:120px;height:40px">Covered</button>
                <div style="position:absolute;inset:0;background:white"></div>
              </div>
              <button style="position:absolute;top:1800px">Later</button>
              <input type="password" value="private-input-value" aria-label="Password">
            '''
            if self.path == "/many":
                buttons = ''.join('<button title="' + 'x' * 1000 + '">Item ' + str(i) + '</button>' for i in range(100))
            body = ('''<!doctype html><html><head><meta charset="utf-8"></head>
              <body style="min-height:2000px"><h1>Ready</h1>''' + buttons + '''
              <script>
                window.clicks = 0; window.hovers = 0; window.focuses = 0;
                document.addEventListener('click', e => {
                  if (e.target.closest('button')) {
                    window.clicks++;
                    document.querySelector('h1').textContent = 'Details';
                  }
                });
                document.addEventListener('mouseover', () => window.hovers++);
                document.addEventListener('focusin', () => window.focuses++);
              </script></body></html>''').encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
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


def find_hint(observation, name):
    return next(item for item in observation.button_hints if f'"{name}"' in item['aria'])


def state(browser):
    return browser.page.evaluate('() => ({x:scrollX,y:scrollY,active:document.activeElement.tagName,clicks:window.clicks,hovers:window.hovers,focuses:window.focuses})')


def test_icon_hints_keep_aria_name_and_observations_are_read_only(button_app):
    with BrowserSession(base_url=button_app, timeout_ms=1500) as browser:
        browser.perform(parse_action({'type':'goto','path':'/'}))
        before = state(browser)
        observation = browser.observe()
        hint = find_hint(observation, '\u25c6')
        assert hint['aria'] == '- button "\u25c6"'
        assert hint['hints'] == {'title':'Show details', 'aria-describedby-text':'Open the details panel'}
        assert hint['click_status'] == 'candidate' and hint['blockers'] == []
        assert hint['visible'] and hint['enabled'] and hint['in_viewport']
        assert observation.button_hints_complete
        assert state(browser) == before  # no trial clicks, scroll, hover, or focus
        assert 'private-input-value' not in json.dumps(observation.button_hints)
        assert observation.to_dict()['button_hints'][0] == hint
        result = browser.perform(parse_action({'type':'click','locator':{'role':'button','name':'\u25c6','exact':True}}))
        assert result.headings == ('Details',)
        assert state(browser)['clicks'] == before['clicks'] + 1


def test_offscreen_disabled_and_obscured_controls_are_distinguished(button_app):
    with BrowserSession(base_url=button_app, timeout_ms=500) as browser:
        observation = browser.perform(parse_action({'type':'goto','path':'/'}))
        offscreen = find_hint(observation, 'Dismiss')
        assert offscreen['visible'] and offscreen['enabled'] and not offscreen['in_viewport']
        assert offscreen['click_status'] == 'blocked_now'
        assert offscreen['blockers'] == ['outside_viewport']
        assert find_hint(observation, 'Unavailable')['blockers'] == ['disabled']
        assert 'center_obscured' in find_hint(observation, 'Covered')['blockers']
        assert find_hint(observation, 'Later')['click_status'] == 'scroll_required'
        assert browser.page.evaluate('scrollY') == 0
        with pytest.raises(PlaywrightTimeoutError):
            browser.perform(parse_action({'type':'click','locator':{'role':'button','name':'Dismiss','exact':True}}))
        assert state(browser)['clicks'] == 0
        # A below-fold document button is not declared permanently unclickable.
        browser.perform(parse_action({'type':'click','locator':{'role':'button','name':'Later','exact':True}}))
        assert state(browser)['clicks'] == 1


def test_hints_are_bounded_and_missing_metadata_is_explicit(button_app):
    with BrowserSession(base_url=button_app, timeout_ms=1500) as browser:
        observation = browser.perform(parse_action({'type':'goto','path':'/many'}))
        assert observation.button_hints and not observation.button_hints_complete
        assert len(observation.button_hints) <= BUTTON_HINT_LIMIT
        assert len(json.dumps(list(observation.button_hints), ensure_ascii=False)) <= BUTTON_HINT_CHAR_LIMIT
        assert all(len(item['hints']['title']) == 200 for item in observation.button_hints)
        browser.page.close()  # deliberate unavailable-page mechanics check
        unavailable = browser.observe()
        assert unavailable.button_hints == () and not unavailable.button_hints_complete


def test_existing_observation_constructor_and_recorded_evidence(button_app, tmp_path):
    old = Observation('about:blank', '', (), '', '2026-01-01T00:00:00Z')
    assert old.to_dict()['button_hints'] == []
    recipe = parse_recipe({'schema_version':1,'name':'button-hints-mechanics',
        'recorded_base_url':button_app,'expectation':{'kind':'heading_text','level':1,'text':'Details'},
        'actions':[{'type':'goto','path':'/'},{'type':'click','locator':{'role':'button','name':'\u25c6','exact':True}}]})
    result = execute_recipe(recipe=recipe,base_url=button_app,run_root=tmp_path,timeout_ms=1500)
    assert result.check.status == 'passed'
    assert result.action_failures == result.execution_errors == []
    assert result.recipe_hash == recipe_fingerprint(recipe)
    record = json.loads(result.run_json_path.read_text(encoding='utf-8'))
    assert [a['type'] for a in record['actions']] == ['goto','click']
    assert record['actions'][0]['observation']['button_hints'][0]['hints']['title'] == 'Show details'
