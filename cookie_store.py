"""Cookie storage with OS keychain primary and file-backed fallback.

Tries `keyring` first (macOS Keychain, Windows Credential Manager,
libsecret on Linux). If no usable backend exists, falls back to
plaintext files under $XDG_CONFIG_HOME/atlassian-mcp/cookies/ with
0600 permissions and warns once per process.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import keyring
from keyring.errors import KeyringError

SERVICE = os.environ.get("KEYCHAIN_SERVICE", "atlassian-mcp")

_FALLBACK_WARNED = False


def _ensure_private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def _fallback_dir() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"))
    root = base / "atlassian-mcp"
    cookies = root / "cookies"
    _ensure_private_dir(root)
    _ensure_private_dir(cookies)
    return cookies


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


def _atomic_write_private(path: Path, data: str) -> None:
    """Write `data` to `path` atomically with mode 0600.

    Creates a tempfile in the same directory with 0600 perms (via O_CREAT
    + mode), writes, fsyncs, then os.replace()s it onto the target. This
    avoids the read-window where a naive write_text() + chmod leaves the
    file world-readable with the default umask.
    """
    directory = path.parent
    fd, tmp_name = tempfile.mkstemp(prefix=".tmp-", dir=str(directory))
    try:
        os.chmod(tmp_name, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def set_cookie(account: str, value: str) -> str:
    """Stores the cookie. Returns a label of where it landed."""
    try:
        keyring.set_password(SERVICE, account, value)
        return "Keychain"
    except KeyringError as exc:
        _warn_fallback_once(str(exc) or type(exc).__name__)

    path = _fallback_path(account)
    _atomic_write_private(path, value)
    return f"file://{path}"
