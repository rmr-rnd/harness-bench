"""Tests for Orchestrator run() summary aggregation, finalize, progress events, run_id."""
from __future__ import annotations

import asyncio
from contextlib import ExitStack
from unittest.mock import patch

import pytest

from framework.config import RunConfig
from framework.models import Message, Sample, SandboxSpec
from framework.orchestrator import Orchestrator
from framework.runners.base import TurnResponse
from tests.conftest import EchoHarness, ExactScorer, FakeBenchmark


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cfg(**overrides) -> RunConfig:
    base = {
        "model": {"base_url": "http://fake", "api_key": "x", "model_name": "fake"},
        "harness": {"type": "echo"},
        "benchmarks": [{"name": "simpleqa", "limit": 10}],
        "parallelism": {"workers": 1, "timeout_per_task": 10, "eval_timeout": 5},
    }
    base.update(overrides)
    return RunConfig.model_validate(base)


def _patch_orch(stack: ExitStack, mock_db, harness_cls=EchoHarness,
                benchmark_cls=None, resolve_fn=None):
    """Enter all standard orchestrator patches into an ExitStack."""
    stack.enter_context(patch("framework.orchestrator.Database", return_value=mock_db))
    stack.enter_context(patch("framework.orchestrator.LLMJudge"))
    stack.enter_context(patch("framework.orchestrator.load_harness_class", return_value=harness_cls))
    if resolve_fn is not None:
        stack.enter_context(patch("framework.orchestrator._resolve_benchmark", side_effect=resolve_fn))
    else:
        cls = benchmark_cls or FakeBenchmark
        stack.enter_context(patch("framework.orchestrator._resolve_benchmark", return_value=cls))


# ---------------------------------------------------------------------------
# Benchmark helpers
# ---------------------------------------------------------------------------

class _MultiBenchmark(FakeBenchmark):
    """Returns N tasks with alternating ground truths."""

    def __init__(self, cfg, n: int = 3):
        super().__init__(cfg)
        self._n = n

    def load_samples(self):
        return [
            Sample(
                id=f"task-{i}",
                benchmark="simpleqa",
                messages=[Message(role="user", content=f"q{i}")],
                ground_truth="CORRECT" if i % 2 == 0 else "WRONG",
            )
            for i in range(self._n)
        ]

    def make_scorer(self):
        return ExactScorer()


class _EvenEchoHarness(EchoHarness):
    """Returns 'CORRECT' for even-indexed tasks, 'NOPE' for odd."""

    async def send_turn(self, messages, tools, system_prompt, ctx, timeout=120, **kw):
        idx = int(messages[-1]["content"][1:])  # "q0" → 0
        text = "CORRECT" if idx % 2 == 0 else "NOPE"
        return TurnResponse(text=text, tool_calls=[], finish_reason="stop",
                            input_tokens=2, output_tokens=1)


class _SecondBenchmark(FakeBenchmark):
    name = "niah"


# ---------------------------------------------------------------------------
# Summary aggregation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_summary_accuracy_calculation(mock_db):
    # 3 tasks: q0→CORRECT, q1→NOPE, q2→CORRECT  →  2/3 correct
    class Bench(_MultiBenchmark):
        def __init__(self, cfg): super().__init__(cfg, n=3)

    with ExitStack() as stack:
        _patch_orch(stack, mock_db, harness_cls=_EvenEchoHarness, benchmark_cls=Bench)
        orch = Orchestrator(_cfg())
        result = await orch.run()

    s = result["benchmarks"]["simpleqa"]
    assert s["completed"] == 3
    assert s["grades"]["CORRECT"] == 2
    assert s["grades"]["INCORRECT"] == 1
    assert s["accuracy"] == round(2 / 3, 4)


@pytest.mark.asyncio
async def test_summary_all_incorrect(mock_db):
    """Runner crash → empty output → scorer returns INCORRECT for every task."""

    class CrashHarness(EchoHarness):
        async def send_turn(self, *a, **kw):
            raise RuntimeError("always fails")

    class Bench(_MultiBenchmark):
        def __init__(self, cfg): super().__init__(cfg, n=3)

    with ExitStack() as stack:
        _patch_orch(stack, mock_db, harness_cls=CrashHarness, benchmark_cls=Bench)
        orch = Orchestrator(_cfg())
        result = await orch.run()

    s = result["benchmarks"]["simpleqa"]
    assert s["completed"] == 3
    assert s["accuracy"] == 0.0


