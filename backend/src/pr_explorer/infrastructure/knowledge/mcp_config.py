"""Jira/Slack knowledge is reached through MCP tools *already configured in the user's Claude CLI*.
We only pass an allow-list of read-only MCP tool names to --allowedTools (default: none).
Claude runs with --strict-mcp-config, so the servers behind those tools must be given via
PR_EXPLORER_MCP_CONFIG (JSON string or file path, passed as --mcp-config)."""

from __future__ import annotations

import os
import re

MCP_TOOL = re.compile(r"^mcp__[A-Za-z0-9_-]+__[A-Za-z0-9_-]+$")
WRITE_HINT = re.compile(r"(create|update|edit|delete|send|post|write|transition|add|upload|schedule)", re.I)


def allowed_mcp_tools(raw: str | None = None) -> list[str]:
    """Parse PR_EXPLORER_MCP_TOOLS (comma-separated). Rejects malformed or write-looking names."""
    raw = os.environ.get("PR_EXPLORER_MCP_TOOLS", "") if raw is None else raw
    out = []
    for name in (n.strip() for n in raw.split(",")):
        if name and MCP_TOOL.match(name) and not WRITE_HINT.search(name.split("__")[-1]):
            out.append(name)
    return out
