"""Tiny demo web app used as the reproduction fixture.

Two modes are served from the same code:

``buggy``
    The profile page reads a profile from a browser cookie cache. Logging out
    clears the session but not that cache, so Alice -> logout -> Bob still
    displays Alice's cached profile.

``fixed``
    Logging out clears both the session and the cached profile.

The header always shows the authenticated session, while the profile card
shows the profile that was actually rendered. That makes a mismatch visible.
"""

from __future__ import annotations

import html
import secrets
import sys
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
MODES = frozenset({"buggy", "fixed"})
COOKIE_SESSION = "bugrepro_session"
COOKIE_PROFILE = "bugrepro_profile"

USERS: dict[str, dict[str, str]] = {
    "alice": {
        "name": "Alice",
        "username": "alice",
        "email": "alice@example.test",
        "role": "Designer",
    },
    "bob": {
        "name": "Bob",
        "username": "bob",
        "email": "bob@example.test",
        "role": "Engineer",
    },
}

SESSIONS: dict[str, str] = {}
_LOCK = threading.Lock()

CSS = """
:root { color-scheme: light; --ink:#172026; --muted:#5b6b76; --line:#d9e1e6; --bug:#b42318; --ok:#067647; }
* { box-sizing: border-box; }
body { margin:0; font:16px/1.5 "Segoe UI", system-ui, sans-serif; color:var(--ink); background:#f6f8f9; }
.topbar { display:flex; justify-content:space-between; align-items:center; gap:16px;
          padding:14px 24px; background:#fff; border-bottom:1px solid var(--line); }
.topbar p { margin:0; }
.topbar nav a { margin-left:16px; }
main { max-width:680px; margin:48px auto; padding:0 24px; }
.card { background:#fff; border:1px solid var(--line); border-radius:12px; padding:28px; }
h1 { margin-top:0; font-size:1.7rem; }
label { display:block; font-weight:600; margin-bottom:6px; }
input[type=text] { width:100%; max-width:320px; padding:9px 11px; border:1px solid #b8c4cc;
                   border-radius:8px; font:inherit; }
button { margin-top:16px; padding:9px 16px; border:0; border-radius:8px;
         background:var(--ink); color:#fff; font:inherit; cursor:pointer; }
button:hover { background:#2b3a44; }
.notice { margin:18px 0; padding:12px 14px; border-left:4px solid var(--bug);
          background:#fef3f2; color:#7a271a; }
.ok { color:var(--ok); }
.mismatch { border-left-color:var(--bug); background:#fef3f2; }
table { border-collapse:collapse; width:100%; margin:18px 0; }
th, td { text-align:left; padding:10px 8px; border-bottom:1px solid var(--line); }
th { color:var(--muted); font-weight:600; width:34%; }
footer { max-width:680px; margin:0 auto 40px; padding:0 24px; color:var(--muted); font-size:.9rem; }
.inline { display:inline; }
.inline button { margin:0; padding:6px 12px; font-size:.9rem; }
code { background:#eef2f4; padding:1px 5px; border-radius:4px; }
"""


def _page(
    *,
    title: str,
    signed_in: str | None,
    body: str,
    mode: str,
    extra_class: str = "",
) -> str:
    session_name = USERS[signed_in]["name"] if signed_in else "Not signed in"
    nav = ""
    if signed_in:
        nav = (
            '<nav><a href="/">Home</a>'
            '<a href="/profile">Profile</a></nav>'
        )
    else:
        nav = '<nav><a href="/">Home</a></nav>'
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <style>{CSS}</style>
</head>
<body>
  <header class="topbar">
    <p class="session">Signed in as: <strong>{html.escape(session_name)}</strong></p>
    {nav}
  </header>
  <main class="{extra_class}">
{body}
  </main>
  <footer>Sample app mode: <code>{html.escape(mode)}</code>.
    Demo accounts: alice, bob. No password and no real credentials.</footer>
