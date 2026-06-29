"""Evaluator for hr_populate_salary_increase_memo — ported from inspect_evals.

Adapted from https://github.com/TheAgentCompany/TheAgentCompany/blob/main/workspaces/tasks/hr-populate-salary-increase-memo/evaluator.py

CP1 (1pt):  HR team WebDAV directory is reachable (PROPFIND → 207)
CP2 (2pt):  Salary_Increase_MEMO.odt exists on OwnCloud (GET → 200) +
            contains template keywords (name, email, salary amount,
            assignment start and end date)
CP4 (4pt):  salary_increase_notice/ dir exists (PROPFIND Depth:0 → 207) +
            contains ≥1 individual notice files (Depth:1 count)
"""
from __future__ import annotations

import asyncio
import base64
import logging
import xml.etree.ElementTree as ET
from io import BytesIO

from framework.benchmarks.theagentcompany.evaluators.base import CheckpointResult

try:
    from odf.opendocument import load  # type: ignore[import-not-found]
    from odf.text import P as OdfParagraph  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover
    load = None  # type: ignore[assignment]
    OdfParagraph = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

CP1_MAX = 1
CP2_MAX = 2
CP4_MAX = 4

MEMO_FILE_NAME = "Salary_Increase_MEMO.odt"
MEMO_DIR_WEBDAV = "Documents/Human%20Resources%20Team"
NOTICE_DIR_WEBDAV = "Documents/Human%20Resources%20Team/salary_increase_notice"

OWNCLOUD_USER = "theagentcompany"
OWNCLOUD_PASS = "theagentcompany"
WEBDAV_BASE = "http://owncloud/remote.php/webdav"
WEBDAV_MEMO_DIR = f"{WEBDAV_BASE}/{MEMO_DIR_WEBDAV}"
WEBDAV_MEMO = f"{WEBDAV_BASE}/{MEMO_DIR_WEBDAV}/{MEMO_FILE_NAME}"
WEBDAV_NOTICE = f"{WEBDAV_BASE}/{NOTICE_DIR_WEBDAV}"

TEMPLATE_KEYWORDS = [
    "name",
    "email",
    "salary amount",
    "assignment start and end date",
]

HTTP_OK = 200
WEBDAV_MULTISTATUS = 207
EXEC_TIMEOUT = 120

CRITERIA = [
    "HR team WebDAV directory is reachable",
    "Salary_Increase_MEMO.odt exists and contains template keywords",
    "salary_increase_notice/ directory exists with individual memo files",
]


def _stdout_text(result: object) -> str:
    out = getattr(result, "stdout", "") or ""
    if isinstance(out, (bytes, bytearray)):
        return out.decode("utf-8", errors="replace")
    return str(out)


async def _exec_bash(sandbox, script: str) -> tuple[bool, str]:
    """Run a short bash snippet in the sandbox; return (success, stdout)."""
    try:
        result = await sandbox.exec(["bash", "-lc", script], timeout=EXEC_TIMEOUT)
    except Exception as e:
        logger.warning("hr_populate_salary_increase_memo: sandbox exec failed: %s", e)
        return False, ""
    ok = bool(getattr(result, "success", False))
    return ok, _stdout_text(result)


async def _sandbox_memo_http_code(sandbox) -> int:
    """GET memo URL; return HTTP status code (0 if request failed)."""
    script = (
        "curl -sS -o /dev/null -w '%{http_code}' "
        f"-u '{OWNCLOUD_USER}:{OWNCLOUD_PASS}' '{WEBDAV_MEMO}'"
    )
    ok, out = await _exec_bash(sandbox, script)
    if not ok:
        return 0
    out = out.strip()
    try:
        return int(out)
    except ValueError:
        return 0


async def _sandbox_propfind_code(sandbox, url: str, depth: str = "0") -> int:
    """PROPFIND on a WebDAV URL; return HTTP status code (0 on failure)."""
    script = (
        "curl -sS -o /dev/null -w '%{http_code}' "
        f"-u '{OWNCLOUD_USER}:{OWNCLOUD_PASS}' "
        f"-X PROPFIND -H 'Depth: {depth}' '{url}'"
    )
    ok, out = await _exec_bash(sandbox, script)
    if not ok:
        return 0
    out = out.strip()
    try:
        return int(out)
    except ValueError:
        return 0


async def _sandbox_memo_bytes_b64(sandbox) -> bytes:
    """Download memo body; empty if missing or curl error."""
    script = (
        f"curl -sf -u '{OWNCLOUD_USER}:{OWNCLOUD_PASS}' '{WEBDAV_MEMO}' | base64 -w0"
    )
    ok, out = await _exec_bash(sandbox, script)
    if not ok or not out.strip():
        return b""
    try:
        return base64.b64decode(out.strip(), validate=False)
    except Exception:
        return b""


async def _sandbox_propfind_b64(sandbox, depth: str) -> tuple[int, bytes]:
    """PROPFIND on notice collection; return (http_code, response body)."""
    script = f"""
T=$(mktemp)
code=$(curl -sS -o "$T" -w '%{{http_code}}' \\
  -u '{OWNCLOUD_USER}:{OWNCLOUD_PASS}' \\
  -X PROPFIND -H 'Depth: {depth}' \\
  '{WEBDAV_NOTICE}' || true)
b64=$(base64 -w0 < "$T" 2>/dev/null || echo -n)
rm -f "$T"
printf '%s\\n%s' "$code" "$b64"
"""
    ok, out = await _exec_bash(sandbox, script)
    if not ok or not out.strip():
        return 0, b""
    parts = out.split("\n", 1)
    try:
        code = int(parts[0].strip())
    except ValueError:
        code = 0
    body_b64 = parts[1].strip() if len(parts) > 1 else ""
    try:
        body = base64.b64decode(body_b64, validate=False) if body_b64 else b""
    except Exception:
        body = b""
    return code, body


