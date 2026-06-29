from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

ANSWER_FILENAME = ".pac1_answer.json"


def _normalize_refs(refs: list[str]) -> list[str]:
    cleaned = []
    for r in refs:
        r = r.removeprefix("/workspace/").lstrip("/")
        if r:
            cleaned.append(r)
    return cleaned


def _read_answer(workspace_dir: Path, task_id: str) -> dict:
    answer_path = workspace_dir / ANSWER_FILENAME
    if not answer_path.exists():
        logger.warning("[%s] %s not found — agent did not submit", task_id, ANSWER_FILENAME)
        return {
            "message": "Agent did not write answer file",
            "outcome": "OUTCOME_ERR_INTERNAL",
            "refs": [],
        }
    try:
        data = json.loads(answer_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("[%s] Failed to parse answer file: %s", task_id, e)
        return {
            "message": "Agent answer file is malformed",
            "outcome": "OUTCOME_ERR_INTERNAL",
            "refs": [],
        }

    message = str(data.get("message", ""))
    outcome = str(data.get("outcome", "OUTCOME_ERR_INTERNAL"))
    refs = _normalize_refs(data.get("refs", []))

    _VALID_OUTCOMES = {
        "OUTCOME_OK",
        "OUTCOME_DENIED_SECURITY",
        "OUTCOME_NONE_CLARIFICATION",
        "OUTCOME_NONE_UNSUPPORTED",
        "OUTCOME_ERR_INTERNAL",
    }
    if outcome not in _VALID_OUTCOMES:
        logger.warning("[%s] Unknown outcome %r → OUTCOME_ERR_INTERNAL", task_id, outcome)
        outcome = "OUTCOME_ERR_INTERNAL"

    return {"message": message, "outcome": outcome, "refs": refs}


def _require_bitgn() -> None:
    """Raise a clear ImportError if the bitgn package is missing."""
    try:
        import vendor.bitgn  # noqa: F401
    except ImportError:
        raise ImportError(
            "PAC1 benchmark requires the 'bitgn' package.\n"
            "Install it with: pip install 'harness-testing[pac1]'"
        )
