"""Abstract base harness for PAC1 (BitGN) benchmark tasks.

Manages the PCM sandbox lifecycle:
    download → _run_agent() → sync_back → pcm.answer() → harness.end_trial()

Subclasses implement only `_run_agent()` for their specific agent.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import time
from abc import abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, TYPE_CHECKING

from framework.harnesses.base import Harness
from framework.utils.work_dir import make_work_dir
from framework.models import AgentTrace, Step
from framework.benchmarks.pac1 import _require_bitgn

if TYPE_CHECKING:
    from framework.context import ExecutionContext
    from framework.models import Task

logger = logging.getLogger(__name__)


@dataclass
class AgentResult:
    """Result returned by _run_agent()."""
    message: str
    outcome: str   # OUTCOME_OK / OUTCOME_DENIED_SECURITY / etc.
    refs: list[str]
    input_tokens: int = 0
    output_tokens: int = 0


_OUTCOME_MAP: dict[str, int] = {}  # filled lazily after bitgn import


def _get_outcome_enum(outcome_str: str):
    """Return protobuf Outcome enum value for outcome string."""
    global _OUTCOME_MAP
    if not _OUTCOME_MAP:
        from vendor.bitgn.vm.pcm_pb2 import Outcome
        _OUTCOME_MAP = {
            "OUTCOME_OK": Outcome.OUTCOME_OK,
            "OUTCOME_DENIED_SECURITY": Outcome.OUTCOME_DENIED_SECURITY,
            "OUTCOME_NONE_CLARIFICATION": Outcome.OUTCOME_NONE_CLARIFICATION,
            "OUTCOME_NONE_UNSUPPORTED": Outcome.OUTCOME_NONE_UNSUPPORTED,
            "OUTCOME_ERR_INTERNAL": Outcome.OUTCOME_ERR_INTERNAL,
        }
    from vendor.bitgn.vm.pcm_pb2 import Outcome
    return _OUTCOME_MAP.get(outcome_str, Outcome.OUTCOME_ERR_INTERNAL)


class Pac1Harness(Harness):
    """Base harness for any agent running on PAC1.

    PCM sandbox management is handled here; subclasses only implement _run_agent().
    Uses legacy run_task() interface (SUPPORTS_RUNNER_PROTOCOL = False).
    """

    SUPPORTS_RUNNER_PROTOCOL = False


    def __init__(
        self,
        model_cfg,
        *,
        bitgn_benchmark_host: str,
        bitgn_api_key: str = "",
        bitgn_run_name: str = "harness-bench",
        skip_git: bool = False,
        **kwargs,
    ) -> None:
        _require_bitgn()
        super().__init__(model_cfg)
        self.bitgn_benchmark_host = bitgn_benchmark_host
        self.bitgn_api_key = bitgn_api_key
        self.bitgn_run_name = bitgn_run_name
        self.skip_git = skip_git
        self._bitgn_run_id: str | None = None
        # task_id -> trial_id, populated as run_task() handles each task.
        # Used by await_run_grades() to map back from GetRunResponse.trials[].
        self._trial_by_task: dict[str, str] = {}

        from vendor.bitgn.harness_connect import HarnessServiceClientSync
        self._harness_client = HarnessServiceClientSync(bitgn_benchmark_host)

    async def send_turn(self, messages, tools, system_prompt, ctx, timeout=120, **kwargs):
        raise NotImplementedError("PAC1 harness uses run_task(), not the Runner protocol")

    async def run_task(self, task: "Task", ctx: "ExecutionContext") -> AgentTrace:
        step_cb = ctx.step_cb
        t0 = time.monotonic()
        steps: list[Step] = []

        # Store run_id once for finalize()
        bitgn_run_id: str = task.metadata.get("bitgn_run_id", "")
        if bitgn_run_id and not self._bitgn_run_id:
            self._bitgn_run_id = bitgn_run_id

        trial_id: str = task.metadata["trial_id"]
        self._trial_by_task[task.id] = trial_id

        # start_trial() gives us the real runtime_url and instruction for this task
        try:
            from vendor.bitgn.harness_pb2 import StartTrialRequest
            trial_resp = await asyncio.to_thread(
                self._harness_client.start_trial,
                StartTrialRequest(trial_id=trial_id),
            )
            runtime_url: str = trial_resp.harness_url
            instruction: str = trial_resp.instruction or ""
            task.metadata["runtime_url"] = runtime_url
            # Populate the instruction into the task message so _run_agent() can read it
            if task.messages:
                task.messages[0].content = instruction
            else:
                from framework.models import Message
                task.messages = [Message(role="user", content=instruction)]
        except Exception as e:
            logger.error("[%s] start_trial failed: %s", task.id, e)
            trace = AgentTrace(task_id=task.id, final_output="", error=str(e),
                               duration_sec=round(time.monotonic() - t0, 2))
            trace._pac1_result = {"score": 0.0, "outcome": "OUTCOME_ERR_INTERNAL"}
            return trace

        # Thread-safe step_cb: tail threads run in worker threads, step_cb may
        # call asyncio code (WebSocket broadcast) — must use call_soon_threadsafe.
        loop = asyncio.get_event_loop()

        def safe_step(stype: str, content) -> None:
            steps.append(Step(type=stype, content=content))
            if step_cb:
                loop.call_soon_threadsafe(lambda: step_cb(stype, content))

        workspace_dir = make_work_dir(prefix="pac1-ws-") / "workspace"

        from framework.benchmarks.pac1.pcm_mirror import PcmMirror
        mirror = PcmMirror(runtime_url, skip_git=self.skip_git)

        try:
            safe_step("status", "Downloading sandbox")
            try:
                await asyncio.to_thread(mirror.download, workspace_dir)
                n = len(mirror._snapshot)
                safe_step("status", f"Downloaded {n} files")
            except Exception as e:
                logger.error("[%s] Download failed: %s", task.id, e)
                safe_step("status", f"Download failed: {e}")
                result = AgentResult(
                    message=f"Sandbox download failed: {e}",
                    outcome="OUTCOME_ERR_INTERNAL",
                    refs=[],
                )
                score = await self._submit_and_end(
                    runtime_url, trial_id, result, task.id
                )
                return self._make_trace(task.id, result, steps, score, t0)

            result: AgentResult = await self._run_agent(
                workspace_dir, task, safe_step
            )

            safe_step("status", "Syncing changes back")
            try:
                await asyncio.to_thread(mirror.sync_back, workspace_dir)
            except Exception as e:
                logger.warning("[%s] Sync failed: %s", task.id, e)

            safe_step("status", "Submitting answer")
            score = await self._submit_and_end(runtime_url, trial_id, result, task.id)
            # Per-trial score is not available until SubmitRun at end of the run.
            safe_step("judge", f"Outcome: {result.outcome} — awaiting run evaluation")

        finally:
            mirror.close()
            shutil.rmtree(workspace_dir.parent, ignore_errors=True)

        return self._make_trace(task.id, result, steps, score, t0)

    def finalize(self) -> None:
        """No-op for PAC1 — submit_run happens inside await_run_grades()."""
        return

    @abstractmethod
    async def _run_agent(
        self,
        workspace_dir: Path,
        task: "Task",
        step_cb: Callable,
    ) -> AgentResult:
        """Run the agent against workspace_dir, return AgentResult."""

    async def _submit_and_end(
        self,
        runtime_url: str,
        trial_id: str,
        result: AgentResult,
        task_id: str,
    ) -> float:
        """Submit answer + close the trial. Returns placeholder score (0.0).

        Real per-task scores are unavailable until submit_run + RUN_STATE_EVALUATED;
        see await_run_grades() which the orchestrator calls after all tasks complete.
        """
        try:
            from vendor.bitgn.vm.pcm_connect import PcmRuntimeClientSync
            from vendor.bitgn.vm.pcm_pb2 import AnswerRequest
            pcm = PcmRuntimeClientSync(runtime_url)
            await asyncio.to_thread(
                pcm.answer,
                AnswerRequest(
                    message=result.message,
                    outcome=_get_outcome_enum(result.outcome),
                    refs=result.refs,
                ),
            )
        except Exception as e:
            logger.warning("[%s] pcm.answer() failed: %s", task_id, e)

        try:
            from vendor.bitgn.harness_pb2 import EndTrialRequest
            await asyncio.to_thread(
                self._harness_client.end_trial,
                EndTrialRequest(trial_id=trial_id),
            )
        except Exception as e:
            logger.warning("[%s] end_trial() failed: %s", task_id, e)

        return 0.0

    async def await_run_grades(self, task_ids: list[str]) -> dict[str, float] | None:
        """Submit the run and pull per-trial scores from SubmitRunResponse.

        BitGN's post-run eval model (see proto bitgn/harness.proto, message
        SubmitRunResponse and ScoredTrialResult): EndTrial is lifecycle-only,
        grading is triggered by SubmitRun, and the per-trial scores come back
        in SubmitRunResponse.trials[] when the benchmark is in OPEN policy
        (BLIND benchmarks return score_available=False — sealed until reveal).
        """
        if not self._bitgn_run_id:
            return None

        from vendor.bitgn.harness_pb2 import SubmitRunRequest
        try:
            resp = await asyncio.to_thread(
                self._harness_client.submit_run,
                SubmitRunRequest(run_id=self._bitgn_run_id, force=True),
            )
        except Exception as e:
            logger.warning("submit_run failed: %s", e)
            return None

        logger.info(
            "BitGN submit_run: state=%s score_available=%s aggregate=%s",
            resp.state,
            getattr(resp, "score_available", False),
            getattr(resp, "score", None),
        )

        if not getattr(resp, "score_available", False):
            # Sealed (BLIND benchmark) — no per-trial scores until reveal.
            return None

        task_by_trial = {tid: task for task, tid in self._trial_by_task.items()}
        scores: dict[str, float] = {}
        for t in getattr(resp, "trials", []) or []:
            task_id = task_by_trial.get(t.trial_id) or t.task_id
            if not task_id:
                continue
            if getattr(t, "score_available", False):
                scores[task_id] = max(float(t.score), 0.0)
            else:
                scores[task_id] = 0.0
        return scores

    @staticmethod
    def _make_trace(
        task_id: str,
        result: AgentResult,
        steps: list[Step],
        score: float,
        t0: float,
    ) -> AgentTrace:
        trace = AgentTrace(
            task_id=task_id,
            final_output=result.message,
            steps=steps,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            duration_sec=round(time.monotonic() - t0, 2),
        )
        trace._pac1_result = {"score": score, "outcome": result.outcome}
        return trace
