"""Runner ABC: protocol between benchmarks and harnesses."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Awaitable, Callable, Any

if TYPE_CHECKING:
    from framework.models import AgentTrace, Sample
    from framework.context import ExecutionContext


@dataclass
class TurnResponse:
    """Return value from harness.send_turn()."""
    text: str
    tool_calls: list[dict]      # [{"id": "...", "name": "func", "arguments": {...}}]
    finish_reason: str
    input_tokens: int = 0
    output_tokens: int = 0
    steps: list = field(default_factory=list)   # list[Step] accumulated during turn


SendTurnFn = Callable[..., Awaitable[TurnResponse]]


class Runner(ABC):
    """Manages the multi-turn protocol for a benchmark category."""

    @abstractmethod
    async def run(
        self,
        task: "Sample",
        send_turn: "SendTurnFn",
        ctx: "ExecutionContext",
    ) -> "AgentTrace": ...
