"""Integration tests for Orchestrator._run_task scenarios."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from framework.config import RunConfig
from framework.models import Message, Sample
from framework.orchestrator import Orchestrator
from framework.runners.base import TurnResponse
from tests.conftest import EchoHarness, ExactScorer, FakeBenchmark


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_cfg(**overrides) -> RunConfig:
    base = {
        "model": {"base_url": "http://fake", "api_key": "x", "model_name": "fake"},
        "harness": {"type": "echo"},
        "benchmarks": [{"name": "simpleqa", "limit": 1}],
        "parallelism": {"workers": 1, "timeout_per_task": 10, "eval_timeout": 5},
    }
    base.update(overrides)
    return RunConfig.model_validate(base)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_happy_path(orchestrator_patches, minimal_cfg):
    orch = Orchestrator(minimal_cfg)
    result = await orch.run()
    summary = result["benchmarks"]["simpleqa"]
    assert summary["grades"].get("CORRECT") == 1
    assert summary["accuracy"] == 1.0
    assert summary["errors"] == 0


# ---------------------------------------------------------------------------
# Agent timeout → grade TIMEOUT
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_agent_timeout(mock_db, minimal_cfg):
    class HangHarness(EchoHarness):
        async def send_turn(self, *a, **kw):
            await asyncio.sleep(999)

    teardown_called = False
    original_teardown = Orchestrator._teardown_ctx

    async def spy_teardown(self, ctx):
        nonlocal teardown_called
        teardown_called = True
        await original_teardown(self, ctx)

    with (
        patch("framework.orchestrator.Database", return_value=mock_db),
        patch("framework.orchestrator.LLMJudge"),
        patch("framework.orchestrator.load_harness_class", return_value=HangHarness),
        patch("framework.orchestrator._resolve_benchmark", return_value=FakeBenchmark),
        patch.object(Orchestrator, "_teardown_ctx", spy_teardown),
    ):
        orch = Orchestrator(minimal_cfg)
        result = await orch.run()

    summary = result["benchmarks"]["simpleqa"]
    assert summary["grades"].get("TIMEOUT") == 1
    assert teardown_called


# ---------------------------------------------------------------------------
# Runner exception → grade ERROR
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_runner_exception(mock_db, minimal_cfg):
    class CrashHarness(EchoHarness):
        async def send_turn(self, *a, **kw):
            raise RuntimeError("boom")

    teardown_called = False
    original_teardown = Orchestrator._teardown_ctx

    async def spy_teardown(self, ctx):
        nonlocal teardown_called
        teardown_called = True
        await original_teardown(self, ctx)

    with (
        patch("framework.orchestrator.Database", return_value=mock_db),
        patch("framework.orchestrator.LLMJudge"),
        patch("framework.orchestrator.load_harness_class", return_value=CrashHarness),
        patch("framework.orchestrator._resolve_benchmark", return_value=FakeBenchmark),
        patch.object(Orchestrator, "_teardown_ctx", spy_teardown),
    ):
        orch = Orchestrator(minimal_cfg)
        result = await orch.run()

    summary = result["benchmarks"]["simpleqa"]
    # RuntimeError in runner → trace.final_output="" → scorer sees empty output → INCORRECT
    assert summary["grades"].get("INCORRECT") == 1
    assert teardown_called


# ---------------------------------------------------------------------------
# Scorer timeout → grade ERROR
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scorer_timeout(mock_db, minimal_cfg):
    class HangScorer(ExactScorer):
        async def __call__(self, *a, **kw):
            await asyncio.sleep(999)

    class BenchWithHangScorer(FakeBenchmark):
        def make_scorer(self):
            return HangScorer()

    teardown_called = False
    original_teardown = Orchestrator._teardown_ctx

    async def spy_teardown(self, ctx):
        nonlocal teardown_called
        teardown_called = True
        await original_teardown(self, ctx)

    with (
        patch("framework.orchestrator.Database", return_value=mock_db),
        patch("framework.orchestrator.LLMJudge"),
        patch("framework.orchestrator.load_harness_class", return_value=EchoHarness),
        patch("framework.orchestrator._resolve_benchmark", return_value=BenchWithHangScorer),
        patch.object(Orchestrator, "_teardown_ctx", spy_teardown),
    ):
        orch = Orchestrator(minimal_cfg)
        result = await orch.run()

    summary = result["benchmarks"]["simpleqa"]
    assert summary["grades"].get("ERROR") == 1
    assert teardown_called


# ---------------------------------------------------------------------------
# Resume: already completed task is skipped
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resume_skip(mock_db, minimal_cfg):
    mock_db.fetch_task_output.return_value = {"status": "done", "final_output": "42"}
    mock_db.fetch_task_eval.return_value = {"score": 1.0, "grade": "CORRECT"}

    events = []

    def progress_cb(**kw):
        events.append(kw.get("event"))

    with (
        patch("framework.orchestrator.Database", return_value=mock_db),
        patch("framework.orchestrator.LLMJudge"),
        patch("framework.orchestrator.load_harness_class", return_value=EchoHarness),
        patch("framework.orchestrator._resolve_benchmark", return_value=FakeBenchmark),
    ):
        orch = Orchestrator(minimal_cfg, progress_cb=progress_cb)
        # Grab harness instance to check call_count
        harness_instance = orch.harness
        await orch.run()

    assert harness_instance.call_count == 0, "send_turn must not be called for resumed task"
    assert "skip" in events


# ---------------------------------------------------------------------------
# Sandbox requires capable harness — ValueError lands in errors count
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sandbox_requires_capable_harness(mock_db, minimal_cfg):
    from framework.models import SandboxSpec

    class BenchWithSandbox(FakeBenchmark):
        def load_samples(self):
            samples = super().load_samples()
            samples[0].sandbox = SandboxSpec(type="docker_run", image="alpine")
            return samples

    with (
        patch("framework.orchestrator.Database", return_value=mock_db),
        patch("framework.orchestrator.LLMJudge"),
        patch("framework.orchestrator.load_harness_class", return_value=EchoHarness),
        patch("framework.orchestrator._resolve_benchmark", return_value=BenchWithSandbox),
    ):
        orch = Orchestrator(minimal_cfg)
        result = await orch.run()

    # EchoHarness.supports_sandbox == False → ValueError escapes _run_task
    # and is caught by asyncio.gather → counted as error, run doesn't crash
    summary = result["benchmarks"]["simpleqa"]
    assert summary["errors"] == 1
    assert result["run_id"].startswith("run_")  # run completed normally


# ---------------------------------------------------------------------------
# Harness override: explicit benchmark_harness in config
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_harness_override_explicit(mock_db):
    class OverrideHarness(EchoHarness):
        type = "echo_simpleqa"

        async def send_turn(self, *a, **kw):
            self.call_count += 1
            return TurnResponse(text="42", tool_calls=[], finish_reason="stop")

    cfg = _make_cfg(**{
        "harness": {
            "type": "echo",
            "benchmark_harness": {"simpleqa": "echo_simpleqa"},
        }
    })

    def harness_factory(name):
        return OverrideHarness if name == "echo_simpleqa" else EchoHarness

    with (
        patch("framework.orchestrator.Database", return_value=mock_db),
        patch("framework.orchestrator.LLMJudge"),
        patch("framework.orchestrator.load_harness_class", side_effect=harness_factory),
        patch("framework.orchestrator._resolve_benchmark", return_value=FakeBenchmark),
    ):
        orch = Orchestrator(cfg)
        override_harness = orch._benchmark_harnesses.get("simpleqa")
        assert isinstance(override_harness, OverrideHarness), \
            "simpleqa should use OverrideHarness per benchmark_harness config"

        await orch.run()

    assert override_harness.call_count == 1
    assert orch.harness.call_count == 0


# ---------------------------------------------------------------------------
# Teardown always runs even when runner raises
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_teardown_always_runs(mock_db, minimal_cfg):
    class CrashHarness(EchoHarness):
        async def send_turn(self, *a, **kw):
            raise ValueError("unexpected failure")

    teardown_count = 0
    original_teardown = Orchestrator._teardown_ctx

    async def counting_teardown(self, ctx):
        nonlocal teardown_count
        teardown_count += 1
        await original_teardown(self, ctx)

    with (
        patch("framework.orchestrator.Database", return_value=mock_db),
        patch("framework.orchestrator.LLMJudge"),
        patch("framework.orchestrator.load_harness_class", return_value=CrashHarness),
        patch("framework.orchestrator._resolve_benchmark", return_value=FakeBenchmark),
        patch.object(Orchestrator, "_teardown_ctx", counting_teardown),
    ):
        orch = Orchestrator(minimal_cfg)
        await orch.run()

    assert teardown_count == 1


# ---------------------------------------------------------------------------
# stop() cancels in-flight tasks without hanging
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stop_cancels_tasks(mock_db, minimal_cfg):
    entered = asyncio.Event()

    class BlockingHarness(EchoHarness):
        async def send_turn(self, *a, **kw):
            entered.set()
            await asyncio.sleep(999)

    with (
        patch("framework.orchestrator.Database", return_value=mock_db),
        patch("framework.orchestrator.LLMJudge"),
        patch("framework.orchestrator.load_harness_class", return_value=BlockingHarness),
        patch("framework.orchestrator._resolve_benchmark", return_value=FakeBenchmark),
    ):
        orch = Orchestrator(minimal_cfg)
        run_task = asyncio.create_task(orch.run())

        # Wait until harness is actually inside send_turn, then stop
        await asyncio.wait_for(entered.wait(), timeout=5)
        orch.stop()

        try:
            await asyncio.wait_for(run_task, timeout=5)
        except (asyncio.CancelledError, Exception):
            pass  # cancellation is expected — just must not hang
