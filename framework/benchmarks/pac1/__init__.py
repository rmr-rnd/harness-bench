"""PAC1 (BitGN) benchmark support for harness_bench.

Requires the optional 'bitgn' package:
    pip install 'harness-testing[pac1]'
"""
from framework.benchmarks.pac1._utils import _require_bitgn
from framework.benchmarks.pac1.benchmark import Pac1Benchmark

__all__ = ["Pac1Benchmark", "_require_bitgn"]
