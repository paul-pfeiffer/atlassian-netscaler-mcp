#!/usr/bin/env python3
"""Unified Atlassian MCP server (Confluence + Jira) over HTTP/SSE transport."""

import os
import shutil
import subprocess
import threading
import json
import httpx
from fastmcp import FastMCP

from cookie_store import get_cookie
from mcp import types as mcp_types
from mcp.server import session as mcp_session

CONFLUENCE_URL = os.environ.get("CONFLUENCE_URL", "").rstrip("/")
JIRA_URL = os.environ.get("JIRA_URL", "").rstrip("/")

NETSCALER_KEYCHAIN_ACCOUNT = "netscaler-session-cookie"
AUTO_NETSCALER_LOGIN = os.environ.get("MCP_AUTO_NETSCALER_LOGIN", "1").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
LOGIN_SCRIPT = os.path.join(os.path.dirname(__file__), "login.py")
_AUTH_LOCK = threading.Lock()
_AUTH_READY = False
_NETSCALER_COOKIE: str = ""
_CONFLUENCE_TOKEN: str = ""
_JIRA_TOKEN: str = ""
TOLERATE_EARLY_REQUESTS = os.environ.get("MCP_TOLERATE_EARLY_REQUESTS", "1").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
INIT_GRACE_SECONDS = float(os.environ.get("MCP_INIT_GRACE_SECONDS", "2.0"))
AUTO_INITIALIZE_ON_EARLY_REQUEST = os.environ.get(
    "MCP_AUTO_INITIALIZE_ON_EARLY_REQUEST",
    "1",
).lower() in {
    "1",
    "true",
    "yes",
    "on",
}
JIRA_CUSTOMER_PROFILE = os.environ.get("JIRA_CUSTOMER_PROFILE", "").strip().lower()
JIRA_CUSTOMER_PROFILE_PATH = os.environ.get("JIRA_CUSTOMER_PROFILE_PATH", "").strip()
CUSTOMER_CONFIG_DIR = os.path.join(os.path.dirname(__file__), "config", "customers")
CUSTOMER_PROFILE_SCHEMA = os.path.join(
    os.path.dirname(__file__), "config", "profile.schema.json"
)
_CUSTOMER_PROFILE_LOCK = threading.Lock()
_CUSTOMER_PROFILE: dict | None = None

if not CONFLUENCE_URL:
    raise RuntimeError("CONFLUENCE_URL env var is required")
if not JIRA_URL:
    raise RuntimeError("JIRA_URL env var is required")


def _patch_server_session_init_tolerance() -> None:
    if not TOLERATE_EARLY_REQUESTS:
        return
    if getattr(mcp_session.ServerSession, "_early_request_patch_applied", False):
        return

    async def _received_request_with_grace(self, responder):
        import anyio

        root = responder.request.root
        if isinstance(root, mcp_types.InitializeRequest):
            params = root.params
            requested_version = params.protocolVersion
            self._initialization_state = mcp_session.InitializationState.Initializing
            self._client_params = params
            with responder:
                await responder.respond(
                    mcp_types.ServerResult(
                        mcp_types.InitializeResult(
                            protocolVersion=requested_version
                            if requested_version in mcp_session.SUPPORTED_PROTOCOL_VERSIONS
                            else mcp_types.LATEST_PROTOCOL_VERSION,
                            capabilities=self._init_options.capabilities,
                            serverInfo=mcp_types.Implementation(
                                name=self._init_options.server_name,
                                version=self._init_options.server_version,
                                websiteUrl=self._init_options.website_url,
                                icons=self._init_options.icons,
                            ),
                            instructions=self._init_options.instructions,
                        )
                    )
                )
            self._initialization_state = mcp_session.InitializationState.Initialized
            return
        if isinstance(root, mcp_types.PingRequest):
            return

        deadline = anyio.current_time() + INIT_GRACE_SECONDS
        while (
            self._initialization_state != mcp_session.InitializationState.Initialized
            and anyio.current_time() < deadline
        ):
            await anyio.sleep(0.02)
        if self._initialization_state != mcp_session.InitializationState.Initialized:
            if AUTO_INITIALIZE_ON_EARLY_REQUEST:
                self._initialization_state = mcp_session.InitializationState.Initialized
            else:
                raise RuntimeError("Received request before initialization was complete")

    mcp_session.ServerSession._received_request = _received_request_with_grace
    mcp_session.ServerSession._early_request_patch_applied = True


