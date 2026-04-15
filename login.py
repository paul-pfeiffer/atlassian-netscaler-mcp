#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["playwright", "keyring"]
# ///
"""
Opens a browser, waits for you to log in via SSO,
then stores the session cookie securely in macOS Keychain.

First-time setup:
  uv run --with playwright python -m playwright install chromium

Usage:
  uv run login.py --target jira
  uv run login.py --target confluence

Configuration (env vars):
  JIRA_URL              Base URL of the Jira instance (required for --target jira)
  CONFLUENCE_URL        Base URL of the Confluence instance (required for --target confluence)
  COOKIE_DOMAIN         Substring to match against cookie domains (e.g. "example.com").
                        Defaults to the registrable suffix of the target URL host.
  LOGIN_TIMEOUT_SECONDS How long to wait for SSO completion (default: 300)
"""

import argparse
import os
import time
from urllib.parse import urlparse

import keyring
from playwright.sync_api import sync_playwright, Page

KEYCHAIN_SERVICE = os.environ.get("KEYCHAIN_SERVICE", "atlassian-mcp")
LEGACY_KEYCHAIN_ACCOUNT = "session-cookie"
SSO_INDICATORS = ["login", "sso", "auth", "saml", "adfs", "idp", "oidc"]

TARGETS = {
    "jira": {
        "url_env": "JIRA_URL",
        "keychain_account": "jira-session-cookie",
    },
    "confluence": {
        "url_env": "CONFLUENCE_URL",
        "keychain_account": "confluence-session-cookie",
    },
}


def default_cookie_domain(base_url: str) -> str:
    host = urlparse(base_url).hostname or ""
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def store_cookie(cookie: str, account: str) -> str:
    keyring.set_password(KEYCHAIN_SERVICE, account, cookie)
    keyring.set_password(KEYCHAIN_SERVICE, LEGACY_KEYCHAIN_ACCOUNT, cookie)
    return "Keychain"


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
    relevant = [c for c in cookies if cookie_domain in c.get("domain", "")]
    return "; ".join(f"{c['name']}={c['value']}" for c in relevant)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", choices=["jira", "confluence"], default="jira")
    args = parser.parse_args()

    target = TARGETS[args.target]
    base_url = os.environ.get(target["url_env"], "").rstrip("/")
    if not base_url:
        raise SystemExit(f"{target['url_env']} env var is required for --target {args.target}")

    cookie_domain = os.environ.get("COOKIE_DOMAIN", "").strip() or default_cookie_domain(base_url)
    keychain_account = target["keychain_account"]
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
    backend = store_cookie(cookie, keychain_account)
    print(f"Cookie stored in {backend} ({keychain_account}) — session is ready.")


if __name__ == "__main__":
    main()
