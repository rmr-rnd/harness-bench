from __future__ import annotations

import json

import pytest

from framework.config import ModelConfig
from framework.benchmarks.pac1._utils import ANSWER_FILENAME
from framework.benchmarks.pac1.omp_runner import run_omp
from framework.harnesses import load_harness_class
from framework.harnesses.omp import (
    OMP_DOCKER_IMAGE,
    OmpHarness,
    OmpRunConfig,
    _run_rpc_process,
    _write_omp_profile,
    build_omp_command,
)


def test_loads_omp_harness_classes():
    assert load_harness_class("omp").type == "omp"
    assert load_harness_class("pac1_omp").type == "pac1_omp"


def test_omp_defaults(model_cfg):
    h = OmpHarness.from_config(model_cfg, {})
    assert h.supports_sandbox is True
    assert h.omp.image == OMP_DOCKER_IMAGE
    assert h.omp.approval_mode == "yolo"


def test_builds_docker_command(tmp_path):
    cmd = build_omp_command(
        run_cfg=OmpRunConfig(image="local/omp:test"),
        model_name="gpt-test",
        cwd=tmp_path,
        profile_dir=tmp_path / "agent",
        system_prompt="system",
    )
    assert cmd[:4] == ["docker", "run", "-i", "--rm"]
    assert "-v" in cmd
    assert f"{tmp_path / 'agent'}:/omp-agent" in cmd
    assert f"{tmp_path}:/workspace" in cmd
    assert "local/omp:test" in cmd
    assert "--cwd" in cmd and "/workspace" in cmd
    assert "--system-prompt" in cmd


def test_write_generated_profile(tmp_path):
    cfg = ModelConfig(
        base_url="https://example.test/v1",
        api_key="secret",
        model_name="test-model",
        max_tokens=1234,
    )
    _write_omp_profile(tmp_path, cfg, mcp_url="http://bridge/mcp")

    models = (tmp_path / "models.yml").read_text()
    config = (tmp_path / "config.yml").read_text()
    mcp = json.loads((tmp_path / "mcp.json").read_text())

    assert "https://example.test/v1" in models
    assert "OMP_BENCH_API_KEY" in models
    assert "test-model" in models
    assert "secret" not in models
    assert "bench/test-model" in config
    assert mcp["mcpServers"]["sandbox-bridge"]["type"] == "http"
    assert mcp["mcpServers"]["sandbox-bridge"]["url"] == "http://bridge/mcp"


class _FakeStdin:
    def __init__(self):
        self.data = b""
        self.closed = False

    def write(self, data: bytes) -> None:
        self.data += data

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    def is_closing(self) -> bool:
        return self.closed


class _FakeStdout:
    def __init__(self, frames: list[dict]):
        self.lines = [(json.dumps(f) + "\n").encode() for f in frames]

    async def readline(self) -> bytes:
        if not self.lines:
            return b""
        return self.lines.pop(0)


class _FakeStderr:
    async def read(self) -> bytes:
        return b""


class _FakeProc:
    def __init__(self, frames: list[dict]):
        self.stdin = _FakeStdin()
        self.stdout = _FakeStdout(frames)
        self.stderr = _FakeStderr()
        self.returncode = 0
        self.killed = False

    async def wait(self) -> int:
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


@pytest.mark.asyncio
async def test_rpc_process_collects_steps_and_text(monkeypatch):
    frames = [
        {"type": "ready"},
        {"id": "turn-0", "type": "response", "command": "prompt", "success": True},
        {
            "type": "message_update",
            "assistantMessageEvent": {"type": "text_delta", "delta": "hello"},
        },
        {"type": "tool_execution_start", "toolExecution": {"toolName": "bash", "arguments": "pwd"}},
        {"type": "tool_execution_end", "toolExecution": {"toolName": "bash", "result": "/tmp"}},
        {"type": "agent_end", "usage": {"input": 3, "output": 4}},
    ]
    proc = _FakeProc(frames)

    async def fake_exec(*args, **kwargs):
        return proc

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    monkeypatch.setattr("framework.harnesses.omp.time.time", lambda: 0)

    result = await _run_rpc_process(
        cmd=["omp", "--mode", "rpc"],
        env={},
        prompt="Say hello",
        timeout=30,
        stream_idle_timeout=5,
        step_cb=None,
        initial_steps=[],
    )

    assert json.loads(proc.stdin.data.decode().strip()) == {
        "id": "turn-0",
        "type": "prompt",
        "message": "Say hello",
    }
    assert result.text == "hello"
    assert result.input_tokens == 3
    assert result.output_tokens == 4
    assert [s.type for s in result.steps] == ["output", "tool_call", "tool_result"]


@pytest.mark.asyncio
async def test_rpc_process_raises_on_prompt_error(monkeypatch):
    proc = _FakeProc([
        {"type": "ready"},
        {"id": "turn-0", "type": "response", "command": "prompt", "success": False, "error": "bad"},
    ])

    async def fake_exec(*args, **kwargs):
        return proc

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    monkeypatch.setattr("framework.harnesses.omp.time.time", lambda: 0)

    with pytest.raises(Exception, match="bad"):
        await _run_rpc_process(
            cmd=["omp", "--mode", "rpc"],
            env={},
            prompt="Say hello",
            timeout=30,
            stream_idle_timeout=5,
            step_cb=None,
            initial_steps=[],
        )


@pytest.mark.asyncio
async def test_pac1_omp_runner_reads_answer_file(monkeypatch, tmp_path, model_cfg):
    async def fake_prompt(**kwargs):
        from framework.harnesses.omp import OmpRunResult

        (kwargs["cwd"] / ANSWER_FILENAME).write_text(
            json.dumps({
                "message": "done",
                "outcome": "OUTCOME_OK",
                "refs": ["/workspace/result.txt"],
            }),
            encoding="utf-8",
        )
        return OmpRunResult(text="ignored", steps=[], input_tokens=10, output_tokens=11)

    monkeypatch.setattr("framework.benchmarks.pac1.omp_runner.run_omp_prompt", fake_prompt)

    result = await run_omp(
        task_id="task-1",
        instruction="do it",
        workspace_dir=tmp_path,
        model_cfg=model_cfg,
        run_cfg=OmpRunConfig(),
    )

    assert result == {
        "message": "done",
        "outcome": "OUTCOME_OK",
        "refs": ["result.txt"],
        "input_tokens": 10,
        "output_tokens": 11,
    }
