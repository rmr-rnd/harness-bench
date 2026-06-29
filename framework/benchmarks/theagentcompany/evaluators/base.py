"""Base helpers shared by all TAC evaluators."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_VERDICT_RE = re.compile(r"\b(yes|no)\b", re.IGNORECASE)


def _verdict_is_yes(raw: str) -> bool:
    """Robustly extract a yes/no verdict from the judge response.

    The judge is typically a reasoning model. When it returns an empty
    ``content``, ``LLMJudge._call`` falls back to ``reasoning_content`` whose
    text begins with the chain-of-thought (e.g. "We need to see…"), not the
    verdict — so a naive ``startswith("yes")`` marks every answer as fail.
    Take the LAST yes/no token instead (the conclusion comes at the end), which
    also handles a clean ``"yes"`` / ``"no"`` content unchanged.
    """
    matches = _VERDICT_RE.findall(raw or "")
    return bool(matches) and matches[-1].lower() == "yes"


@dataclass
class CheckpointResult:
    id: int
    value: float      # points earned
    max_value: float  # max possible points
    note: str = ""


def summarize_checkpoints(results: list["CheckpointResult"]) -> tuple[float, str, str]:
    """Aggregate checkpoint results into (score, grade, details).

    Matches the inspect_evals `checkpoints_metric`: normalise each checkpoint to
    clamp(value/max, 0..1), then take the MEAN across checkpoints (each weighted
    equally — NOT points-weighted). This is the reference's per-sample rate.
    """
    rates = [
        max(0.0, min(1.0, r.value / r.max_value))
        for r in results
        if r.max_value > 0
    ]
    score = sum(rates) / len(rates) if rates else 0.0
    grade = "CORRECT" if score >= 1.0 else ("PARTIAL" if score > 0 else "INCORRECT")
    details = " | ".join(
        f"cp{r.id}={r.value}/{r.max_value} ({r.note})" for r in results
    )
    return score, grade, details


async def llm_judge(judge, content: str, criteria: str, context: str = "") -> tuple[bool, str]:
    """Call LLMJudge and return (passed, raw_response)."""
    parts = [
        "You are a grading assistant. You will be given some content and "
        "evaluation criteria. Determine whether the content meets the criteria.",
        "",
        "--- CONTENT ---",
        content.strip(),
        "--- END CONTENT ---",
    ]
    if context:
        parts += ["", "--- CONTEXT ---", context.strip(), "--- END CONTEXT ---"]
    parts += [
        "",
        f"Criteria: {criteria}",
        "",
        'Does the content meet the criteria? Respond with ONLY "yes" or "no".',
    ]
    prompt = "\n".join(parts)
    raw = judge._call(prompt)
    return _verdict_is_yes(raw), raw.strip()
