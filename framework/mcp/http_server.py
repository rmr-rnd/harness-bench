"""MCP Streamable HTTP server — runs on the host, reachable by Hermes via host.docker.internal.

Implements the MCP Streamable HTTP transport (protocol version 2025-11-25):
  - Single POST endpoint at /mcp
  - Synchronous JSON-RPC request/response
  - No SSE session management needed

Usage:
  server = MCPHttpServer(container_id="abc123", tool_groups=["shell", "filesystem", "browser"])
  port = await server.start()
  # Hermes connects to http://host.docker.internal:{port}
  await server.stop()
"""
from __future__ import annotations

import asyncio
import logging
import socket

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from framework.mcp.docker_bridge import DockerBridge
from framework.mcp.server import MCPServer, _load_tools

logger = logging.getLogger(__name__)

# Protocol version Hermes sends in initialize (2025-11-25)
_PROTOCOL_VERSION = "2025-11-25"


_REQUEST_HEARTBEAT_SCHEMA = {
    "type": "boolean",
    "description": "Set to false when the task is complete.",
}


def _inject_request_heartbeat(tools: list) -> None:
    """Add request_heartbeat to each tool's inputSchema so Letta can control loop termination."""
    for tool in tools:
        schema = tool.get("inputSchema")
        if not schema:
            schema = {}
            tool["inputSchema"] = schema
        props = schema.setdefault("properties", {})
        props["request_heartbeat"] = _REQUEST_HEARTBEAT_SCHEMA


class MCPHttpServer:
    """FastAPI Streamable HTTP MCP server that bridges tool calls to a Docker container.

    Supports two kinds of tools:
    - Standard tool groups (shell, filesystem, browser) — dispatched via DockerBridge
    - Custom SandboxTools — dispatched via sandbox.exec_stdin() with JSON on stdin
    """

    def __init__(
        self,
        container_id: str,
        tool_groups: list[str],
        step_cb=None,
        sandbox=None,           # Sandbox instance (for custom tool dispatch)
        custom_tools=None,      # list[SandboxTool]
    ) -> None:
        bridge = DockerBridge(container_id)
        tools = _load_tools(tool_groups)
        self._mcp = MCPServer(bridge, tools)
        self._mcp.PROTOCOL_VERSION = _PROTOCOL_VERSION
        self._port: int | None = None
        self._server_task: asyncio.Task | None = None
        self._step_cb = step_cb
        self._sandbox = sandbox
        self._custom_tools: dict = {t.name: t for t in (custom_tools or [])}
        self.app = self._build_app()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> int:
        """Start the HTTP server on a free port. Returns the port number."""
        import uvicorn

        self._port = _free_port()
        config = uvicorn.Config(
            self.app,
            host="0.0.0.0",
            port=self._port,
            log_level="warning",
            access_log=False,
        )
        server = uvicorn.Server(config)
        self._server_task = asyncio.create_task(server.serve())

        for _ in range(50):
            await asyncio.sleep(0.1)
            if server.started:
                break

        self._tool_names = list(self._mcp._tools.keys()) + list(self._custom_tools.keys())
        logger.info("MCP Streamable HTTP server started on port %d with tools: %s", self._port, self._tool_names)
        return self._port

    async def stop(self) -> None:
        if self._server_task:
            self._server_task.cancel()
            try:
                await self._server_task
            except (asyncio.CancelledError, Exception):
                pass
            self._server_task = None
        logger.info("MCP HTTP server stopped")

    @property
    def port(self) -> int | None:
        return self._port

    # ------------------------------------------------------------------
    # FastAPI app — single /mcp endpoint, synchronous JSON response
    # ------------------------------------------------------------------

    def _build_app(self) -> FastAPI:
        app = FastAPI(title="Harness MCP Bridge")

        @app.post("/mcp")
        async def mcp_endpoint(request: Request):
            body = await request.json()
            method = body.get("method")
            req_id = body.get("id")
            logger.debug("MCP request: %s", method)

            # Merge custom tools into tools/list response
            if method == "tools/list" and self._custom_tools:
                response = await self._mcp._handle(body)
                if response and "result" in response:
                    custom_defs = [
                        {
                            "name": t.name,
                            "description": t.description,
                            "inputSchema": t.parameters,
                        }
                        for t in self._custom_tools.values()
                    ]
                    response["result"].setdefault("tools", []).extend(custom_defs)
                    _inject_request_heartbeat(response["result"].get("tools", []))
                return JSONResponse(content=response)

            # Dispatch custom tool calls via sandbox.exec_stdin
            if method == "tools/call":
                params = body.get("params", {})
                tool_name = params.get("name", "")
                tool_args = params.get("arguments") or {}

                if tool_name in self._custom_tools and self._sandbox is not None:
                    if self._step_cb:
                        self._step_cb("tool_call", {"name": tool_name, "args": tool_args})

                    response = await self._dispatch_custom_tool(req_id, tool_name, tool_args)

                    if self._step_cb:
                        content_list = response.get("result", {}).get("content", [])
                        text = "\n".join(c.get("text", "") for c in content_list if c.get("type") == "text")
                        self._step_cb("tool_result", text)

                    return JSONResponse(content=response)

                # Standard tool — capture step trace and delegate to MCPServer
                if self._step_cb:
                    self._step_cb("tool_call", {"name": tool_name, "args": tool_args})

            response = await self._mcp._handle(body)
            if response is None:
                return Response(status_code=202)

            if method == "tools/list" and response and "result" in response:
                _inject_request_heartbeat(response["result"].get("tools", []))

            if method == "tools/call" and self._step_cb and response:
                result_content = response.get("result", {})
                content_list = result_content.get("content", [])
                text = "\n".join(
                    c.get("text", "") for c in content_list if c.get("type") == "text"
                ) if content_list else str(result_content)
                self._step_cb("tool_result", text)

            logger.debug("MCP response id=%s", response.get("id"))
            return JSONResponse(content=response)

        @app.post("/")
        async def mcp_root(request: Request):
            return await mcp_endpoint(request)

        return app

    async def _dispatch_custom_tool(self, req_id, tool_name: str, tool_args: dict) -> dict:
        """Execute a SandboxTool via sandbox.exec_stdin and return a JSON-RPC response."""
        import json as _json
        tool = self._custom_tools[tool_name]
        stdin_data = _json.dumps(tool_args).encode()
        entrypoint = f"/.sandbox_tools/{tool_name}/{tool.entrypoint}"

        stdout, stderr, exit_code = await self._sandbox.exec_stdin(
            tool.runner, entrypoint, stdin_data, timeout=tool.timeout
        )

        is_error = exit_code in tool.error_exit_codes
        output = stdout or stderr
        if len(output.encode()) > tool.max_output_bytes:
            output = output.encode()[:tool.max_output_bytes].decode(errors="replace") + "\n[TRUNCATED]"

        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "content": [{"type": "text", "text": output}],
                "isError": is_error,
            },
        }


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]
