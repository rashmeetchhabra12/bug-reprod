"""Shared fixtures: launch the real sample app as a subprocess on free ports."""

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


@pytest.fixture(scope="session")
def sample_apps() -> dict[str, str]:
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

    try:
        started["buggy"] = start("buggy")
        started["fixed"] = start("fixed")
        yield started
    finally:
        for process in processes:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)