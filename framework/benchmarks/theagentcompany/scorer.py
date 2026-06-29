"""TheAgentCompany scorer — dynamically loads per-task evaluator modules."""
from __future__ import annotations

import importlib
import logging
from typing import TYPE_CHECKING

from framework.scorers.base import Scorer

if TYPE_CHECKING:
    from framework.models import AgentTrace, Sample, Score
    from framework.evaluators.llm_judge import LLMJudge
    from framework.sandbox import Sandbox

logger = logging.getLogger(__name__)


class SandboxEvalScorer(Scorer):
    """Delegate scoring to a per-task evaluator module.

    Used by TheAgentCompany: each task has an `evaluator_module` in metadata,
    and the module exposes `evaluate(sandbox_compat, judge) -> list[CheckpointResult]`.

    Args:
        module_prefix: Python module prefix, e.g.
            "framework.benchmarks.theagentcompany.evaluators"
    """

    def __init__(self, module_prefix: str) -> None:
        self._prefix = module_prefix

    async def __call__(
        self,
        sample: "Sample",
        trace: "AgentTrace",
        judge: "LLMJudge",
        sandbox: "Sandbox | None" = None,
    ) -> "Score":
        from framework.models import Score

        evaluator_module = sample.metadata.get("evaluator_module")
        if not evaluator_module:
            return Score(
                sample_id=sample.id,
                score=0.0,
                grade="INCORRECT",
                explanation="No evaluator_module in sample.metadata",
            )

        if sandbox is None:
            return Score(
                sample_id=sample.id,
                score=0.0,
                grade="ERROR",
                explanation="SandboxEvalScorer requires a sandbox",
            )

        # Wrap sandbox to match the old inspect_ai-style API expected by TAC evaluators
        from framework.benchmarks.theagentcompany.benchmark import _EvalSandboxCompat
        compat = _EvalSandboxCompat(sandbox)

        try:
            mod = importlib.import_module(f"{self._prefix}.{evaluator_module}")
            checkpoint_results = await mod.evaluate(compat, judge)  # always "original"
        except Exception as exc:
            logger.warning("Evaluator %s failed: %s", evaluator_module, exc)
            return Score(
                sample_id=sample.id,
                score=0.0,
                grade="ERROR",
                explanation=f"Evaluator error: {exc}",
            )

        from framework.benchmarks.theagentcompany.evaluators.base import summarize_checkpoints
        score, grade, details = summarize_checkpoints(checkpoint_results)
        return Score(
            sample_id=sample.id,
            score=score,
            grade=grade,
            explanation=f"score={score:.3f} | {details}",
        )
