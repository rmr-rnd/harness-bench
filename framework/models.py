from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Message:
    role: str  # system | user | assistant | tool
    content: str | list[Any]
    tool_call_id: str | None = None
    tool_calls: list[Any] | None = None


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass
class SandboxSpec:
    """Declares which Docker environment to spin up for a sample."""
    type: str  # "docker_run" | "docker_compose" | "swe_bench" | ...
    image: str = ""           # docker_run: image name
    compose_file: str = ""    # docker_compose: path to compose file
    target_service: str = ""  # docker_compose: which service to exec into
    pull: bool = True
    config: dict = field(default_factory=dict)  # sandbox-type-specific params


@dataclass
class SandboxTool:
    """A custom tool injected into the sandbox and exposed to the agent via MCP."""
    name: str
    description: str
    parameters: dict          # JSON Schema (MCP inputSchema format)
    source_dir: str           # absolute path to tool directory on host
    entrypoint: str = "main.py"
    runner: str = "python3"   # binary available inside the sandbox image
    install_cmd: str = ""     # optional: run after injection
    timeout: int = 60
    max_output_bytes: int = 32_768
    error_exit_codes: list[int] = field(default_factory=lambda: [2, 3, 126, 127])
    exclude: list[str] = field(default_factory=list)


@dataclass
class Checkpoint:
    """A single pass/fail check run inside the sandbox after the agent finishes."""
    name: str
    cmd: str
    weight: float
    target_exit_code: int = 0
    timeout: int = 30


@dataclass
class Sample:
    """Core data unit. Replaces Task. Use `ground_truth` field during transition;
    rename to `target` in Phase 6."""
    id: str
    benchmark: str
    messages: list[Message]
    ground_truth: Any          # renamed to `target` in Phase 6

    system_prompt: str = ""
    tools: list[Tool] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    epochs: int = 1

    # Infrastructure — declared in data, not in benchmark class attributes
    sandbox: SandboxSpec | None = None
    mcp_tool_groups: list[str] = field(default_factory=list)
    web_search: bool = False          # renamed to `needs_web_search` in Phase 6
    sandbox_tools: list[SandboxTool] = field(default_factory=list)
    checkpoints: list[Checkpoint] = field(default_factory=list)


# New-name aliases — swap with actual field names in Phase 6
Sample.needs_web_search = property(  # type: ignore[attr-defined]
    fget=lambda self: self.web_search,
    fset=lambda self, v: setattr(self, "web_search", v),
)

Sample.target = property(  # type: ignore[attr-defined]
    fget=lambda self: self.ground_truth,
    fset=lambda self, v: setattr(self, "ground_truth", v),
)


@dataclass
class Step:
    type: str  # thinking | tool_call | tool_result | output
    content: Any
    ts: float = field(default_factory=time.time)


@dataclass
class AgentTrace:
    task_id: str
    final_output: str
    steps: list[Step] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    duration_sec: float = 0.0
    error: str | None = None


@dataclass
class Score:
    """Evaluation result. Replaces EvalResult. DB column is still `task_id`; mapped explicitly in db.py."""
    sample_id: str
    score: float              # 0.0 – 1.0
    grade: str                # CORRECT | INCORRECT | NOT_ATTEMPTED | etc.
    explanation: str = ""
    judge_model: str = ""
    judge_input: str = ""
    judge_output: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


# Backward-compat alias — removed in Phase 6
Score.task_id = property(  # type: ignore[attr-defined]
    fget=lambda self: self.sample_id,
    fset=lambda self, v: setattr(self, "sample_id", v),
)


# Transition class aliases — removed in Phase 6
Task = Sample
EvalResult = Score