def _count_propfind_responses(xml_text: str) -> int:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return 0
    return sum(1 for el in root.iter() if el.tag.endswith("}response"))


def _extract_text_from_odt(binary_content: bytes) -> str:
    if not binary_content or load is None or OdfParagraph is None:
        raise RuntimeError("odfpy is not available on host or empty ODT")
    document = load(BytesIO(binary_content))
    text_content = ""
    for paragraph in document.getElementsByType(OdfParagraph):
        if paragraph.firstChild is not None:
            text_content += str(paragraph.firstChild) + "\n"
    return text_content


def _check_keywords_in_content(
    content: str, keywords: list[str]
) -> tuple[list[str], float]:
    content_l = content.lower()
    found = [kw for kw in keywords if kw.lower() in content_l]
    score = len(found) / len(keywords) if keywords else 0.0
    return found, score


def _grade_checkpoint2_on_host(memo_http: int, memo_bytes: bytes) -> int:
    score = 0.0
    if memo_http != HTTP_OK:
        return 0
    score += 1.0
    try:
        text_content = _extract_text_from_odt(memo_bytes)
        _, keyword_score = _check_keywords_in_content(text_content, TEMPLATE_KEYWORDS)
        score += keyword_score
        return int(score)
    except Exception as e:
        logger.warning(
            "hr_populate_salary_increase_memo: CP2 ODT/keyword failed: %s", e
        )
        return int(score)


def _grade_checkpoint4_on_host(d0_code: int, d1_code: int, d1_xml: str) -> int:
    score = 0.0
    if d0_code != WEBDAV_MULTISTATUS:
        return 0
    score += 1.0
    if d1_code != WEBDAV_MULTISTATUS:
        return int(score)
    file_count = max(0, _count_propfind_responses(d1_xml) - 1)
    score += min(int(file_count / 10 * 3), 3)
    return int(score)


def grade_checkpoint1(hr_dir_propfind_status: int) -> bool:
    """Pass when the HR team WebDAV directory is reachable."""
    return hr_dir_propfind_status == WEBDAV_MULTISTATUS


def _webdav_all_reachable(
    *,
    hr_dir_propfind_status: int,
    memo_http: int,
    notice_propfind_depth0: int,
    notice_propfind_depth1: int,
) -> bool:
    """Mirror the reference `_raise_on_webdav_failure` gate.

    The original grader raises (→ empty checkpoints, INCORRECT) unless every
    WebDAV endpoint returns its expected status. Without this gate the HR dir
    (`Documents/Human Resources Team`) pre-exists, so CP1 would pass for free
    and a no-op agent would score ~0.33 instead of 0.
    """
    return (
        hr_dir_propfind_status == WEBDAV_MULTISTATUS
        and memo_http == HTTP_OK
        and notice_propfind_depth0 == WEBDAV_MULTISTATUS
        and notice_propfind_depth1 == WEBDAV_MULTISTATUS
    )


async def _fetch_webdav_for_grading(sandbox) -> tuple[int, bytes, int, int, str]:
    """Parallel sandbox fetches: memo status, memo body, two PROPFINDs."""
    memo_code_task = asyncio.create_task(_sandbox_memo_http_code(sandbox))
    memo_b64_task = asyncio.create_task(_sandbox_memo_bytes_b64(sandbox))
    prop0_task = asyncio.create_task(_sandbox_propfind_b64(sandbox, "0"))
    prop1_task = asyncio.create_task(_sandbox_propfind_b64(sandbox, "1"))

    memo_code = await memo_code_task
    memo_bytes = await memo_b64_task
    d0_code, _d0_body = await prop0_task
    d1_code, d1_body = await prop1_task

    d1_xml = d1_body.decode("utf-8", errors="replace")
    return memo_code, memo_bytes, d0_code, d1_code, d1_xml


async def evaluate(sandbox, judge) -> list[CheckpointResult]:
    cp1_status = await _sandbox_propfind_code(sandbox, WEBDAV_MEMO_DIR, depth="0")
    memo_code, memo_bytes, d0_code, d1_code, d1_xml = await _fetch_webdav_for_grading(sandbox)

    # Reference gate: any WebDAV request failure → empty checkpoints (INCORRECT,
    # metric 0.0). Equivalent to the original `_raise_on_webdav_failure`, which
    # raises and is caught by build_scorer to return checkpoints=[].
    if not _webdav_all_reachable(
        hr_dir_propfind_status=cp1_status,
        memo_http=memo_code,
        notice_propfind_depth0=d0_code,
        notice_propfind_depth1=d1_code,
    ):
        logger.warning(
            "hr_populate_salary_increase_memo: WebDAV gate failed "
            "(hr_dir=%s, memo=%s, notice_d0=%s, notice_d1=%s) → 0",
            cp1_status, memo_code, d0_code, d1_code,
        )
        return []

    cp1 = int(grade_checkpoint1(cp1_status))
    cp2 = _grade_checkpoint2_on_host(memo_code, memo_bytes)
    cp4 = _grade_checkpoint4_on_host(d0_code, d1_code, d1_xml)

    # Reference grades CP1/CP2/CP4 deterministically via WebDAV (no LLM).
    cp2_note = f"memo GET={memo_code}, keywords"

    return [
        CheckpointResult(1, float(cp1), float(CP1_MAX), f"PROPFIND HR dir={cp1_status}"),
        CheckpointResult(2, float(cp2), float(CP2_MAX), cp2_note),
        CheckpointResult(4, float(cp4), float(CP4_MAX), f"notice PROPFIND d0={d0_code} d1={d1_code}"),
    ]
