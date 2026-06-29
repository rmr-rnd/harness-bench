"""TheAgentCompany scoring fidelity tests.

Verify the inspect_evals alignment:
  - partial-credit formula = normalized per-checkpoint mean (not points-weighted);
  - `original` logic (the only mode) does NOT invoke the LLM for tasks the
    reference grades deterministically ("string judges");
  - hr_populate gates on all WebDAV endpoints (no free CP1 credit);
  - sde_copy preserves the reference's intentional error → True fall-through.
"""
from __future__ import annotations

import pytest

from framework.benchmarks.theagentcompany.evaluators import (
    ds_sql_exercise,
    hr_populate_salary_increase_memo,
    hr_resume_categorization,
    sde_copy_table_from_pdf_to_xlsx,
)
from framework.benchmarks.theagentcompany.evaluators.base import (
    CheckpointResult,
    _verdict_is_yes,
    summarize_checkpoints,
)


# ── partial-credit formula ───────────────────────────────────────────────────

def test_formula_is_normalized_mean_not_points_weighted():
    # checkpoints with maxes 1/2/3; only the max-3 one fully passes.
    res = [CheckpointResult(1, 0.0, 1.0), CheckpointResult(2, 0.0, 2.0), CheckpointResult(3, 3.0, 3.0)]
    score, grade, _ = summarize_checkpoints(res)
    # reference checkpoints_metric: mean(0/1, 0/2, 3/3) = mean(0,0,1) = 0.333
    assert round(score, 3) == 0.333
    # NOT the old points-weighted sum(value)/sum(max) = 3/6 = 0.5
    assert score != 0.5
    assert grade == "PARTIAL"


def test_formula_all_pass_is_correct():
    res = [CheckpointResult(1, 1.0, 1.0), CheckpointResult(2, 2.0, 2.0)]
    score, grade, _ = summarize_checkpoints(res)
    assert score == 1.0 and grade == "CORRECT"


def test_formula_empty_is_incorrect():
    score, grade, _ = summarize_checkpoints([])
    assert score == 0.0 and grade == "INCORRECT"


# ── string-judge tasks never call the LLM (judge spy) ─────────────────────────
# Scoring is always "original" — the deterministic ("string judge") tasks must
# never invoke the LLM. (LLM is used only by ds_answer / research_answer, which
# the reference also grades with an LLM.)

class _JudgeSpy:
    def __init__(self, response="yes"):
        self.calls = 0
        self.response = response
        class _Cfg:
            model_name = "spy"
        self.cfg = _Cfg()

    def _call(self, prompt, system=""):
        self.calls += 1
        return self.response


class _FakeSandbox:
    """Minimal sandbox: read_file raises (files absent) → checkpoints fail;
    exec returns empty stdout. Enough to confirm no LLM call happens."""
    async def read_file(self, path):
        raise FileNotFoundError(path)

    async def exec(self, cmd, timeout=None):
        class _R:
            stdout = ""
            success = True
            returncode = 0
        return _R()


class _FilesSandbox:
    """Sandbox returning fixed file bytes (for tasks that read /workspace files)."""
    def __init__(self, files: dict):
        self.files = files

    async def read_file(self, path):
        if path in self.files:
            return self.files[path]
        raise FileNotFoundError(path)

    async def exec(self, cmd, timeout=None):
        class _R:
            stdout = ""
            success = True
            returncode = 0
        return _R()


@pytest.mark.asyncio
async def test_ds_sql_improved_uses_judge():
    # ds_sql is now scored with the reference IMPROVED LLM judge (weighted 1/2/3).
    judge = _JudgeSpy(response="yes")
    sb = _FilesSandbox({
        ds_sql_exercise.TASK1_PATH: b"20",
        ds_sql_exercise.TASK2_PATH: b"3",
        ds_sql_exercise.TASK3_PATH: b'"Gaming Laptop G1",1799.99',
    })
    res = await ds_sql_exercise.evaluate(sb, judge)
    assert judge.calls == 3                       # one LLM call per non-empty file
    assert len(res) == 3
    assert all(r.value == r.max_value for r in res)  # spy="yes" → all pass
    assert [r.max_value for r in res] == [1.0, 2.0, 3.0]


