"""TheAgentCompany-specific LLM judge logic."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from framework.evaluators.llm_judge import LLMJudge

GENERIC_JUDGE_TEMPLATE = """You are a grading assistant. You will be given some content and evaluation criteria. Determine whether the content meets the criteria.

--- CONTENT ---
{content}
--- END CONTENT ---

--- CONTEXT ---
{context}
--- END CONTEXT ---

Criteria: {criteria}

Does the content meet the criteria? Respond with ONLY "yes" or "no"."""


def grade_yes_no(judge: "LLMJudge", content: str, context: str, criteria: str) -> tuple[bool, str]:
    """Return (passed, raw_judge_output)."""
    prompt = GENERIC_JUDGE_TEMPLATE.format(
        content=content, context=context, criteria=criteria
    )
    raw = judge._call(prompt)
    passed = raw.lower().startswith("yes")
    return passed, raw
