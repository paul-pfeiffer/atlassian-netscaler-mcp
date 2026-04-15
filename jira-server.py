#!/usr/bin/env python3
"""Compatibility entrypoint for the unified Atlassian MCP server."""

import os
from server import mcp


if __name__ == "__main__":
    mcp.run(
        transport=os.environ.get("MCP_TRANSPORT", "sse"),
        host=os.environ.get("MCP_HOST", "127.0.0.1"),
        port=int(os.environ.get("MCP_PORT", "8000")),
    )
