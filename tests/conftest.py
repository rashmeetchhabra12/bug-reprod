"""Shared fixtures: launch the real sample apps as subprocesses on free ports."""

from __future__ import annotations

import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RECIPE_PATH = PROJECT_ROOT / "recipes" / "sample-alice-bob.json"
TASK_TITLE = "Prepare hackathon demo"
TASK_EXPECTATION = {
    "kind": "region_text_exact",
    "text": TASK_TITLE,
    "region": "Tasks",
}


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for_health(base_url: str, process: subprocess.Popen[str]) -> None:
    deadline = time.time() + 20
    health_url = base_url + "/healthz"
    while time.time() < deadline:
        if process.poll() is not None:
            stderr = process.stderr.read() if process.stderr else ""
            raise RuntimeError(
                f"sample app exited during startup (code {process.returncode}): {stderr}"
            )
        try:
            with urllib.request.urlopen(health_url, timeout=1) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            time.sleep(0.1)
    raise RuntimeError(f"sample app did not become healthy at {health_url}")


def _start_pair(app: str) -> tuple[dict[str, str], list[subprocess.Popen[str]]]:
    processes: list[subprocess.Popen[str]] = []
    started: dict[str, str] = {}

    def start(mode: str) -> str:
        port = free_port()
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "bugrepro",
                "serve",
                "--app",
                app,
                "--mode",
                mode,
                "--port",
                str(port),
            ],
            cwd=PROJECT_ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        processes.append(process)
        base_url = f"http://127.0.0.1:{port}"
        _wait_for_health(base_url, process)
        return base_url

    started["buggy"] = start("buggy")
    started["fixed"] = start("fixed")
    return started, processes


def _stop_all(processes: list[subprocess.Popen[str]]) -> None:
    for process in processes:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


@pytest.fixture(scope="session")
def sample_apps() -> dict[str, str]:
    """Account-switching fixture (Phase 1)."""
    started, processes = _start_pair("accounts")
    try:
        yield started
    finally:
        _stop_all(processes)


@pytest.fixture(scope="session")
def task_apps() -> dict[str, str]:
    """Task-list fixture (Phase 3)."""
    started, processes = _start_pair("tasks")
    try:
        yield started
    finally:
        _stop_all(processes)