"""Task-list fixture.

Deliberately separate from the account-switching fixture. Both modes render
the same accessible controls and layout:

* a labelled task-title textbox,
* an "Add task" button,
* a region named "Tasks" containing the added tasks as list items.

``buggy``
    Submitting a task stores it only in a one-shot flash cookie. The redirect
    renders it once, and the following GET clears the flash. Reloading ``/``
    therefore shows an empty task list, so the task disappears.

``fixed``
    Submitting a task stores it in a per-context task cookie and redirects back
    to ``/``; reloading shows the task again.

Storage lives in a cookie, so a fresh browser context (one per investigation or
replay) starts with no tasks. The app never uses server-side global state.
"""

from __future__ import annotations

import html
import json
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlsplit

HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
MODES = frozenset({"buggy", "fixed"})
COOKIE_TASKS = "bugrepro_tasks"
COOKIE_FLASH = "bugrepro_flash"

MAX_TASKS = 10
MAX_TITLE_LENGTH = 120

CSS = """
:root { color-scheme: light; --ink:#172026; --muted:#5b6b76; --line:#d9e1e6; --bug:#b42318; }
* { box-sizing: border-box; }
body { margin:0; font:16px/1.5 "Segoe UI", system-ui, sans-serif; color:var(--ink); background:#f6f8f9; }
.topbar { display:flex; justify-content:space-between; align-items:center; gap:16px;
          padding:14px 24px; background:#fff; border-bottom:1px solid var(--line); }
.topbar p { margin:0; }
main { max-width:680px; margin:48px auto; padding:0 24px; }
.card { background:#fff; border:1px solid var(--line); border-radius:12px; padding:28px; }
h1 { margin-top:0; font-size:1.7rem; }
h2 { font-size:1.1rem; margin:0 0 12px; }
label { display:block; font-weight:600; margin-bottom:6px; }
input[type=text] { width:100%; max-width:360px; padding:9px 11px; border:1px solid #b8c4cc;
                   border-radius:8px; font:inherit; }
button { margin-top:14px; padding:9px 16px; border:0; border-radius:8px;
         background:var(--ink); color:#fff; font:inherit; cursor:pointer; }
button:hover { background:#2b3a44; }
form { margin:0 0 26px; }
.tasks { border-top:1px solid var(--line); padding-top:22px; }
ul { margin:0; padding-left:22px; }
li { padding:6px 0; border-bottom:1px solid #eef2f4; }
li:last-child { border-bottom:0; }
.empty { color:var(--muted); margin:0; }
.notice { margin:18px 0 0; padding:12px 14px; border-left:4px solid var(--bug);
          background:#fef3f2; color:#7a271a; }
footer { max-width:680px; margin:0 auto 40px; padding:0 24px; color:var(--muted); font-size:.9rem; }
code { background:#eef2f4; padding:1px 5px; border-radius:4px; }
"""


def _page(*, mode: str, tasks: list[str], error: str | None = None) -> str:
    if tasks:
        items = "\n".join(
            f"        <li>{html.escape(task)}</li>" for task in tasks
        )
        task_body = f"      <ul>\n{items}\n      </ul>"
    else:
        task_body = '      <p class="empty">No tasks yet.</p>'

    error_block = ""
    if error:
        error_block = (
            f'    <p class="notice" role="alert">{html.escape(error)}</p>\n'
        )

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Task list</title>
  <style>{CSS}</style>
</head>
<body>
  <header class="topbar">
    <p><strong>Task list demo</strong></p>
    <p>Mode: <code>{html.escape(mode)}</code></p>
  </header>
  <main>
    <section class="card">
      <h1>Task list</h1>
      <form method="post" action="/">
        <label for="task-title">Task title</label>
        <input id="task-title" name="title" type="text" autocomplete="off"
               spellcheck="false" maxlength="{MAX_TITLE_LENGTH}">
        <button type="submit">Add task</button>
      </form>
{error_block}      <section class="tasks" role="region" aria-label="Tasks">
        <h2>Tasks</h2>
{task_body}
      </section>
    </section>
  </main>
  <footer>Sample app mode: <code>{html.escape(mode)}</code>.
    Tasks are stored per browser context.</footer>
