"""SingleTurnRunner: default one-shot runner for benchmarks that need no special protocol."""
from __future__ import annotations

from typing import TYPE_CHECKING

from framework.runners.base import Runner

if TYPE_CHECKING:
    from framework.models import AgentTrace, Sample
    from framework.context import ExecutionContext
    from framework.runners.base import SendTurnFn


class SingleTurnRunner(Runner):
    """Send a single turn and wrap the response in an AgentTrace."""

    async def run(
        self,
        task: "Sample",
        send_turn: "SendTurnFn",
        ctx: "ExecutionContext",
    ) -> "AgentTrace":
        from framework.models import AgentTrace

        messages: list[dict] = []
        if task.system_prompt:
            messages.append({"role": "system", "content": task.system_prompt})
        for m in task.messages:
            messages.append({"role": m.role, "content": m.content})

        tools = task.metadata.get("functions", [])

        response = await send_turn(
            messages=messages,
            tools=tools,
            system_prompt=task.system_prompt or "",
            ctx=ctx,
            timeout=ctx.timeout,
        )

        trace = AgentTrace(
            task_id=task.id,
            final_output=response.text,
            steps=response.steps,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
        )
        if response.tool_calls:
            trace._bfcl_tool_calls = [  # type: ignore[attr-defined]
                {tc["name"]: tc["arguments"]} for tc in response.tool_calls
            ]
        return trace
