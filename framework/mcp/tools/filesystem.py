"""Filesystem tool group: read_file, write_file, list_dir."""
from __future__ import annotations

from framework.mcp.types import MCPTool


async def _read_file(args: dict, bridge) -> str:
    return await bridge.read_file(args.get("path") or "")


async def _write_file(args: dict, bridge) -> str:
    return await bridge.write_file(args.get("path") or "", args.get("content") or "")


async def _list_dir(args: dict, bridge) -> str:
    return await bridge.list_dir(args.get("path") or ".")


TOOLS: list[MCPTool] = [
    MCPTool(
        name="read_file",
        description="Read a file from the sandbox filesystem. Returns file content as text.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path to the file"},
            },
            "required": ["path"],
        },
        handler=_read_file,
    ),
    MCPTool(
        name="write_file",
        description="Write content to a file in the sandbox filesystem.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path to write to"},
                "content": {"type": "string", "description": "File content to write"},
            },
            "required": ["path", "content"],
        },
        handler=_write_file,
    ),
    MCPTool(
        name="list_dir",
        description="List directory contents in the sandbox filesystem.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory path (default: current directory)"},
            },
            "required": [],
        },
        handler=_list_dir,
    ),
]