_patch_server_session_init_tolerance()


def _keychain_cookie() -> str:
    return get_cookie(NETSCALER_KEYCHAIN_ACCOUNT)


_SSO_URL_MARKERS = ("login", "logon", "sso", "saml", "adfs", "oidc")


def _netscaler_cookie_is_valid(*, base_url: str, cookie: str) -> bool:
    """Heuristic: hit the base URL and check we didn't land on a login page.

    NetScaler often serves SSO pages inline as HTTP 200 with HTML body
    (no redirect), so accepting "any <400" would false-positive. We treat
    responses that either redirect to an SSO URL OR return HTML containing
    SSO markers as 'not logged in'.
    """
    with httpx.Client(follow_redirects=False) as client:
        resp = client.get(
            base_url,
            headers={"Cookie": cookie, "Accept": "text/html,application/json"},
        )
    if resp.status_code in (301, 302, 303, 307, 308):
        location = resp.headers.get("location", "").lower()
        return not any(marker in location for marker in _SSO_URL_MARKERS)
    if resp.status_code >= 400:
        return False
    content_type = resp.headers.get("content-type", "").lower()
    if "text/html" in content_type:
        body = resp.text[:4096].lower()
        if any(marker in body for marker in _SSO_URL_MARKERS):
            return False
    return True


def _confluence_token() -> str:
    token = os.environ.get("CONFLUENCE_TOKEN", "").strip()
    if not token:
        token = os.environ.get("ATLASSIAN_TOKEN", "").strip()
    return token


def _jira_token() -> str:
    token = os.environ.get("JIRA_TOKEN", "").strip()
    if not token:
        token = os.environ.get("ATLASSIAN_TOKEN", "").strip()
    return token


def _run_netscaler_login() -> None:
    if not shutil.which("uv"):
        raise RuntimeError(
            "Automatic NetScaler login requires 'uv' on PATH "
            f"to run {LOGIN_SCRIPT}."
        )
    subprocess.run(
        [
            "uv",
            "run",
            "--with",
            "playwright",
            "--with",
            "keyring",
            "python",
            LOGIN_SCRIPT,
        ],
        check=True,
    )


def _load_netscaler_cookie() -> str:
    """Load a NetScaler session cookie, triggering browser login if needed."""
    cookie = os.environ.get("NETSCALER_COOKIE", "").strip()
    if not cookie:
        cookie = _keychain_cookie()

    if cookie and _netscaler_cookie_is_valid(base_url=JIRA_URL, cookie=cookie):
        return cookie

    if not AUTO_NETSCALER_LOGIN:
        raise RuntimeError(
            "No valid NetScaler session cookie found.\n"
            "Run `uv run login.py` to authenticate via browser, or set "
            "MCP_AUTO_NETSCALER_LOGIN=1 to let the server do it."
        )

    _run_netscaler_login()
    cookie = _keychain_cookie()
    if not cookie:
        raise RuntimeError("NetScaler login completed but no cookie was stored.")
    return cookie


mcp = FastMCP("atlassian")


def _ensure_auth_loaded() -> None:
    global _AUTH_READY, _NETSCALER_COOKIE, _CONFLUENCE_TOKEN, _JIRA_TOKEN
    if _AUTH_READY:
        return
    with _AUTH_LOCK:
        if _AUTH_READY:
            return
        confluence_token = _confluence_token()
        jira_token = _jira_token()
        if not confluence_token:
            raise RuntimeError(
                "CONFLUENCE_TOKEN (or ATLASSIAN_TOKEN) env var is required — "
                "the Confluence API is accessed via PAT."
            )
        if not jira_token:
            raise RuntimeError(
                "JIRA_TOKEN (or ATLASSIAN_TOKEN) env var is required — "
                "the Jira API is accessed via PAT."
            )
        _NETSCALER_COOKIE = _load_netscaler_cookie()
        _CONFLUENCE_TOKEN = confluence_token
        _JIRA_TOKEN = jira_token
        _AUTH_READY = True


def _confluence_headers() -> dict:
    _ensure_auth_loaded()
    return {
        "Cookie": _NETSCALER_COOKIE,
        "Authorization": f"Bearer {_CONFLUENCE_TOKEN}",
        "Accept": "application/json",
    }


