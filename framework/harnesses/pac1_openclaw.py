"""PAC1 harness using OpenClaw agent (Pi built-in tools, /workspace volume mount)."""
from __future__ import annotations

from pathlib import Path
from typing import Callable, TYPE_CHECKING

from pydantic import BaseModel

from framework.harnesses.pac1_base import AgentResult, Pac1Harness

if TYPE_CHECKING:
    from framework.config import ModelConfig
    from framework.models import Task


class Pac1OpenClawConfig(BaseModel):
    openclaw_image: str = "ghcr.io/openclaw/openclaw:latest"
    openclaw_token: str = ""
    openclaw_approvals_off: bool = True
    agent_max_seconds: int = 300
    bitgn_benchmark_host: str = ""
    bitgn_api_key: str = ""
    bitgn_run_name: str = "harness-bench"
    skip_git: bool = False


class Pac1OpenClawHarness(Pac1Harness):
    """Runs PAC1 tasks with OpenClaw in Docker.

    OpenClaw uses its built-in Pi tools (bash, read, write, ls, find, grep)
    which operate directly on the /workspace volume mount — no extra MCP server needed.
    """

    type = "pac1_openclaw"
    config_model = Pac1OpenClawConfig

    def __init__(
        self,
        model_cfg: "ModelConfig",
        *,
        openclaw_image: str = "ghcr.io/openclaw/openclaw:latest",
        openclaw_token: str = "",
        openclaw_approvals_off: bool = True,
        agent_max_seconds: int = 300,
        **kwargs,
    ) -> None:
        super().__init__(model_cfg, **kwargs)
        self.openclaw_image = openclaw_image
        self.openclaw_token = openclaw_token
        self.openclaw_approvals_off = openclaw_approvals_off
        self.agent_max_seconds = agent_max_seconds

    async def _run_agent(
        self,
        workspace_dir: Path,
        task: "Task",
        step_cb: Callable,
    ) -> AgentResult:
        from framework.benchmarks.pac1.openclaw_runner import run_openclaw

        instruction = task.messages[-1].content if task.messages else task.metadata.get("instruction", "")

        raw = await run_openclaw(
            task_id=task.id,
            instruction=instruction,
            workspace_dir=workspace_dir,
            openclaw_image=self.openclaw_image,
            openclaw_token=self.openclaw_token,
            openclaw_approvals_off=self.openclaw_approvals_off,
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
