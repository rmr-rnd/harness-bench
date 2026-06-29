"""MCP stdio server — Docker bridge.

Runs as a subprocess. Reads JSON-RPC 2.0 requests from stdin (newline-delimited),
writes responses to stdout. Tool calls are dispatched to a target Docker container
via docker exec / docker cp — no SSH required.

Usage:
  python -m framework.mcp.server --container-id <id> --tools shell,filesystem,browser
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from framework.mcp.docker_bridge import DockerBridge
from framework.mcp.types import MCPTool

# Registry of available tool groups
_TOOL_GROUPS: dict[str, str] = {
    "shell":      "framework.mcp.tools.shell",
    "filesystem": "framework.mcp.tools.filesystem",
    "browser":    "framework.mcp.tools.browser",
}


def _load_tools(groups: list[str]) -> dict[str, MCPTool]:
    import importlib
    tools: dict[str, MCPTool] = {}
    for group in groups:
        if group not in _TOOL_GROUPS:
            raise ValueError(f"Unknown tool group: {group!r}. Available: {list(_TOOL_GROUPS)}")
        mod = importlib.import_module(_TOOL_GROUPS[group])
        for tool in mod.TOOLS:
            tools[tool.name] = tool
    return tools


class MCPServer:
    """Async MCP stdio server with a DockerBridge backend."""

    PROTOCOL_VERSION = "2024-11-05"

    def __init__(self, bridge: DockerBridge, tools: dict[str, MCPTool]) -> None:
        self._bridge = bridge
        self._tools = tools

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        loop = asyncio.get_event_loop()

        reader = asyncio.StreamReader()
        read_proto = asyncio.StreamReaderProtocol(reader)
        await loop.connect_read_pipe(lambda: read_proto, sys.stdin)

        # Use sys.stdout.buffer directly for writing
        while True:
            line = await reader.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                request = json.loads(line.decode())
            except json.JSONDecodeError:
                continue

            response = await self._handle(request)
            if response is not None:  # notifications have no response
                sys.stdout.buffer.write((json.dumps(response) + "\n").encode())
                sys.stdout.buffer.flush()

    # ------------------------------------------------------------------
    # Request dispatcher
    # ------------------------------------------------------------------

    async def _handle(self, req: dict) -> dict | None:
        method: str = req.get("method", "")
        req_id: Any = req.get("id")
        params: dict = req.get("params") or {}

        # Notifications (no id) — acknowledge silently
        if req_id is None:
            return None

        if method == "initialize":
            return self._ok(req_id, {
                "protocolVersion": self.PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "harness-docker-bridge", "version": "1.0"},
            })

        if method == "tools/list":
            return self._ok(req_id, {
                "tools": [
                    {
                        "name": t.name,
                        "description": t.description,
                        "inputSchema": t.parameters,
                    }
                    for t in self._tools.values()
                ]
            })

        if method == "tools/call":
            return await self._call_tool(req_id, params)

        return self._err(req_id, -32601, f"Method not found: {method}")

    async def _call_tool(self, req_id: Any, params: dict) -> dict:
        name: str = params.get("name", "")
        args: dict = params.get("arguments") or {}

        if name not in self._tools:
            return self._err(req_id, -32602, f"Unknown tool: {name!r}")

        try:
            text = await self._tools[name].handler(args, self._bridge)
        except Exception as exc:
            text = f"[tool error: {exc}]"

        return self._ok(req_id, {
            "content": [{"type": "text", "text": text}],
            "isError": False,
        })

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _ok(req_id: Any, result: Any) -> dict:
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    @staticmethod
    def _err(req_id: Any, code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Harness MCP Docker bridge server")
    p.add_argument("--container-id", required=True, help="Target Docker container ID or name")
    p.add_argument(
        "--tools", default="shell,filesystem,browser",
        help="Comma-separated tool groups to expose (default: shell,filesystem,browser)",
    )
    return p.parse_args()


async def _main() -> None:
    args = _parse_args()
    groups = [g.strip() for g in args.tools.split(",") if g.strip()]
    bridge = DockerBridge(args.container_id)
    tools = _load_tools(groups)
    server = MCPServer(bridge, tools)
    await server.run()


if __name__ == "__main__":
    asyncio.run(_main())
