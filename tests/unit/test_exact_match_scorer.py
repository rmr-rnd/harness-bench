"""Tests for ExactMatchScorer."""
from __future__ import annotations

import pytest

from framework.models import AgentTrace, Message, Sample
from framework.scorers.exact_match import ExactMatchScorer


def _sample(ground_truth: str) -> Sample:
    return Sample(
        id="s1",
        benchmark="test",
        messages=[Message(role="user", content="q")],
        ground_truth=ground_truth,
    )


def _trace(output: str) -> AgentTrace:
    return AgentTrace(task_id="s1", final_output=output)


@pytest.mark.asyncio
async def test_correct_match():
    scorer = ExactMatchScorer(parser_fn=lambda x: x.strip())
    result = await scorer(_sample("42"), _trace("42"), judge=None)
    assert result.grade == "CORRECT"
    assert result.score == 1.0


@pytest.mark.asyncio
async def test_incorrect_match():
    scorer = ExactMatchScorer(parser_fn=lambda x: x.strip())
    result = await scorer(_sample("42"), _trace("43"), judge=None)
    assert result.grade == "INCORRECT"
    assert result.score == 0.0


@pytest.mark.asyncio
async def test_empty_output_does_not_raise():
    scorer = ExactMatchScorer(parser_fn=lambda x: x.strip())
    result = await scorer(_sample("42"), _trace(""), judge=None)
    assert result.grade == "INCORRECT"
    assert result.score == 0.0


@pytest.mark.asyncio
async def test_parser_applied_before_comparison():
    # parser normalises to lowercase — "Answer" should match "answer"
    scorer = ExactMatchScorer(parser_fn=lambda x: x.strip().lower())
    result = await scorer(_sample("answer"), _trace("  Answer  "), judge=None)
    assert result.grade == "CORRECT"


@pytest.mark.asyncio
async def test_sample_id_propagated():
    scorer = ExactMatchScorer(parser_fn=lambda x: x)
    result = await scorer(_sample("42"), _trace("42"), judge=None)
    assert result.sample_id == "s1"
