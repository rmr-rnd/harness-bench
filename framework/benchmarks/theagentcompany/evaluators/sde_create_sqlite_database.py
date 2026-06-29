"""Evaluator for sde_create_sqlite_database — ported from inspect_evals.

Adapted from https://github.com/TheAgentCompany/TheAgentCompany/blob/main/workspaces/tasks/sde-create-sqlite-database/evaluator.py
"""
from __future__ import annotations

import json
import logging
from typing import Any

from framework.benchmarks.theagentcompany.evaluators.base import CheckpointResult

EXPECTED_JULY_REVENUE_SUM = 95_000
EXPECTED_TOTAL_INCOME_VARIANCE = -35_000

logger = logging.getLogger(__name__)

CRITERIA = [
    "financial_report.db exists in /workspace",
    "July-September 2024 Financials.ods downloaded to /workspace",
    "sqlite3 Python module is importable",
    "At least one table exists in financial_report.db",
    "financial_categories table has correct columns (category_id, category_name, category_type)",
    "financial_details table has correct columns (detail_id, category_id, month, actual, budget, variance, percent_of_budget)",
    "Total Income variance query returns expected row (2024-08, -35000)",
    "July revenue sum for Software Development Services + Consulting = 95000",
]


async def _sqlite_fetchall(sandbox, query: str) -> tuple[bool, list[list[Any]]]:
    """Run a SQLite query inside the sandbox (via Python) and return rows."""
    # Use a quoted heredoc to avoid shell interpreting SQL/quotes.
    cmd = (
        "cd /workspace && python - <<'PY'\n"
        "import json, sqlite3\n"
        "conn = sqlite3.connect('financial_report.db')\n"
        "cur = conn.cursor()\n"
        f"cur.execute({query!r})\n"
        "rows = cur.fetchall()\n"
        "print(json.dumps(rows))\n"
        "PY"
    )
    result = await sandbox.exec(["bash", "-lc", cmd], timeout=20)
    if not result.success:
        return False, []

    try:
        parsed = json.loads(result.stdout or "null")
    except json.JSONDecodeError:
        return False, []

    if not isinstance(parsed, list):
        return False, []
    return True, parsed  # type: ignore[return-value]


async def _sqlite_importable(sandbox) -> bool:
    """Return True if Python's sqlite3 module can be imported in the sandbox."""
    try:
        res = await sandbox.exec(
            ["bash", "-lc", 'python -c "import sqlite3"'],
            timeout=10,
        )
        return res.success
    except Exception:
        return False


async def evaluate(sandbox, judge) -> list[CheckpointResult]:
    checkpoints: list[CheckpointResult] = []

    # Checkpoint 1: output database exists in /workspace.
    try:
        exists_res = await sandbox.exec(
            ["bash", "-lc", "test -f '/workspace/financial_report.db'"],
            timeout=10,
        )
        cp1_pass = bool(exists_res.success)
    except Exception:
        cp1_pass = False
    checkpoints.append(CheckpointResult(1, 1.0 if cp1_pass else 0.0, 1.0, f"db_exists={cp1_pass}"))

    # Checkpoint 2: source ODS file exists in /workspace.
    try:
        exists_res = await sandbox.exec(
            [
                "bash",
                "-lc",
                "test -f '/workspace/July-September 2024 Financials.ods'",
            ],
            timeout=10,
        )
        cp2_pass = bool(exists_res.success)
    except Exception:
        cp2_pass = False
    checkpoints.append(CheckpointResult(2, 1.0 if cp2_pass else 0.0, 1.0, f"ods_exists={cp2_pass}"))

    # Checkpoint 3: sqlite3 is importable in Python.
    cp3_pass = await _sqlite_importable(sandbox)
    checkpoints.append(CheckpointResult(3, 1.0 if cp3_pass else 0.0, 1.0, f"sqlite3_ok={cp3_pass}"))

    # Checkpoint 4: at least one table exists in financial_report.db.
    try:
        ok, rows = await _sqlite_fetchall(
            sandbox, "SELECT name FROM sqlite_master WHERE type='table';"
        )
        table_names = [r[0] for r in rows if r]
        cp4_pass = ok and bool(table_names)
    except Exception:
        table_names = []
        cp4_pass = False
    checkpoints.append(CheckpointResult(4, 1.0 if cp4_pass else 0.0, 1.0, f"tables={table_names}"))

    # Checkpoint 5: financial_categories schema matches expected columns.
    expected_categories = {
        "category_id",
        "category_name",
        "category_type",
    }
    try:
        ok, rows = await _sqlite_fetchall(
            sandbox, "PRAGMA table_info(financial_categories);"
        )
        # PRAGMA output: cid, name, type, notnull, dflt_value, pk
        columns = {r[1] for r in rows if len(r) > 1 and r[1]}
        cp5_pass = ok and columns == expected_categories
    except Exception:
        columns = set()
        cp5_pass = False
    checkpoints.append(CheckpointResult(5, 1.0 if cp5_pass else 0.0, 1.0, f"categories_cols={cp5_pass}"))

    # Checkpoint 6: financial_details schema matches expected columns.
    expected_details = {
        "detail_id",
        "category_id",
        "month",
        "actual",
        "budget",
        "variance",
        "percent_of_budget",
    }
    try:
        ok, rows = await _sqlite_fetchall(sandbox, "PRAGMA table_info(financial_details);")
        columns = {r[1] for r in rows if len(r) > 1 and r[1]}
        cp6_pass = ok and columns == expected_details
    except Exception:
        cp6_pass = False
    checkpoints.append(CheckpointResult(6, 1.0 if cp6_pass else 0.0, 1.0, f"details_cols_ok={cp6_pass}"))

    # Checkpoint 7: Total Income variance query returns the expected row.
    try:
        sql = """
SELECT month, variance
FROM financial_details
JOIN financial_categories ON financial_details.category_id = financial_categories.category_id
WHERE category_name = 'Total Income'
ORDER BY variance ASC
LIMIT 1;
""".strip()
        ok, rows = await _sqlite_fetchall(sandbox, sql)
        cp7_pass = (
            ok
            and len(rows) == 1
            and rows[0][0] == "2024-08"
            and int(float(rows[0][1])) == EXPECTED_TOTAL_INCOME_VARIANCE
        )
    except Exception:
        cp7_pass = False
    checkpoints.append(CheckpointResult(7, 1.0 if cp7_pass else 0.0, 1.0, f"total_income_variance_ok={cp7_pass}"))

    # Checkpoint 8: July revenue sum over specified categories matches expected value.
    try:
        sql = """
SELECT SUM(actual)
FROM financial_details
JOIN financial_categories ON financial_details.category_id = financial_categories.category_id
WHERE category_name IN ('Software Development Services', 'Consulting')
AND month = '2024-07';
""".strip()
        ok, rows = await _sqlite_fetchall(sandbox, sql)
        value: Any = None
        if ok and len(rows) == 1 and rows[0] and rows[0][0] is not None:
            value = int(float(rows[0][0]))
        cp8_pass = value == EXPECTED_JULY_REVENUE_SUM
    except Exception:
        cp8_pass = False
    checkpoints.append(CheckpointResult(8, 1.0 if cp8_pass else 0.0, 1.0, f"july_revenue_ok={cp8_pass}"))

    # Reference grades this task deterministically (string judge, no LLM).
    return checkpoints