def _jira_headers() -> dict:
    _ensure_auth_loaded()
    return {
        "Cookie": _NETSCALER_COOKIE,
        "Authorization": f"Bearer {_JIRA_TOKEN}",
        "Accept": "application/json",
    }


def _check_confluence(resp: httpx.Response):
    if resp.status_code in (301, 302, 303, 307, 308):
        raise RuntimeError(
            "Confluence request was redirected — NetScaler cookie may be expired.\n"
            "Run `uv run login.py` to refresh the session cookie."
        )
    resp.raise_for_status()


def _check_jira(resp: httpx.Response):
    if resp.status_code in (301, 302, 303, 307, 308):
        raise RuntimeError(
            f"Redirected to {resp.headers.get('location', '?')} — token blocked by NetScaler."
        )
    if resp.status_code != 204 and not resp.content:
        redacted = {
            k: ("<redacted>" if k.lower() in {"set-cookie", "cookie", "authorization"} else v)
            for k, v in resp.headers.items()
        }
        raise RuntimeError(
            f"Empty response (HTTP {resp.status_code}) — NetScaler may be intercepting. "
            f"Headers: {redacted}"
        )
    content_type = resp.headers.get("content-type", "")
    if "text/html" in content_type:
        raise RuntimeError(
            f"Got HTML instead of JSON (HTTP {resp.status_code}) — likely a NetScaler login page. "
            f"Run `uv run login.py` to refresh the session cookie."
        )
    if resp.status_code == 400:
        details: str
        try:
            payload = resp.json()
            details = json.dumps(payload, ensure_ascii=True)
        except Exception:
            details = resp.text[:800]
        raise RuntimeError(f"Jira rejected request (HTTP 400): {details}")
    resp.raise_for_status()


def _lookup_case_insensitive(mapping: dict, key: str):
    wanted = key.strip().lower()
    for candidate_key, candidate_value in mapping.items():
        if str(candidate_key).strip().lower() == wanted:
            return candidate_value
    return None


def _customer_profile_file() -> str:
    if JIRA_CUSTOMER_PROFILE_PATH:
        if os.path.isabs(JIRA_CUSTOMER_PROFILE_PATH):
            return JIRA_CUSTOMER_PROFILE_PATH
        return os.path.abspath(
            os.path.join(os.path.dirname(__file__), JIRA_CUSTOMER_PROFILE_PATH)
        )
    if JIRA_CUSTOMER_PROFILE:
        profile_value = JIRA_CUSTOMER_PROFILE.strip()
        candidates: list[str] = []
        if os.path.isabs(profile_value):
            if profile_value.lower().endswith(".json"):
                candidates.append(profile_value)
            else:
                candidates.append(f"{profile_value}.json")
                candidates.append(os.path.join(profile_value, "profile.json"))
        else:
            if profile_value.lower().endswith(".json"):
                candidates.append(os.path.join(CUSTOMER_CONFIG_DIR, profile_value))
            else:
                candidates.append(os.path.join(CUSTOMER_CONFIG_DIR, f"{profile_value}.json"))
                candidates.append(os.path.join(CUSTOMER_CONFIG_DIR, profile_value, "profile.json"))

        for candidate in candidates:
            if os.path.exists(candidate):
                return candidate
        return candidates[0] if candidates else ""

    if not JIRA_CUSTOMER_PROFILE:
        if not os.path.isdir(CUSTOMER_CONFIG_DIR):
            return ""
        candidates = sorted(
            os.path.join(root, name)
            for root, _, files in os.walk(CUSTOMER_CONFIG_DIR)
            for name in files
            if name.lower().endswith(".json")
        )
        if len(candidates) == 1:
            return candidates[0]
        preferred_candidates = [
            path
            for path in candidates
            if path.lower().endswith("/profile.json") or path.lower().endswith("/default.json")
        ]
        if len(preferred_candidates) == 1:
            return preferred_candidates[0]
        return ""
    return ""


def _customer_profile_data() -> dict:
    global _CUSTOMER_PROFILE
    if _CUSTOMER_PROFILE is not None:
        return _CUSTOMER_PROFILE

    with _CUSTOMER_PROFILE_LOCK:
        if _CUSTOMER_PROFILE is not None:
            return _CUSTOMER_PROFILE
        profile_file = _customer_profile_file()
        if not profile_file:
            _CUSTOMER_PROFILE = {}
            return _CUSTOMER_PROFILE
        if not os.path.exists(profile_file):
            _CUSTOMER_PROFILE = {}
            return _CUSTOMER_PROFILE

        with open(profile_file, "r", encoding="utf-8") as handle:
            loaded = json.load(handle)
        if not isinstance(loaded, dict):
            raise RuntimeError(f"Customer profile must be a JSON object: {profile_file}")
        _validate_customer_profile(loaded, profile_file)
        _CUSTOMER_PROFILE = loaded
        return _CUSTOMER_PROFILE


