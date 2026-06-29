"""Shell tool group: bash, python."""
from __future__ import annotations

from framework.mcp.types import MCPTool


async def _bash(args: dict, bridge) -> str:
    cmd = args.get("cmd") or args.get("command") or ""
    return await bridge.exec_bash(cmd)


async def _python(args: dict, bridge) -> str:
    code = args.get("code") or ""
    return await bridge.exec_python(code)


TOOLS: list[MCPTool] = [
    MCPTool(
        name="bash",
        description="Run a bash command in the sandbox environment. Returns stdout+stderr.",
        parameters={
            "type": "object",
            "properties": {
                "cmd": {"type": "string", "description": "The bash command to run"},
            },
            "required": ["cmd"],
        },
        handler=_bash,
    ),
    MCPTool(
        name="python",
        description="Run Python code in the sandbox environment. Returns stdout+stderr.",
        parameters={
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Python code to execute"},
            },
            "required": ["code"],
        },
        handler=_python,
    ),
]
