"""Thin wrapper around engine grading for use by the orchestrator."""
from __future__ import annotations

from framework.benchmarks.swe_bench.engine.grading import get_eval_report
from framework.benchmarks.swe_bench.engine.test_spec.test_spec import make_test_spec
from framework.benchmarks.swe_bench.engine.constants import (
    FAIL_TO_PASS,
    PASS_TO_PASS,
    ResolvedStatus,
)


def grade_swe_instance(
    instance: dict,
    model_patch: str,
    eval_log_content: str,
    namespace: str = "swebench",
) -> dict:
    """
    Grade a single SWE-bench instance.

    Args:
        instance: full SWE-bench instance dict
        model_patch: unified diff produced by the agent (git diff HEAD)
        eval_log_content: stdout+stderr output of eval.sh as a string
        namespace: Docker image namespace (default "swebench")

    Returns dict with:
        resolution_status: "RESOLVED_FULL" | "RESOLVED_PARTIAL" | "RESOLVED_NO"
        patch_applied: bool
        patch_exists: bool
        f2p_success: list[str]
        f2p_failure: list[str]
        p2p_success: list[str]
        p2p_failure: list[str]
    """
    test_spec = make_test_spec(instance, namespace=namespace)
    prediction = {
        "instance_id": instance["instance_id"],
        "model_patch": model_patch or None,
    }

    report_map = get_eval_report(
        test_spec=test_spec,
        prediction=prediction,
        eval_log_content=eval_log_content,
        include_tests_status=True,
    )

    instance_id = instance["instance_id"]
    instance_report = report_map.get(instance_id, {})

    patch_exists = instance_report.get("patch_exists", False)
    patch_applied = instance_report.get("patch_successfully_applied", False)
    resolved = instance_report.get("resolved", False)
    tests_status = instance_report.get("tests_status", {})

    f2p = tests_status.get(FAIL_TO_PASS, {})
    p2p = tests_status.get(PASS_TO_PASS, {})
    f2p_success = f2p.get("success", [])
    f2p_failure = f2p.get("failure", [])
    p2p_success = p2p.get("success", [])
    p2p_failure = p2p.get("failure", [])

    # Determine resolution status
    f2p_total = len(f2p_success) + len(f2p_failure)
    p2p_total = len(p2p_success) + len(p2p_failure)

    if resolved:
        resolution_status = ResolvedStatus.FULL.value
    elif patch_applied and f2p_total > 0 and len(f2p_success) > 0 and len(p2p_failure) == 0:
        resolution_status = ResolvedStatus.PARTIAL.value
    else:
        resolution_status = ResolvedStatus.NO.value

    return {
        "resolution_status": resolution_status,
        "patch_exists": patch_exists,
        "patch_applied": patch_applied,
        "f2p_success": f2p_success,
        "f2p_failure": f2p_failure,
        "p2p_success": p2p_success,
        "p2p_failure": p2p_failure,
    }
