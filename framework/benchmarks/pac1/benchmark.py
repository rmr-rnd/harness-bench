"""PAC1 (BitGN) benchmark adapter.

Tasks are loaded from the BitGN Harness API (not from local files).
Evaluation uses the score returned by harness.end_trial() — already stored
in trace._pac1_result by Pac1Harness.run_task().
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from framework.benchmarks.base import Benchmark, register_benchmark
from framework.models import Message, Sample, Score, Task
from framework.benchmarks.pac1._utils import _require_bitgn
from framework.scorers.base import Scorer

if TYPE_CHECKING:
    from framework.config import BenchmarkConfig, HarnessConfig
    from framework.evaluators.llm_judge import LLMJudge
    from framework.models import AgentTrace
    from framework.sandbox import Sandbox

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT_PATH = Path(__file__).parent / "system_prompt.md"


def _load_system_prompt() -> str:
    if _SYSTEM_PROMPT_PATH.exists():
        return _SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
    return "{instruction}"


def _outcome_to_grade(outcome: str, score: float) -> str:
    if outcome == "OUTCOME_OK":
        return "CORRECT" if score >= 1.0 else "INCORRECT"
    if outcome in (
        "OUTCOME_DENIED_SECURITY",
        "OUTCOME_NONE_CLARIFICATION",
        "OUTCOME_NONE_UNSUPPORTED",
    ):
        return "CORRECT" if score >= 1.0 else "NOT_ATTEMPTED"
    return "ERROR"  # OUTCOME_ERR_INTERNAL; TIMEOUT is set by orchestrator


@register_benchmark('pac1')
class Pac1Benchmark(Benchmark):
    """Loads tasks from BitGN Harness API; delegates evaluation to harness score."""

    name = "pac1"
    display_name  = "PAC1"
    description   = (
        "PAC1 — agentic benchmark от BitGN. Задачи выполняются через внешний API. "
        "ERROR означает технический сбой при выполнении, NOT_ATTEMPTED — агент не попытался выполнить задачу."
    )
    category      = "Личный ассистент / Agent"
    default_paths = ("", "")   # tasks loaded from BitGN API, no local files
    mcp_tool_groups: list[str] = []

    def __init__(self, cfg: "BenchmarkConfig", harness_cfg: "HarnessConfig | None" = None) -> None:
        _require_bitgn()
        super().__init__(cfg)
        self._harness_cfg = harness_cfg
        self._bitgn_run_id: str | None = None
        self._system_prompt = _load_system_prompt()

    def load_samples(self) -> list[Sample]:
        hcfg = self._harness_cfg
        if hcfg is None:
            raise RuntimeError(
                "Pac1Benchmark requires harness_cfg with bitgn_* fields. "
                "Make sure harness.type = 'pac1_hermes' in your config."
            )

        from vendor.bitgn.harness_connect import HarnessServiceClientSync
        from vendor.bitgn.harness_pb2 import (
            GetBenchmarkRequest,
            StartRunRequest,
            StatusRequest,
        )

        client = HarnessServiceClientSync(hcfg.bitgn_benchmark_host)

        # Verify connectivity
        client.status(StatusRequest())

        benchmark_id = hcfg.bitgn_benchmark_id
        res = client.get_benchmark(GetBenchmarkRequest(benchmark_id=benchmark_id))
        logger.info("PAC1 benchmark: %s (%d tasks)", benchmark_id, len(res.tasks))

        # Start run
        run_resp = client.start_run(StartRunRequest(
            benchmark_id=benchmark_id,
            name=hcfg.bitgn_run_name,
            api_key=hcfg.bitgn_api_key,
        ))
        self._bitgn_run_id = run_resp.run_id
        logger.info("BitGN run_id: %s", self._bitgn_run_id)

        all_tasks = list(res.tasks)
        trial_ids = list(run_resp.trial_ids)
        limit = self.cfg.limit

        tasks: list[Task] = []
        for i, t in enumerate(all_tasks):
            if limit is not None and i >= limit:
                break
            trial_id = trial_ids[i] if i < len(trial_ids) else ""
            # instruction and runtime_url come from start_trial; store trial_id for harness
            tasks.append(Task(
                id=t.task_id,
                benchmark="pac1",
                messages=[Message(role="user", content="")],  # filled by start_trial() in run_task()
                ground_truth=None,
                system_prompt=self._system_prompt,
                metadata={
                    "trial_id": trial_id,
                    "runtime_url": "",           # filled by start_trial() in run_task()
                    "bitgn_run_id": self._bitgn_run_id,
                    "category": getattr(t, "task_type", "") or "",
                },
            ))

        logger.info("Loaded %d PAC1 tasks (run_id=%s)", len(tasks), self._bitgn_run_id)
        return tasks

    def format_prompt(self, task: Task) -> list[dict]:
        return [{"role": "user", "content": task.messages[0].content}]

    def make_scorer(self) -> Scorer:
        class _Pac1Scorer(Scorer):
            async def __call__(
                self,
                sample: Sample,
                trace: "AgentTrace",
                judge: "LLMJudge",
                sandbox: "Sandbox | None" = None,
            ) -> Score:
                pac1 = getattr(trace, "_pac1_result", {})
                outcome: str = str(pac1.get("outcome", "OUTCOME_ERR_INTERNAL"))
                # Per-trial scores are not available from BitGN at task close —
                # they come from SubmitRunResponse after the whole run is graded.
                # Internal errors are final immediately; everything else stays
                # EVALUATING until orchestrator._apply_run_grades rewrites it.
                if outcome == "OUTCOME_ERR_INTERNAL":
                    grade = "ERROR"
                else:
                    grade = "EVALUATING"
                return Score(
                    sample_id=sample.id,
                    score=0.0,
                    grade=grade,
                    explanation=outcome,
                    judge_model="bitgn_harness",
                    judge_input="",
                    judge_output=outcome,
                )
        return _Pac1Scorer()