@pytest.mark.asyncio
async def test_ds_sql_improved_skips_empty_files():
    # Empty file content fails WITHOUT calling the judge (reference _judge_one).
    judge = _JudgeSpy()
    res = await ds_sql_exercise.evaluate(_FakeSandbox(), judge)
    assert judge.calls == 0
    assert len(res) == 3 and all(r.value == 0 for r in res)


@pytest.mark.asyncio
async def test_string_judge_task_never_calls_judge():
    # hr_resume_categorization is a "string judge" task in the reference.
    judge = _JudgeSpy()
    await hr_resume_categorization.evaluate(_FakeSandbox(), judge)
    assert judge.calls == 0


# ── hr_populate WebDAV gate (no free CP1 credit) ─────────────────────────────
# Reference `_raise_on_webdav_failure`: unless ALL four WebDAV endpoints return
# their expected status the whole sample scores 0 (empty checkpoints). The HR
# dir pre-exists, so without the gate a no-op agent would bank CP1 → ~0.33.

class _NoOpAgentSandbox:
    """Simulates a fresh task where the agent did nothing:
    - HR Documents dir PROPFIND → 207 (pre-exists);
    - memo GET → 404, notice dir PROPFIND → 404 (agent created neither).
    """
    async def read_file(self, path):
        raise FileNotFoundError(path)

    async def exec(self, cmd, timeout=None):
        script = cmd[-1]

        class _R:
            success = True
            returncode = 0
            stdout = ""

        r = _R()
        if "PROPFIND" in script and "salary_increase_notice" in script:
            r.stdout = "404\n"            # notice dir missing (propfind_b64: code\nb64)
        elif "PROPFIND" in script:
            r.stdout = "207"              # HR Documents dir pre-exists
        elif "base64" in script:
            r.success = False             # curl -sf on missing memo fails
        else:
            r.stdout = "404"              # memo GET http_code
        return r


@pytest.mark.asyncio
async def test_hr_populate_gate_zeroes_when_memo_and_notice_missing():
    judge = _JudgeSpy()
    res = await hr_populate_salary_increase_memo.evaluate(_NoOpAgentSandbox(), judge)
    # Gate failed → empty checkpoints → metric 0.0 (NOT 0.33 from a free CP1).
    assert res == []
    score, grade, _ = summarize_checkpoints(res)
    assert score == 0.0 and grade == "INCORRECT"
    assert judge.calls == 0


def test_hr_populate_gate_predicate():
    ok = dict(
        hr_dir_propfind_status=207, memo_http=200,
        notice_propfind_depth0=207, notice_propfind_depth1=207,
    )
    assert hr_populate_salary_increase_memo._webdav_all_reachable(**ok) is True
    # Any single endpoint off → gate fails.
    assert hr_populate_salary_increase_memo._webdav_all_reachable(
        **{**ok, "memo_http": 404}) is False


# ── sde_copy: reference's intentional error → True fall-through ───────────────

# ── judge verdict parsing: robust to reasoning-model output ──────────────────
# A reasoning judge may return an empty `content`; LLMJudge._call then falls back
# to `reasoning_content`, whose text starts with the chain-of-thought, not the
# verdict. The old `startswith("yes")` marked every correct answer as fail
# (this is what zeroed research_answer). Parse the LAST yes/no token instead.

def test_verdict_clean_yes_no():
    assert _verdict_is_yes("yes") is True
    assert _verdict_is_yes("no") is False
    assert _verdict_is_yes("Yes.") is True


def test_verdict_reasoning_prefixed_concludes_yes():
    raw = ('We need to see if content indicates "RATS dataset". '
           'The content is "RATS dataset". That indicates it. So answer yes.')
    assert _verdict_is_yes(raw) is True          # old startswith("yes") → False (the bug)


def test_verdict_reasoning_prefixed_concludes_no():
    raw = "The content says 88.5 but criteria wants 12.0, so the answer is no"
    assert _verdict_is_yes(raw) is False


def test_verdict_empty_is_not_yes():
    assert _verdict_is_yes("") is False


def test_sde_copy_found_matching_row_error_returns_true():
    pd = pytest.importorskip("pandas")
    # 1-column frame; sheet entry references column index 1 → iloc raises
    # IndexError → reference grader falls through to `return True` (kept for
    # fidelity; our old code returned False and was stricter than the original).
    df = pd.DataFrame({"a": ["x"]})
    sheet = [[["x"], ["y"], 1.0]]
    assert sde_copy_table_from_pdf_to_xlsx.found_matching_row(df, sheet) is True
