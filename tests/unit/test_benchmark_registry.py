"""Tests for benchmark registration and resolution."""
from __future__ import annotations

import pytest
from unittest.mock import patch

import framework.benchmarks.base as registry_module
from framework.benchmarks.base import Benchmark, register_benchmark, resolve_benchmark


class _FakeBenchmarkA(Benchmark):
    name = "fake_a"
    def load_samples(self): return []
    def make_scorer(self): return None


class _FakeBenchmarkB(Benchmark):
    name = "fake_b"
    def load_samples(self): return []
    def make_scorer(self): return None


# ---------------------------------------------------------------------------

def test_register_and_resolve():
    with patch.dict(registry_module._BENCHMARK_REGISTRY, {}, clear=True):
        register_benchmark("fake_a")(_FakeBenchmarkA)
        resolved = resolve_benchmark("fake_a")
        assert resolved is _FakeBenchmarkA


def test_resolve_unknown_raises_value_error():
    with patch.dict(registry_module._BENCHMARK_REGISTRY, {}, clear=True):
        with pytest.raises(ValueError, match="Unknown benchmark"):
            resolve_benchmark("does_not_exist")


def test_resolve_error_is_not_key_error():
    with patch.dict(registry_module._BENCHMARK_REGISTRY, {}, clear=True):
        with pytest.raises(ValueError):
            resolve_benchmark("does_not_exist")
        # confirm it's not a KeyError leaking through
        try:
            resolve_benchmark("does_not_exist")
        except KeyError:
            pytest.fail("resolve_benchmark raised KeyError instead of ValueError")
        except ValueError:
            pass


def test_duplicate_name_overwrites():
    with patch.dict(registry_module._BENCHMARK_REGISTRY, {}, clear=True):
        register_benchmark("dup")(_FakeBenchmarkA)
        register_benchmark("dup")(_FakeBenchmarkB)
        # second registration silently replaces the first
        assert resolve_benchmark("dup") is _FakeBenchmarkB
