"""Cookie storage with OS keychain primary and file-backed fallback.

Tries `keyring` first (macOS Keychain, Windows Credential Manager,
libsecret on Linux). If no usable backend exists, falls back to
plaintext files under $XDG_CONFIG_HOME/atlassian-mcp/cookies/ with
0600 permissions and warns once per process.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import keyring
from keyring.errors import KeyringError

SERVICE = os.environ.get("KEYCHAIN_SERVICE", "atlassian-mcp")

_FALLBACK_WARNED = False


def _fallback_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    path = Path(base) / "atlassian-mcp" / "cookies"
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def _fallback_path(account: str) -> Path:
    safe = account.replace("/", "_").replace(os.sep, "_")
    return _fallback_dir() / safe


def _warn_fallback_once(reason: str) -> None:
    global _FALLBACK_WARNED
    if _FALLBACK_WARNED:
        return
    _FALLBACK_WARNED = True
    print(
        f"[atlassian-mcp] WARNING: OS keychain unavailable ({reason}); "
        f"falling back to plaintext file storage under {_fallback_dir()}",
        file=sys.stderr,
    )


def get_cookie(account: str) -> str:
    try:
        value = keyring.get_password(SERVICE, account)
        if value:
            return value
    except KeyringError as exc:
        _warn_fallback_once(str(exc) or type(exc).__name__)

    path = _fallback_path(account)
    if path.exists():
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""
    return ""


def set_cookie(account: str, value: str) -> str:
    """Stores the cookie. Returns a label of where it landed."""
    try:
        keyring.set_password(SERVICE, account, value)
        return "Keychain"
    except KeyringError as exc:
        _warn_fallback_once(str(exc) or type(exc).__name__)

    path = _fallback_path(account)
    path.write_text(value, encoding="utf-8")
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    return f"file://{path}"
