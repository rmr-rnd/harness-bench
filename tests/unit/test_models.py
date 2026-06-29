"""Tests for models.py transition aliases."""
from __future__ import annotations

import pytest

from framework.models import AgentTrace, EvalResult, Message, Sample, Score, Task


def _make_sample(**kwargs) -> Sample:
    defaults = dict(
        id="s1",
        benchmark="test",
        messages=[Message(role="user", content="hi")],
        ground_truth="answer",
    )
    defaults.update(kwargs)
    return Sample(**defaults)


def _make_score(**kwargs) -> Score:
    defaults = dict(sample_id="s1", score=1.0, grade="CORRECT")
    defaults.update(kwargs)
    return Score(**defaults)


# ---------------------------------------------------------------------------
# Class-level aliases
# ---------------------------------------------------------------------------

def test_task_is_sample():
    assert Task is Sample


def test_eval_result_is_score():
    assert EvalResult is Score


# ---------------------------------------------------------------------------
# Sample.target property
# ---------------------------------------------------------------------------

def test_sample_target_alias_read():
    s = _make_sample(ground_truth="expected")
    assert s.target == "expected"
    assert s.target == s.ground_truth


def test_sample_target_alias_write():
    s = _make_sample(ground_truth="old")
    s.target = "new"
    assert s.ground_truth == "new"


# ---------------------------------------------------------------------------
# Score.task_id property
# ---------------------------------------------------------------------------

def test_score_task_id_alias_read():
    sc = _make_score(sample_id="abc")
    assert sc.task_id == "abc"
    assert sc.task_id == sc.sample_id


def test_score_task_id_alias_write():
    sc = _make_score(sample_id="abc")
    sc.task_id = "xyz"
    assert sc.sample_id == "xyz"
