"""Tests for the file-fallback branch of cookie_store.

We force the fallback by raising KeyringError from keyring.set_password
/ get_password, then inspect the on-disk files directly.
"""

import os
import stat

import pytest
from keyring.errors import KeyringError


def _force_keyring_error(monkeypatch):
    import keyring

    def _raise(*args, **kwargs):
        raise KeyringError("no usable backend (test)")

    monkeypatch.setattr(keyring, "set_password", _raise)
    monkeypatch.setattr(keyring, "get_password", _raise)


def test_roundtrip_via_file_fallback(xdg_tmp, monkeypatch):
    _force_keyring_error(monkeypatch)
    import cookie_store

    cookie_store.set_cookie("acc", "abc=123; xsrf=zzz")
    assert cookie_store.get_cookie("acc") == "abc=123; xsrf=zzz"


def test_fallback_file_is_mode_0600(xdg_tmp, monkeypatch):
    _force_keyring_error(monkeypatch)
    import cookie_store

    cookie_store.set_cookie("acc", "secret")
    path = xdg_tmp / "atlassian-mcp" / "cookies" / "acc"
    assert path.exists()
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"


def test_fallback_dir_is_mode_0700(xdg_tmp, monkeypatch):
    _force_keyring_error(monkeypatch)
    import cookie_store

    cookie_store.set_cookie("acc", "x")
    for sub in ("atlassian-mcp", "atlassian-mcp/cookies"):
        path = xdg_tmp / sub
        mode = stat.S_IMODE(os.stat(path).st_mode)
        assert mode == 0o700, f"{sub}: expected 0700, got {oct(mode)}"


def test_account_with_slash_stays_inside_cookies_dir(xdg_tmp, monkeypatch):
    """Separator chars in the account name must not escape the cookies dir."""
    _force_keyring_error(monkeypatch)
    import cookie_store

    cookie_store.set_cookie("evil/../escape", "x")
    cookies_dir = xdg_tmp / "atlassian-mcp" / "cookies"
    files = [f for f in cookies_dir.iterdir() if not f.name.startswith(".tmp-")]
    assert len(files) == 1
    assert files[0].parent == cookies_dir
    assert "/" not in files[0].name


def test_warn_fallback_only_once(xdg_tmp, monkeypatch, capsys):
    _force_keyring_error(monkeypatch)
    import cookie_store

    cookie_store.set_cookie("a", "1")
    cookie_store.set_cookie("b", "2")
    cookie_store.get_cookie("a")

    err = capsys.readouterr().err
    assert err.count("OS keychain unavailable") == 1


def test_get_returns_empty_for_missing(xdg_tmp, monkeypatch):
    _force_keyring_error(monkeypatch)
    import cookie_store

    assert cookie_store.get_cookie("nope") == ""


def test_keychain_path_preferred_when_available(xdg_tmp, monkeypatch):
    """When keyring works, nothing should be written to the fallback dir."""
    import keyring
    import cookie_store

    store = {}
    monkeypatch.setattr(keyring, "set_password", lambda s, a, v: store.update({(s, a): v}))
    monkeypatch.setattr(keyring, "get_password", lambda s, a: store.get((s, a)))

    where = cookie_store.set_cookie("acc", "v")
    assert where == "Keychain"
    assert cookie_store.get_cookie("acc") == "v"
    assert not (xdg_tmp / "atlassian-mcp" / "cookies" / "acc").exists()
