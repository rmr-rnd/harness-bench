from __future__ import annotations

from abc import ABC
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from framework.config import BenchmarkConfig
    from framework.models import Sample
    from framework.scorers.base import Scorer
    from framework.runners.base import Runner

_BENCHMARK_REGISTRY: dict[str, type["Benchmark"]] = {}


def register_benchmark(name: str):
    def decorator(cls):
        _BENCHMARK_REGISTRY[name] = cls
        return cls
    return decorator


def resolve_benchmark(cfg_name: str) -> type["Benchmark"]:
    cls = _BENCHMARK_REGISTRY.get(cfg_name)
    if cls is None:
        raise ValueError(
            f"Unknown benchmark: {cfg_name!r}. "
            f"Available: {sorted(_BENCHMARK_REGISTRY)}"
        )
    return cls


class Benchmark(ABC):
    name: str
    display_name: str = ""        # human-readable label; fallback → name
    description:  str = ""        # used in analyzer.py prompt
    category:     str = ""        # grouping in report.py (e.g. "Tool Calling")
    default_paths: tuple[str, str] = ("", "")  # (tasks_dir, answers_dir)

    def __init__(self, cfg: "BenchmarkConfig") -> None:
        self.cfg = cfg
        # Apply default_paths from class if cfg paths were not set explicitly.
        # The config validator leaves tasks_dir/answers_dir empty when the user
        # did not provide them; we fill them here so every benchmark subclass
        # only needs to declare default_paths once.
        if not cfg.tasks_dir and not cfg.answers_dir:
            if self.default_paths != ("", ""):
                cfg.tasks_dir, cfg.answers_dir = self.default_paths
            else:
                cfg.tasks_dir  = f"benchmarks_data/{cfg.name}/questions"
                cfg.answers_dir = f"benchmarks_data/{cfg.name}/answers"

    def load_samples(self) -> list["Sample"]:
        raise NotImplementedError(f"{self.__class__.__name__} must implement load_samples()")

    def make_scorer(self) -> "Scorer":
        raise NotImplementedError(f"{self.__class__.__name__} must implement make_scorer()")

    def make_runner(self, model_cfg=None) -> "Runner":
        """Return the runner that orchestrates turns for this benchmark.

        Override in benchmark subclasses that need custom multi-turn logic.
        Default returns SingleTurnRunner (one send_turn call per task).
        """
        from framework.runners.single_turn import SingleTurnRunner
        return SingleTurnRunner()

    def format_prompt(self, sample: "Sample") -> list[dict]:
        """Default: system + user messages. Override only for non-standard formats."""
        msgs = []
        if sample.system_prompt:
            msgs.append({"role": "system", "content": sample.system_prompt})
        for m in sample.messages:
            msgs.append({"role": m.role, "content": m.content})
        return msgs
