"""Launch OMP for one PAC1 task and read the standard PAC1 answer file."""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from framework.benchmarks.pac1._utils import _read_answer
from framework.harnesses.omp import OmpRunConfig, run_omp_prompt

_SYSTEM_PROMPT_PATH = Path(__file__).parent / "system_prompt.md"


def _build_prompt(instruction: str) -> str:
    return _SYSTEM_PROMPT_PATH.read_text(encoding="utf-8").replace("{instruction}", instruction)


async def run_omp(
    *,
    task_id: str,
    instruction: str,
    workspace_dir: Path,
    model_cfg,
    run_cfg: OmpRunConfig,
    step_cb: Callable | None = None,
) -> dict:
    """Run OMP against workspace_dir.

    Returns dict: {message, outcome, refs, input_tokens, output_tokens}.
    """
    result = await run_omp_prompt(
        prompt=_build_prompt(instruction),
        system_prompt="",
        model_cfg=model_cfg,
        run_cfg=run_cfg,
        timeout=run_cfg.agent_max_seconds,
        stream_idle_timeout=0,
        step_cb=step_cb,
        cwd=workspace_dir,
    )
    answer = _read_answer(workspace_dir, task_id)
    answer["input_tokens"] = result.input_tokens
    answer["output_tokens"] = result.output_tokens
    if step_cb:
        step_cb("output", answer["message"])
    return answer
