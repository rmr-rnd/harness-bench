"""Oh My Pi (OMP) harness using the OMP RPC JSONL protocol."""
from __future__ import annotations

import asyncio
import json
import os
import platform
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

import yaml
from pydantic import BaseModel

from framework.harnesses.base import AgentDeadError, Harness
from framework.models import Step
from framework.utils.docker import is_daemon_down
from framework.utils.work_dir import make_work_dir

if TYPE_CHECKING:
    from framework.config import ModelConfig
    from framework.context import ExecutionContext
    from framework.runners.base import TurnResponse


OMP_VERSION = "16.2.8"
OMP_DOCKER_IMAGE = f"harness-bench/omp:{OMP_VERSION}"
MCP_SCHEMA = "https://raw.githubusercontent.com/can1357/oh-my-pi/main/packages/coding-agent/src/config/mcp-schema.json"


class OmpConfig(BaseModel):
    omp_image: str = OMP_DOCKER_IMAGE
    omp_approval_mode: str = "yolo"
    omp_agent_max_seconds: int = 300


@dataclass
class OmpRunConfig:
    image: str = OMP_DOCKER_IMAGE
    approval_mode: str = "yolo"
    agent_max_seconds: int = 300


@dataclass
class OmpRunResult:
    text: str
    steps: list[Step]
    input_tokens: int = 0
    output_tokens: int = 0


class OmpHarness(Harness):
    type = "omp"
    config_model = OmpConfig
    supports_sandbox = True

    def __init__(
        self,
        model_cfg: "ModelConfig",
        *,
        omp_image: str = OMP_DOCKER_IMAGE,
        omp_approval_mode: str = "yolo",
        omp_agent_max_seconds: int = 300,
    ) -> None:
        super().__init__(model_cfg)
        self.omp = OmpRunConfig(
            image=omp_image,
            approval_mode=omp_approval_mode,
            agent_max_seconds=omp_agent_max_seconds,
        )

    async def send_turn(
        self,
        messages: list[dict],
        tools: list[dict],
        system_prompt: str,
        ctx: "ExecutionContext",
        timeout: int = 120,
        continue_session: bool = True,
    ) -> "TurnResponse":
        from framework.runners.base import TurnResponse

        user_msg = next(
            (m["content"] for m in reversed(messages) if m.get("role") == "user"), ""
        )
        if not isinstance(user_msg, str):
            user_msg = json.dumps(user_msg, ensure_ascii=False)

        step_list: list[Step] = [Step(type="input", content=messages)]
        if ctx.step_cb:
            ctx.step_cb("input", messages)

        result = await run_omp_prompt(
            prompt=user_msg,
            system_prompt=system_prompt,
            model_cfg=self.model_cfg,
            run_cfg=self.omp,
            mcp_url=ctx.mcp_url or None,
            timeout=min(timeout, self.omp.agent_max_seconds),
            stream_idle_timeout=ctx.stream_idle_timeout,
            step_cb=ctx.step_cb,
            initial_steps=step_list,
        )

        return TurnResponse(
            text=result.text,
            tool_calls=[],
            finish_reason="stop",
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            steps=result.steps,
        )


async def run_omp_prompt(
    *,
    prompt: str,
    system_prompt: str,
    model_cfg: "ModelConfig",
    run_cfg: OmpRunConfig,
    mcp_url: str | None = None,
    timeout: int = 300,
    stream_idle_timeout: int = 60,
    step_cb: Callable[..., None] | None = None,
    initial_steps: list[Step] | None = None,
    cwd: Path | None = None,
) -> OmpRunResult:
    """Run one OMP RPC prompt and return the final assistant text."""
    tmpdir = make_work_dir(prefix="omp-run-")
    owns_cwd = cwd is None
    work_dir = cwd or (tmpdir / "workspace")
    work_dir.mkdir(parents=True, exist_ok=True)

    profile_dir = tmpdir / "agent"
    _write_omp_profile(profile_dir, model_cfg, mcp_url=mcp_url)

    env = os.environ.copy()
    env["PI_CODING_AGENT_DIR"] = str(profile_dir)
    env["OMP_BENCH_API_KEY"] = model_cfg.api_key or "sk-none"
    env.setdefault("OMP_MCP_TIMEOUT_MS", "900000")

    cmd = build_omp_command(
        run_cfg=run_cfg,
        model_name=model_cfg.model_name,
        cwd=work_dir,
        profile_dir=profile_dir,
        system_prompt=system_prompt,
    )
    if step_cb:
        step_cb("status", "Starting OMP container")

    try:
        result = await _run_rpc_process(
            cmd=cmd,
            env=env,
            prompt=prompt,
            timeout=timeout,
            stream_idle_timeout=stream_idle_timeout,
            step_cb=step_cb,
            initial_steps=initial_steps,
        )
        return result
    finally:
        if owns_cwd:
            shutil.rmtree(work_dir, ignore_errors=True)
        shutil.rmtree(profile_dir, ignore_errors=True)
        shutil.rmtree(tmpdir, ignore_errors=True)