def _validate_customer_profile(profile: dict, source: str) -> None:
    """Validate a profile against config/profile.schema.json.

    If the `jsonschema` package isn't installed, skip silently — the
    server still works, callers just lose the up-front error. The
    schema file is optional at runtime; if it's missing, skip.
    """
    if not os.path.exists(CUSTOMER_PROFILE_SCHEMA):
        return
    try:
        import jsonschema
    except ImportError:
        return
    with open(CUSTOMER_PROFILE_SCHEMA, "r", encoding="utf-8") as handle:
        schema = json.load(handle)
    try:
        jsonschema.validate(profile, schema)
    except jsonschema.ValidationError as exc:
        path = "/".join(str(p) for p in exc.absolute_path) or "<root>"
        raise RuntimeError(
            f"Customer profile {source} failed validation at '{path}': {exc.message}"
        ) from exc


def _customer_issue_overrides(project_key: str, issue_type_name: str) -> dict:
    profile = _customer_profile_data()
    if not profile:
        return {}

    root = profile.get("jira", profile)
    if not isinstance(root, dict):
        return {}
    project_overrides = root.get("project_overrides", {})
    if not isinstance(project_overrides, dict):
        return {}
    project_cfg = _lookup_case_insensitive(project_overrides, project_key)
    if not isinstance(project_cfg, dict):
        return {}
    issue_type_overrides = project_cfg.get("issue_type_overrides", {})
    if not isinstance(issue_type_overrides, dict):
        return {}
    issue_cfg = _lookup_case_insensitive(issue_type_overrides, issue_type_name)
    if not isinstance(issue_cfg, dict):
        return {}
    return issue_cfg


def _issue_fields_from_legacy_createmeta(project_key: str, issue_type: str) -> tuple[dict, dict]:
    data: dict | None = None
    with httpx.Client(follow_redirects=False) as client:
        for path in (
            "/rest/api/2/issue/createmeta",
            "/rest/api/latest/issue/createmeta",
        ):
            resp = client.get(
                f"{JIRA_URL}{path}",
                params={
                    "projectKeys": project_key,
                    "expand": "projects.issuetypes.fields",
                },
                headers=_jira_headers(),
            )
            if resp.status_code == 404:
                continue
            _check_jira(resp)
            data = resp.json()
            break
    if data is None:
        return {}, {}

    projects = data.get("projects", [])
    if not projects:
        return {}, {}
    project = projects[0]
    issuetypes = project.get("issuetypes", [])
    requested_issue_type = issue_type.strip().lower()
    issue_type_meta = next(
        (
            it
            for it in issuetypes
            if str(it.get("id", "")).lower() == requested_issue_type
            or it.get("name", "").lower() == requested_issue_type
        ),
        None,
    )
    if not issue_type_meta:
        available = ", ".join(it.get("name", "?") for it in issuetypes) or "none"
        raise ValueError(
            f"Issue type '{issue_type}' is not valid for project '{project_key}'. "
            f"Available types: {available}"
        )
    fields = issue_type_meta.get("fields", {})
    if not isinstance(fields, dict):
        fields = {}
    return issue_type_meta, fields


