"""Shared types for the MCP bridge layer."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class MCPTool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable  # async (args: dict, bridge: DockerBridge) -> str
