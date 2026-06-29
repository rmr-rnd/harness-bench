"""Tests for BenchmarkConfig path filling and Benchmark.__init__ default_paths."""
from __future__ import annotations

from framework.config import BenchmarkConfig


def test_known_benchmark_paths_applied_by_init():
    # Paths come from Benchmark.default_paths, applied in __init__, not in config.
    # Import triggers @register_benchmark so the class is available.
    from framework.benchmarks.simpleqa import SimpleQABenchmark
    cfg = BenchmarkConfig(name="simpleqa")
    # Before __init__: cfg paths are empty (validator no longer fills them)
    assert cfg.tasks_dir == ""
    assert cfg.answers_dir == ""
    # After instantiation: __init__ applies default_paths
    bench = SimpleQABenchmark(cfg)
    assert bench.cfg.tasks_dir  == "benchmarks_data/simpleqa/questions"
    assert bench.cfg.answers_dir == "benchmarks_data/simpleqa/answers"


def test_unknown_benchmark_gets_conventional_paths():
    from framework.benchmarks.base import Benchmark, register_benchmark

    @register_benchmark("_test_unknown_bench")
    class _UnknownBench(Benchmark):
        name = "my_custom_bench"
        def load_samples(self): return []
        def make_scorer(self): return None

    cfg = BenchmarkConfig(name="my_custom_bench")
    bench = _UnknownBench(cfg)
    assert bench.cfg.tasks_dir  == "benchmarks_data/my_custom_bench/questions"
    assert bench.cfg.answers_dir == "benchmarks_data/my_custom_bench/answers"


def test_tasks_dir_given_answers_inferred():
    cfg = BenchmarkConfig(name="simpleqa", tasks_dir="custom/tasks")
    # answers_dir is inferred by validator as sibling "answers" dir
    assert cfg.answers_dir == "custom/answers"


def test_both_explicit_not_overwritten():
    cfg = BenchmarkConfig(
        name="simpleqa",
        tasks_dir="my/tasks",
        answers_dir="my/answers",
    )
    assert cfg.tasks_dir  == "my/tasks"
    assert cfg.answers_dir == "my/answers"


def test_limit_none_by_default():
    cfg = BenchmarkConfig(name="simpleqa")
    assert cfg.limit is None


def test_limit_set():
    cfg = BenchmarkConfig(name="simpleqa", limit=10)
    assert cfg.limit == 10