def _issue_fields_from_modern_createmeta(project_key: str, issue_type: str) -> tuple[dict, dict]:
    with httpx.Client(follow_redirects=False) as client:
        issue_types_resp = client.get(
            f"{JIRA_URL}/rest/api/2/issue/createmeta/{project_key}/issuetypes",
            headers=_jira_headers(),
        )
        if issue_types_resp.status_code == 404:
            return {}, {}
        _check_jira(issue_types_resp)
        issue_types = issue_types_resp.json().get("values", [])
        requested_issue_type = issue_type.strip().lower()
        issue_type_meta = next(
            (
                it
                for it in issue_types
                if str(it.get("id", "")).lower() == requested_issue_type
                or it.get("name", "").lower() == requested_issue_type
            ),
            None,
        )
        if not issue_type_meta:
            available = ", ".join(it.get("name", "?") for it in issue_types) or "none"
            raise ValueError(
                f"Issue type '{issue_type}' is not valid for project '{project_key}'. "
                f"Available types: {available}"
            )
        issue_type_id = str(issue_type_meta.get("id", "")).strip()
        if not issue_type_id:
            return issue_type_meta, {}

        issue_fields_resp = client.get(
            f"{JIRA_URL}/rest/api/2/issue/createmeta/{project_key}/issuetypes/{issue_type_id}",
            params={"maxResults": 200},
            headers=_jira_headers(),
        )
        _check_jira(issue_fields_resp)
        field_values = issue_fields_resp.json().get("values", [])
        fields: dict = {}
        for field_meta in field_values:
            if isinstance(field_meta, dict):
                field_id = field_meta.get("fieldId")
                if field_id:
                    fields[str(field_id)] = field_meta
        return issue_type_meta, fields


def _jira_createmeta(project_key: str, issue_type: str) -> tuple[dict, dict]:
    issue_type_meta, fields = _issue_fields_from_legacy_createmeta(project_key, issue_type)
    if issue_type_meta:
        return issue_type_meta, fields
    issue_type_meta, fields = _issue_fields_from_modern_createmeta(project_key, issue_type)
    if issue_type_meta:
        return issue_type_meta, fields
    # Some Jira deployments disable create metadata; profile/default validation still applies.
    return {"name": issue_type}, {}


def _field_allowed_values(field_meta: dict, limit: int = 8) -> str:
    allowed_values = field_meta.get("allowedValues") or []
    names: list[str] = []
    for value in allowed_values:
        if isinstance(value, dict):
            name = (
                value.get("name")
                or value.get("value")
                or value.get("key")
                or str(value.get("id", ""))
            )
            if name:
                names.append(str(name))
        elif value is not None:
            names.append(str(value))
    if not names:
        return ""
    if len(names) > limit:
        return ", ".join(names[:limit]) + ", ..."
    return ", ".join(names)


def _required_missing_fields(
    issue_fields: dict,
    provided_field_keys: set[str],
    required_overrides: list[str] | None = None,
) -> list[str]:
    missing: list[str] = []
    seen: set[str] = set()
    for field_key, meta in issue_fields.items():
        if not meta.get("required"):
            continue
        if field_key in {"project", "issuetype", "summary"}:
            continue
        if field_key not in provided_field_keys:
            if field_key in seen:
                continue
            seen.add(field_key)
            missing.append(field_key)
    for field_key in required_overrides or []:
        normalized = str(field_key).strip()
        if not normalized or normalized in {"project", "issuetype", "summary"}:
            continue
        if normalized in provided_field_keys or normalized in seen:
            continue
        seen.add(normalized)
        missing.append(normalized)
    return missing


@mcp.tool()
def list_spaces(limit: int = 50) -> str:
    """List all Confluence spaces you have access to."""
    with httpx.Client(follow_redirects=False) as client:
        resp = client.get(
            f"{CONFLUENCE_URL}/rest/api/space",
            params={"limit": limit},
            headers=_confluence_headers(),
        )
        _check_confluence(resp)
        spaces = resp.json().get("results", [])
        if not spaces:
            return "No spaces found."
        return "\n".join(f"{s['key']}: {s['name']}" for s in spaces)


@mcp.tool()
def search_pages(query: str, space_key: str = "", limit: int = 10) -> str:
    """Search Confluence pages by text. Optionally filter by space_key."""
    cql = f'text ~ "{query}" AND type = page'
    if space_key:
        cql += f' AND space.key = "{space_key}"'
    with httpx.Client(follow_redirects=False) as client:
        resp = client.get(
            f"{CONFLUENCE_URL}/rest/api/content/search",
            params={"cql": cql, "limit": limit, "expand": "space"},
            headers=_confluence_headers(),
        )
        _check_confluence(resp)
        results = resp.json().get("results", [])
        if not results:
            return "No pages found."
        return "\n".join(
            f"[{r['id']}] {r['space']['key']} / {r['title']}" for r in results
        )