def build_omp_command(
    *,
    run_cfg: OmpRunConfig,
    model_name: str,
    cwd: Path,
    profile_dir: Path,
    system_prompt: str = "",
) -> list[str]:
    args = [
        "--mode", "rpc",
        "--cwd", "/workspace",
        "--model", f"bench/{model_name}",
        "--approval-mode", run_cfg.approval_mode,
        "--max-time", str(run_cfg.agent_max_seconds),
        "--no-session",
        "--no-title",
        "--no-extensions",
        "--no-skills",
    ]
    if system_prompt:
        args.extend(["--system-prompt", system_prompt])

    docker_gateway_args = (
        [] if platform.system() == "Darwin"
        else ["--add-host=host.docker.internal:host-gateway"]
    )
    network = os.environ.get("HARNESS_DOCKER_NETWORK", "")
    network_args = ["--network", network] if network else []
    return [
        "docker", "run", "-i", "--rm",
        *docker_gateway_args,
        *network_args,
        "-v", f"{profile_dir}:/omp-agent",
        "-v", f"{cwd}:/workspace",
        "-e", "PI_CODING_AGENT_DIR=/omp-agent",
        "-e", "OMP_BENCH_API_KEY",
        "-e", "OMP_MCP_TIMEOUT_MS",
        run_cfg.image,
        "omp",
        *args,
    ]


