from __future__ import annotations

from typing import TYPE_CHECKING

from framework.scorers.base import Scorer

if TYPE_CHECKING:
    from framework.models import AgentTrace, Sample, Score
    from framework.evaluators.llm_judge import LLMJudge
    from framework.sandbox import Sandbox


class CheckpointScorer(Scorer):
    """Score by running sample.checkpoints inside the sandbox."""

    async def __call__(
        self,
        sample: "Sample",
        trace: "AgentTrace",
        judge: "LLMJudge",
        sandbox: "Sandbox | None" = None,
    ) -> "Score":
        from framework.models import Score
        from framework.sandbox import run_checkpoints

        if not sample.checkpoints:
            return Score(
                sample_id=sample.id,
                score=0.0,
                grade="ERROR",
                explanation="CheckpointScorer: no checkpoints defined on sample",
            )
        if sandbox is None:
            return Score(
                sample_id=sample.id,
                score=0.0,
                grade="ERROR",
                explanation="CheckpointScorer requires a sandbox",
            )

        eval_result = await run_checkpoints(sandbox, sample.id, sample.checkpoints)
        return Score(
            sample_id=sample.id,
            score=eval_result.score,
            grade=eval_result.grade,
            explanation=eval_result.explanation,
        )
