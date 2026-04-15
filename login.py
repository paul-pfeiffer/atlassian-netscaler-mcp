#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["playwright", "keyring"]
# ///
"""
Opens a browser, waits for you to log in via Citrix NetScaler / SSO,
then stores the resulting session cookie in the OS keychain
(macOS Keychain / libsecret / Windows Credential Manager) with a
file-backed fallback under $XDG_CONFIG_HOME/atlassian-mcp/cookies/.

Only ONE login is needed — the NetScaler cookie fronts both Jira and
Confluence. The actual Jira/Confluence API calls authenticate via PAT
(see CONFLUENCE_TOKEN / JIRA_TOKEN).

First-time setup:
  uv run --with playwright python -m playwright install chromium

Usage:
  uv run login.py

Configuration (env vars):
  JIRA_URL              Base URL used for the login flow (preferred).
  CONFLUENCE_URL        Fallback if JIRA_URL is not set.
  NETSCALER_LOGIN_URL   Explicit override for the login URL.
  COOKIE_DOMAIN         Substring to match against cookie domains
                        (e.g. "example.com"). Defaults to the
                        registrable suffix of the login URL host.
  LOGIN_TIMEOUT_SECONDS How long to wait for SSO completion (default: 300)
"""

import os
import sys
import time
from urllib.parse import urlparse

# PEP-723 `uv run` scripts don't include the script's own directory on
# sys.path, so we add it here to import the sibling cookie_store module.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from playwright.sync_api import sync_playwright, Page

from cookie_store import set_cookie

KEYCHAIN_ACCOUNT = "netscaler-session-cookie"
LEGACY_KEYCHAIN_ACCOUNT = "session-cookie"
SSO_INDICATORS = ["login", "sso", "auth", "saml", "adfs", "idp", "oidc"]


def default_cookie_domain(base_url: str) -> str:
    """Default to the full host — safer than guessing a registrable suffix.

    Naive suffix-splitting gets tripped up by multi-label TLDs like
    .co.uk (would match every *.co.uk cookie). Users with non-host-only
    cookies (e.g. a wildcard SSO domain) can override via COOKIE_DOMAIN.
    """
    return urlparse(base_url).hostname or ""


def _domain_matches(cookie_domain: str, wanted: str) -> bool:
    """Suffix-match with a leading-dot boundary so 'example.com' does NOT
    match 'evil-example.com.attacker'. Handles leading dots in cookie
    domains (RFC 6265: a leading dot is equivalent to no dot)."""
    cookie_domain = cookie_domain.lstrip(".").lower()
    wanted = wanted.lstrip(".").lower()
    if not cookie_domain or not wanted:
        return False
    return cookie_domain == wanted or cookie_domain.endswith("." + wanted)


def resolve_login_url() -> str:
    for env in ("NETSCALER_LOGIN_URL", "JIRA_URL", "CONFLUENCE_URL"):
        value = os.environ.get(env, "").strip().rstrip("/")
        if value:
            return value
    raise SystemExit(
        "Set NETSCALER_LOGIN_URL (or JIRA_URL / CONFLUENCE_URL) so the "
        "login flow knows which URL to open."
    )


def store_cookie(cookie: str) -> str:
    where = set_cookie(KEYCHAIN_ACCOUNT, cookie)
    set_cookie(LEGACY_KEYCHAIN_ACCOUNT, cookie)
    return where


def is_logged_in(page: Page, base_url: str) -> bool:
    url = page.url.lower()
    if not url.startswith(base_url.lower()):
        return False
    if any(s in url for s in SSO_INDICATORS):
        return False
    cookies = {c["name"] for c in page.context.cookies()}
    return bool(cookies & {"JSESSIONID", "atl.xsrf.token", "seraph.rememberme.cookie"})


def cookie_string(page: Page, cookie_domain: str) -> str:
    cookies = page.context.cookies()
    relevant = [c for c in cookies if _domain_matches(c.get("domain", ""), cookie_domain)]
    return "; ".join(f"{c['name']}={c['value']}" for c in relevant)


def main():
    base_url = resolve_login_url()
    cookie_domain = os.environ.get("COOKIE_DOMAIN", "").strip() or default_cookie_domain(base_url)
    timeout_seconds = int(os.environ.get("LOGIN_TIMEOUT_SECONDS", "300"))

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page = browser.new_page()

        print(f"Opening {base_url}")
        print("→ Complete the SSO login in the browser window")
        print("→ This script closes automatically once the session is valid\n")

        deadline = time.monotonic() + timeout_seconds
        try:
            page.goto(base_url)

            while not is_logged_in(page, base_url):
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"Timed out after {timeout_seconds}s waiting for a valid session."
                    )
                page.wait_for_timeout(800)

            cookie = cookie_string(page, cookie_domain)
            if not cookie:
                raise RuntimeError(
                    f"No cookies matching domain '{cookie_domain}' captured after successful login."
                )
            n = len(page.context.cookies())
        finally:
            browser.close()

    print(f"Logged in — captured {n} cookies")
    backend = store_cookie(cookie)
    print(f"Cookie stored in {backend} as '{KEYCHAIN_ACCOUNT}' — session is ready.")


if __name__ == "__main__":
    main()