def _write_omp_profile(profile_dir: Path, model_cfg: "ModelConfig", mcp_url: str | None = None) -> None:
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "models.yml").write_text(
        yaml.safe_dump(
            {
                "providers": {
                    "bench": {
                        "baseUrl": model_cfg.base_url,
                        "apiKey": "OMP_BENCH_API_KEY",
                        "api": "openai-completions",
                        "authHeader": True,
                        "models": [
                            {
                                "id": model_cfg.model_name,
                                "name": model_cfg.model_name,
                                "reasoning": bool(model_cfg.reasoning_effort and model_cfg.reasoning_effort != "none"),
                                "input": ["text"],
                                "contextWindow": 200000,
                                "maxTokens": model_cfg.max_tokens or 4096,
                                "compat": {
                                    "supportsStore": False,
                                    "supportsDeveloperRole": False,
                                    "maxTokensField": "max_tokens",
                                },
                            }
                        ],
                    }
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (profile_dir / "config.yml").write_text(
        yaml.safe_dump(
            {
                "modelRoles": {
                    "default": f"bench/{model_cfg.model_name}",
                    "smol": f"bench/{model_cfg.model_name}",
                    "slow": f"bench/{model_cfg.model_name}",
                    "plan": f"bench/{model_cfg.model_name}",
                },
                "tools": {"approvalMode": "yolo"},
                "memory": {"backend": "off"},
                "advisor": {"enabled": False},
                "startup": {"checkUpdate": False, "quiet": True, "showSplash": False},
                "marketplace": {"autoUpdate": "off"},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    if mcp_url:
        (profile_dir / "mcp.json").write_text(
            json.dumps(
                {
                    "$schema": MCP_SCHEMA,
                    "mcpServers": {
                        "sandbox-bridge": {
                            "type": "http",
                            "url": mcp_url,
                            "timeout": 900000,
                        }
                    },
                },
                indent=2,
            ),
            encoding="utf-8",
        )


async def _run_rpc_process(
    *,
    cmd: list[str],
    env: dict[str, str],
    prompt: str,
    timeout: int,
    stream_idle_timeout: int,
    step_cb: Callable[..., None] | None,
    initial_steps: list[Step] | None,
) -> OmpRunResult:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        limit=2 ** 24,
    )
    assert proc.stdin is not None
    assert proc.stdout is not None

    steps = initial_steps if initial_steps is not None else []
    text_parts: list[str] = []
    input_tokens = 0
    output_tokens = 0
    prompt_id = f"turn-{int(time.time() * 1000)}"
    saw_ready = False
    saw_agent_end = False
    deadline = time.monotonic() + timeout
    idle = stream_idle_timeout if stream_idle_timeout and stream_idle_timeout > 0 else timeout

    def emit(stype: str, content: Any) -> None:
        steps.append(Step(type=stype, content=content))
        if step_cb:
            step_cb(stype, content)

    async def read_frame() -> dict:
        nonlocal saw_ready
        remaining = max(0.1, min(idle, deadline - time.monotonic()))
        try:
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=remaining)
        except asyncio.TimeoutError:
            raise AgentDeadError("stream_idle", f">{remaining:.0f}s no OMP RPC activity")
        if not line:
            stderr = await _read_stderr(proc)
            raise AgentDeadError("process_exit", stderr[:500])
        try:
            frame = json.loads(line.decode(errors="replace"))
        except json.JSONDecodeError:
            return {"type": "invalid_json", "raw": line.decode(errors="replace")}
        if frame.get("type") == "ready":
            saw_ready = True
        return frame

    try:
        while not saw_ready:
            if time.monotonic() > deadline:
                raise AgentDeadError("startup_timeout", f">{timeout}s waiting for OMP ready")
            frame = await read_frame()
            if frame.get("type") == "extension_error":
                raise AgentDeadError("extension_error", str(frame.get("error", "")))

        proc.stdin.write(
            (json.dumps({"id": prompt_id, "type": "prompt", "message": prompt}) + "\n").encode()
        )
        await proc.stdin.drain()

        while not saw_agent_end:
            if time.monotonic() > deadline:
                raise AgentDeadError("timeout", f">{timeout}s waiting for OMP response")
            frame = await read_frame()
            ftype = frame.get("type")

            if ftype == "response" and frame.get("id") == prompt_id:
                if not frame.get("success", False):
                    raise AgentDeadError("rpc_error", str(frame.get("error", "")))
                if frame.get("data", {}).get("agentInvoked") is False:
                    break
                continue

            if ftype == "message_update":
                delta = _extract_message_delta(frame)
                if delta:
                    text_parts.append(delta)
                    emit("output", delta)
                thinking = _extract_thinking_delta(frame)
                if thinking:
                    emit("thinking", thinking)
                continue

            if ftype == "tool_execution_start":
                emit("tool_call", _extract_tool_payload(frame))
                continue

            if ftype in {"tool_execution_update", "tool_execution_end"}:
                payload = _extract_tool_payload(frame)
                emit("tool_result", payload)
                continue

            if ftype == "agent_end":
                saw_agent_end = True
                input_tokens, output_tokens = _extract_usage(frame)
                final = _extract_final_text(frame)
                if final and not "".join(text_parts).strip():
                    text_parts.append(final)
                    emit("output", final)
                break

            if ftype == "extension_error":
                emit("status", f"OMP extension error: {frame.get('error', '')}")

    finally:
        if proc.stdin and not proc.stdin.is_closing():
            proc.stdin.close()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            proc.kill()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass

    if proc.returncode not in (0, None) and not text_parts:
        stderr = await _read_stderr(proc)
        if is_daemon_down(stderr):
            raise RuntimeError("Docker daemon is not running. Start Docker and retry.")
        raise AgentDeadError("process_exit", f"rc={proc.returncode}: {stderr[:500]}")

    return OmpRunResult(
        text="".join(text_parts).strip(),
        steps=steps,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


async def _read_stderr(proc: asyncio.subprocess.Process) -> str:
    if proc.stderr is None:
        return ""
    try:
        data = await asyncio.wait_for(proc.stderr.read(), timeout=1)
        return data.decode(errors="replace")
    except Exception:
        return ""


def _extract_message_delta(frame: dict) -> str:
    ev = frame.get("assistantMessageEvent") or {}
    candidates = [ev, frame]
    for obj in candidates:
        if not isinstance(obj, dict):
            continue
        etype = str(obj.get("type", ""))
        if "text" in etype and obj.get("delta"):
            return str(obj.get("delta", ""))
        if "text" in etype and obj.get("text"):
            return str(obj.get("text", ""))
        if obj.get("content") and isinstance(obj.get("content"), str):
            return str(obj["content"])
    return ""


def _extract_thinking_delta(frame: dict) -> str:
    ev = frame.get("assistantMessageEvent") or {}
    if not isinstance(ev, dict):
        return ""
    etype = str(ev.get("type", ""))
    if "thinking" in etype or "reasoning" in etype:
        return str(ev.get("delta") or ev.get("text") or "")
    return ""


def _extract_tool_payload(frame: dict) -> dict[str, Any]:
    tool = frame.get("toolExecution") or frame.get("tool") or frame
    if not isinstance(tool, dict):
        return {"name": "", "args": tool}
    return {
        "name": tool.get("toolName") or tool.get("name") or tool.get("id") or "",
        "args": tool.get("arguments") or tool.get("input") or tool.get("args") or "",
        "result": tool.get("result") or tool.get("output") or tool.get("error") or "",
    }


def _extract_usage(frame: dict) -> tuple[int, int]:
    usage = frame.get("usage") or frame.get("tokenUsage") or {}
    if not usage and isinstance(frame.get("stats"), dict):
        usage = frame["stats"].get("usage", {})
    if not isinstance(usage, dict):
        return 0, 0
    inp = usage.get("input_tokens") or usage.get("prompt_tokens") or usage.get("input") or 0
    out = usage.get("output_tokens") or usage.get("completion_tokens") or usage.get("output") or 0
    return int(inp or 0), int(out or 0)


def _extract_final_text(frame: dict) -> str:
    messages = frame.get("messages") or []
    if not isinstance(messages, list):
        return ""
    for msg in reversed(messages):
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        content = msg.get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, dict):
                    val = part.get("text") or part.get("content")
                    if val:
                        parts.append(str(val))
                elif isinstance(part, str):
                    parts.append(part)
            return "".join(parts)
    return ""
