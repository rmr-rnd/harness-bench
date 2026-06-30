"""PAC1 harness using Oh My Pi (OMP) in the task workspace."""
from __future__ import annotations

from pathlib import Path
from typing import Callable, TYPE_CHECKING

from pydantic import BaseModel

from framework.harnesses.omp import OMP_DOCKER_IMAGE, OmpRunConfig
from framework.harnesses.pac1_base import AgentResult, Pac1Harness

if TYPE_CHECKING:
    from framework.config import ModelConfig
    from framework.models import Task


class Pac1OmpConfig(BaseModel):
    omp_image: str = OMP_DOCKER_IMAGE
    omp_approval_mode: str = "yolo"
    omp_agent_max_seconds: int = 300
    bitgn_benchmark_host: str = ""
    bitgn_api_key: str = ""
    bitgn_run_name: str = "harness-bench"
    skip_git: bool = False


class Pac1OmpHarness(Pac1Harness):
    """Runs PAC1 tasks with OMP against the mirrored /workspace tree."""

    type = "pac1_omp"
    config_model = Pac1OmpConfig

    def __init__(
        self,
        model_cfg: "ModelConfig",
        *,
        omp_image: str = OMP_DOCKER_IMAGE,
        omp_approval_mode: str = "yolo",
        omp_agent_max_seconds: int = 300,
        **kwargs,
    ) -> None:
        super().__init__(model_cfg, **kwargs)
        self.omp = OmpRunConfig(
            image=omp_image,
            approval_mode=omp_approval_mode,
            agent_max_seconds=omp_agent_max_seconds,
        )

    async def _run_agent(
        self,
        workspace_dir: Path,
        task: "Task",
        step_cb: Callable,
    ) -> AgentResult:
        from framework.benchmarks.pac1.omp_runner import run_omp

        instruction = task.messages[-1].content if task.messages else task.metadata.get("instruction", "")
        raw = await run_omp(
            task_id=task.id,
            instruction=instruction,
            workspace_dir=workspace_dir,
            model_cfg=self.model_cfg,
            run_cfg=self.omp,
            step_cb=step_cb,
        )
        return AgentResult(
            message=raw["message"],
            outcome=raw["outcome"],
            refs=raw["refs"],
            input_tokens=raw.get("input_tokens", 0),
            output_tokens=raw.get("output_tokens", 0),
        )
