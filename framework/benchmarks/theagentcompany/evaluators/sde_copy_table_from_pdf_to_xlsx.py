"""Evaluator for sde_copy_table_from_pdf_to_xlsx — ported from inspect_evals.

Adapted from https://github.com/TheAgentCompany/TheAgentCompany/blob/main/workspaces/tasks/sde-copy-table-from-pdf-to-xlsx/evaluator.py
"""
from __future__ import annotations

import logging
import math
import re
from io import BytesIO

from framework.benchmarks.theagentcompany.evaluators.base import CheckpointResult

try:
    import pandas as pd
except ImportError:  # pragma: no cover
    pd = None  # type: ignore[assignment]

LINK_FILE = "/workspace/link.txt"
LOCAL_DST = "/workspace/openhands_evaluation.xlsx"

logger = logging.getLogger(__name__)

NAN = float("nan")

sheet1: list[list[object]] = [
    [["Lemur"], ["Lemur-chat-70b"], NAN, 5.3, NAN, NAN],
    [["CodeActAgent", "v1.8"], ["claude-3-5-sonnet"], 26.0, 15.3, 52.0, NAN],
]

sheet2: list[list[object]] = [
    [["SWE-agent", "1-shot"], ["gpt-4-turbo"], 87.7, NAN],
    [["OH", "CodeActAgent", "v1.5"], ["gpt-3.5-turbo-16k-0613"], 20.1, 0.11],
]

sheet3: list[list[object]] = [
    [["WebArena", "Agent"], ["Llama3-chat-70b"], 7.0, NAN],
]

sheet4: list[list[object]] = [
    [["OH", "CodeActAgent", "v1.5"], ["gpt-3.5-turbo-0125"], 11.8, 0.006],
]

LINK_PATTERN = re.compile(r"^https?://\S+$")

CRITERIA = [
    "link.txt contains a valid share URL (http/https, single line)",
    "Sheet 1 of openhands_evaluation.xlsx matches Table 3 expected rows",
    "Sheet 2 matches Table 4 expected rows",
    "Sheet 3 matches Table 5 expected rows",
    "Sheet 4 matches Table 6 expected rows",
]


def found_matching_row(
    df: "pd_types.DataFrame | None", sheet: list[list[object]]
) -> bool:
    """Matches original implementation."""
    if df is None:
        return False
    if pd is None:  # pragma: no cover
        return False
    try:
        for entry in sheet:
            column = 0
            cond = pd.Series(True, index=df.index)
            for keywords in entry:
                if isinstance(keywords, list):
                    for key in keywords:
                        cond &= df.iloc[:, column].str.contains(key, case=False)
                elif isinstance(keywords, float) and math.isnan(keywords):
                    cond &= df.iloc[:, column].isna()
                else:
                    cond &= df.iloc[:, column] == keywords

                column += 1
            matching_rows = df[cond]
            if matching_rows.empty:
                return False
    except Exception as e:
        logger.error("Error finding matching row: %s", e)
        # Reference deliberately falls through to `return True` here — a known
        # upstream false-positive on error, kept unchanged for fidelity (do NOT
        # `return False`; that would make us stricter than the original grader).
    return True


def read_all_sheets_from_bytes(content: bytes) -> "list[pd_types.DataFrame]":
    """Return all sheets from xlsx bytes, or empty list on error."""
    if pd is None:
        return []
    if not content:
        return []
    try:
        xls = pd.ExcelFile(BytesIO(content))
    except Exception as e:
        logger.error("Error opening Excel from bytes: %s", e)
        return []

    sheets: list["pd_types.DataFrame"] = []
    for sheet_name in xls.sheet_names:
        try:
            sheets.append(xls.parse(sheet_name))
        except Exception as e:
            logger.error("Error reading sheet %s: %s", sheet_name, e)
    return sheets


def found_matching_sheet(
    dfs: "list[pd_types.DataFrame]", sheet: list[list[object]]
) -> bool:
    for df in dfs:
        if found_matching_row(df, sheet):
            return True
    return False


def grade_checkpoint1(link: str | None) -> bool:
    if link is None:
        return False
    stripped = link.strip()
    if not stripped:
        return False
    if "\n" in link or "\r" in link:
        return False
    return bool(LINK_PATTERN.match(stripped))


async def evaluate(sandbox, judge) -> list[CheckpointResult]:
    checkpoints: list[CheckpointResult] = []

    try:
        link = (await sandbox.read_file(LINK_FILE)).decode(errors="replace").strip()
    except Exception:
        link = ""
    cp1_pass = grade_checkpoint1(link)
    checkpoints.append(CheckpointResult(1, 1.0 if cp1_pass else 0.0, 1.0, f"link={link[:80]!r}"))

    xlsx_bytes: bytes | None = None
    try:
        raw = await sandbox.read_file(LOCAL_DST)
        xlsx_bytes = raw if isinstance(raw, bytes) else bytes(raw)
    except Exception as e:
        logger.warning("Could not read %s: %s", LOCAL_DST, e)

    dfs = read_all_sheets_from_bytes(xlsx_bytes or b"")

    # Match original grader: fixed sheet indices (0..3), deterministic pandas
    # row matching (no LLM). Distinct checkpoint ids (2..5) for debugging.
    sheet_checks = [
        (2, 0, sheet1),
        (3, 1, sheet2),
        (4, 2, sheet3),
        (5, 3, sheet4),
    ]
    for checkpoint_id, sheet_idx, expected_sheet in sheet_checks:
        cp_pass = False
        try:
            if 0 <= sheet_idx < len(dfs):
                cp_pass = found_matching_row(dfs[sheet_idx], expected_sheet)
        except Exception:
            cp_pass = False

        checkpoints.append(CheckpointResult(checkpoint_id, 1.0 if cp_pass else 0.0, 1.0,
                                            f"sheet{sheet_idx}_matched={cp_pass}"))

    return checkpoints
