"""Evaluator for hr_resume_categorization — ported from inspect_evals.

Adapted from https://github.com/TheAgentCompany/TheAgentCompany/blob/main/workspaces/tasks/hr-resume-categorization/evaluator.py
"""
from __future__ import annotations

import logging
from pathlib import Path

from framework.benchmarks.theagentcompany.evaluators.base import CheckpointResult

workspace_path = "/workspace"
subdirs = ["early-career", "mid-level", "senior", "unsure"]

TOTAL_EXPECTED_RESUMES = 94
EARLY_CAREER_EXPECTED = 58

logger = logging.getLogger(__name__)

CRITERIA = [
    f"total PDFs across all subdirs equals {TOTAL_EXPECTED_RESUMES}",
    f"early-career subdir contains exactly {EARLY_CAREER_EXPECTED} PDFs",
    "mid-level subdir contains exactly 1 PDF named Alex_Chen.pdf",
    "senior subdir contains exactly 1 PDF named Emily_Zhang.pdf",
]


async def _list_pdfs(sandbox, subdir: str) -> list[str]:
    # Use `|| true` so `ls` failures don't raise/mark the whole exec as failed.
    res = await sandbox.exec(
        [
            "bash",
            "-lc",
            f"ls -1 '{workspace_path}/{subdir}'/*.pdf 2>/dev/null || true",
        ],
        timeout=20,
    )
    stdout = (res.stdout or "").strip()
    if not stdout:
        return []
    # stdout is expected to be one filepath per line
    return [Path(line.strip()).name for line in stdout.splitlines() if line.strip()]


async def evaluate(sandbox, judge) -> list[CheckpointResult]:
    pdfs_by_subdir: dict[str, list[str]] = {}
    for subdir in subdirs:
        pdfs_by_subdir[subdir] = await _list_pdfs(sandbox, subdir)

    total_pdfs = sum(len(pdfs_by_subdir.get(subdir, [])) for subdir in subdirs)

    cp1_pass = total_pdfs == TOTAL_EXPECTED_RESUMES
    cp2_pass = len(pdfs_by_subdir.get("early-career", [])) == EARLY_CAREER_EXPECTED

    mid_level_pdfs = pdfs_by_subdir.get("mid-level", [])
    cp3_pass = len(mid_level_pdfs) == 1 and mid_level_pdfs[0] == "Alex_Chen.pdf"

    senior_pdfs = pdfs_by_subdir.get("senior", [])
    cp4_pass = len(senior_pdfs) == 1 and senior_pdfs[0] == "Emily_Zhang.pdf"

    scores = [1.0 if cp1_pass else 0.0, 1.0 if cp2_pass else 0.0,
              1.0 if cp3_pass else 0.0, 1.0 if cp4_pass else 0.0]

    # Reference grades this task deterministically (string judge, no LLM).
    return [
        CheckpointResult(1, scores[0], 1.0, f"total={total_pdfs}"),
        CheckpointResult(2, scores[1], 1.0, f"early-career={len(pdfs_by_subdir.get('early-career', []))}"),
        CheckpointResult(3, scores[2], 1.0, f"mid-level={mid_level_pdfs}"),
        CheckpointResult(4, scores[3], 1.0, f"senior={senior_pdfs}"),
    ]