</body>
</html>
"""


def _home_page(signed_in: str | None, mode: str) -> str:
    if signed_in:
        user = USERS[signed_in]
        body = f"""
    <section class="card">
      <h1>Demo home</h1>
      <p>Signed in as <strong>{html.escape(user['name'])}</strong>.</p>
      <p><a href="/profile">View profile</a></p>
      <form method="post" action="/logout">
        <button type="submit">Log out</button>
      </form>
    </section>
"""
    else:
        body = """
    <section class="card">
      <h1>Demo home</h1>
      <p>Not signed in.</p>
      <form method="post" action="/login">
        <label for="username">Username</label>
        <input id="username" name="user" type="text" autocomplete="off"
               spellcheck="false" placeholder="alice or bob">
        <button type="submit">Log in</button>
      </form>
      <p>Demo accounts: <code>alice</code>, <code>bob</code>. No password.</p>
    </section>
"""
    return _page(title="ReproWitness demo home", signed_in=signed_in, body=body, mode=mode)


def _profile_page(signed_in: str | None, shown: str, mode: str, cache_state: str) -> str:
    if signed_in is None:
        raise ValueError("signed_in is required for the profile page")
    user = USERS[shown]
    signed_in_name = USERS[signed_in]["name"]
    mismatch = ""
    if shown != signed_in:
        mismatch = (
            f'<p class="notice mismatch" role="status">'
            f"Mismatch: signed in as {html.escape(signed_in_name)}, but showing "
            f"the cached profile for {html.escape(user['name'])}."
            "</p>"
        )
    body = f"""
    <section class="card" data-cache-state="{html.escape(cache_state)}">
      <h1>Profile for {html.escape(user['name'])}</h1>
      {mismatch}
      <table>
        <tr><th>Name</th><td>{html.escape(user['name'])}</td></tr>
        <tr><th>Username</th><td>{html.escape(user['username'])}</td></tr>
        <tr><th>Email</th><td>{html.escape(user['email'])}</td></tr>
        <tr><th>Team role</th><td>{html.escape(user['role'])}</td></tr>
      </table>
      <p><a href="/">Back home</a></p>
      <form method="post" action="/logout">
        <button type="submit">Log out</button>
      </form>
    </section>
"""
    return _page(
        title=f"Profile for {user['name']}",
        signed_in=signed_in,
        body=body,
        mode=mode,
    )


def _error_page(message: str, mode: str) -> str:
    body = f"""
    <section class="card">
      <h1>Request rejected</h1>
      <p class="notice">{html.escape(message)}</p>
      <p><a href="/">Back home</a></p>
    </section>