@pytest.mark.asyncio
async def test_summary_token_totals(mock_db):
    class TokenHarness(EchoHarness):
        async def send_turn(self, *a, **kw):
            return TurnResponse(text="42", tool_calls=[], finish_reason="stop",
                                input_tokens=10, output_tokens=5)

    class Bench(_MultiBenchmark):
        def __init__(self, cfg): super().__init__(cfg, n=3)

    with ExitStack() as stack:
        _patch_orch(stack, mock_db, harness_cls=TokenHarness, benchmark_cls=Bench)
        orch = Orchestrator(_cfg())
        result = await orch.run()

    s = result["benchmarks"]["simpleqa"]
    assert s["input_tokens"] == 30   # 3 tasks × 10
    assert s["output_tokens"] == 15  # 3 tasks × 5


@pytest.mark.asyncio
async def test_summary_categories(mock_db):
    """metadata.category groups tasks into cat_summary."""

    class CatBenchmark(FakeBenchmark):
        def load_samples(self):
            return [
                Sample(id="t0", benchmark="simpleqa",
                       messages=[Message(role="user", content="q")],
                       ground_truth="42", metadata={"category": "math"}),
                Sample(id="t1", benchmark="simpleqa",
                       messages=[Message(role="user", content="q")],
                       ground_truth="42", metadata={"category": "math"}),
                Sample(id="t2", benchmark="simpleqa",
                       messages=[Message(role="user", content="q")],
                       ground_truth="42", metadata={"category": "science"}),
            ]

    with ExitStack() as stack:
        _patch_orch(stack, mock_db, benchmark_cls=CatBenchmark)
        orch = Orchestrator(_cfg())
        result = await orch.run()

    cats = result["benchmarks"]["simpleqa"]["categories"]
    assert "math" in cats
    assert "science" in cats
    assert cats["math"]["n"] == 2
    assert cats["science"]["n"] == 1
    assert cats["math"]["accuracy"] == 1.0


# ---------------------------------------------------------------------------
# Multiple benchmarks
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_multiple_benchmarks_both_in_result(mock_db):
    cfg = RunConfig.model_validate({
        "model": {"base_url": "http://fake", "api_key": "x", "model_name": "fake"},
        "harness": {"type": "echo"},
        "benchmarks": [{"name": "simpleqa", "limit": 1}, {"name": "niah", "limit": 1}],
        "parallelism": {"workers": 1, "timeout_per_task": 10, "eval_timeout": 5},
    })

    def resolve(name):
        return _SecondBenchmark if name == "niah" else FakeBenchmark

    with ExitStack() as stack:
        _patch_orch(stack, mock_db, resolve_fn=resolve)
        orch = Orchestrator(cfg)
        result = await orch.run()

    assert "simpleqa" in result["benchmarks"]
    assert "niah" in result["benchmarks"]


@pytest.mark.asyncio
async def test_first_benchmark_error_does_not_skip_second(mock_db):
    """Error in benchmark 1 tasks must not prevent benchmark 2 from running."""

    class CrashBenchmark(FakeBenchmark):
        name = "simpleqa"
        def load_samples(self):
            samples = super().load_samples()
            samples[0].sandbox = SandboxSpec(type="docker_run", image="alpine")
            return samples

    cfg = RunConfig.model_validate({
        "model": {"base_url": "http://fake", "api_key": "x", "model_name": "fake"},
        "harness": {"type": "echo"},
        "benchmarks": [{"name": "simpleqa", "limit": 1}, {"name": "niah", "limit": 1}],
        "parallelism": {"workers": 1, "timeout_per_task": 10, "eval_timeout": 5},
    })

    def resolve(name):
        return _SecondBenchmark if name == "niah" else CrashBenchmark

    with ExitStack() as stack:
        _patch_orch(stack, mock_db, resolve_fn=resolve)
        orch = Orchestrator(cfg)
        result = await orch.run()

    assert result["benchmarks"]["simpleqa"]["errors"] == 1
    assert result["benchmarks"]["niah"]["grades"].get("CORRECT") == 1


# ---------------------------------------------------------------------------
# finalize()
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_finalize_called_once(mock_db):
    finalize_count = 0

    def counting_finalize(self):
        nonlocal finalize_count
        finalize_count += 1

    def strict_load(name):
        # Only "echo" is a valid harness; auto-detection of "simpleqa_echo" must fail
        if name == "echo":
            return EchoHarness
        raise ValueError(f"no harness: {name}")

    with ExitStack() as stack:
        stack.enter_context(patch("framework.orchestrator.Database", return_value=mock_db))
        stack.enter_context(patch("framework.orchestrator.LLMJudge"))
        stack.enter_context(patch("framework.orchestrator.load_harness_class", side_effect=strict_load))
        stack.enter_context(patch("framework.orchestrator._resolve_benchmark", return_value=FakeBenchmark))
        stack.enter_context(patch.object(EchoHarness, "finalize", counting_finalize))
        orch = Orchestrator(_cfg())
        await orch.run()

    assert finalize_count == 1


