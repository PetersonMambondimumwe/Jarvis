"""
core/mcp_client.py
──────────────────
Model Context Protocol (MCP) Client for JARVIS.

Enables JARVIS to act as an MCP Client by connecting to local (stdio) and
remote (SSE/HTTP) MCP servers defined in config/mcp_servers.json.

Key Capabilities:
  1. Spawns/connects to configured MCP servers using official `mcp` SDK.
  2. Discovers tools dynamically via `session.list_tools()`.
  3. Converts MCP JSON Schema into Google Gemini Function Declarations.
  4. Dispatches tool calls from Gemini Live to the respective MCP server session.
  5. Formats results cleanly for Gemini reasoning and TTS speech.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    _MCP_AVAILABLE = True
except ImportError:
    _MCP_AVAILABLE = False


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


CONFIG_PATH = _base_dir() / "config" / "mcp_servers.json"


# ── Schema Conversion Helpers ─────────────────────────────────────────────

_JSON_TO_GEMINI_TYPE = {
    "string": "STRING",
    "integer": "INTEGER",
    "number": "NUMBER",
    "boolean": "BOOLEAN",
    "array": "ARRAY",
    "object": "OBJECT",
}


def _clean_identifier(name: str) -> str:
    """Sanitize name to match Gemini function name regex: ^[a-zA-Z_][a-zA-Z0-9_]*$ (max 64 chars)."""
    clean = re.sub(r"[^a-zA-Z0-9_]", "_", name)
    if not clean or not (clean[0].isalpha() or clean[0] == "_"):
        clean = f"mcp_{clean}"
    return clean[:64]


def _convert_schema_property(prop: dict) -> dict:
    """Recursively convert a JSON Schema property dict to Gemini parameter format."""
    out: dict[str, Any] = {}
    
    prop_type = prop.get("type", "string")
    if isinstance(prop_type, list):
        # Filter out 'null' if present
        types = [t for t in prop_type if t != "null"]
        prop_type = types[0] if types else "string"
        
    out["type"] = _JSON_TO_GEMINI_TYPE.get(str(prop_type).lower(), "STRING")
    
    if "description" in prop:
        out["description"] = str(prop["description"])
        
    if "enum" in prop and isinstance(prop["enum"], list):
        out["enum"] = [str(e) for e in prop["enum"]]

    if out["type"] == "ARRAY" and "items" in prop and isinstance(prop["items"], dict):
        out["items"] = _convert_schema_property(prop["items"])

    if out["type"] == "OBJECT" and "properties" in prop and isinstance(prop["properties"], dict):
        out["properties"] = {
            k: _convert_schema_property(v)
            for k, v in prop["properties"].items()
            if isinstance(v, dict)
        }
        if "required" in prop and isinstance(prop["required"], list):
            out["required"] = [str(r) for r in prop["required"]]

    return out


def mcp_tool_to_gemini_declaration(server_name: str, tool: Any) -> Tuple[str, dict]:
    """Convert an MCP Tool object into a unique Gemini Function Declaration."""
    # Prefix with server name to avoid any collision: mcp__<server>__<tool>
    unique_name = _clean_identifier(f"mcp__{server_name}__{tool.name}")
    
    desc = tool.description or f"Tool '{tool.name}' from MCP server '{server_name}'."
    
    input_schema = getattr(tool, "inputSchema", None) or {}
    properties = {}
    required = []
    
    if isinstance(input_schema, dict):
        raw_props = input_schema.get("properties", {})
        if isinstance(raw_props, dict):
            for k, v in raw_props.items():
                if isinstance(v, dict):
                    properties[k] = _convert_schema_property(v)
        raw_req = input_schema.get("required", [])
        if isinstance(raw_req, list):
            required = [str(r) for r in raw_req]

    declaration = {
        "name": unique_name,
        "description": desc,
        "parameters": {
            "type": "OBJECT",
            "properties": properties,
            "required": required,
        }
    }
    return unique_name, declaration


# ── Server Session Container ───────────────────────────────────────────────

@dataclass
class MCPServerContext:
    name: str
    config: dict
    session: Optional[ClientSession] = None
    tools: Dict[str, Any] = field(default_factory=dict)         # raw tool_name -> Tool
    gemini_names: Dict[str, str] = field(default_factory=dict)  # gemini_func_name -> raw tool_name


# ── Main MCP Client Manager ───────────────────────────────────────────────

class MCPClientManager:
    """Manages all active MCP server sessions and dynamic tool dispatch for JARVIS."""

    def __init__(self, config_path: Optional[Path] = None):
        self.config_path = config_path or CONFIG_PATH
        self._exit_stack = contextlib.AsyncExitStack()
        self._servers: Dict[str, MCPServerContext] = {}
        self._tool_to_server: Dict[str, Tuple[str, str]] = {}  # gemini_name -> (server_name, raw_tool_name)
        self._function_declarations: List[dict] = []
        self._initialized = False

    @property
    def is_available(self) -> bool:
        return _MCP_AVAILABLE

    def load_config(self) -> dict:
        """Load and parse config/mcp_servers.json."""
        if not self.config_path.exists():
            return {}
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data.get("mcpServers", data)
        except Exception as exc:
            print(f"[MCP] ⚠️ Error reading {self.config_path}: {exc}")
            return {}

    async def initialize(self) -> List[dict]:
        """Connect to all enabled MCP servers and discover their tools."""
        if not _MCP_AVAILABLE:
            print("[MCP] [!] 'mcp' Python SDK not installed. MCP integration disabled.")
            return []

        if self._initialized:
            return self._function_declarations

        server_configs = self.load_config()
        if not server_configs:
            print("[MCP] (i) No MCP servers configured in config/mcp_servers.json.")
            self._initialized = True
            return []

        enabled_count = sum(1 for c in server_configs.values() if c.get("enabled", True))
        print(f"[MCP] Initializing {len(server_configs)} configured MCP server(s) ({enabled_count} enabled)...")

        for srv_name, srv_cfg in server_configs.items():
            # Check if enabled (default to true unless explicitly false)
            if not srv_cfg.get("enabled", True):
                print(f"[MCP] [paused] Server '{srv_name}' is disabled in config. Skipping.")
                continue

            try:
                await self._connect_server(srv_name, srv_cfg)
            except Exception as exc:
                print(f"[MCP] [x] Failed to connect to server '{srv_name}': {exc}")

        self._initialized = True
        print(f"[MCP] [OK] Ready -- {len(self._function_declarations)} external tool(s) registered across {len(self._servers)} server(s).")
        return self._function_declarations

    async def _connect_server(self, name: str, config: dict) -> None:
        """Connect to a single stdio MCP server."""
        command = config.get("command")
        if not command:
            print(f"[MCP] [!] Server '{name}' missing 'command'. Skipping.")
            return

        args = config.get("args", [])
        env = dict(os.environ)
        if "env" in config and isinstance(config["env"], dict):
            env.update({k: str(v) for k, v in config["env"].items()})

        # Windows-specific: resolve npx/uvx via shell if needed
        server_params = StdioServerParameters(
            command=command,
            args=args,
            env=env,
        )

        ctx = MCPServerContext(name=name, config=config)

        # Enter stdio client context
        read_stream, write_stream = await self._exit_stack.enter_async_context(
            stdio_client(server_params)
        )
        session = await self._exit_stack.enter_async_context(
            ClientSession(read_stream, write_stream)
        )

        await session.initialize()
        ctx.session = session

        # Discover tools
        tool_list_resp = await session.list_tools()
        raw_tools = getattr(tool_list_resp, "tools", [])

        for tool in raw_tools:
            ctx.tools[tool.name] = tool
            gemini_name, decl = mcp_tool_to_gemini_declaration(name, tool)
            ctx.gemini_names[gemini_name] = tool.name
            self._tool_to_server[gemini_name] = (name, tool.name)
            self._function_declarations.append(decl)
            print(f"[MCP]   -> Registered: {gemini_name} ({tool.name})")

        self._servers[name] = ctx

    def get_function_declarations(self) -> List[dict]:
        """Return all Gemini function declarations discovered from MCP servers."""
        return self._function_declarations

    def is_mcp_tool(self, tool_name: str) -> bool:
        """Check if a tool name belongs to an MCP server."""
        return tool_name in self._tool_to_server

    async def execute_tool(self, gemini_tool_name: str, arguments: dict) -> str:
        """Execute an MCP tool call and return a formatted string result."""
        if gemini_tool_name not in self._tool_to_server:
            return f"[MCP] Error: Tool '{gemini_tool_name}' is not a registered MCP tool."

        server_name, raw_tool_name = self._tool_to_server[gemini_tool_name]
        srv_ctx = self._servers.get(server_name)

        if not srv_ctx or not srv_ctx.session:
            return f"[MCP] Error: Server '{server_name}' is not currently connected."

        print(f"[MCP] >> Executing {server_name}/{raw_tool_name} with args: {arguments}")

        try:
            result = await srv_ctx.session.call_tool(raw_tool_name, arguments=arguments)

            # Process output content blocks
            output_parts = []
            if hasattr(result, "content") and result.content:
                for block in result.content:
                    if hasattr(block, "text") and block.text:
                        output_parts.append(block.text)
                    elif hasattr(block, "data"):
                        output_parts.append(f"[Binary/Image data: {len(block.data)} bytes]")
                    else:
                        output_parts.append(str(block))

            text_result = "\n".join(output_parts) if output_parts else "Tool completed with no output."

            if getattr(result, "isError", False):
                return f"[MCP Error from {server_name}/{raw_tool_name}]: {text_result}"

            return text_result

        except Exception as exc:
            return f"[MCP] Execution error in {server_name}/{raw_tool_name}: {exc}"

    async def shutdown(self) -> None:
        """Gracefully close all MCP server subprocesses and connections."""
        print("[MCP] Closing MCP server connections...")
        try:
            await self._exit_stack.aclose()
            self._servers.clear()
            self._tool_to_server.clear()
            self._function_declarations.clear()
            self._initialized = False
            print("[MCP] All MCP sessions terminated.")
        except Exception as exc:
            print(f"[MCP] Error during shutdown: {exc}")
