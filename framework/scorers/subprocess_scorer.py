from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from framework.scorers.base import Scorer

if TYPE_CHECKING:
    from framework.models import AgentTrace, Sample, Score
    from framework.evaluators.llm_judge import LLMJudge
    from framework.sandbox import Sandbox


class SubprocessScorer(Scorer):
    """Score by running a subprocess (e.g. pytest). The runner_fn receives (sample, trace)
    and returns (passed: bool, explanation: str)."""

    def __init__(self, runner_fn: Callable) -> None:
        self._runner_fn = runner_fn

    async def __call__(
        self,
        sample: "Sample",
        trace: "AgentTrace",
        judge: "LLMJudge",
        sandbox: "Sandbox | None" = None,
    ) -> "Score":
        from framework.models import Score
        passed, explanation = self._runner_fn(sample, trace)
        return Score(
            sample_id=sample.id,
            score=1.0 if passed else 0.0,
            grade="CORRECT" if passed else "INCORRECT",
            explanation=explanation[:500],
        )
