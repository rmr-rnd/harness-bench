"""BFCL-specific LLM judge logic for multi-turn evaluation."""
from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from framework.evaluators.llm_judge import LLMJudge

MULTI_TURN_JUDGE_TEMPLATE = """You are evaluating whether an agent's sequence of API function calls achieves the same outcome as a ground truth sequence, even if the exact calls differ.

The agent and ground truth may use different intermediate steps (e.g., agent does cd() then find(path=".") while ground truth does find(path="subdir")) — these are semantically equivalent if they produce the same final state and results.

Task description / conversation:
{task_description}

Ground truth function calls (per turn):
{ground_truth}

Agent function calls (per turn):
{agent_calls}

Are the agent's calls semantically equivalent to the ground truth — i.e., would they achieve the same final state and produce equivalent results?

Answer with ONLY "yes" or "no"."""


def grade_multi_turn(
    judge: "LLMJudge",
    task_description: str,
    ground_truth: list[list[str]],
    agent_calls: list[list[str]],
) -> tuple[bool, str]:
    """Return (semantically_equivalent, raw_judge_output)."""
    prompt = MULTI_TURN_JUDGE_TEMPLATE.format(
        task_description=task_description,
        ground_truth=json.dumps(ground_truth, ensure_ascii=False, indent=2),
        agent_calls=json.dumps(agent_calls, ensure_ascii=False, indent=2),
    )
    raw = judge._call(prompt)
    passed = raw.lower().startswith("yes")
    return passed, raw