@mcp.tool()
def get_page(page_id: str) -> str:
    """Get the full content of a Confluence page by its numeric ID."""
    with httpx.Client(follow_redirects=False) as client:
        resp = client.get(
            f"{CONFLUENCE_URL}/rest/api/content/{page_id}",
            params={"expand": "body.storage,version,space"},
            headers=_confluence_headers(),
        )
        _check_confluence(resp)
        page = resp.json()
        return (
            f"# {page['title']}\n"
            f"ID: {page['id']} | Space: {page['space']['key']} | Version: {page['version']['number']}\n\n"
            f"{page['body']['storage']['value']}"
        )


@mcp.tool()
def get_page_by_title(title: str, space_key: str = "") -> str:
    """Find a Confluence page by its title. Optionally filter by space_key."""
    params: dict = {"title": title, "expand": "body.storage,version,space"}
    if space_key:
        params["spaceKey"] = space_key
    with httpx.Client(follow_redirects=False) as client:
        resp = client.get(
            f"{CONFLUENCE_URL}/rest/api/content",
            params=params,
            headers=_confluence_headers(),
        )
        _check_confluence(resp)
        results = resp.json().get("results", [])
        if not results:
            return f"No page found with title '{title}'."
        page = results[0]
        return (
            f"# {page['title']}\n"
            f"ID: {page['id']} | Space: {page['space']['key']} | Version: {page['version']['number']}\n\n"
            f"{page['body']['storage']['value']}"
        )


@mcp.tool()
def get_child_pages(page_id: str) -> str:
    """List child pages of a given page ID."""
    with httpx.Client(follow_redirects=False) as client:
        resp = client.get(
            f"{CONFLUENCE_URL}/rest/api/content/{page_id}/child/page",
            params={"expand": "space"},
            headers=_confluence_headers(),
        )
        _check_confluence(resp)
        results = resp.json().get("results", [])
        if not results:
            return "No child pages found."
        return "\n".join(f"[{r['id']}] {r['title']}" for r in results)


@mcp.tool()
def search_issues(jql: str, limit: int = 20) -> str:
    """Search Jira issues using JQL. Example: project = FOO AND status = 'In Progress'"""
    with httpx.Client(follow_redirects=False) as client:
        resp = client.post(
            f"{JIRA_URL}/rest/api/2/search",
            json={"jql": jql, "maxResults": limit, "fields": ["summary", "status", "assignee", "priority", "issuetype"]},
            headers=_jira_headers(),
        )
        _check_jira(resp)
        issues = resp.json().get("issues", [])
        if not issues:
            return "No issues found."
        lines = []
        for issue in issues:
            fields = issue["fields"]
            assignee = fields["assignee"]["displayName"] if fields.get("assignee") else "Unassigned"
            lines.append(f"[{issue['key']}] {fields['summary']} | {fields['status']['name']} | {assignee}")
        return "\n".join(lines)


@mcp.tool()
def get_issue(issue_key: str) -> str:
    """Get full details of a Jira issue by key (e.g. PROJ-123)."""
    with httpx.Client(follow_redirects=False) as client:
        resp = client.get(
            f"{JIRA_URL}/rest/api/2/issue/{issue_key}",
            params={"expand": "renderedFields"},
            headers=_jira_headers(),
        )
        if resp.status_code == 404:
            project_hint = ""
            if "-" in issue_key:
                project_hint = issue_key.split("-", 1)[0].upper()
            hint = (
                f" Try: search_issues(\"project = {project_hint} ORDER BY updated DESC\", limit=20)."
                if project_hint
                else ""
            )
            return (
                f"Issue '{issue_key}' was not found (or you don't have permission to view it)."
                f"{hint}"
            )
        _check_jira(resp)
        issue = resp.json()
        fields = issue["fields"]
        assignee = fields["assignee"]["displayName"] if fields.get("assignee") else "Unassigned"
        reporter = fields["reporter"]["displayName"] if fields.get("reporter") else "Unknown"
        description = fields.get("description") or "No description"
        return (
            f"# [{issue['key']}] {fields['summary']}\n"
            f"Type: {fields['issuetype']['name']} | Status: {fields['status']['name']} | Priority: {fields['priority']['name']}\n"
            f"Assignee: {assignee} | Reporter: {reporter}\n\n"
            f"{description}"
        )


@mcp.tool()
def list_projects(limit: int = 50) -> str:
    """List all Jira projects you have access to."""
    with httpx.Client(follow_redirects=False) as client:
        resp = client.get(
            f"{JIRA_URL}/rest/api/2/project",
            params={"maxResults": limit},
            headers=_jira_headers(),
        )
        _check_jira(resp)
        projects = resp.json()
        if not projects:
            return "No projects found."
        return "\n".join(f"{project['key']}: {project['name']}" for project in projects)


