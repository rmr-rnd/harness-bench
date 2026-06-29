from __future__ import annotations

import importlib
import inspect

from framework.harnesses.base import Harness


def load_harness_class(harness_type: str) -> type[Harness]:
    """Dynamically load a Harness subclass by type name.

    Looks for framework/harnesses/<harness_type>.py (dashes replaced with underscores).
    The file must contain exactly one Harness subclass with type == harness_type.

    Usage:
        cls = load_harness_class("hermes")
        harness = cls.from_config(model_cfg, extra_dict)
    """
    module_name = harness_type.replace("-", "_")
    try:
        mod = importlib.import_module(f"framework.harnesses.{module_name}")
    except ImportError:
        raise ValueError(
            f"Harness '{harness_type}' not found. "
            f"Create framework/harnesses/{module_name}.py with a Harness subclass "
            f"that has type = '{harness_type}'."
        ) from None

    for _, obj in inspect.getmembers(mod, inspect.isclass):
        if (
            obj is not Harness
            and issubclass(obj, Harness)
            and getattr(obj, "type", None) == harness_type
        ):
            return obj

    raise ValueError(
        f"No Harness subclass with type='{harness_type}' found in "
        f"framework/harnesses/{module_name}.py"
    )
