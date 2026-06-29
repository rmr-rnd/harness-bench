"""PAC1 harness using OpenCode agent (/workspace volume mount, SSE events)."""
from __future__ import annotations

from pathlib import Path
from typing import Callable, TYPE_CHECKING

from pydantic import BaseModel

from framework.harnesses.pac1_base import AgentResult, Pac1Harness

if TYPE_CHECKING:
    from framework.config import ModelConfig
    from framework.models import Task


class Pac1OpenCodeConfig(BaseModel):
    opencode_image: str = "ghcr.io/anomalyco/opencode:latest"
    opencode_token: str = ""
    opencode_approvals_off: bool = True
    agent_max_seconds: int = 300
    bitgn_benchmark_host: str = ""
    bitgn_api_key: str = ""
    bitgn_run_name: str = "harness-bench"
    skip_git: bool = False


class Pac1OpenCodeHarness(Pac1Harness):
    """Runs PAC1 tasks with OpenCode in Docker (opencode serve, SSE events)."""

    type = "pac1_opencode"
    config_model = Pac1OpenCodeConfig

    def __init__(
        self,
        model_cfg: "ModelConfig",
        *,
        opencode_image: str = "ghcr.io/anomalyco/opencode:latest",
        opencode_token: str = "",
        opencode_approvals_off: bool = True,
        agent_max_seconds: int = 300,
        **kwargs,
    ) -> None:
        super().__init__(model_cfg, **kwargs)
        self.opencode_image = opencode_image
        self.opencode_token = opencode_token
        self.opencode_approvals_off = opencode_approvals_off
        self.agent_max_seconds = agent_max_seconds

    async def _run_agent(
        self,
        workspace_dir: Path,
        task: "Task",
        step_cb: Callable,
    ) -> AgentResult:
        from framework.benchmarks.pac1.opencode_runner import run_opencode

        instruction = task.messages[-1].content if task.messages else task.metadata.get("instruction", "")

        raw = await run_opencode(
            task_id=task.id,
            instruction=instruction,
            workspace_dir=workspace_dir,
            opencode_image=self.opencode_image,
            opencode_token=self.opencode_token,
            opencode_approvals_off=self.opencode_approvals_off,
            openai_base_url=self.model_cfg.base_url,
            openai_api_key=self.model_cfg.api_key,
            model_id=self.model_cfg.model_name,
            agent_max_seconds=self.agent_max_seconds,
            step_cb=step_cb,
        )

        return AgentResult(
            message=raw["message"],
            outcome=raw["outcome"],
            refs=raw["refs"],
            input_tokens=raw.get("input_tokens", 0),
            output_tokens=raw.get("output_tokens", 0),
        )
