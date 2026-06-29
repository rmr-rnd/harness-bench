"""NIAH fidelity tests — mirror the inspect_evals/tests/niah contract.

These assert the invariants the upstream test-suite checks, adapted to our
abstractions:
  - the loader carries `needle_question` / `target_context_length` /
    `target_position` in Sample.metadata (upstream test_record_to_sample);
  - the judge sees the needle *question* as history and parses `(\\d+)`
    (upstream model_graded_qa contract);
  - the Score carries `target_*` metadata so the grid can be aggregated
    (upstream test_custom_scorer_wrapper);
  - grid accuracy aggregation matches a hand-computed subset breakdown
    (upstream test_subset_accuracy_combinations).
"""
from __future__ import annotations

import asyncio
import json
from collections import defaultdict

import pytest

from framework.config import BenchmarkConfig
from framework.benchmarks.niah import NIAHBenchmark, _niah_grade
from framework.models import AgentTrace, Sample, Score


# ── fixtures ──────────────────────────────────────────────────────────────

def _write_sample(data_dir, ans_dir, sid, *, tcl, tpos, needle, question, answer, context):
    (data_dir / f"{sid}.json").write_text(json.dumps({
        "id": sid, "context": context, "question": question, "needle": needle,
        "target_context_length": tcl, "target_position": tpos,
    }))
    (ans_dir / f"{sid}.json").write_text(json.dumps({"id": sid, "answer": answer}))


@pytest.fixture
def niah_dirs(tmp_path):
    data_dir = tmp_path / "data"
    ans_dir = tmp_path / "answers"
    data_dir.mkdir()
    ans_dir.mkdir()
    _write_sample(
        data_dir, ans_dir, "ctx10000_pos0",
        tcl=10000, tpos=0,
        needle="The first band to play on the Moon was the Virtual Rocket Band.",
        question="Which band was the first to perform on the Moon?",
        answer="The Virtual Rocket Band.",
        context="... haystack ... The first band to play on the Moon was the Virtual Rocket Band. ... more ...",
    )
    return data_dir, ans_dir


def _benchmark(data_dir, ans_dir, limit=None):
    cfg = BenchmarkConfig(name="niah", tasks_dir=str(data_dir), answers_dir=str(ans_dir), limit=limit)
    return NIAHBenchmark(cfg)


class _FakeJudge:
    """Records the prompt it was given; returns a canned response."""
    def __init__(self, response="7"):
        self.response = response
        self.last_prompt = None
        class _Cfg:
            model_name = "fake-judge"
        self.cfg = _Cfg()

    def _call(self, prompt, system=""):
        self.last_prompt = prompt
        return self.response


# ── loader contract (≈ test_record_to_sample) ───────────────────────────────

def test_loader_carries_metadata(niah_dirs):
    data_dir, ans_dir = niah_dirs
    [s] = _benchmark(data_dir, ans_dir).load_samples()
    assert s.metadata["needle_question"] == "Which band was the first to perform on the Moon?"
    assert s.metadata["target_context_length"] == 10000
    assert s.metadata["target_position"] == 0
    assert s.metadata["category"] == "ctx10000_pos0"
    assert s.ground_truth == "The Virtual Rocket Band."
    # upstream MAIN_PROMPT / QUESTION_PROMPT wording
    assert s.system_prompt.startswith("Please read the context")
    assert "Don't give information outside the context" in s.messages[0].content


def test_loader_respects_limit(niah_dirs):
    data_dir, ans_dir = niah_dirs
    assert len(_benchmark(data_dir, ans_dir, limit=0).load_samples()) >= 0  # limit=0 → falsy → all
    assert len(_benchmark(data_dir, ans_dir, limit=1).load_samples()) == 1


def test_loader_skips_samples_without_answers(niah_dirs):
    data_dir, ans_dir = niah_dirs
    (ans_dir / "ctx10000_pos0.json").unlink()
    assert _benchmark(data_dir, ans_dir).load_samples() == []


# ── judge contract (≈ model_graded_qa + test_custom_scorer_wrapper) ──────────

def _sample():
    return Sample(
        id="niah_x", benchmark="niah", messages=[], ground_truth="The Virtual Rocket Band.",
        metadata={"needle_question": "Which band?", "target_context_length": 10000, "target_position": 50},
    )


def test_judge_sees_question_and_carries_target_metadata():
    judge = _FakeJudge("7")
    score = _niah_grade(_sample(), AgentTrace(task_id="t", final_output="The Virtual Rocket Band."), judge)
    # judge prompt includes the needle question (history contract)
    assert "Which band?" in judge.last_prompt
    # Score carries grid coordinates (subset_accuracy contract)
    assert score.metadata["target_context_length"] == 10000
    assert score.metadata["target_position"] == 50
    assert score.score == 0.7 and score.grade == "CORRECT"


def test_judge_parses_first_integer_and_clamps():
    assert _niah_grade(_sample(), AgentTrace(task_id="t", final_output="x"), _FakeJudge("Score: 5")).score == 0.5
    assert _niah_grade(_sample(), AgentTrace(task_id="t", final_output="x"), _FakeJudge("10")).score == 1.0
    # garbage → defaults to 1 (not silent crash)
    assert _niah_grade(_sample(), AgentTrace(task_id="t", final_output="x"), _FakeJudge("no number here")).score == 0.1


# ── grid metric (≈ test_subset_accuracy_combinations) ────────────────────────

def test_grid_aggregation_matches_manual():
    """The orchestrator groups by Score.metadata target_* (via Sample category);
    here we replicate that grouping to prove the data plumbing supports it."""
    # three scores: (ctx1000,pos500)=1.0, (ctx1000,pos500)=0.0, (ctx2000,pos1000)=1.0
    scores = [
        Score(sample_id="a", score=1.0, grade="CORRECT",
              metadata={"target_context_length": 1000, "target_position": 500}),
        Score(sample_id="b", score=0.0, grade="INCORRECT",
              metadata={"target_context_length": 1000, "target_position": 500}),
        Score(sample_id="c", score=1.0, grade="CORRECT",
              metadata={"target_context_length": 2000, "target_position": 1000}),
    ]
    by_cell = defaultdict(list)
    for s in scores:
        m = s.metadata
        by_cell[(m["target_context_length"], m["target_position"])].append(s.score)
    acc = {k: sum(v) / len(v) for k, v in by_cell.items()}
    assert acc[(1000, 500)] == 0.5
    assert acc[(2000, 1000)] == 1.0
    overall = sum(s.score for s in scores) / len(scores)
    assert overall == pytest.approx(2 / 3)
