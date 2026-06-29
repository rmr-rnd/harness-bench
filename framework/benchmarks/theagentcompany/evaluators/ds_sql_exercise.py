"""Evaluator for ds_sql_exercise — ported from inspect_evals, no inspect_ai dep.

Adapted from https://github.com/TheAgentCompany/TheAgentCompany/blob/main/workspaces/tasks/ds-sql-exercise/evaluator.py
"""
from __future__ import annotations

import logging
from typing import Any

from framework.benchmarks.theagentcompany.evaluators.base import CheckpointResult, llm_judge

logger = logging.getLogger(__name__)

TASK1_PATH = "/workspace/task1.txt"
TASK2_PATH = "/workspace/task2.txt"
TASK3_PATH = "/workspace/task3.txt"

CRITERIA = [
    "task1.txt contains the correct count of products (20)",
    "task2.txt contains the correct count of categories (3)",
    "task3.txt contains the top-3 most expensive products with correct names and prices",
]

# This task is scored with the reference's IMPROVED mode (deliberate, by user
# request): the strict ORIGINAL exact match (int(content)==20, exact name+price)
# rejects reasonable formatting like "20 products" or "$1,799.99". IMPROVED
# LLM-judges each file against weighted criteria (cp1=1, cp2=2, cp3=3), tolerating
# whitespace and price formatting while still requiring exact names/values.
IMPROVED_JUDGE_ITEMS: list[dict[str, Any]] = [
    {
        "id": 1,
        "path": TASK1_PATH,
        "pass_value": 1,
        "criteria": (
            "Does this file contain only the correct answer that there are exactly 20 "
            "products in the database? The content should be just the integer 20 (allow "
            "trivial whitespace)."
        ),
    },
    {
        "id": 2,
        "path": TASK2_PATH,
        "pass_value": 2,
        "criteria": (
            "Does this file contain only the correct answer that there are exactly 3 "
            "different product categories? The content should be just the integer 3 "
            "(allow trivial whitespace)."
        ),
    },
    {
        "id": 3,
        "path": TASK3_PATH,
        "pass_value": 3,
        "criteria": (
            "Does this file correctly list the top 3 most expensive products in the "
            "Electronics category in descending price order? The three lines must be: "
            '(1) product name "Gaming Laptop G1" with price 1799.99, (2) '
            '"Professional Laptop T1" with 1299.99, (3) "Tablet Pro T1" with 899.99. '
            "Prices may be formatted with two decimal places; product names must match "
            "exactly; order must be most expensive first."
        ),
    },
]


async def evaluate(sandbox, judge) -> list[CheckpointResult]:
    """Reference IMPROVED scoring: LLM-judge each task file (weighted 1/2/3).

    Empty file content fails without calling the judge (mirrors the reference
    _judge_one, which skips the model call on empty file bodies).
    """
    def _to_str(x: Any) -> str:
        if isinstance(x, (bytes, bytearray)):
            return x.decode("utf-8", errors="replace")
        return str(x)

    results: list[CheckpointResult] = []
    for item in IMPROVED_JUDGE_ITEMS:
        try:
            raw = await sandbox.read_file(item["path"])
        except Exception:
            raw = b""
        content = _to_str(raw).strip()
        pv = float(item["pass_value"])

        if not content:
            results.append(CheckpointResult(item["id"], 0.0, pv, "empty file"))
            continue

        passed, _ = await llm_judge(judge, content, item["criteria"])
        results.append(CheckpointResult(
            item["id"], pv if passed else 0.0, pv,
            f"improved={'pass' if passed else 'fail'} | {content[:40]!r}",
        ))
    return results
