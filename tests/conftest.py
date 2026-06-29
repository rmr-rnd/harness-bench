"""Shared fixtures and mock classes for all tests."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from framework.config import ModelConfig, RunConfig
from framework.harnesses.base import Harness
from framework.models import AgentTrace, Message, Sample, Score
from framework.runners.base import TurnResponse
from framework.scorers.base import Scorer


# ---------------------------------------------------------------------------
# Mock harness
# ---------------------------------------------------------------------------

class EchoHarness(Harness):
    """Returns text='42' for every send_turn call. No Docker required."""

    type = "echo"
    supports_sandbox = False
    needs_docker = False
    SUPPORTS_RUNNER_PROTOCOL = True

    def __init__(self, model_cfg: ModelConfig) -> None:
        super().__init__(model_cfg)
        self.call_count = 0

    async def send_turn(
        self,
        messages: list[dict],
        tools: list[dict],
        system_prompt: str,
        ctx,
        timeout: int = 120,
        **kwargs,
    ) -> TurnResponse:
        self.call_count += 1
        return TurnResponse(text="42", tool_calls=[], finish_reason="stop")

    def finalize(self) -> None:
        pass

    @classmethod
    def from_config(cls, model_cfg: ModelConfig, raw: dict) -> "EchoHarness":
        return cls(model_cfg)


# ---------------------------------------------------------------------------
# Mock scorer
# ---------------------------------------------------------------------------

class ExactScorer(Scorer):
    """Returns CORRECT if trace.final_output == sample.ground_truth."""

    async def __call__(self, sample, trace, judge, sandbox=None) -> Score:
        correct = trace.final_output == sample.ground_truth
        return Score(
            sample_id=sample.id,
            score=1.0 if correct else 0.0,
            grade="CORRECT" if correct else "INCORRECT",
        )


# ---------------------------------------------------------------------------
# Mock benchmark
# ---------------------------------------------------------------------------

class FakeBenchmark:
    """Single-task benchmark with ground_truth='42'."""

    name = "simpleqa"

    def __init__(self, cfg):
        self.cfg = cfg

    def load_samples(self) -> list[Sample]:
        return [
            Sample(
                id="task-1",
                benchmark="simpleqa",
                messages=[Message(role="user", content="What is 6 × 7?")],
                ground_truth="42",
            )
        ]

    def make_scorer(self) -> Scorer:
        return ExactScorer()

    def make_runner(self, model_cfg=None):
        from framework.runners.single_turn import SingleTurnRunner
        return SingleTurnRunner()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def model_cfg() -> ModelConfig:
    return ModelConfig(base_url="http://fake", api_key="x", model_name="fake")


@pytest.fixture
def minimal_cfg() -> RunConfig:
    return RunConfig.model_validate({
        "model": {"base_url": "http://fake", "api_key": "x", "model_name": "fake"},
        "harness": {"type": "echo"},
        "benchmarks": [{"name": "simpleqa", "limit": 1}],
        "parallelism": {"workers": 1, "timeout_per_task": 10, "eval_timeout": 5},
    })


@pytest.fixture
def mock_db() -> AsyncMock:
    db = AsyncMock()
    db.fetch_task_output.return_value = None
    db.fetch_task_eval.return_value = None
    return db


@pytest.fixture
def orchestrator_patches(mock_db):
    """Holds all orchestrator patches open for the duration of a test."""
    with (
        patch("framework.orchestrator.Database", return_value=mock_db),
        patch("framework.orchestrator.LLMJudge"),
        patch("framework.orchestrator.load_harness_class", return_value=EchoHarness),
        patch("framework.orchestrator._resolve_benchmark", return_value=FakeBenchmark),
    ):
        yield
