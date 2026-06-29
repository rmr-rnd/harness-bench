from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, model_validator


def _expand_env(value: str) -> str:
    """Expand ${VAR}, $VAR, and ${VAR:-default} patterns from environment."""
    def _replace(m: re.Match) -> str:
        full = m.group(0)
        var = m.group(1)
        default = m.group(2)  # None if no :- present
        result = os.environ.get(var)
        if result is not None:
            return result
        if default is not None:
            return default
        return full
    return re.sub(r"\$\{(\w+)(?::-(.*?))?\}|\$(\w+)", lambda m: (
        os.environ.get(m.group(1) or m.group(3),
                       m.group(2) if m.group(2) is not None else m.group(0))
        if (m.group(1) or m.group(3)) in os.environ
        else (m.group(2) if m.group(2) is not None else m.group(0))
    ), value)


class ModelConfig(BaseModel):
    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    model_name: str = "gpt-4o"
    temperature: float = 0.0
    max_tokens: int = 4096
    reasoning_effort: str | None = None  # none / low / medium / high / xhigh

    @model_validator(mode="before")
    @classmethod
    def expand_env_vars(cls, data: Any) -> Any:
        if isinstance(data, dict):
            for k, v in data.items():
                if isinstance(v, str):
                    data[k] = _expand_env(v)
        return data


class SearchConfig(BaseModel):
    tavily_api_key: str = ""

    @model_validator(mode="before")
    @classmethod
    def expand_env_vars(cls, data: Any) -> Any:
        if isinstance(data, dict):
            for k, v in data.items():
                if isinstance(v, str):
                    data[k] = _expand_env(v)
        return data


class HarnessConfig(BaseModel):
    model_config = ConfigDict(extra="allow")  # unknown fields → model_extra (backward-compat)

    type: str = "hermes"
    # Per-benchmark harness overrides: benchmark_name -> harness_type.
    # Params come from the same harness section (model_extra).
    # Example: benchmark_harness: {pac1: pac1_hermes}
    benchmark_harness: dict[str, str] = {}

    @model_validator(mode="before")
    @classmethod
    def expand_env_vars(cls, data: Any) -> Any:
        if isinstance(data, dict):
            for k, v in data.items():
                if isinstance(v, str):
                    data[k] = _expand_env(v)
        return data


class BenchmarkConfig(BaseModel):
    name: str
    tasks_dir: str = ""  # optional for API-backed benchmarks (e.g. pac1)
    answers_dir: str = ""
    limit: int | None = None
    web_search: bool = False
    categories: list[str] = []
    task_types: list[str] = []

    @model_validator(mode="after")
    def set_default_paths(self) -> "BenchmarkConfig":
        # Only infer answers_dir when the user provided tasks_dir but not answers_dir.
        # Full path defaults (including benchmark-specific non-standard paths) are
        # applied later in Benchmark.__init__, once the class is known.
        if not self.answers_dir and self.tasks_dir:
            self.answers_dir = str(Path(self.tasks_dir).parent / "answers")
        return self


class ParallelismConfig(BaseModel):
    workers: int = 1
    timeout_per_task: int = 180
    eval_timeout: int = 120  # seconds for evaluate_sandbox() in sandbox tasks
    # Max seconds of stream inactivity (no SSE event/output) before the harness
    # declares the agent dead and aborts the turn (grade AGENT_DEAD), instead of
    # waiting out timeout_per_task. Must exceed the agent's keepalive interval
    # (Hermes emits a keepalive every 30s). 0 disables the idle watchdog.
    stream_idle_timeout: int = 60


class DockerConfig(BaseModel):
    network: str = "bridge"
    cleanup_after: bool = True


class DatabaseConfig(BaseModel):
    host: str = "localhost"
    port: int = 5432
    name: str = "harness_results"
    user: str = "harness"
    password: str = ""
    url: str = ""

    @model_validator(mode="after")
    def build_url(self) -> "DatabaseConfig":
        if not self.url:
            self.url = f"postgresql://{self.user}:{self.password}@{self.host}:{self.port}/{self.name}"
        return self

    @model_validator(mode="before")
    @classmethod
    def expand_env_vars(cls, data: Any) -> Any:
        if isinstance(data, dict):
            for k, v in data.items():
                if isinstance(v, str):
                    data[k] = _expand_env(v)
        return data


def database_from_env() -> "DatabaseConfig | None":
    """Build a DatabaseConfig from deployment env vars (12-factor).

    Returns None unless DB_HOST is set — DB_HOST is the trigger that says
    "the deployment provides the database". Used by the Web UI service so the
    DB is a property of the deployment, not of any run-config preset.
    """
    if not os.environ.get("DB_HOST"):
        return None
    return DatabaseConfig(
        host=os.environ["DB_HOST"],
        port=int(os.environ.get("DB_PORT", "5432")),
        name=os.environ.get("DB_NAME", "harness_results"),
        user=os.environ.get("DB_USER", "harness"),
        password=os.environ.get("DB_PASSWORD", ""),
    )


def resolve_database(cfg_db: "DatabaseConfig | None") -> "DatabaseConfig | None":
    """Effective DB connection: env-override → config-file → none."""
    return database_from_env() or cfg_db


class SWEBenchConfig(BaseModel):
    namespace: str = "swebench"
    image_mode: str = "pull"  # pull | build (build not implemented for MVP)


class AnalysisModelConfig(BaseModel):
    """Model config for AI analysis in reports. Independent from benchmark model."""
    base_url: str = "https://api.anthropic.com/v1"
    api_key: str = ""
    model_name: str = "claude-haiku-4-5-20251001"
    temperature: float = 0.3
    # Sample collection
    max_bad_samples: int = 3
    max_good_samples: int = 2
    # Step truncation limits (chars)
    limit_task: int = 600
    limit_thinking: int = 350
    limit_tool_call: int = 200
    limit_tool_result: int = 100
    limit_output: int = 400

    @model_validator(mode="before")
    @classmethod
    def expand_env_vars(cls, data: Any) -> Any:
        if isinstance(data, dict):
            for k, v in data.items():
                if isinstance(v, str):
                    data[k] = _expand_env(v)
        return data


class RunConfig(BaseModel):
    run_id: str = ""
    model: ModelConfig = ModelConfig()
    judge_model: ModelConfig | None = None
    search: SearchConfig = SearchConfig()
    harness: HarnessConfig = HarnessConfig()
    benchmarks: list[BenchmarkConfig] = []
    parallelism: ParallelismConfig = ParallelismConfig()
    docker: DockerConfig = DockerConfig()
    database: DatabaseConfig | None = None
    swe_bench: SWEBenchConfig | None = None
    analysis_model: AnalysisModelConfig = AnalysisModelConfig()

    @classmethod
    def from_yaml(cls, path: str | Path) -> "RunConfig":
        with open(path) as f:
            data = yaml.safe_load(f)
        return cls(**data)
