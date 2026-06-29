"""Async MCP client — starts the server subprocess and proxies tool calls."""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from typing import Any

logger = logging.getLogger(__name__)


class MCPClient:
    """Manages the MCP server subprocess lifetime and sends JSON-RPC requests over stdio."""

    def __init__(self) -> None:
        self._proc: asyncio.subprocess.Process | None = None
        self._req_id = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self, container_id: str, tool_groups: list[str]) -> None:
        """Start the MCP server subprocess connected to *container_id*."""
        groups_arg = ",".join(tool_groups)
        self._proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "framework.mcp.server",
            "--container-id", container_id,
            "--tools", groups_arg,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        logger.debug("MCP server started (pid=%s, container=%s, groups=%s)",
                     self._proc.pid, container_id[:12], groups_arg)

        await self._request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "harness-bench", "version": "1.0"},
        })
        self._notify("notifications/initialized", {})

    async def stop(self) -> None:
        if not self._proc:
            return
        try:
            self._proc.stdin.close()
            await asyncio.wait_for(self._proc.wait(), timeout=5)
        except Exception:
            try:
                self._proc.kill()
            except Exception:
                pass
        self._proc = None

    # ------------------------------------------------------------------
    # Tool API
    # ------------------------------------------------------------------

    async def list_tools(self) -> list[dict]:
        result = await self._request("tools/list", {})
        return result.get("tools", [])

    async def call_tool(self, name: str, args: dict, timeout: int = 300) -> str:
        """Call a tool on the server and return the text result."""
        result = await self._request(
            "tools/call",
            {"name": name, "arguments": args},
            timeout=timeout,
        )
        content = result.get("content", [])
        return "".join(c.get("text", "") for c in content if c.get("type") == "text")

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------

    async def _request(self, method: str, params: dict, timeout: int = 30) -> dict:
        assert self._proc is not None, "MCPClient not started"
        self._req_id += 1
        msg = json.dumps({"jsonrpc": "2.0", "id": self._req_id, "method": method, "params": params})
        self._proc.stdin.write((msg + "\n").encode())
        await self._proc.stdin.drain()

        line = await asyncio.wait_for(self._proc.stdout.readline(), timeout=timeout)
        if not line:
            raise RuntimeError("MCP server closed connection unexpectedly")
        resp = json.loads(line.decode())
        if "error" in resp:
            raise RuntimeError(f"MCP error: {resp['error']}")
        return resp.get("result", {})

    def _notify(self, method: str, params: dict) -> None:
        assert self._proc is not None
        msg = json.dumps({"jsonrpc": "2.0", "method": method, "params": params})
        self._proc.stdin.write((msg + "\n").encode())

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "MCPClient":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.stop()
