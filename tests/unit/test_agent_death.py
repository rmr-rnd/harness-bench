"""Agent-death detection tests.

Covers the early-abort path: each harness turns an agent terminal signal (or a
silent stream) into AgentDeadError; the orchestrator classifies any trace whose
error starts with the "AgentDead" sentinel as grade AGENT_DEAD (skipping the
scorer). See PLAN.md.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from framework.harnesses.base import AgentDeadError, AGENT_DEAD_SENTINEL
from framework.models import AgentTrace


# ── AgentDeadError sentinel + the two classification paths ───────────────────

def test_sentinel_prefix_and_fields():
    e = AgentDeadError("response_failed", "model unreachable")
    assert str(e).startswith(AGENT_DEAD_SENTINEL)
    assert e.reason == "response_failed"
    assert e.detail == "model unreachable"


def test_sentinel_without_detail():
    e = AgentDeadError("stream_idle")
    assert str(e).startswith(AGENT_DEAD_SENTINEL)
    assert "stream_idle" in str(e)


def test_returned_in_trace_path_preserves_prefix():
    # BFCL runners swallow the exception into AgentTrace(error=str(exc)) and return.
    # The orchestrator's `trace.error.startswith("AgentDead")` branch must still fire.
    exc = AgentDeadError("response_failed", "boom")
    trace = AgentTrace(task_id="t", final_output="", error=str(exc))
    assert trace.error.startswith("AgentDead")  # → grade AGENT_DEAD, scorer skipped


def test_is_runtime_error_subclass():
    # So existing `except Exception`/`except RuntimeError` handlers still catch it.
    assert issubclass(AgentDeadError, RuntimeError)


# ── OpenCode: info.error in a 200 body, and HTTP 5xx, both → AgentDeadError ───

class _FakeResp:
    def __init__(self, json_data, status=200):
        self._json = json_data
        self.status_code = status

    def raise_for_status(self):
        import httpx
        if self.status_code >= 400:
            req = httpx.Request("POST", "http://x/message")
            raise httpx.HTTPStatusError(
                "err", request=req, response=httpx.Response(self.status_code, request=req)
            )

    def json(self):
        return self._json


class _FakeClient:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, *a, **k):
        return self._resp


def _patch_httpx(monkeypatch, resp):
    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _FakeClient(resp))


def _opencode_stub():
    from framework.harnesses.opencode import OpenCodeHarness
    return SimpleNamespace(
        model_cfg=SimpleNamespace(model_name="m"),
        _auth_header="Basic x",
    ), OpenCodeHarness


@pytest.mark.asyncio
async def test_opencode_info_error_raises_agentdead(monkeypatch):
    stub, OpenCodeHarness = _opencode_stub()
    resp = _FakeResp({"info": {"error": {"name": "APIError",
                                         "data": {"message": "rate limited"}}}})
    _patch_httpx(monkeypatch, resp)
    with pytest.raises(AgentDeadError) as ei:
        await OpenCodeHarness._send_message(stub, "http://b", "sid", "p")
    assert ei.value.reason == "APIError"
    assert "rate limited" in str(ei.value)


@pytest.mark.asyncio
async def test_opencode_http_500_raises_agentdead(monkeypatch):
    stub, OpenCodeHarness = _opencode_stub()
    _patch_httpx(monkeypatch, _FakeResp({}, status=500))
    with pytest.raises(AgentDeadError) as ei:
        await OpenCodeHarness._send_message(stub, "http://b", "sid", "p")
    assert ei.value.reason == "http_error"


@pytest.mark.asyncio
async def test_opencode_clean_response_no_raise(monkeypatch):
    # Regression: a healthy response (no info.error) must NOT be treated as death.
    stub, OpenCodeHarness = _opencode_stub()
    resp = _FakeResp({"info": {"tokens": {"input": 11, "output": 4}}, "parts": []})
    _patch_httpx(monkeypatch, resp)
    data, in_tok, out_tok = await OpenCodeHarness._send_message(stub, "http://b", "sid", "p")
    assert in_tok == 11 and out_tok == 4
    assert "info" in data


# ── Hermes: idle watchdog, keepalive non-trip, response.failed, curl rc ──────

class _FakeStdin:
    def write(self, b):  # noqa: D401
        pass

    async def drain(self):
        pass

    def close(self):
        pass


class _FakeStdout:
    def __init__(self, readline):
        self._readline = readline

    async def readline(self):
        return await self._readline()


class _FakeProc:
    def __init__(self, readline, returncode=0):
        self.stdin = _FakeStdin()
        self.stdout = _FakeStdout(readline)
        self.returncode = returncode

    async def wait(self):
        return self.returncode


def _seq_reader(lines):
    """Return an async readline yielding each str as a line, then EOF (b'')."""
    it = iter(lines)

    async def _readline():
        try:
            s = next(it)
        except StopIteration:
            return b""
        return (s + "\n").encode()

    return _readline


async def _hang_forever():
    await asyncio.sleep(100)


def _patch_subprocess(monkeypatch, module, proc):
    async def _fake_exec(*a, **k):
        return proc
    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", _fake_exec)


def _hermes_harness():
    from framework.harnesses.hermes import HermesHarness
    return HermesHarness(SimpleNamespace(max_tokens=None, temperature=None))


@pytest.mark.asyncio
async def test_hermes_idle_watchdog_trips(monkeypatch):
    import framework.harnesses.hermes as hmod
    _patch_subprocess(monkeypatch, hmod, _FakeProc(_hang_forever))
    h = _hermes_harness()
    with pytest.raises(AgentDeadError) as ei:
        await h._stream_responses(
            container_id="c", req={}, emit_step=lambda *a: None,
            timeout=60, stream_idle_timeout=0.05,
        )
    assert ei.value.reason == "stream_idle"


@pytest.mark.asyncio
async def test_hermes_keepalive_does_not_false_trip(monkeypatch):
    import framework.harnesses.hermes as hmod
    # keepalive comments are skipped (not data:) but DO arrive as lines → reset timer.
    completed = ('data: {"type":"response.completed","response":'
                 '{"id":"r1","usage":{"input_tokens":5,"output_tokens":2}}}')
    proc = _FakeProc(_seq_reader([": keepalive", ": keepalive", completed]))
    _patch_subprocess(monkeypatch, hmod, proc)
    h = _hermes_harness()
    text, in_tok, out_tok, rid = await h._stream_responses(
        container_id="c", req={}, emit_step=lambda *a: None,
        timeout=60, stream_idle_timeout=0.05,
    )
    assert rid == "r1" and in_tok == 5 and out_tok == 2


@pytest.mark.asyncio
async def test_hermes_response_failed_raises(monkeypatch):
    import framework.harnesses.hermes as hmod
    line = ('data: {"type":"response.failed","response":'
            '{"error":{"message":"truncated","type":"server_error"}}}')
    _patch_subprocess(monkeypatch, hmod, _FakeProc(_seq_reader([line])))
    h = _hermes_harness()
    with pytest.raises(AgentDeadError) as ei:
        await h._stream_responses(container_id="c", req={}, emit_step=lambda *a: None,
                                  timeout=60, stream_idle_timeout=30)
    assert ei.value.reason == "response_failed"


@pytest.mark.asyncio
async def test_hermes_curl_nonzero_empty_raises(monkeypatch):
    import framework.harnesses.hermes as hmod
    # stream ends immediately (EOF) with curl rc=28 (--max-time) and no text produced.
    _patch_subprocess(monkeypatch, hmod, _FakeProc(_seq_reader([]), returncode=28))
    h = _hermes_harness()
    with pytest.raises(AgentDeadError) as ei:
        await h._stream_responses(container_id="c", req={}, emit_step=lambda *a: None,
                                  timeout=60, stream_idle_timeout=30)
    assert ei.value.reason == "curl_failed"


# ── OpenClaw: response.failed parse, and the activity watchdog ───────────────

def _openclaw_harness():
    from framework.harnesses.openclaw import OpenClawHarness
    return OpenClawHarness(SimpleNamespace(max_tokens=None, temperature=None))


class _FakeCommProc:
    """Fake proc whose communicate() returns canned SSE bytes."""
    def __init__(self, stdout_bytes):
        self._out = stdout_bytes

    async def communicate(self, input=None):
        return self._out, b""


@pytest.mark.asyncio
async def test_openclaw_response_failed_raises(monkeypatch):
    import framework.harnesses.openclaw as omod
    sse = (b'data: {"type":"response.failed","response":'
           b'{"error":{"code":"api_error","message":"down"}}}\n')

    async def _fake_exec(*a, **k):
        return _FakeCommProc(sse)
    monkeypatch.setattr(omod.asyncio, "create_subprocess_exec", _fake_exec)

    h = _openclaw_harness()
    with pytest.raises(AgentDeadError) as ei:
        await h._stream_responses(container_id="c", req={}, emit_step=lambda *a: None, timeout=5)
    assert ei.value.reason == "response_failed"


@pytest.mark.asyncio
async def test_openclaw_activity_watchdog_trips(monkeypatch):
    from framework.context import ExecutionContext
    h = _openclaw_harness()

    # Both streams hang without ever calling emit_step → no activity → watchdog fires.
    h._stream_events = lambda cid, emit: _hang_forever()
    h._stream_responses = lambda **k: _hang_forever()

    ctx = ExecutionContext(timeout=60, stream_idle_timeout=0.05)
    ctx.extras["harness_session"] = {"container_id": "c", "previous_response_id": None}

    with pytest.raises(AgentDeadError) as ei:
        await h.send_turn(
            messages=[{"role": "user", "content": "hi"}],
            tools=[], system_prompt="s", ctx=ctx, timeout=60,
        )
    assert ei.value.reason == "stream_idle"