@mcp.tool()
def get_my_issues(limit: int = 20) -> str:
    """Get all issues assigned to you."""
    with httpx.Client(follow_redirects=False) as client:
        resp = client.post(
            f"{JIRA_URL}/rest/api/2/search",
            json={"jql": "assignee = currentUser() AND resolution = Unresolved ORDER BY updated DESC",
                  "maxResults": limit,
                  "fields": ["summary", "status", "priority", "project"]},
            headers=_jira_headers(),
        )
        _check_jira(resp)
        issues = resp.json().get("issues", [])
        if not issues:
            return "No open issues assigned to you."
        return "\n".join(
            f"[{issue['key']}] {issue['fields']['summary']} | {issue['fields']['status']['name']}"
            for issue in issues
        )


@mcp.tool()
def get_create_requirements(project_key: str, issue_type: str = "Task") -> str:
    """Get required/optional fields for creating a Jira issue in a project + issue type."""
    issue_type_meta, issue_fields = _jira_createmeta(project_key, issue_type)
    issue_type_name = issue_type_meta.get("name", issue_type)
    customer_overrides = _customer_issue_overrides(project_key, issue_type_name)
    customer_required = customer_overrides.get("required_fields", [])
    if not isinstance(customer_required, list):
        customer_required = []
    customer_defaults = customer_overrides.get("default_fields", {})
    if not isinstance(customer_defaults, dict):
        customer_defaults = {}

    required_lines: list[str] = []
    optional_lines: list[str] = []
    for field_key, meta in issue_fields.items():
        field_name = meta.get("name", field_key)
        schema = meta.get("schema", {})
        schema_type = schema.get("type") or schema.get("custom") or "any"
        allowed = _field_allowed_values(meta)
        line = f"- {field_key} ({field_name}) | type={schema_type}"
        if allowed:
            line += f" | allowed={allowed}"
        if meta.get("required") or field_key in customer_required:
            required_lines.append(line)
        else:
            optional_lines.append(line)

    for field_key in customer_required:
        if field_key in issue_fields:
            continue
        required_lines.append(f"- {field_key} (customer profile override)")

    defaults_lines = [
        f"- {field_key}: {json.dumps(value, ensure_ascii=True)}"
        for field_key, value in customer_defaults.items()
    ]

    required_block = "\n".join(required_lines) if required_lines else "- none"
    optional_block = "\n".join(optional_lines[:30]) if optional_lines else "- none"
    defaults_block = "\n".join(defaults_lines) if defaults_lines else "- none"
    return (
        f"Create requirements for project '{project_key}', issue type '{issue_type_name}':\n\n"
        f"Required fields:\n{required_block}\n\n"
        f"Optional fields (first 30):\n{optional_block}\n\n"
        f"Customer profile defaults:\n{defaults_block}\n\n"
        "Tip: pass custom required fields via create_issue(..., additional_fields_json='{\"customfield_12345\":\"value\"}')"
    )


