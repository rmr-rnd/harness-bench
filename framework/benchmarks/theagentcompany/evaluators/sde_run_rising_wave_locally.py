"""Evaluator for sde_run_rising_wave_locally — ported from inspect_evals.

Adapted from https://github.com/TheAgentCompany/TheAgentCompany/blob/main/workspaces/tasks/sde-run-rising-wave-locally/evaluator.py
"""
from __future__ import annotations

import logging

from framework.benchmarks.theagentcompany.evaluators.base import CheckpointResult

logger = logging.getLogger(__name__)

RISINGWAVE_BIN = "/workspace/risingwave"

# Strings that must appear in `SELECT * FROM average_exam_scores` output.
REQUIRED_QUERY_TOKENS: list[str] = [
    "exam_id",
    "average_score",
    "total_scores",
    "101",
    "102",
    "87.7",
    "88.6",
    "5",
]

CRITERIA = [
    "/workspace/risingwave binary exists and runs (exits 0 or times out within 0.1s)",
    "average_exam_scores table has expected data (exam_id, average_score, total_scores with correct values)",
]

# Run inside sandbox: same semantics as host run_command — exit 0 if the
# binary exits 0 within 0.1s, or is still running when killed by timeout (124).
_CP1_BASH = (
    r"""
set +e
timeout 0.1 """
    + RISINGWAVE_BIN
    + r""" >/dev/null 2>&1
ec=$?
if [ "$ec" -eq 0 ] || [ "$ec" -eq 124 ]; then
  exit 0
else
  exit 1
fi
"""
)

# Run inside sandbox: start RisingWave, query, tear down; stdout is psql output.
_CP2_BASH = (
    r"""
set +e
"""
    + RISINGWAVE_BIN
    + r""" >/dev/null 2>&1 &
RW_PID=$!
sleep 8
OUT=$(psql -h localhost -p 4566 -d dev -U root -c "SELECT * FROM average_exam_scores")
kill "$RW_PID" 2>/dev/null || true
wait "$RW_PID" 2>/dev/null || true
printf '%s' "$OUT"
"""
)


def query_output_matches_checkpoint(output: str) -> bool:
    return all(token in output for token in REQUIRED_QUERY_TOKENS)


async def _sandbox_checkpoint1(sandbox) -> bool:
    try:
        result = await sandbox.exec(["bash", "-lc", _CP1_BASH], timeout=30)
    except Exception as e:
        logger.warning("sde_run_rising_wave_locally: sandbox cp1 exec failed: %s", e)
        return False
    return bool(result.success and result.returncode == 0)


def _stdout_to_str(stdout: object) -> str:
    if stdout is None:
        return ""
    if isinstance(stdout, (bytes, bytearray)):
        return bytes(stdout).decode("utf-8", errors="replace")
    return str(stdout)


async def _sandbox_checkpoint2(sandbox) -> tuple[bool, str]:
    """Return (passed, query_output_text)."""
    try:
        result = await sandbox.exec(["bash", "-lc", _CP2_BASH], timeout=120)
    except Exception as e:
        logger.warning("sde_run_rising_wave_locally: sandbox cp2 exec failed: %s", e)
        return False, ""
    if not result.success:
        return False, ""
    output = _stdout_to_str(result.stdout)
    logger.info("---------QUERY OUTPUT (sandbox)---------\n%s", output)
    return query_output_matches_checkpoint(output), output


async def evaluate(sandbox, judge) -> list[CheckpointResult]:
    cp1_pass = await _sandbox_checkpoint1(sandbox)
    cp2_pass, cp2_output = await _sandbox_checkpoint2(sandbox)

    # Reference grades this task deterministically (token matching, no LLM).
    # Use distinct checkpoint ids for easier debugging/analysis.
    return [
        CheckpointResult(1, 1.0 if cp1_pass else 0.0, 1.0, f"risingwave_runs={cp1_pass}"),
        CheckpointResult(2, 1.0 if cp2_pass else 0.0, 1.0, f"query_matches={cp2_pass}"),
    ]
