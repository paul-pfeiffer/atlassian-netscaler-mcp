"""Shared fixtures.

`server.py` raises at import time if CONFLUENCE_URL / JIRA_URL aren't
set, so we seed them here before any test touches the module.
"""

import os
import sys

import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)

os.environ.setdefault("CONFLUENCE_URL", "https://confluence.example.test")
os.environ.setdefault("JIRA_URL", "https://jira.example.test")
os.environ.setdefault("MCP_AUTO_NETSCALER_LOGIN", "0")


@pytest.fixture
def xdg_tmp(tmp_path, monkeypatch):
    """Point $XDG_CONFIG_HOME at a per-test tmpdir and reset the
    warn-once flag so each test starts clean."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    import cookie_store
    monkeypatch.setattr(cookie_store, "_FALLBACK_WARNED", False)
    return tmp_path
