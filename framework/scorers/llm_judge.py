from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from framework.scorers.base import Scorer

if TYPE_CHECKING:
    from framework.models import AgentTrace, Sample, Score
    from framework.evaluators.llm_judge import LLMJudge
    from framework.sandbox import Sandbox


class LLMJudgeScorer(Scorer):
    """Score by calling an LLM judge with a prompt template.

    The template receives {question}, {target}, {predicted} interpolations.
    The judge is expected to return a grade string that the grader_fn parses
    into (grade: str, score: float, raw: str).
    """

    def __init__(
        self,
        grader_fn: Callable,
    ) -> None:
        self._grader_fn = grader_fn

    async def __call__(
        self,
        sample: "Sample",
        trace: "AgentTrace",
        judge: "LLMJudge",
        sandbox: "Sandbox | None" = None,
    ) -> "Score":
        from framework.models import Score
        return self._grader_fn(sample, trace, judge)
