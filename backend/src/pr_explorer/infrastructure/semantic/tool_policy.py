"""Claude tool permissions: read-only only (spec §11)."""

from __future__ import annotations

READ_ONLY_TOOLS = ("Read", "Grep", "Glob")
DISALLOWED_TOOLS = ("Bash", "Edit", "Write", "MultiEdit", "NotebookEdit", "WebFetch", "WebSearch", "Task", "Agent")


def allowed_tools(mcp_tools: list[str]) -> list[str]:
    return list(READ_ONLY_TOOLS) + [t for t in mcp_tools if t not in DISALLOWED_TOOLS]
