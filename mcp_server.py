"""Phase 5: MCP server exposing graph tools over stdio.

The Model Context Protocol server exposes the graph tools so agents (including
the guardian) can discover and call them programmatically. The guardian uses
this to run its tool loop.

Run:  python mcp_server.py
(reads stdin, writes to stdout; designed for MCP stdio transport)

Add to Claude Desktop config (~/.config/claude-desktop/config.json):
{
  "mcpServers": {
    "why": {
      "command": "python",
      "args": ["C:\\\\Users\\\\ishan\\\\Desktop\\\\Project\\\\mcp_server.py"],
      "env": {"PYTHONPATH": "C:\\\\Users\\\\ishan\\\\Desktop\\\\Project"}
    }
  }
}
"""

from __future__ import annotations

import json
from typing import Any

from mcp.server.fastmcp import FastMCP

import config
import graph_tools as gt

mcp = FastMCP("why")


@mcp.tool()
def find_file(query: str, limit: int = 10) -> str:
    """Fuzzy-match a file path in the repository graph."""
    return json.dumps(gt.find_file(query, limit))


@mcp.tool()
def file_history(path: str, limit: int = 25) -> str:
    """Merged PRs that touched a file, newest first, with authors and churn."""
    return json.dumps(gt.file_history(path, limit))


@mcp.tool()
def decisions_for_file(path: str, limit: int = 20) -> str:
    """Recorded decisions attached to a file, with the PR/issue where they were made."""
    return json.dumps(gt.decisions_for_file(path, limit))


@mcp.tool()
def search_decisions(query: str, limit: int = 5) -> str:
    """Search decision rationale and PR/issue text, then trace graph neighbors."""
    return json.dumps(gt.search_decisions(query, limit))


@mcp.tool()
def blast_radius(paths: list[str], limit: int = 12) -> str:
    """Files likely to be affected by changing the given files, ranked by a graph algorithm."""
    return json.dumps(gt.blast_radius(paths, limit))


@mcp.tool()
def suggest_reviewers(paths: list[str], exclude: str = "", limit: int = 5) -> str:
    """Suggested reviewers for changes to the given files, ranked by recent authoring and reviewing."""
    return json.dumps(gt.suggest_reviewers(paths, exclude or None, limit))


@mcp.tool()
def who_knows_file(path: str, limit: int = 8) -> str:
    """People ranked by recent authoring and reviewing on a file or its directory."""
    return json.dumps(gt.who_knows_file(path, limit))


def main() -> int:
    """Start the MCP server over stdio."""
    mcp.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
