"""MCP server — exposes the claude-lcm vault to MCP clients.

Registers 5 tools: lcm_grep, lcm_describe, lcm_expand, lcm_status,
lcm_doctor. Handlers are thin wrappers around the lifted handlers in
`claude_lcm.tools`, which operate against a ClaudeLcmEngine instance.

The engine's `_session_id` is set per-client-request via a required
`session_id` tool argument (or read from the LCM_SESSION_ID env var if
the client can't pass arguments). If no session is specified, tools
operate on the whole vault.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

import mcp.types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from claude_lcm.config import ClaudeLcmConfig
from claude_lcm.engine import ClaudeLcmEngine
from claude_lcm.schemas import (
    LCM_DESCRIBE,
    LCM_DOCTOR,
    LCM_EXPAND,
    LCM_GREP,
    LCM_RECENT,
    LCM_STATUS,
)
from claude_lcm.tools import (
    lcm_describe,
    lcm_doctor,
    lcm_expand,
    lcm_grep,
    lcm_recent,
    lcm_status,
)

logger = logging.getLogger("claude_lcm.mcp")

HANDLERS = {
    "lcm_grep": lcm_grep,
    "lcm_describe": lcm_describe,
    "lcm_expand": lcm_expand,
    "lcm_status": lcm_status,
    "lcm_doctor": lcm_doctor,
    "lcm_recent": lcm_recent,
}

SCHEMAS = [LCM_GREP, LCM_RECENT, LCM_DESCRIBE, LCM_EXPAND, LCM_STATUS, LCM_DOCTOR]


def _tool_def(schema: dict) -> types.Tool:
    return types.Tool(
        name=schema["name"],
        description=schema["description"],
        inputSchema=schema["parameters"],
    )


def _make_engine(session_id: str | None = None) -> ClaudeLcmEngine:
    return ClaudeLcmEngine(
        config=ClaudeLcmConfig.from_env(),
        session_id=session_id or os.environ.get("LCM_SESSION_ID"),
    )


async def run() -> None:
    server: Server = Server("claude-lcm")

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [_tool_def(s) for s in SCHEMAS]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
        handler = HANDLERS.get(name)
        if handler is None:
            payload = json.dumps({"error": f"unknown tool: {name}"})
            return [types.TextContent(type="text", text=payload)]

        session_id = arguments.pop("session_id", None) if isinstance(arguments, dict) else None
        engine = _make_engine(session_id)
        try:
            result = handler(arguments or {}, engine=engine)
        except Exception as exc:
            logger.exception("tool %s crashed", name)
            result = json.dumps({"error": f"{name} crashed: {exc}"})
        finally:
            try:
                engine.close()
            except Exception:
                pass

        return [types.TextContent(type="text", text=result)]

    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LCM_LOG_LEVEL", "WARNING").upper(),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    asyncio.run(run())


if __name__ == "__main__":
    main()
