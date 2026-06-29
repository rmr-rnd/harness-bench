"""Auto-discovery of benchmark modules.

Calling discover_all() imports every module under framework.benchmarks/ so
that @register_benchmark decorators fire and all benchmarks appear in the
registry.  The call is idempotent — subsequent calls are no-ops.
"""
from __future__ import annotations

import importlib
import pkgutil
import sys

import framework.benchmarks as _pkg

_discovered = False


def discover_all() -> None:
    global _discovered
    if _discovered:
        return
    _discovered = True

    for _finder, name, _ispkg in pkgutil.iter_modules(_pkg.__path__, _pkg.__name__ + "."):
        short = name.split(".")[-1]
        # Skip internal helpers and the base module (no @register_benchmark there)
        if short.startswith("_") or short == "base":
            continue
        if name in sys.modules:
            continue
        try:
            importlib.import_module(name)
        except ImportError:
            # Benchmarks with optional dependencies (e.g. a future benchmark
            # requiring an extra package) are silently skipped if the dep is
            # missing.  pac1 itself is safe to import without bitgn — this
            # guard exists for user-added benchmarks with top-level optional
            # imports.
            pass
