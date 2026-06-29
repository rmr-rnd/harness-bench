"""Tests for TurnResponse dataclass contract."""
from __future__ import annotations

from framework.runners.base import TurnResponse


def test_defaults():
    tr = TurnResponse(text="hi", tool_calls=[], finish_reason="stop")
    assert tr.input_tokens == 0
    assert tr.output_tokens == 0
    assert tr.steps == []


def test_steps_not_shared_between_instances():
    a = TurnResponse(text="a", tool_calls=[], finish_reason="stop")
    b = TurnResponse(text="b", tool_calls=[], finish_reason="stop")
    a.steps.append("step")
    assert b.steps == [], "steps list must not be shared between instances"


def test_explicit_values_stored():
    tr = TurnResponse(
        text="answer",
        tool_calls=[{"id": "1", "name": "fn", "arguments": {}}],
        finish_reason="tool_calls",
        input_tokens=10,
        output_tokens=5,
    )
    assert tr.text == "answer"
    assert len(tr.tool_calls) == 1
    assert tr.finish_reason == "tool_calls"
    assert tr.input_tokens == 10
    assert tr.output_tokens == 5
