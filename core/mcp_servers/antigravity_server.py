"""
core/mcp_servers/antigravity_server.py
──────────────────────────────────────
Antigravity Bridge MCP Server for JARVIS.

Exposes tools for delegating autonomous coding tasks, code refactoring,
and test execution directly to Antigravity.
"""

import asyncio
import os
import sys
from pathlib import Path

# Add project root to sys.path
BASE_DIR = Path(__file__).resolve().parent.parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from actions.antigravity_bridge import delegate_task, check_task_status, list_tasks
from mcp.server.mcpserver import MCPServer

server = MCPServer("AntigravityBridgeServer")


@server.tool()
def delegate_coding_task(instruction: str, project: str = "", priority: str = "normal") -> str:
    """Delegate a complex coding, refactoring, debugging, or full-stack software development task to Antigravity."""
    return delegate_task(task_description=instruction, priority=priority, project=project)


@server.tool()
def check_antigravity_task_status(task_id: str) -> str:
    """Check the status, summary, and modified files of a delegated Antigravity task."""
    return check_task_status(task_id=task_id)


@server.tool()
def list_antigravity_coding_tasks(limit: int = 5) -> str:
    """List recent pending and completed tasks delegated to Antigravity."""
    return list_tasks(limit=limit)


if __name__ == "__main__":
    asyncio.run(server.run_stdio_async())
