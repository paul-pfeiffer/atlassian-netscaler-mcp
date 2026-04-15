"""Tests for pure helpers in login.py — the Playwright flow itself
isn't unit-tested (it needs a real browser + SSO IdP)."""

import sys
import types

import pytest


@pytest.fixture
def login_mod():
    # login.py imports playwright at module level. Stub it out so the
    # pure helpers below can be imported without the real package.
    if "playwright" not in sys.modules:
        fake = types.ModuleType("playwright")
        fake_sync = types.ModuleType("playwright.sync_api")
        fake_sync.sync_playwright = lambda: None
        fake_sync.Page = type("Page", (), {})
        sys.modules["playwright"] = fake
        sys.modules["playwright.sync_api"] = fake_sync
    import login
    return login


def test_default_cookie_domain_uses_full_host(login_mod):
    # The old implementation returned "co.uk" here — would match every
    # *.co.uk cookie in the browser.
    assert login_mod.default_cookie_domain("https://jira.example.co.uk") == "jira.example.co.uk"


def test_default_cookie_domain_plain_host(login_mod):
    assert login_mod.default_cookie_domain("https://jira.example.com") == "jira.example.com"


def test_domain_matches_exact(login_mod):
    assert login_mod._domain_matches("example.com", "example.com")


def test_domain_matches_subdomain(login_mod):
    assert login_mod._domain_matches("a.example.com", "example.com")


def test_domain_matches_leading_dot_equivalence(login_mod):
    # RFC 6265: leading dot on a cookie domain is equivalent to no dot.
    assert login_mod._domain_matches(".example.com", "example.com")
    assert login_mod._domain_matches("example.com", ".example.com")


def test_domain_matches_rejects_evil_suffix(login_mod):
    # Previously the code used `cookie_domain in c["domain"]`, which would
    # accept this. The new suffix+boundary match must reject it.
    assert not login_mod._domain_matches("evil-example.com", "example.com")
    assert not login_mod._domain_matches("notexample.com", "example.com")


def test_domain_matches_case_insensitive(login_mod):
    assert login_mod._domain_matches("API.Example.COM", "example.com")


def test_domain_matches_empty(login_mod):
    assert not login_mod._domain_matches("", "example.com")
    assert not login_mod._domain_matches("example.com", "")


def test_resolve_login_url_prefers_explicit(monkeypatch, login_mod):
    monkeypatch.setenv("NETSCALER_LOGIN_URL", "https://netscaler.example.test/")
    monkeypatch.setenv("JIRA_URL", "https://jira.example.test")
    monkeypatch.setenv("CONFLUENCE_URL", "https://confluence.example.test")
    assert login_mod.resolve_login_url() == "https://netscaler.example.test"


def test_resolve_login_url_falls_through_to_jira(monkeypatch, login_mod):
    monkeypatch.delenv("NETSCALER_LOGIN_URL", raising=False)
    monkeypatch.setenv("JIRA_URL", "https://jira.example.test/")
    monkeypatch.setenv("CONFLUENCE_URL", "https://confluence.example.test/")
    assert login_mod.resolve_login_url() == "https://jira.example.test"


def test_resolve_login_url_falls_through_to_confluence(monkeypatch, login_mod):
    monkeypatch.delenv("NETSCALER_LOGIN_URL", raising=False)
    monkeypatch.delenv("JIRA_URL", raising=False)
    monkeypatch.setenv("CONFLUENCE_URL", "https://confluence.example.test/")
    assert login_mod.resolve_login_url() == "https://confluence.example.test"


def test_resolve_login_url_raises_when_none_set(monkeypatch, login_mod):
    monkeypatch.delenv("NETSCALER_LOGIN_URL", raising=False)
    monkeypatch.delenv("JIRA_URL", raising=False)
    monkeypatch.delenv("CONFLUENCE_URL", raising=False)
    with pytest.raises(SystemExit):
        login_mod.resolve_login_url()