@mcp.tool()
def create_issue(
    project_key: str,
    summary: str,
    description: str = "",
    issue_type: str = "Task",
    assignee_name: str = "",
    assignee_account_id: str = "",
    priority: str = "",
    additional_fields_json: str = "",
) -> str:
    """Create a Jira issue."""
    issue_type_meta, issue_fields = _jira_createmeta(project_key, issue_type)
    issue_type_name = issue_type_meta.get("name", issue_type)
    customer_overrides = _customer_issue_overrides(project_key, issue_type_name)
    customer_defaults = customer_overrides.get("default_fields", {})
    if not isinstance(customer_defaults, dict):
        customer_defaults = {}
    customer_required = customer_overrides.get("required_fields", [])
    if not isinstance(customer_required, list):
        customer_required = []

    additional_fields: dict = {}
    if additional_fields_json:
        try:
            decoded = json.loads(additional_fields_json)
        except json.JSONDecodeError as exc:
            raise ValueError(f"additional_fields_json must be valid JSON: {exc}") from exc
        if not isinstance(decoded, dict):
            raise ValueError("additional_fields_json must decode to a JSON object.")
        additional_fields = decoded

    blocked_overrides = {"project", "summary", "issuetype"} & set(additional_fields)
    if blocked_overrides:
        blocked = ", ".join(sorted(blocked_overrides))
        raise ValueError(f"additional_fields_json cannot override core fields: {blocked}")

    merged_custom_fields = dict(customer_defaults)
    merged_custom_fields.update(additional_fields)

    provided_field_keys: set[str] = {"project", "summary", "issuetype"}
    if description:
        provided_field_keys.add("description")
    if assignee_name or assignee_account_id:
        provided_field_keys.add("assignee")
    if priority:
        provided_field_keys.add("priority")
    provided_field_keys.update(merged_custom_fields.keys())

    missing_required = _required_missing_fields(
        issue_fields,
        provided_field_keys,
        required_overrides=[str(field) for field in customer_required],
    )
    if missing_required:
        details = []
        for key in missing_required:
            meta = issue_fields.get(key, {})
            details.append(f"{key} ({meta.get('name', key)})")
        raise ValueError(
            "Missing required fields for issue creation: "
            + ", ".join(details)
            + ". Use get_create_requirements(project_key, issue_type) and provide them via additional_fields_json."
        )

    fields: dict = {
        "project": {"key": project_key},
        "summary": summary,
        "issuetype": (
            {"id": issue_type_meta.get("id")}
            if issue_type_meta.get("id")
            else {"name": issue_type_meta.get("name", issue_type)}
        ),
    }
    if description:
        fields["description"] = description
    if assignee_account_id:
        fields["assignee"] = {"accountId": assignee_account_id}
    elif assignee_name:
        fields["assignee"] = {"name": assignee_name}
    if priority:
        fields["priority"] = {"name": priority}
    fields.update(merged_custom_fields)

    with httpx.Client(follow_redirects=False) as client:
        resp = client.post(
            f"{JIRA_URL}/rest/api/2/issue",
            json={"fields": fields},
            headers=_jira_headers(),
        )
        _check_jira(resp)
        issue = resp.json()
        return f"Issue created: {issue.get('key', '?')} (id: {issue.get('id', '?')})."


@mcp.tool()
def add_worklog(
    issue_key: str,
    time_spent: str,
    comment: str = "",
    started: str = "",
    adjust_estimate: str = "auto",
    new_estimate: str = "",
    reduce_by: str = "",
) -> str:
    """Create a Jira time log entry for an issue."""
    allowed_adjust_estimates = {"auto", "leave", "new", "manual"}
    if adjust_estimate not in allowed_adjust_estimates:
        raise ValueError(
            "adjust_estimate must be one of: auto, leave, new, manual"
        )
    if adjust_estimate == "new" and not new_estimate:
        raise ValueError("new_estimate is required when adjust_estimate='new'")
    if adjust_estimate == "manual" and not reduce_by:
        raise ValueError("reduce_by is required when adjust_estimate='manual'")

    params: dict = {"adjustEstimate": adjust_estimate}
    if adjust_estimate == "new":
        params["newEstimate"] = new_estimate
    if adjust_estimate == "manual":
        params["reduceBy"] = reduce_by

    payload: dict = {"timeSpent": time_spent}
    if comment:
        payload["comment"] = comment
    if started:
        payload["started"] = started

    with httpx.Client(follow_redirects=False) as client:
        resp = client.post(
            f"{JIRA_URL}/rest/api/2/issue/{issue_key}/worklog",
            params=params,
            json=payload,
            headers=_jira_headers(),
        )
        _check_jira(resp)
        worklog = resp.json()
        return (
            f"Worklog added to {issue_key}: "
            f"{worklog.get('timeSpent', time_spent)} "
            f"(id: {worklog.get('id', '?')})."
        )


@mcp.tool()
def add_comment(issue_key: str, comment: str) -> str:
    """Add a comment to a Jira issue."""
    with httpx.Client(follow_redirects=False) as client:
        resp = client.post(
            f"{JIRA_URL}/rest/api/2/issue/{issue_key}/comment",
            json={"body": comment},
            headers=_jira_headers(),
        )
        _check_jira(resp)
        return f"Comment added to {issue_key}."


if __name__ == "__main__":
    mcp.run(
        transport=os.environ.get("MCP_TRANSPORT", "sse"),
        host=os.environ.get("MCP_HOST", "127.0.0.1"),
        port=int(os.environ.get("MCP_PORT", "8000")),
    )
