"""Evaluator for research_answer_questions_on_paper — ported from inspect_evals.

Adapted from https://github.com/TheAgentCompany/TheAgentCompany/blob/main/workspaces/tasks/research-answer-questions-on-paper/evaluator.py
"""
from __future__ import annotations

import logging
from typing import Any

from framework.benchmarks.theagentcompany.evaluators.base import CheckpointResult, llm_judge

logger = logging.getLogger(__name__)

ANALYSIS_SHEET = "noise_simulation_analysis_sheet.txt"
RESEARCH_PAPER_PDF = "noise_simulation_paper.pdf"
REFERENCE_ANSWERS = "/utils/answers.txt"
TOTAL_QUESTIONS = 11
MIN_QA_PARTS = 2
FIRST_QUESTION_ID = 1

CRITERIA = [
    "noise_simulation_paper.pdf and noise_simulation_analysis_sheet.txt exist in /workspace",
] + [f"Question {i} answered correctly" for i in range(1, TOTAL_QUESTIONS + 1)]


def _decode_sandbox_text(raw: bytes | bytearray | str) -> str:
    if isinstance(raw, (bytes, bytearray)):
        return raw.decode("utf-8", errors="replace")
    return str(raw)


def _normalize_ref_answers(raw: list[str]) -> list[str]:
    """Drop trailing blank lines so trailing newlines in answers.txt do not shift indices."""
    out = list(raw)
    while out and out[-1].strip() == "":
        out.pop()
    return out


def _original_judge_criteria(reference_answer: str) -> str:
    return f"Does the content indicate {reference_answer!r}?"


async def grade_checkpoint_1(sandbox) -> list[dict[str, Any]]:
    """Checkpoint 1: research PDF and analysis sheet exist under /workspace."""
    paper_path = f"/workspace/{RESEARCH_PAPER_PDF}"
    analysis_path = f"/workspace/{ANALYSIS_SHEET}"
    ok = False
    try:
        paper_res = await sandbox.exec(
            ["bash", "-lc", f"test -f '{paper_path}' && echo ok || true"],
            timeout=20,
        )
        analysis_res = await sandbox.exec(
            ["bash", "-lc", f"test -f '{analysis_path}' && echo ok || true"],
            timeout=20,
        )
        ok = "ok" in (paper_res.stdout or "") and "ok" in (analysis_res.stdout or "")
    except Exception:
        ok = False
    return [{"id": 1, "value": 1 if ok else 0}]


def build_original_judge_items(
    analysis_text: str,
    ref_answers: list[str],
) -> list[dict[str, Any]] | None:
    """Build 11 LLM-judge items (ids 2-12)."""
    lines = analysis_text.splitlines()
    items: list[dict[str, Any]] = []
    refs = _normalize_ref_answers(ref_answers)

    for i in range(TOTAL_QUESTIONS):
        line_idx = i
        line_id = FIRST_QUESTION_ID + i
        if line_idx >= len(lines):
            items.append(
                {
                    "id": line_id,
                    "content": "",
                    "criteria": _original_judge_criteria(
                        refs[i] if i < len(refs) else ""
                    ),
                    "pass_value": 1,
                }
            )
            continue

        qa_pair = lines[line_idx].split(":", 1)
        if len(qa_pair) < MIN_QA_PARTS:
            logger.warning(
                "Analysis line %s is corrupted: %s", line_idx, lines[line_idx]
            )
            return None
        agent_answer = qa_pair[1].strip()
        ref = refs[i] if i < len(refs) else ""

        items.append(
            {
                "id": line_id,
                "content": agent_answer,
                "criteria": _original_judge_criteria(ref),
                "pass_value": 1,
            }
        )

    return items


async def evaluate(sandbox, judge) -> list[CheckpointResult]:
    # Reference "original": grade each question with an LLM judge using the
    # reference original judge items (build_original_judge_items).
    analysis_path = f"/workspace/{ANALYSIS_SHEET}"

    cp1_rows = await grade_checkpoint_1(sandbox)
    first = cp1_rows[0] if cp1_rows else {}
    cp1_pass = int(first.get("value", 0)) == 1

    ref_answers: list[str] = []
    analysis_text = ""
    try:
        ref_raw = await sandbox.read_file(REFERENCE_ANSWERS)
        ref_answers = _normalize_ref_answers(
            [ln.strip() for ln in _decode_sandbox_text(ref_raw).splitlines()]
        )
        analysis_raw = await sandbox.read_file(analysis_path)
        analysis_text = _decode_sandbox_text(analysis_raw)
    except Exception:
        pass

    # Checkpoint id=0 is "files present"
    checkpoints: list[CheckpointResult] = [
        CheckpointResult(0, 1.0 if cp1_pass else 0.0, 1.0, "files_present")
    ]

    items = build_original_judge_items(analysis_text, ref_answers)

    if items is None:
        for j in range(FIRST_QUESTION_ID, FIRST_QUESTION_ID + TOTAL_QUESTIONS):
            checkpoints.append(CheckpointResult(j, 0.0, 1.0, "parse_error"))
    else:
        for item in items:
            cp_id = int(item["id"])
            content = item.get("content", "")
            criteria = item.get("criteria", "")
            passed = False
            if judge is not None:
                try:
                    passed, _ = await llm_judge(judge, content=content, criteria=criteria)
                    logger.info(
                        "research_answer_questions_on_paper q%d: llm=%s", cp_id, passed
                    )
                except Exception as e:
                    logger.warning(
                        "research_answer_questions_on_paper: LLM judge q%d failed: %s", cp_id, e
                    )
            checkpoints.append(CheckpointResult(cp_id, 1.0 if passed else 0.0, 1.0,
                                                f"q{cp_id}={content[:60]!r}"))

    # Ensure exactly 12 checkpoints (id=0, 1..11)
    expected_ids = [0, *range(FIRST_QUESTION_ID, FIRST_QUESTION_ID + TOTAL_QUESTIONS)]
    if len(checkpoints) != len(expected_ids):
        value_by_id = {cp.id: cp.value for cp in checkpoints}
        checkpoints = [
            CheckpointResult(cid, value_by_id.get(cid, 0.0), 1.0, "")
            for cid in expected_ids
        ]

    return checkpoints
