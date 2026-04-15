# atlassian-netscaler-mcp

[![tests](https://github.com/paul-pfeiffer/atlassian-netscaler-mcp/actions/workflows/tests.yml/badge.svg)](https://github.com/paul-pfeiffer/atlassian-netscaler-mcp/actions/workflows/tests.yml)

An MCP (Model Context Protocol) server for Jira and Confluence Server / Data Center deployments that sit behind Citrix NetScaler SSO.

It captures the NetScaler session cookie via a real browser (Playwright), stores it in the OS keychain (or a 0600 file under `$XDG_CONFIG_HOME/atlassian-mcp/cookies/` when no keychain is available), and attaches it to every Jira / Confluence request. API calls themselves authenticate with a Personal Access Token.

## Why

Most Atlassian MCP servers assume Atlassian Cloud or a simple API token. Enterprise Server/DC installs typically need more:

- NetScaler / SSO in front of every endpoint
- Session cookies that expire on their own schedule
- Per-project custom fields that are required on create but not discoverable from the API
- Separate Jira and Confluence URLs, one shared gate

This server handles that and exposes a single MCP surface.

## Features

- Unified Jira + Confluence tools in one server
- Single NetScaler login fronts both APIs — one browser window, one cookie
- OS keychain when available (macOS Keychain, libsecret, Windows Credential Manager); atomic 0600 file fallback otherwise, with a one-time warning
- Per-customer profiles for required custom fields and defaults
- SSE / HTTP transport via [FastMCP](https://github.com/jlowin/fastmcp)
- Tolerant init handshake for clients that fire tool calls before the MCP `initialize` handshake completes

## How auth works

```
┌──────────────┐    MCP/SSE    ┌──────────────────────┐   HTTPS + cookie + PAT   ┌──────────────┐
│  MCP client  │ ────────────► │   server.py (this)   │ ───────────────────────► │  Jira / Conf │
│              │ ◄──────────── │                      │ ◄─────────────────────── │   behind SSO │
└──────────────┘               └──────────┬───────────┘                          └──────────────┘
                                          │ on expired cookie
                                          ▼
                                  ┌───────────────┐   Playwright   ┌──────────────┐
                                  │   login.py    │ ─────────────► │  SSO browser │
                                  └───────┬───────┘                └──────────────┘
                                          │ store cookie
                                          ▼
                                OS keychain / 0600 file
```

The NetScaler cookie alone won't authenticate you to Jira/Confluence — it only gets you past the gate. The PAT you set in `CONFLUENCE_TOKEN` / `JIRA_TOKEN` is what the APIs actually check. Both are required.

## Install

Requires Python 3.11+ and [`uv`](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/paul-pfeiffer/atlassian-netscaler-mcp.git
cd atlassian-netscaler-mcp

# One-time: install the browser used for the SSO login flow
uv run --with playwright python -m playwright install chromium
```

## Configure

```bash
cp .envrc.example .envrc
$EDITOR .envrc     # fill in URLs and tokens
direnv allow       # if you use direnv
```

Required:

| Variable            | Description                                                      |
| ------------------- | ---------------------------------------------------------------- |
| `CONFLUENCE_URL`    | Base URL of your Confluence instance                             |
| `JIRA_URL`          | Base URL of your Jira instance                                   |
| `CONFLUENCE_TOKEN`  | Confluence PAT                                                   |
| `JIRA_TOKEN`        | Jira PAT                                                         |

`ATLASSIAN_TOKEN` is accepted as a fallback for both `CONFLUENCE_TOKEN` and `JIRA_TOKEN` if you have a single PAT that works for both.

Optional:

| Variable                    | Default | Description                                                    |
| --------------------------- | ------- | -------------------------------------------------------------- |
| `MCP_TRANSPORT`             | `sse`   | FastMCP transport (`sse`, `http`, `stdio`)                     |
| `MCP_HOST`                  | `127.0.0.1` | Bind host                                                  |
| `MCP_PORT`                  | `8000`  | Bind port                                                      |
| `MCP_AUTO_NETSCALER_LOGIN`  | `1`     | Trigger `login.py` automatically when the cookie is stale      |
| `NETSCALER_LOGIN_URL`       | —       | Explicit URL for the login flow (defaults to `JIRA_URL`)       |
| `NETSCALER_COOKIE`          | —       | Inject a cookie directly, bypassing the store                  |
| `COOKIE_DOMAIN`             | host    | Suffix-match for cookies captured during login                 |
| `JIRA_CUSTOMER_PROFILE`     | —       | Profile name under `config/customers/<name>/`                  |
| `KEYCHAIN_SERVICE`          | `atlassian-mcp` | Keychain service name                                  |

## Run

```bash
./scripts/mcp-start
```

The first call without a valid cookie opens a Chromium window for SSO; complete the login and the server resumes.

### Manual login

```bash
uv run login.py
```

One login covers both Jira and Confluence. The script exits as soon as you've reached the authenticated Atlassian view.

### Wire into an MCP client

```jsonc
// e.g. Claude Desktop config
{
  "mcpServers": {
    "atlassian": {
      "url": "http://127.0.0.1:8000/sse"
    }
  }
}
```

## Customer profiles

Some Jira instances require custom fields on issue creation that aren't discoverable from the API. Drop a profile under `config/customers/<name>/profile.json` — see [`config/customers/example/profile.json`](config/customers/example/profile.json):

```json
{
  "jira": {
    "project_overrides": {
      "MYPROJ": {
        "issue_type_overrides": {
          "Story": {
            "required_fields": ["customfield_12345"],
            "default_fields": {
              "customfield_12345": [{ "key": "DEFAULT-1" }]
            }
          }
        }
      }
    }
  }
}
```

Then set `JIRA_CUSTOMER_PROFILE=<name>`.

## Security notes

- The NetScaler cookie is the only secret stored on disk (or in the keychain). PATs come from env vars and are never persisted.
- File-fallback cookies are written atomically with mode `0600` under `$XDG_CONFIG_HOME/atlassian-mcp/cookies/`. The process logs a warning on first fallback use.
- Nothing in this repo sends data anywhere other than your configured Jira / Confluence URLs.

## Roadmap

- [ ] Headless cookie refresh where the SSO flow allows it
- [ ] Schema-driven profile validation
- [ ] A minimal test suite

## Contributing

Issues and PRs welcome. Don't commit `.envrc` or customer profile files — `config/customers/*/` is gitignored except for `example/`.

## License

[MIT](LICENSE).

Use at your own risk. This software interacts with corporate auth systems and stores session cookies locally; make sure your use complies with your employer's policies and the terms of service of the systems you connect to.
