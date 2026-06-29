from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from framework.config import ModelConfig
    from framework.context import ExecutionContext
    from framework.models import AgentTrace, Task
    from framework.runners.base import TurnResponse
    from pydantic import BaseModel


# Sentinel prefix carried by str(AgentDeadError). The orchestrator classifies a
# task as grade AGENT_DEAD when trace.error starts with this — which works both
# when the exception propagates (SingleTurnRunner) AND when a runner swallows it
# into AgentTrace(error=str(exc)) (BFCL runners). Keep these in sync.
AGENT_DEAD_SENTINEL = "AgentDead"


class AgentDeadError(RuntimeError):
    """Raised when the agent emits a terminal death signal (response.failed,
    info.error, WS lifecycle phase=error) or goes silent past the idle watchdog.

    str(exc) ALWAYS begins with AGENT_DEAD_SENTINEL ("AgentDead"); the orchestrator
    relies on this prefix to assign the AGENT_DEAD grade and skip the scorer.
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        msg = f"{AGENT_DEAD_SENTINEL}[{reason}]"
        if detail:
            msg = f"{msg}: {detail}"
        super().__init__(msg)


class Harness(ABC):
    """Base class for all agent harnesses.

    To add a new harness:
    1. Create framework/harnesses/<type_name>.py
    2. Subclass Harness, set type = "<type_name>"
    3. Optionally declare config_model (Pydantic) for parameter validation
    4. Implement send_turn(messages, tools, system_prompt, ctx, timeout) -> TurnResponse

    Legacy harnesses (PAC1) that pre-date the Runner protocol may set
    SUPPORTS_RUNNER_PROTOCOL = False and implement run_task() instead.
    """

    type: str
    config_model: type["BaseModel"] | None = None
    supports_sandbox: bool = False  # set True if harness can reach Docker via host.docker.internal
    needs_docker: bool = True       # set False only for a pure-API harness (no containers)
    SUPPORTS_RUNNER_PROTOCOL: ClassVar[bool] = True

    def __init__(self, model_cfg: "ModelConfig") -> None:
        self.model_cfg = model_cfg

    @classmethod
    def from_config(cls, model_cfg: "ModelConfig", raw: dict) -> "Harness":
        """Instantiate from raw dict (HarnessConfig.model_extra + tavily_api_key).

        Validates via config_model if declared, then passes as kwargs.
        Override if construction logic is more complex.
        """
        if cls.config_model is not None:
            params = cls.config_model(**raw).model_dump()
        else:
            params = {k: v for k, v in raw.items()}
        return cls(model_cfg, **params)

    @abstractmethod
    async def send_turn(
        self,
        messages: list[dict],
        tools: list[dict],
        system_prompt: str,
        ctx: "ExecutionContext",
        timeout: int = 120,
        **kwargs,
    ) -> "TurnResponse":
        """Send one turn to the agent and return TurnResponse.

        Called by Runner on each turn. Harness is responsible for starting/reusing
        its agent process (e.g. Docker container) and registering cleanup via
        ctx.cleanup_fns.
        """

    async def run_task(
        self,
        task: "Task",
        ctx: "ExecutionContext",
    ) -> "AgentTrace":
        """Legacy interface for harnesses that don't use the Runner protocol.

        Override in PAC1-style harnesses. Modern harnesses implement send_turn()
        instead and are driven by Runner.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not implement run_task(). "
            "Implement send_turn() and use the Runner protocol."
        )

    def finalize(self) -> None:
        """Called by orchestrator after all tasks complete (via asyncio.to_thread).

        Override for post-run cleanup or submission (e.g. BitGN submit_run).
        """

    async def await_run_grades(self, task_ids: list[str]) -> dict[str, float] | None:
        """Called by orchestrator after all tasks of a benchmark complete.

        Return a mapping {task_id: score} when the harness exposes grading
        only at run level (e.g. BitGN PAC1, where scores are computed after
        submit_run + RUN_STATE_EVALUATED). Returning None means per-task
        scores from run_task() are authoritative.
        """
        return None