@pytest.mark.asyncio
async def test_finalize_shared_instance_called_once(mock_db):
    """Same harness instance as default and override → finalize() runs exactly once."""
    finalize_count = 0

    class TrackHarness(EchoHarness):
        def finalize(self):
            nonlocal finalize_count
            finalize_count += 1

    cfg = RunConfig.model_validate({
        "model": {"base_url": "http://fake", "api_key": "x", "model_name": "fake"},
        "harness": {"type": "echo", "benchmark_harness": {"simpleqa": "echo"}},
        "benchmarks": [{"name": "simpleqa", "limit": 1}],
        "parallelism": {"workers": 1, "timeout_per_task": 10, "eval_timeout": 5},
    })

    with ExitStack() as stack:
        _patch_orch(stack, mock_db, harness_cls=TrackHarness)
        orch = Orchestrator(cfg)
        orch._benchmark_harnesses["simpleqa"] = orch.harness  # force same instance
        await orch.run()

    assert finalize_count == 1


@pytest.mark.asyncio
async def test_finalize_exception_does_not_crash_run(mock_db):
    class BoomFinalizeHarness(EchoHarness):
        def finalize(self):
            raise RuntimeError("finalize boom")

    with ExitStack() as stack:
        _patch_orch(stack, mock_db, harness_cls=BoomFinalizeHarness)
        orch = Orchestrator(_cfg())
        result = await orch.run()

    assert "simpleqa" in result["benchmarks"]


# ---------------------------------------------------------------------------
# progress_cb events
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_progress_events_sequence(mock_db):
    events = []

    def cb(**kw):
        events.append(kw["event"])

    with ExitStack() as stack:
        _patch_orch(stack, mock_db)
        orch = Orchestrator(_cfg(), progress_cb=cb)
        await orch.run()

    # Run-level "log" events (DB status, and Docker preflight when needs_docker)
    # may precede benchmark_start, so don't assert it is the very first event.
    assert "benchmark_start" in events
    assert events.index("benchmark_start") < events.index("start")
    assert "start" in events
    assert "done" in events
    assert events[-1] == "benchmark_done"


@pytest.mark.asyncio
async def test_preflight_aborts_when_docker_unavailable(mock_db):
    """A docker-requiring harness aborts the run with DockerUnavailableError
    (and a log event) instead of launching doomed tasks."""
    from framework.utils.docker import DockerUnavailableError

    class DockerHarness(EchoHarness):
        needs_docker = True

    events = []

    async def fake_status(timeout: int = 10):
        return False, "Docker daemon is not running. Start Docker and retry."

    with ExitStack() as stack:
        _patch_orch(stack, mock_db, harness_cls=DockerHarness)
        stack.enter_context(patch("framework.orchestrator.docker_status", side_effect=fake_status))
        orch = Orchestrator(_cfg(), progress_cb=lambda **kw: events.append(kw))
        with pytest.raises(DockerUnavailableError):
            await orch.run()

    # An error-level log was emitted and no tasks were started.
    assert any(e["event"] == "log" and e.get("level") == "error" for e in events)
    assert not any(e["event"] in ("benchmark_start", "start") for e in events)


@pytest.mark.asyncio
async def test_progress_done_contains_grade_and_score(mock_db):
    done_events = []

    def cb(**kw):
        if kw.get("event") == "done":
            done_events.append(kw)

    with ExitStack() as stack:
        _patch_orch(stack, mock_db)
        orch = Orchestrator(_cfg(), progress_cb=cb)
        await orch.run()

    assert len(done_events) == 1
    ev = done_events[0]
    assert ev["grade"] == "CORRECT"
    assert ev["score"] == 1.0
    assert "ground_truth" in ev


# ---------------------------------------------------------------------------
# run_id
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_explicit_run_id_used(mock_db):
    cfg = _cfg(**{"run_id": "my-custom-run"})
    with ExitStack() as stack:
        _patch_orch(stack, mock_db)
        orch = Orchestrator(cfg)
        result = await orch.run()
    assert result["run_id"] == "my-custom-run"


@pytest.mark.asyncio
async def test_generated_run_id_format(mock_db):
    with ExitStack() as stack:
        _patch_orch(stack, mock_db)
        orch = Orchestrator(_cfg())
        result = await orch.run()
    assert result["run_id"].startswith("run_")


# ---------------------------------------------------------------------------
# workers > 1
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_parallel_workers_all_complete(mock_db):
    """3 tasks with workers=2: all must complete without deadlock."""

    class Bench(_MultiBenchmark):
        def __init__(self, cfg): super().__init__(cfg, n=3)

    cfg = _cfg(**{"parallelism": {"workers": 2, "timeout_per_task": 10, "eval_timeout": 5}})

    with ExitStack() as stack:
        _patch_orch(stack, mock_db, benchmark_cls=Bench)
        orch = Orchestrator(cfg)
        result = await orch.run()

    assert result["benchmarks"]["simpleqa"]["completed"] == 3
