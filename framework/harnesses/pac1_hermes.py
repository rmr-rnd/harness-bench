"""PAC1 harness using Hermes agent (--yolo --toolsets terminal, mount workspace)."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Callable, TYPE_CHECKING

from pydantic import BaseModel

from framework.harnesses.pac1_base import AgentResult, Pac1Harness

if TYPE_CHECKING:
    from framework.config import ModelConfig
    from framework.models import Task


class Pac1HermesConfig(BaseModel):
    hermes_image: str = "hermes-agent:latest"
    agent_max_seconds: int = 300
    # PAC1 / BitGN connection params
    bitgn_benchmark_host: str = ""
    bitgn_api_key: str = ""
    bitgn_run_name: str = "harness-bench"
    skip_git: bool = False


class Pac1HermesHarness(Pac1Harness):
    """Runs PAC1 tasks with Hermes in Docker (--yolo --toolsets terminal)."""

    type = "pac1_hermes"
    config_model = Pac1HermesConfig

    def __init__(
        self,
        model_cfg: "ModelConfig",
        *,
        hermes_image: str = "hermes-agent:latest",
        agent_max_seconds: int = 300,
        **kwargs,
    ) -> None:
        super().__init__(model_cfg, **kwargs)
        self.hermes_image = hermes_image
        self.agent_max_seconds = agent_max_seconds

    async def _run_agent(
        self,
        workspace_dir: Path,
        task: "Task",
        step_cb: Callable,
    ) -> AgentResult:
        from framework.benchmarks.pac1.hermes_runner import run_hermes

        # System prompt is stored in task.system_prompt; instruction is the last user message
        system_prompt_template = task.system_prompt or _load_default_system_prompt()
        instruction = task.messages[-1].content if task.messages else task.metadata.get("instruction", "")

        raw = await asyncio.to_thread(
            run_hermes,
            task_id=task.id,
            run_id=task.metadata.get("bitgn_run_id", "unknown"),
            instruction=instruction,
            workspace_dir=workspace_dir,
            system_prompt_template=system_prompt_template,
            hermes_image=self.hermes_image,
            openai_api_key=self.model_cfg.api_key,
            openai_base_url=self.model_cfg.base_url,
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


def _load_default_system_prompt() -> str:
    from pathlib import Path as _Path
    p = _Path(__file__).parent.parent / "pac1" / "system_prompt.md"
    return p.read_text(encoding="utf-8") if p.exists() else "{instruction}"
