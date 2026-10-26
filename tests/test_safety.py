"""Loopback-only target validation tests."""

from __future__ import annotations

import pytest

from bugrepro.safety import (
    TargetError,
    is_loopback_url,
    resolve_target,
    validate_base_url,
    validate_path,
)


@pytest.mark.parametrize(
    "raw",
    [
        "http://127.0.0.1:8101",
        "http://localhost:8101/",
        "https://[::1]:8443",
        "http://127.0.0.1",
    ],
)
def test_loopback_base_urls_are_accepted(raw: str) -> None:
    assert validate_base_url(raw).startswith(("http://", "https://"))


@pytest.mark.parametrize(
    "raw",
    [
        "https://example.com",
        "http://10.0.0.5:8101",
        "http://127.0.0.1.evil.test:8101",
        "http://user:pass@127.0.0.1:8101",
        "file:///C:/Windows/win.ini",
        "javascript:alert(1)",
        "http://127.0.0.1:8101/profile",
        "http://127.0.0.1:8101/?x=1",
        "http://127.0.0.1:8101/#frag",
        " http://127.0.0.1:8101",
        "http://127.0.0.1:99999",
        "http://127.0.0.1:8101\\@evil.test",
    ],
)
def test_unsafe_base_urls_are_rejected(raw: str) -> None:
    with pytest.raises(TargetError):
        validate_base_url(raw)


@pytest.mark.parametrize(
    "path",
    ["/", "/profile", "/search?q=alice", "/a/b-c_d"],
)
def test_same_origin_paths_are_accepted(path: str) -> None:
    assert validate_path(path) == path


@pytest.mark.parametrize(
    "path",
    ["", "profile", "//evil.test/profile", "https://evil.test", "/a\\b", "/a\nb"],
)
def test_unsafe_paths_are_rejected(path: str) -> None:
    with pytest.raises(TargetError):
        validate_path(path)


def test_resolve_target_concatenates_only_validated_parts() -> None:
    assert resolve_target("http://127.0.0.1:8101", "/profile") == (
        "http://127.0.0.1:8101/profile"
    )
    with pytest.raises(TargetError):
        resolve_target("http://127.0.0.1:8101", "//evil.test")


@pytest.mark.parametrize(
    ("url", "allowed"),
    [
        ("http://127.0.0.1:8101/static/app.css", True),
        ("https://localhost:8443/x", True),
        ("http://[::1]:8101/x", True),
        ("https://example.com/", False),
        ("http://evil.test/", False),
        ("ws://127.0.0.1:8101/socket", False),
        ("data:text/html,<h1>x</h1>", False),
        ("about:blank", False),
    ],
)
def test_page_requests_must_stay_on_loopback(url: str, allowed: bool) -> None:
    assert is_loopback_url(url) is allowed