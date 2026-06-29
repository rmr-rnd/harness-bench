"""Tests for Harness base class contract."""
from __future__ import annotations

import pytest
from pydantic import BaseModel

from framework.harnesses.base import Harness
from framework.models import Message, Sample
from framework.runners.base import TurnResponse


class _MinimalHarness(Harness):
    """Concrete harness for testing the base class."""
    type = "minimal"

    async def send_turn(self, messages, tools, system_prompt, ctx, timeout=120, **kwargs):
        return TurnResponse(text="ok", tool_calls=[], finish_reason="stop")


class _ConfiguredHarness(Harness):
    """Harness with a Pydantic config_model."""
    type = "configured"

    class _Config(BaseModel):
        token: str = ""
        retries: int = 3

    config_model = _Config

    def __init__(self, model_cfg, token: str = "", retries: int = 3):
        super().__init__(model_cfg)
        self.token = token
        self.retries = retries

    async def send_turn(self, messages, tools, system_prompt, ctx, timeout=120, **kwargs):
        return TurnResponse(text="ok", tool_calls=[], finish_reason="stop")


# ---------------------------------------------------------------------------

def test_supports_sandbox_default(model_cfg):
    h = _MinimalHarness(model_cfg)
    assert h.supports_sandbox is False


def test_supports_runner_protocol_default(model_cfg):
    assert _MinimalHarness.SUPPORTS_RUNNER_PROTOCOL is True


@pytest.mark.asyncio
async def test_run_task_raises(model_cfg):
    h = _MinimalHarness(model_cfg)
    with pytest.raises(NotImplementedError):
        await h.run_task(None, None)


def test_from_config_no_model_passes_raw_as_kwargs(model_cfg):
    # Without config_model, raw dict is passed as **kwargs to __init__.
    # Harness subclasses that accept extra kwargs will get them.
    class KwargsHarness(Harness):
        type = "kwargs"

        def __init__(self, model_cfg, token: str = "", **kwargs):
            super().__init__(model_cfg)
            self.token = token

        async def send_turn(self, *a, **kw):
            return TurnResponse(text="ok", tool_calls=[], finish_reason="stop")

    h = KwargsHarness.from_config(model_cfg, {"token": "abc", "extra": "ignored"})
    assert h.token == "abc"


def test_from_config_with_model_validates(model_cfg):
    h = _ConfiguredHarness.from_config(model_cfg, {"token": "abc", "retries": 5, "extra": "ignored"})
    assert h.token == "abc"
    assert h.retries == 5


def test_from_config_with_model_uses_defaults(model_cfg):
    h = _ConfiguredHarness.from_config(model_cfg, {})
    assert h.token == ""
    assert h.retries == 3
