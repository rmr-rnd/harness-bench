"""Tests for SingleTurnRunner."""
from __future__ import annotations

import pytest

from framework.context import ExecutionContext
from framework.models import AgentTrace, Message, Sample
from framework.runners.base import TurnResponse
from framework.runners.single_turn import SingleTurnRunner


def _make_task(*, system_prompt: str = "", messages=None, metadata=None) -> Sample:
    return Sample(
        id="t1",
        benchmark="test",
        messages=messages or [Message(role="user", content="hello")],
        ground_truth="x",
        system_prompt=system_prompt,
        metadata=metadata or {},
    )


def _make_ctx() -> ExecutionContext:
    return ExecutionContext(timeout=30)


async def _echo_turn(messages, tools, system_prompt, ctx, timeout=30, **kw) -> TurnResponse:
    return TurnResponse(
        text="result",
        tool_calls=[],
        finish_reason="stop",
        input_tokens=7,
        output_tokens=3,
    )


# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_system_prompt_prepended():
    runner = SingleTurnRunner()
    captured = {}

    async def capturing_turn(messages, tools, system_prompt, ctx, timeout=30, **kw):
        captured["messages"] = messages
        return TurnResponse(text="r", tool_calls=[], finish_reason="stop")

    task = _make_task(system_prompt="Be concise.")
    await runner.run(task, capturing_turn, _make_ctx())

    assert captured["messages"][0] == {"role": "system", "content": "Be concise."}


@pytest.mark.asyncio
async def test_no_system_prompt_no_extra_message():
    runner = SingleTurnRunner()
    captured = {}

    async def capturing_turn(messages, tools, system_prompt, ctx, timeout=30, **kw):
        captured["messages"] = messages
        return TurnResponse(text="r", tool_calls=[], finish_reason="stop")

    task = _make_task(system_prompt="", messages=[Message(role="user", content="hi")])
    await runner.run(task, capturing_turn, _make_ctx())

    assert len(captured["messages"]) == 1
    assert captured["messages"][0]["role"] == "user"


@pytest.mark.asyncio
async def test_tokens_propagated():
    runner = SingleTurnRunner()
    trace = await runner.run(_make_task(), _echo_turn, _make_ctx())
    assert trace.input_tokens == 7
    assert trace.output_tokens == 3


@pytest.mark.asyncio
async def test_tool_calls_written_to_trace():
    runner = SingleTurnRunner()

    async def tool_turn(messages, tools, system_prompt, ctx, timeout=30, **kw):
        return TurnResponse(
            text="",
            tool_calls=[{"id": "c1", "name": "get_weather", "arguments": {"city": "Paris"}}],
            finish_reason="tool_calls",
        )

    trace = await runner.run(_make_task(), tool_turn, _make_ctx())
    assert hasattr(trace, "_bfcl_tool_calls")
    assert trace._bfcl_tool_calls == [{"get_weather": {"city": "Paris"}}]


@pytest.mark.asyncio
async def test_no_tool_calls_no_bfcl_attr():
    runner = SingleTurnRunner()
    trace = await runner.run(_make_task(), _echo_turn, _make_ctx())
    assert not hasattr(trace, "_bfcl_tool_calls")


@pytest.mark.asyncio
async def test_no_functions_in_metadata_does_not_raise():
    runner = SingleTurnRunner()
    captured = {}

    async def capturing_turn(messages, tools, system_prompt, ctx, timeout=30, **kw):
        captured["tools"] = tools
        return TurnResponse(text="r", tool_calls=[], finish_reason="stop")

    task = _make_task(metadata={})  # no "functions" key
    await runner.run(task, capturing_turn, _make_ctx())
    assert captured["tools"] == []
