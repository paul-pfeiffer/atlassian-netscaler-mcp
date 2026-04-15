"""Tests for pure server.py helpers. Import-time side effects (the
CONFLUENCE_URL / JIRA_URL checks) are satisfied by conftest.py."""

import json

import httpx
import pytest


@pytest.fixture
def server_mod():
    import server
    return server


def _mock_transport(responder):
    def handler(request: httpx.Request) -> httpx.Response:
        return responder(request)

    return httpx.MockTransport(handler)


def test_netscaler_cookie_valid_on_200_json(server_mod, monkeypatch):
    def respond(_req):
        return httpx.Response(200, json={"ok": True})

    real_client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kw: real_client(**{**kw, "transport": _mock_transport(respond)}),
    )
    assert server_mod._netscaler_cookie_is_valid(base_url="https://jira.x", cookie="c=1") is True


def test_netscaler_cookie_invalid_on_200_html_login(server_mod, monkeypatch):
    body = "<html><head><title>SSO Login</title></head><body>Please log in</body></html>"

    def respond(_req):
        return httpx.Response(200, headers={"content-type": "text/html"}, text=body)

    real_client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kw: real_client(**{**kw, "transport": _mock_transport(respond)}),
    )
    # Old implementation accepted this (<400 without redirect). The fix
    # sniffs the HTML for SSO markers.
    assert server_mod._netscaler_cookie_is_valid(base_url="https://jira.x", cookie="c=1") is False


def test_netscaler_cookie_invalid_on_redirect_to_sso(server_mod, monkeypatch):
    def respond(_req):
        return httpx.Response(302, headers={"location": "https://netscaler.x/vpn/index.html?sso=1"})

    real_client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kw: real_client(**{**kw, "transport": _mock_transport(respond)}),
    )
    assert server_mod._netscaler_cookie_is_valid(base_url="https://jira.x", cookie="c=1") is False


def test_netscaler_cookie_valid_on_benign_redirect(server_mod, monkeypatch):
    def respond(_req):
        return httpx.Response(302, headers={"location": "/dashboard"})

    real_client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kw: real_client(**{**kw, "transport": _mock_transport(respond)}),
    )
    assert server_mod._netscaler_cookie_is_valid(base_url="https://jira.x", cookie="c=1") is True


def test_netscaler_cookie_invalid_on_5xx(server_mod, monkeypatch):
    def respond(_req):
        return httpx.Response(502)

    real_client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kw: real_client(**{**kw, "transport": _mock_transport(respond)}),
    )
    assert server_mod._netscaler_cookie_is_valid(base_url="https://jira.x", cookie="c=1") is False


def test_confluence_token_prefers_specific(monkeypatch, server_mod):
    monkeypatch.setenv("CONFLUENCE_TOKEN", "specific")
    monkeypatch.setenv("ATLASSIAN_TOKEN", "shared")
    assert server_mod._confluence_token() == "specific"


def test_confluence_token_falls_back_to_shared(monkeypatch, server_mod):
    monkeypatch.delenv("CONFLUENCE_TOKEN", raising=False)
    monkeypatch.setenv("ATLASSIAN_TOKEN", "shared")
    assert server_mod._confluence_token() == "shared"


def test_jira_token_prefers_specific(monkeypatch, server_mod):
    monkeypatch.setenv("JIRA_TOKEN", "specific")
    monkeypatch.setenv("ATLASSIAN_TOKEN", "shared")
    assert server_mod._jira_token() == "specific"


def test_lookup_case_insensitive_hit(server_mod):
    assert server_mod._lookup_case_insensitive({"Foo": 1, "bar": 2}, "foo") == 1
    assert server_mod._lookup_case_insensitive({"Foo": 1, "bar": 2}, "BAR") == 2


def test_lookup_case_insensitive_miss(server_mod):
    assert server_mod._lookup_case_insensitive({"Foo": 1}, "baz") is None


def test_lookup_case_insensitive_strips_whitespace(server_mod):
    assert server_mod._lookup_case_insensitive({" Foo ": 1}, "foo") == 1
