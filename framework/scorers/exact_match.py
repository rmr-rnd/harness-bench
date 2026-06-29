from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from framework.scorers.base import Scorer

if TYPE_CHECKING:
    from framework.models import AgentTrace, Sample, Score
    from framework.evaluators.llm_judge import LLMJudge
    from framework.sandbox import Sandbox


class ExactMatchScorer(Scorer):
    """Score by exact string match after applying a parser function to the trace output."""

    def __init__(self, parser_fn: Callable[[str], str]) -> None:
        self._parser_fn = parser_fn

    async def __call__(
        self,
        sample: "Sample",
        trace: "AgentTrace",
        judge: "LLMJudge",
        sandbox: "Sandbox | None" = None,
    ) -> "Score":
        from framework.models import Score
        predicted = self._parser_fn(trace.final_output or "")
        target = sample.target
        correct = predicted == target
        return Score(
            sample_id=sample.id,
            score=1.0 if correct else 0.0,
            grade="CORRECT" if correct else "INCORRECT",
            explanation=f"predicted={predicted!r} gold={target!r}",
        )
