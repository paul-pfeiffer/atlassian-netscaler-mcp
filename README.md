# atlassian-netscaler-mcp

[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)
[![FastMCP](https://img.shields.io/badge/built%20with-FastMCP-6f42c1.svg)](https://github.com/jlowin/fastmcp)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](#license)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](#contributing)

A unified **Model Context Protocol (MCP)** server for **Jira** and **Confluence** Server / Data Center deployments — including the painful ones sitting behind **Citrix NetScaler / SSO**.

It captures your SSO session cookie via a real browser (Playwright), stores it securely in your OS keychain, and reuses it transparently for all Atlassian API calls. Bring your own corporate Atlassian, get a clean MCP interface for your AI tools.

---

## Why?

Most Atlassian MCP servers assume Atlassian Cloud or a plain API token. Enterprise installs usually aren't that friendly:

- 🔒 SSO / SAML / NetScaler in front of every endpoint
- 🍪 Short-lived session cookies that expire mid-task
- 🧩 Customer-specific custom fields that are *required* on creation
- 🧱 Mixed Jira + Confluence URLs, separate auth flows

This server handles all of that, and exposes one tidy MCP surface to Claude / Cursor / any MCP client.

## Features

- ✅ **Unified Jira + Confluence** tools in a single MCP server
- ✅ **Automatic SSO login** — pops a browser when the cookie is gone, then gets out of your way
- ✅ **macOS Keychain** session storage (no plaintext cookies on disk)
- ✅ **Per-customer profiles** for required custom fields, default values, project overrides
- ✅ **1Password CLI** integration for API tokens (optional)
- ✅ **SSE / HTTP transport** via [FastMCP](https://github.com/jlowin/fastmcp)
- ✅ **Tolerant init handshake** for clients that send tool calls before `initialize` completes

## Architecture

```
┌──────────────┐    MCP/SSE    ┌──────────────────────┐    HTTPS+cookie    ┌──────────────┐
│  MCP client  │ ────────────► │   server.py (this)   │ ─────────────────► │  Jira / Conf │
│ (Claude etc) │ ◄──────────── │  + customer profile  │ ◄───────────────── │   behind SSO │
└──────────────┘               └──────────┬───────────┘                    └──────────────┘
                                          │ on 401
                                          ▼
                                  ┌───────────────┐    Playwright    ┌──────────────┐
                                  │   login.py    │ ───────────────► │  SSO browser │
                                  └───────┬───────┘                  └──────────────┘
                                          │ store cookie
                                          ▼
                                   macOS Keychain
```

## Installation

Requires Python **3.11+** and [`uv`](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/paul-pfeiffer/atlassian-netscaler-mcp.git
cd atlassian-netscaler-mcp

# One-time: install Playwright's chromium for the SSO login flow
uv run --with playwright python -m playwright install chromium
```

## Configuration

Copy the example envrc and fill in your endpoints:

```bash
cp .envrc.example .envrc
$EDITOR .envrc          # set CONFLUENCE_URL / JIRA_URL
direnv allow            # if you use direnv
```

Required:

| Variable          | Description                                     |
| ----------------- | ----------------------------------------------- |
| `CONFLUENCE_URL`  | Base URL of your Confluence instance            |
| `JIRA_URL`        | Base URL of your Jira instance                  |

Useful optional:

| Variable                        | Default            | Description                                          |
| ------------------------------- | ------------------ | ---------------------------------------------------- |
| `MCP_TRANSPORT`                 | `sse`              | FastMCP transport (`sse`, `http`, `stdio`)           |
| `MCP_PORT`                      | `8000`             | Port for SSE/HTTP transport                          |
| `MCP_AUTO_NETSCALER_LOGIN`      | `1`                | Auto-trigger `login.py` when cookie missing/expired  |
| `MCP_AUTO_NETSCALER_TARGETS`    | `jira,confluence`  | Which targets to auto-login                          |
| `JIRA_CUSTOMER_PROFILE`         | —                  | Profile name under `config/customers/<name>/`        |
| `CONFLUENCE_TOKEN` / `JIRA_TOKEN` | —                | API tokens (alternative to cookie auth)              |

## Usage

### Start the server

```bash
./scripts/mcp-start
```

The first call against an endpoint without a valid session will pop a Chromium window for SSO; complete the login and the server resumes automatically.

### Manual login

```bash
uv run login.py --target jira
uv run login.py --target confluence
```

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

Some Jira instances require custom fields on issue creation that aren't discoverable from the API. Drop a profile under `config/customers/<name>/profile.json`:

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

Then set `JIRA_CUSTOMER_PROFILE=<name>`. See `config/customers/example/` for a full template.

## Roadmap

- [ ] Cross-platform keychain support (Windows Credential Manager, libsecret)
- [ ] Headless cookie refresh via stored credentials (where SSO allows)
- [ ] Schema-driven profile validation

## Contributing

PRs welcome! Please:

1. Open an issue first for non-trivial changes
2. Keep customer-specific config under `config/customers/<name>/` (gitignored except `example/`)
3. Don't commit `.envrc` or anything containing real URLs / cookies / tokens

## License & Disclaimer

[MIT](LICENSE) © Paul Pfeiffer.

**All liability is excluded** to the maximum extent permitted by law. This
software interacts with corporate auth systems and stores session cookies
locally — use at your own risk and ensure your use complies with your
employer's policies and the terms of service of the systems you connect to.
See [LICENSE](LICENSE) for full disclaimer.

## Acknowledgments

- [FastMCP](https://github.com/jlowin/fastmcp) — the MCP framework doing the heavy lifting
- [Playwright](https://playwright.dev/) — for the SSO browser dance
- The [Model Context Protocol](https://modelcontextprotocol.io) spec
