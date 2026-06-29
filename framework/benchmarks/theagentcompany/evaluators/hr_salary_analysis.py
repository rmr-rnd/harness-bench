"""Evaluator for hr_salary_analysis — ported from inspect_evals.

Adapted from https://github.com/TheAgentCompany/TheAgentCompany/blob/main/workspaces/tasks/hr-salary-analysis/evaluator.py
"""
from __future__ import annotations

import base64
import logging
from io import BytesIO, StringIO

from framework.benchmarks.theagentcompany.evaluators.base import CheckpointResult, llm_judge


logger = logging.getLogger(__name__)

REPORT_FILE_NAME = "Salary_Analysis_Report.odt"
REPORT_DIR_WEBDAV = "Documents/Human%20Resources%20Team"
SANDBOX_SALARY_CSV = "/utils/salary.txt"
OWNCLOUD_USER = "theagentcompany"
OWNCLOUD_PASS = "theagentcompany"
WEBDAV_URL = f"http://owncloud/remote.php/webdav/{REPORT_DIR_WEBDAV}/{REPORT_FILE_NAME}"

REQUEST_TIMEOUT_SEC = 20

# This task is scored with the reference's IMPROVED mode (deliberate, by user
# request): the strict ORIGINAL keyword check looks for the bare integer (e.g.
# "78000") and misses formatted numbers ("$78,000.00"). IMPROVED grades the ODT
# text with an LLM ("reported as `X`, or something similar") over four stats.
CRITERIA = [
    "average salary by department reported (or something similar)",
    "highest salary reported (or something similar)",
    "lowest salary reported (or something similar)",
    "median salary across roles reported (or something similar)",
]


async def fetch_report_binary_from_sandbox(sandbox) -> bytes | None:
    """Download the ODT via the sandbox network (owncloud hostname)."""
    url = WEBDAV_URL
    # Binary-safe transport: curl in the task image, base64 on stdout.
    cmd = f"curl -sf -u {OWNCLOUD_USER}:{OWNCLOUD_PASS} {url!s} | base64 -w0"
    try:
        result = await sandbox.exec(["bash", "-lc", cmd], timeout=REQUEST_TIMEOUT_SEC)
    except Exception as e:
        logger.warning("sandbox fetch of Salary_Analysis_Report.odt failed: %s", e)
        return None
    if result.returncode != 0:
        logger.warning(
            "sandbox curl owncloud failed (rc=%s): %s",
            result.returncode,
            getattr(result, "stderr", "") or "",
        )
        return None
    out = (result.stdout or "").strip()
    if not out:
        return None
    try:
        return base64.b64decode(out)
    except Exception as e:
        logger.warning("decode sandbox ODT payload failed: %s", e)
        return None


def calculate_salary_statistics_improved(csv_text: str) -> list[int]:
    """Compute [mean, max, min, median] from the CSV Salary column (IMPROVED).

    Mirrors the reference improved stats: use only the ``Salary`` column,
    coerce to numeric, and — if the CSV columns are shifted (the documented
    original pandas bug: N header cols, N+1 data cols) — fall back to the
    column that holds the numeric salaries.
    """
    try:
        import pandas as pd
    except ImportError:
        raise RuntimeError("pandas is not available on host")
    df = pd.read_csv(StringIO(csv_text))
    salary_col = df["Salary"] if "Salary" in df.columns else None
    if salary_col is None:
        salary = pd.Series(dtype="object")
    else:
        salary = pd.to_numeric(salary_col, errors="coerce")
    if int(salary.notna().sum()) == 0:
        best_salary = None
        best_count = 0
        for col in df.columns:
            numeric_col = pd.to_numeric(df[col], errors="coerce")
            count = int(numeric_col.notna().sum())
            if count > best_count:
                best_salary = numeric_col
                best_count = count
        tied_best = []
        if best_count > 0:
            for col in df.columns:
                if int(pd.to_numeric(df[col], errors="coerce").notna().sum()) == best_count:
                    tied_best.append(str(col))
        if best_salary is None or best_count == 0:
            raise ValueError("No numeric salary values found in CSV.")
        if len(tied_best) > 1:
            raise ValueError(
                "Ambiguous numeric salary column candidates: " + ", ".join(sorted(tied_best))
            )
        salary = best_salary
    return [
        int(salary.mean()),
        int(salary.max()),
        int(salary.min()),
        int(salary.median()),
    ]


def extract_text_from_odt(binary_content: bytes) -> str:
    try:
        from odf.opendocument import load
        from odf.text import P as OdfParagraph
    except ImportError:
        raise RuntimeError("odfpy is not available on host")
    odt_file = BytesIO(binary_content)
    document = load(odt_file)
    text_content = ""
    for paragraph in document.getElementsByType(OdfParagraph):
        if paragraph.firstChild is not None:
            text_content += str(paragraph.firstChild) + "\n"
    return text_content


async def evaluate(sandbox, judge) -> list[CheckpointResult]:
    """Reference IMPROVED scoring: grade the ODT text with an LLM against four
    stats ("reported as `X`, or something similar"), one checkpoint each."""
    def _to_str(x: object) -> str:
        if isinstance(x, (bytes, bytearray)):
            return x.decode("utf-8", errors="replace")
        return str(x)

    salary_csv = ""
    try:
        raw = await sandbox.read_file(SANDBOX_SALARY_CSV)
        salary_csv = _to_str(raw)
    except Exception as e:
        logger.warning("hr_salary_analysis: read_file %s failed: %s", SANDBOX_SALARY_CSV, e)

    binary_content = await fetch_report_binary_from_sandbox(sandbox)

    # Reference improved: missing report or CSV → empty checkpoints (INCORRECT, 0.0).
    if not binary_content or not salary_csv.strip():
        logger.warning("hr_salary_analysis: missing report content or salary CSV → 0")
        return []

    try:
        text_content = extract_text_from_odt(binary_content)
        keywords = calculate_salary_statistics_improved(salary_csv)
    except Exception as e:
        logger.warning("hr_salary_analysis: improved scoring failed: %s", e)
        return []

    logger.info("Keywords: %s", keywords)
    llm_criteria = [
        f"The average salary by department is reported as `{keywords[0]}`, or something similar.",
        f"The highest salary is reported as `{keywords[1]}`, or something similar.",
        f"The lowest salary is reported as `{keywords[2]}`, or something similar.",
        f"The median salary across roles is reported as `{keywords[3]}`, or something similar.",
    ]

    results: list[CheckpointResult] = []
    for idx, criterion in enumerate(llm_criteria, start=1):
        passed, _ = await llm_judge(judge, text_content, criterion)
        results.append(CheckpointResult(
            idx, 1.0 if passed else 0.0, 1.0,
            f"improved={'pass' if passed else 'fail'} (`{keywords[idx-1]}`)",
        ))
    return results