"""
    return _page(title="Request rejected", signed_in=None, body=body, mode=mode)


def _cookie_value(header: str | None, name: str) -> str | None:
    for part in (header or "").split(";"):
        key, separator, value = part.strip().partition("=")
        if separator and key == name:
            return value
    return None


class SampleAppHandler(BaseHTTPRequestHandler):
    """Shared handler; ``mode`` is set on the per-server subclass."""

    mode = "buggy"
    server_version = "ReproWitnessSample/0.1"

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = urlsplit(self.path).path
        if path == "/healthz":
            self._send(HTTPStatus.OK, "ok\n", "text/plain; charset=utf-8")
        elif path == "/":
            self._send_html(
                HTTPStatus.OK,
                _home_page(self._current_user(), self.mode),
            )
        elif path == "/profile":
            self._profile()
        elif path == "/redirect-external":
            # Test-only route: proves the browser refuses to follow an app
            # redirect to a non-loopback origin.
            self._redirect("https://example.com/")
        elif path == "/favicon.ico":
            self._send(HTTPStatus.NO_CONTENT, "")
        else:
            self._send_html(
                HTTPStatus.NOT_FOUND,
                _error_page(f"No route for {path}.", self.mode),
            )

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = urlsplit(self.path).path
        if path == "/login":
            self._login()
        elif path == "/logout":
            self._logout()
        else:
            self._send_html(
                HTTPStatus.NOT_FOUND,
                _error_page(f"No route for {path}.", self.mode),
            )

    def _profile(self) -> None:
        signed_in = self._current_user()
        if signed_in is None:
            self._redirect("/")
            return

        cached = _cookie_value(self.headers.get("Cookie"), COOKIE_PROFILE)
        cookies: list[str] = []
        if cached in USERS:
            shown = cached
            cache_state = "cache-hit"
        else:
            shown = signed_in
            cache_state = "cache-miss"
            cookies.append(self._profile_cookie(shown))

        self._send_html(
            HTTPStatus.OK,
            _profile_page(signed_in, shown, self.mode, cache_state),
            cookies=cookies,
        )

    def _login(self) -> None:
        form = self._read_form()
        username = (form.get("user") or [""])[0].strip().lower()
        if username not in USERS:
            self._send_html(
                HTTPStatus.BAD_REQUEST,
                _error_page("Unknown demo user. Use 'alice' or 'bob'.", self.mode),
            )
            return

        token = secrets.token_urlsafe(24)
        with _LOCK:
            SESSIONS[token] = username
        self._redirect("/", cookies=[self._session_cookie(token)])

    def _logout(self) -> None:
        token = _cookie_value(self.headers.get("Cookie"), COOKIE_SESSION)
        if token:
            with _LOCK:
                SESSIONS.pop(token, None)

        cookies = [self._session_cookie(None)]
        if self.mode == "fixed":
            # The whole point of the fixed build: cached profile state dies
            # together with the session.
            cookies.append(self._profile_cookie(None))
        self._redirect("/", cookies=cookies)

    def _current_user(self) -> str | None:
        token = _cookie_value(self.headers.get("Cookie"), COOKIE_SESSION)
        if not token:
            return None
        with _LOCK:
            return SESSIONS.get(token)

    def _read_form(self) -> dict[str, list[str]]:
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        if length > 4096:
            return {}
        raw = self.rfile.read(length).decode("utf-8", "replace")
        return parse_qs(raw)

    @staticmethod
    def _session_cookie(token: str | None) -> str:
        if token is None:
            return f"{COOKIE_SESSION}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"
        return f"{COOKIE_SESSION}={token}; Path=/; HttpOnly; SameSite=Lax"

    @staticmethod
    def _profile_cookie(username: str | None) -> str:
        if username is None:
            return f"{COOKIE_PROFILE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"
        return f"{COOKIE_PROFILE}={username}; Path=/; HttpOnly; SameSite=Lax"

    def _send_html(
        self,
        status: HTTPStatus,
        body: str,
        *,
        cookies: list[str] | None = None,
    ) -> None:
        self._send(status, body, "text/html; charset=utf-8", cookies=cookies)

    def _send(
        self,
        status: HTTPStatus,
        body: str,
        content_type: str = "text/html; charset=utf-8",
        *,
        cookies: list[str] | None = None,
        location: str | None = None,
    ) -> None:
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        if location is not None:
            self.send_header("Location", location)
        for cookie in cookies or []:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        if encoded:
            self.wfile.write(encoded)

    def _redirect(self, location: str, *, cookies: list[str] | None = None) -> None:
        self._send(
            HTTPStatus.SEE_OTHER,
            "",
            "text/plain; charset=utf-8",
            cookies=cookies,
            location=location,
        )

    def log_message(self, format: str, *args: Any) -> None:
        sys.stderr.write(f"[sample-app:{self.mode}] {format % args}\n")


class SampleAppServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def create_server(
    *, mode: str, host: str = "127.0.0.1", port: int = 8101
) -> SampleAppServer:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {sorted(MODES)}")
    if host not in HOSTS:
        raise ValueError("The sample app only binds to a loopback host.")
    if not (0 <= port <= 65535):
        raise ValueError("port must be between 0 and 65535.")
    handler = type(f"SampleAppHandler_{mode}", (SampleAppHandler,), {"mode": mode})
    return SampleAppServer((host, port), handler)


def serve(*, mode: str, host: str = "127.0.0.1", port: int = 8101) -> None:
    httpd = create_server(mode=mode, host=host, port=port)
    actual_port = httpd.server_address[1]
    display_host = f"[{host}]" if ":" in host else host
    print(
        f"ReproWitness sample app ({mode}) listening on "
        f"http://{display_host}:{actual_port}",
        flush=True,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
