"""Evaluator for ml_grade_exam — ported from inspect_evals.

Adapted from https://github.com/TheAgentCompany/TheAgentCompany/blob/main/workspaces/tasks/ml-grade-exam/evaluator.py
"""
from __future__ import annotations

import logging
from typing import Any

from framework.benchmarks.theagentcompany.evaluators.base import CheckpointResult

logger = logging.getLogger(__name__)

CRITERIA = [
    "All 6 AgentCompany_ML_Quiz_Attempt*.pdf files downloaded to /workspace",
    "AgentCompany_ML_Quiz_Attempt1_1*.pdf scored correctly",
    "AgentCompany_ML_Quiz_Attempt2_15*.pdf scored correctly",
    "AgentCompany_ML_Quiz_Attempt3_9*.pdf scored correctly",
    "AgentCompany_ML_Quiz_Attempt4_12*.pdf scored correctly",
    "AgentCompany_ML_Quiz_Attempt5_11*.pdf scored correctly",
    "AgentCompany_ML_Quiz_Attempt6_10*.pdf scored correctly",
    "AgentCompany_ML_Quiz_Attempt2_15-submit.pdf selected for submission",
]


def _files_to_workspace_lines(stdout: str) -> list[str]:
    return [line.strip() for line in stdout.splitlines() if line.strip()]


def grade_checkpoint_1(files: list[str]) -> list[dict[str, Any]]:
    """Check that all 6 quiz attempt files exist."""
    base_name = "AgentCompany_ML_Quiz_Attempt"
    files_found = {f"{base_name}{i}": False for i in range(1, 7)}

    for file in files:
        file_prefix = (
            file[: len(base_name) + 1] if len(file) >= len(base_name) + 1 else ""
        )
        if file_prefix in files_found:
            files_found[file_prefix] = True

    all_files_found = True
    for key in files_found:
        if not files_found[key]:
            logger.warning("File starting with %s not found", key)
            all_files_found = False

    return [{"id": 1, "value": 1 if all_files_found else 0}]


def grade_checkpoint_2(files: list[str]) -> list[dict[str, Any]]:
    """Grade checkpoint 2 through 7 (6 sub-checkpoints): each expected graded filename prefix appears in workspace."""
    base_name = "AgentCompany_ML_Quiz_Attempt"
    files_graded = {
        "AgentCompany_ML_Quiz_Attempt1_1": False,
        "AgentCompany_ML_Quiz_Attempt2_15": False,
        "AgentCompany_ML_Quiz_Attempt3_9": False,
        "AgentCompany_ML_Quiz_Attempt4_12": False,
        "AgentCompany_ML_Quiz_Attempt5_11": False,
        "AgentCompany_ML_Quiz_Attempt6_10": False,
    }

    for file in files:
        file_prefix = file.split(".")[0].split("-")[0]
        if file_prefix in files_graded:
            files_graded[file_prefix] = True

    checkpoints: list[dict[str, Any]] = []
    checkpoint_id = 1
    for key in files_graded:
        checkpoint_id += 1
        if not files_graded[key]:
            logger.warning(
                "File starting with %s not graded correctly",
                key[: len(base_name) + 1],
            )
            checkpoints.append({"id": checkpoint_id, "value": 0})
        else:
            checkpoints.append({"id": checkpoint_id, "value": 1})

    return checkpoints


def grade_checkpoint_3(files: list[str]) -> list[dict[str, Any]]:
    """Grade checkpoint 3, at ID 8: the correct file is selected for submission."""
    correct_file_selected = True
    for file in files:
        if "submit" in file and file != "AgentCompany_ML_Quiz_Attempt2_15-submit.pdf":
            correct_file_selected = False

    if "AgentCompany_ML_Quiz_Attempt2_15-submit.pdf" not in files:
        correct_file_selected = False

    return [{"id": 8, "value": 1 if correct_file_selected else 0}]


def grade_checkpoints_original(files: list[str]) -> list[dict[str, Any]]:
    """Mirrors evaluator from original implementation."""
    checkpoints: list[dict[str, Any]] = [
        *grade_checkpoint_1(files),
        *grade_checkpoint_2(files),
        *grade_checkpoint_3(files),
    ]
    return checkpoints


async def evaluate(sandbox, judge) -> list[CheckpointResult]:
    res = await sandbox.exec(
        ["bash", "-lc", "ls -1 /workspace 2>/dev/null || true"],
        timeout=20,
    )
    stdout = (res.stdout or "").strip()
    if isinstance(stdout, (bytes, bytearray)):
        stdout = stdout.decode("utf-8", errors="replace")
    files = _files_to_workspace_lines(str(stdout))

    # Reference grades ml_grade_exam deterministically (no LLM).
    checkpoints = grade_checkpoints_original(files)

    return [
        CheckpointResult(
            id=int(cp["id"]),
            value=float(cp.get("value", 0)),
            max_value=1.0,
            note="",
        )
        for cp in checkpoints
    ]
