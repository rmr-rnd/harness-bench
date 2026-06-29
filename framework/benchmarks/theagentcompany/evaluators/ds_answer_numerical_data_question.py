"""Evaluator for ds_answer_numerical_data_question — ported from inspect_evals.

Adapted from https://github.com/TheAgentCompany/TheAgentCompany/blob/main/workspaces/tasks/ds-answer-numerical-data-question/evaluator.py
"""
from __future__ import annotations

import io
import logging
import zipfile
from typing import Any
from xml.etree import ElementTree as ET

from framework.benchmarks.theagentcompany.evaluators.base import CheckpointResult, llm_judge

logger = logging.getLogger(__name__)

ANSWER_FILE = "/workspace/answer.docx"

# Original predicates — expect answers to three decimal places (per-line grading).
# Kept for reference; this task now grades with the IMPROVED criteria below.
ORIGINAL_LLM_CRITERIA: list[str] = [
    "the content contains the correct answer `Tea (including ice tea)`",
    "the content contains the correct answer `88.475`",
    "the content has the correct answer `74.775`",
]

# Improved predicates (from the reference grader) — accept rounded values and
# percent signs, and grade the whole document at once (robust to newline splits).
IMPROVED_LLM_CRITERIA: list[str] = [
    "The beginning of the content includes `Tea (including ice tea)`, or something similar",
    (
        "The first numerical value in the content has the value `88.475` (or `88.475%`) or a rounded "
        "equivalent such as `88.5` / `88.5%`"
    ),
    (
        "The second numerical value in the content has the value `74.775` (or `74.775%`) or a rounded "
        "equivalent such as `74.8` / `74.8%`"
    ),
]

# This task is scored with the reference's IMPROVED criteria (deliberate, by user
# request): the strict ORIGINAL mode penalises reasonable rounding / percent signs.
CRITERIA = IMPROVED_LLM_CRITERIA

# Checkpoint(2, int(passed) * 2)
POINTS_PER_CHECKPOINT = 2

_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _paragraph_text_from_p(p_elem: ET.Element) -> str:
    """Text of a w:p (same idea as paragraph.text in python-docx)."""
    parts: list[str] = []
    t_tag = f"{{{_W_NS}}}t"
    for t in p_elem.iter(t_tag):
        if t.text:
            parts.append(t.text)
        if t.tail:
            parts.append(t.tail)
    return "".join(parts)


def _main_story_paragraph_texts(root: ET.Element) -> list[str]:
    """Paragraph texts in main body order (excludes table cells), like Document.paragraphs."""
    body_tag = f"{{{_W_NS}}}body"
    p_tag = f"{{{_W_NS}}}p"

    body: ET.Element | None = None
    if root.tag == body_tag:
        body = root
    else:
        body = root.find(body_tag)

    if body is None:
        return []

    texts: list[str] = []
    for child in body:
        if child.tag == p_tag:
            texts.append(_paragraph_text_from_p(child))
        # Skip w:tbl (table cell paragraphs are not in doc.paragraphs).
    return texts


def read_docx_like_python_docx(docx_bytes: bytes) -> str:
    """Reproduce python-docx Document: join main-story paragraph texts with newlines."""
    if not docx_bytes:
        return ""
    try:
        with zipfile.ZipFile(io.BytesIO(docx_bytes)) as zf:
            xml_bytes = zf.read("word/document.xml")
    except (KeyError, zipfile.BadZipFile, OSError, ValueError) as e:
        logger.warning("ds_answer_numerical_data_question: could not open docx: %s", e)
        return ""

    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        logger.warning("ds_answer_numerical_data_question: bad document.xml: %s", e)
        return ""

    paragraphs = _main_story_paragraph_texts(root)
    return "\n".join(paragraphs)


def lines_for_grading(docx_bytes: bytes) -> list[str]:
    """Same as original: full doc string, strip, split on newlines (keeps empty lines)."""
    content = read_docx_like_python_docx(docx_bytes)
    return content.strip().split("\n")


async def evaluate(sandbox, judge) -> list[CheckpointResult]:
    def _to_bytes(x: Any) -> bytes:
        if isinstance(x, (bytes, bytearray)):
            return bytes(x)
        if x is None:
            return b""
        return str(x).encode("utf-8", errors="replace")

    raw: bytes = b""
    try:
        data = await sandbox.read_file(ANSWER_FILE)
        raw = _to_bytes(data)
    except Exception as e:
        logger.warning(
            "ds_answer_numerical_data_question: read_file %s failed: %s",
            ANSWER_FILE,
            e,
        )

    lines = lines_for_grading(raw)

    # Reference "improved": grade the WHOLE document against each IMPROVED
    # criterion (accepts rounded values / percent signs; robust to newline
    # splits). Mirrors the reference get_improved_judge_items.
    joined_response = "\n".join(lines)
    criteria = IMPROVED_LLM_CRITERIA

    results = []
    for i, criterion in enumerate(criteria):
        passed, raw_resp = await llm_judge(judge, joined_response, criterion)
        score = float(POINTS_PER_CHECKPOINT) if passed else 0.0
        note = f"improved={'pass' if passed else 'fail'}"
        results.append(CheckpointResult(
            id=i + 1,
            value=score,
            max_value=float(POINTS_PER_CHECKPOINT),
            note=note,
        ))
    return results