</body>
</html>
"""


def _read_tasks(header: str | None, name: str = COOKIE_TASKS) -> list[str]:
    raw = _cookie_value(header, name)
    if not raw:
        return []
    try:
        parsed = json.loads(unquote(raw))
    except (ValueError, json.JSONDecodeError):
        return []
    if not isinstance(parsed, list):
        return []
    tasks = [
        item
        for item in parsed
        if isinstance(item, str) and item.strip() and len(item) <= MAX_TITLE_LENGTH
    ]
    return tasks[:MAX_TASKS]


def _cookie_value(header: str | None, name: str) -> str | None:
    for part in (header or "").split(";"):
        key, separator, value = part.strip().partition("=")
        if separator and key == name:
            return value
    return None


def _encode_tasks(tasks: list[str]) -> str:
    return quote(json.dumps(tasks, ensure_ascii=False), safe="")


def _cookie(name: str, value: str | None) -> str:
    if value is None:
        return f"{name}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"
    return f"{name}={value}; Path=/; Max-Age=86400; HttpOnly; SameSite=Lax"


class TaskAppHandler(BaseHTTPRequestHandler):
    mode = "buggy"
    server_version = "BugReproTaskApp/0.1"

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = urlsplit(self.path).path
        if path == "/healthz":
            self._send(HTTPStatus.OK, "ok\n", "text/plain; charset=utf-8")
            return
        if path in ("/", "/index.html"):
            cookies: list[str] = []
            if self.mode == "fixed":
                tasks = _read_tasks(self.headers.get("Cookie"), COOKIE_TASKS)
            else:
                # The buggy build only ever shows a one-shot flash task, and
                # clears it in the same response that renders it.
                tasks = _read_tasks(self.headers.get("Cookie"), COOKIE_FLASH)
                if tasks:
                    cookies.append(_cookie(COOKIE_FLASH, None))
            self._send_html(
                HTTPStatus.OK, _page(mode=self.mode, tasks=tasks), cookies=cookies
            )
            return
        if path == "/favicon.ico":
            self._send(HTTPStatus.NO_CONTENT, "")
            return
        self._send_html(
            HTTPStatus.NOT_FOUND,
            _page(mode=self.mode, tasks=[], error=f"No route for {path}."),
        )

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = urlsplit(self.path).path
        if path != "/":
            self._send_html(
                HTTPStatus.NOT_FOUND,
                _page(mode=self.mode, tasks=[], error=f"No route for {path}."),
            )
            return

        form = self._read_form()
        title = (form.get("title") or [""])[0].strip()
        if not title:
            tasks = (
                _read_tasks(self.headers.get("Cookie"), COOKIE_TASKS)
                if self.mode == "fixed"
                else _read_tasks(self.headers.get("Cookie"), COOKIE_FLASH)
            )
            self._send_html(
                HTTPStatus.BAD_REQUEST,
                _page(mode=self.mode, tasks=tasks, error="Enter a task title."),
            )
            return
        if len(title) > MAX_TITLE_LENGTH:
            tasks = (
                _read_tasks(self.headers.get("Cookie"), COOKIE_TASKS)
                if self.mode == "fixed"
                else _read_tasks(self.headers.get("Cookie"), COOKIE_FLASH)
            )
            self._send_html(
                HTTPStatus.BAD_REQUEST,
                _page(
                    mode=self.mode,
                    tasks=tasks,
                    error=f"Task titles are limited to {MAX_TITLE_LENGTH} characters.",
                ),
            )
            return

        if self.mode == "buggy":
            # Looks like it worked: the redirect renders the new task once.
            # Only a flash cookie holds it, and the next GET clears it.
            self._redirect(
                "/", cookies=[_cookie(COOKIE_FLASH, _encode_tasks([title]))]
            )
            return

        tasks = _read_tasks(self.headers.get("Cookie"), COOKIE_TASKS)
        tasks.append(title)
        tasks = tasks[-MAX_TASKS:]
        self._redirect(
            "/",
            cookies=[_cookie(COOKIE_TASKS, _encode_tasks(tasks))],
        )

    def _read_form(self) -> dict[str, list[str]]:
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = 0
        if length <= 0 or length > 8192:
            return {}
        raw = self.rfile.read(length).decode("utf-8", "replace")
        return parse_qs(raw)

    def _send_html(
        self,
        status: HTTPStatus,
        body: str,
        *,
        cookies: list[str] | None = None,
        location: str | None = None,
    ) -> None:
        self._send(
            status,
            body,
            "text/html; charset=utf-8",
            cookies=cookies,
            location=location,
        )

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
        sys.stderr.write(f"[task-app:{self.mode}] {format % args}\n")


class TaskAppServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def create_server(
    *, mode: str, host: str = "127.0.0.1", port: int = 8103
) -> TaskAppServer:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {sorted(MODES)}")
    if host not in HOSTS:
        raise ValueError("The task app only binds to a loopback host.")
    if not (0 <= port <= 65535):
        raise ValueError("port must be between 0 and 65535.")
    handler = type(f"TaskAppHandler_{mode}", (TaskAppHandler,), {"mode": mode})
    return TaskAppServer((host, port), handler)


def serve(*, mode: str, host: str = "127.0.0.1", port: int = 8103) -> None:
    httpd = create_server(mode=mode, host=host, port=port)
    actual_port = httpd.server_address[1]
    display_host = f"[{host}]" if ":" in host else host
    print(
        f"Bug Repro task app ({mode}) listening on "
        f"http://{display_host}:{actual_port}",
        flush=True,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()