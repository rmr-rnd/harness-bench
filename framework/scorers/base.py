from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from framework.models import AgentTrace, Sample, Score
    from framework.evaluators.llm_judge import LLMJudge
    from framework.sandbox import Sandbox


class Scorer(ABC):
    """Evaluate one agent trace against a sample. Reusable across benchmarks."""

    @abstractmethod
    async def __call__(
        self,
        sample: "Sample",
        trace: "AgentTrace",
        judge: "LLMJudge",
        sandbox: "Sandbox | None" = None,
    ) -> "Score": ...
