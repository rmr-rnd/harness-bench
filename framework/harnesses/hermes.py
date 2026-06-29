"""Hermes agent harness — runs NousResearch/hermes-agent in Docker, communicates via /v1/runs SSE."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from framework.harnesses.base import Harness, AgentDeadError
from framework.utils.network import get_mcp_host
from framework.utils.docker import is_daemon_down
from framework.utils.work_dir import make_work_dir
from framework.models import AgentTrace, Step

if TYPE_CHECKING:
    from framework.config import ModelConfig
    from framework.context import ExecutionContext
    from framework.models import Task
    from framework.runners.base import TurnResponse

_HERMES_INTERNAL_PORT = 8642


class HermesConfig(BaseModel):
    hermes_image: str = "nousresearch/hermes-agent:latest"
    hermes_api_key: str = ""
    hermes_approvals_off: bool = True
    tavily_api_key: str = ""


def _call_strings_to_dicts(calls: list[str]) -> list[dict]:
    """Convert ['func(a=1, b="x")'] → [{'func': {'a': 1, 'b': 'x'}}]."""
    import ast
    result = []
    for call in calls:
        try:
            tree = ast.parse(call, mode="eval")
            node = tree.body
            if not isinstance(node, ast.Call):
                continue
            parts: list[str] = []
            fn = node.func
            while isinstance(fn, ast.Attribute):
                parts.append(fn.attr)
                fn = fn.value
            if isinstance(fn, ast.Name):
                parts.append(fn.id)
            func_name = ".".join(reversed(parts))
            kwargs: dict = {}
            for kw in node.keywords:
                try:
                    kwargs[kw.arg] = ast.literal_eval(kw.value)
                except Exception:
                    if isinstance(kw.value, ast.Name):
                        kwargs[kw.arg] = kw.value.id
            result.append({func_name: kwargs})
        except Exception:
            continue
    return result


class HermesHarness(Harness):
    type = "hermes"
    config_model = HermesConfig
    supports_sandbox = True

    def __init__(
        self,
        model_cfg: "ModelConfig",
        tavily_api_key: str = "",
        hermes_image: str = "nousresearch/hermes-agent:latest",
        hermes_api_key: str = "",
        hermes_approvals_off: bool = True,
    ) -> None:
        super().__init__(model_cfg)
        import secrets
        self.tavily_api_key = tavily_api_key
        self.hermes_image = hermes_image
        self.hermes_approvals_off = hermes_approvals_off
        # Hermes rejects short/placeholder keys
        self.hermes_api_key = hermes_api_key or secrets.token_hex(32)

    async def send_turn(
        self,
        messages: list[dict],
        tools: list[dict],
        system_prompt: str,
        ctx: "ExecutionContext",
        timeout: int = 120,
        continue_session: bool = True,
    ) -> "TurnResponse":
        """Send one turn to Hermes via /v1/responses.

        Lazily starts the container on the first call and reuses it for subsequent
        turns (via previous_response_id), unless continue_session=False.
        """
        import platform as _platform
        from framework.runners.base import TurnResponse

        session = ctx.extras.setdefault("harness_session", {})
        container_id = session.get("container_id")

        if container_id is None:
            _status = ctx.step_cb
            await self._ensure_image(step_cb=_status)
            tmpdir = str(make_work_dir(prefix="hermes_turn_"))
            mcp_port = ctx.extras.get("bfcl_mcp_port")

            add_host_args = (
                [] if _platform.system() == "Darwin"
                else ["--add-host=host.docker.internal:host-gateway"]
            )
            network = os.environ.get("HARNESS_DOCKER_NETWORK", "")
            network_args = ["--network", network] if network else []
            cfg_path = os.path.join(tmpdir, "config.yaml")
            with open(cfg_path, "w") as f:
                f.write(self._make_config_yaml(
                    mcp_port=mcp_port,
                    mcp_url=ctx.mcp_url or None,
                ))

            _status and _status("status", "Starting container")
            proc = await asyncio.create_subprocess_exec(
                "docker", "run", "-d", "--rm",
                *add_host_args,
                *network_args,
                "-v", f"{cfg_path}:/opt/data/config.yaml:ro",
                *self._base_env_args(web_search=ctx.web_search),
                self.hermes_image, "gateway", "run",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=900)
            if proc.returncode != 0:
                err = stderr.decode(errors="replace")[:300]
                if is_daemon_down(err):
                    raise RuntimeError("Docker daemon is not running. Start Docker and retry.")
                raise RuntimeError(f"Container failed to start: {err}")
            container_id = stdout.decode().strip()
            _status and _status("status", f"Container started ({container_id[:12]})")
            await self._wait_ready(container_id, timeout=900, step_cb=_status)

            session["container_id"] = container_id
            session["tmpdir"] = tmpdir
            session["previous_response_id"] = None
            ctx.extras["harness_session"] = session

            _cid = container_id
            _td = tmpdir

            async def _cleanup() -> None:
                await self._stop_container(_cid)
                shutil.rmtree(_td, ignore_errors=True)

            ctx.cleanup_fns.append(_cleanup)

        # Extract last user message
        user_msg = next(
            (m["content"] for m in reversed(messages) if m["role"] == "user"), ""
        )

        req: dict = {"input": user_msg, "store": True, "stream": True}
        prev_id = session.get("previous_response_id") if continue_session else None
        if prev_id:
            req["previous_response_id"] = prev_id
        else:
            req["instructions"] = system_prompt

        from framework.models import Step
        step_cb = ctx.step_cb
        step_list: list[Step] = []

        step_list.append(Step(type="input", content=messages))
        if step_cb:
            step_cb("input", messages)

        def _emit_step(stype: str, content: Any) -> None:
            step_list.append(Step(type=stype, content=content))
            if step_cb:
                step_cb(stype, content)

        text, in_tok, out_tok, response_id = await self._stream_responses(
            container_id=container_id,
            req=req,
            emit_step=_emit_step,
            timeout=timeout,
            stream_idle_timeout=ctx.stream_idle_timeout,
        )
        if continue_session and response_id:
            session["previous_response_id"] = response_id

        return TurnResponse(
            text=text,
            tool_calls=[],
            finish_reason="stop",
            input_tokens=in_tok,
            output_tokens=out_tok,
            steps=step_list,
        )

    def _make_config_yaml(self, mcp_port: int | None = None, mcp_url: str | None = None) -> str:
        """Build Hermes config.yaml.

        mcp_port: used when we own the MCP server (BFCL).
        mcp_url: used when orchestrator pre-started the bridge (sandbox/SWE).
        """
        model_section = (
            f"model:\n"
            f"  provider: custom\n"
            f"  base_url: \"{self.model_cfg.base_url}\"\n"
            f"  api_key: \"{self.model_cfg.api_key or 'sk-none'}\"\n"
            f"  default: \"{self.model_cfg.model_name}\"\n"
            f"  temperature: {self.model_cfg.temperature}\n"
            f"  max_tokens: {self.model_cfg.max_tokens or 4096}\n"
        )

        url = mcp_url or (f"http://{get_mcp_host()}:{mcp_port}" if mcp_port else None)
        if url:
            extra = (
                f"platform_toolsets:\n"
                f"  api_server: []\n"
                f"mcp_servers:\n"
                f"  sandbox-bridge:\n"
                f"    url: {url}\n"
                f"    timeout: 900\n"
            )
        else:
            extra = "terminal:\n  backend: local\n"

        agent_section = ""
        if self.model_cfg.reasoning_effort:
            agent_section = f"agent:\n  reasoning_effort: \"{self.model_cfg.reasoning_effort}\"\n"

        return (
            model_section
            + extra
            + "compression:\n  enabled: false\n"
            + ("approvals:\n  mode: off\n" if self.hermes_approvals_off else "")
            + agent_section
        )

    async def _ensure_image(self, step_cb=None) -> None:
        def _s(msg: str) -> None:
            if step_cb:
                step_cb("status", msg)
        _s(f"Inspecting image {self.hermes_image}")
        check = await asyncio.create_subprocess_exec(
            "docker", "image", "inspect", self.hermes_image,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await check.communicate()
        if check.returncode == 0:
            return
        _s(f"Image not found locally, pulling {self.hermes_image}")
        proc = await asyncio.create_subprocess_exec(
            "docker", "pull", self.hermes_image,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=900)
        if proc.returncode != 0:
            err = stderr.decode(errors="replace").strip()
            if is_daemon_down(err):
                raise RuntimeError("Docker daemon is not running. Start Docker and retry.")
            raise RuntimeError(f"Image pull failed for {self.hermes_image}: {err[:300]}")
        _s("Image pulled")

    def _base_env_args(self, web_search: bool = False) -> list[str]:
        args = [
            "-e", "API_SERVER_ENABLED=true",
            "-e", "API_SERVER_HOST=0.0.0.0",
            "-e", f"API_SERVER_PORT={_HERMES_INTERNAL_PORT}",
            "-e", f"API_SERVER_KEY={self.hermes_api_key}",
            "-e", "GATEWAY_ALLOW_ALL_USERS=true",
        ]
        if self.model_cfg.api_key:
            args += [
                "-e", f"OPENAI_API_KEY={self.model_cfg.api_key}",
                "-e", f"OPENROUTER_API_KEY={self.model_cfg.api_key}",
            ]
        if web_search and self.tavily_api_key:
            args += ["-e", f"TAVILY_API_KEY={self.tavily_api_key}"]
        return args

    async def _stream_responses(
        self,
        container_id: str,
        req: dict,
        emit_step: Any,
        timeout: int,
        stream_idle_timeout: int = 0,
    ) -> tuple[str, int, int, str]:
        """POST /v1/responses with stream=True, parse SSE, emit steps in real-time.

        Returns (final_text, input_tokens, output_tokens, response_id).

        Raises AgentDeadError when the agent emits response.failed, when the SSE
        stream is silent longer than stream_idle_timeout (idle watchdog; the agent
        emits a `: keepalive` comment every 30s while alive, so a >idle gap means a
        dead connection), or when curl exits non-zero without producing any text.
        """
        proc = await asyncio.create_subprocess_exec(
            "docker", "exec", "-i", container_id,
            "curl", "-sf", "--no-buffer",
            "-X", "POST",
            f"http://localhost:{_HERMES_INTERNAL_PORT}/v1/responses",
            "-H", "Content-Type: application/json",
            "-H", f"Authorization: Bearer {self.hermes_api_key}",
            "-H", "Accept: text/event-stream",
            "--max-time", str(timeout),
            "--data-binary", "@-",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=2 ** 24,  # 16MB — prevents LimitOverrunError on large SSE lines
        )
        # Write request body and close stdin so curl starts sending
        proc.stdin.write(json.dumps(req).encode())
        await proc.stdin.drain()
        proc.stdin.close()
        final_text = ""
        input_tokens = 0
        output_tokens = 0
        response_id = ""
        text_buffer = ""
        pending_tool: dict | None = None

        # Idle watchdog: trip after stream_idle_timeout of no line at all. Keepalive
        # comments (every 30s) count as lines and reset the timer, so a live-but-busy
        # agent never trips. Fall back to the old timeout+10 ceiling when disabled (0).
        idle = stream_idle_timeout if stream_idle_timeout and stream_idle_timeout > 0 else timeout + 10

        async def _read_lines():
            while True:
                try:
                    line_bytes = await asyncio.wait_for(proc.stdout.readline(), timeout=idle)
                except asyncio.TimeoutError:
                    raise AgentDeadError("stream_idle", f">{idle}s no stream activity")
                if not line_bytes:
                    break
                yield line_bytes.decode(errors="replace").rstrip("\n")

        async for line in _read_lines():
            if not line.startswith("data:"):
                continue
            data_str = line[5:].strip()
            if not data_str or data_str == "[DONE]":
                continue
            try:
                ev = json.loads(data_str)
            except Exception:
                continue

            etype = ev.get("type", "")

            if etype == "response.output_text.delta":
                delta = ev.get("delta", "")
                text_buffer += delta

            elif etype == "response.output_item.added":
                item = ev.get("item", {})
                if item.get("type") == "function_call":
                    pending_tool = {"name": item.get("name", ""), "args": ""}
                elif item.get("type") == "function_call_output":
                    emit_step("tool_result", item.get("output", ""))

            elif etype == "response.output_text.done":
                if text_buffer:
                    emit_step("output", text_buffer)
                    final_text += text_buffer
                    text_buffer = ""

            elif etype == "response.output_item.done":
                item = ev.get("item", {})
                if item.get("type") == "function_call":
                    name = item.get("name", "")
                    try:
                        args = json.loads(item.get("arguments", "{}"))
                    except Exception:
                        args = item.get("arguments", "")
                    emit_step("tool_call", {"name": name, "args": args})
                    pending_tool = None
                elif item.get("type") == "message":
                    if text_buffer:
                        emit_step("output", text_buffer)
                        final_text += text_buffer
                        text_buffer = ""

            elif etype == "response.completed":
                resp = ev.get("response", {})
                response_id = resp.get("id", "")
                usage = resp.get("usage", {})
                input_tokens = usage.get("input_tokens", 0)
                output_tokens = usage.get("output_tokens", 0)
                if text_buffer:
                    emit_step("output", text_buffer)
                    final_text += text_buffer
                    text_buffer = ""

            elif etype == "response.failed":
                error = ev.get("response", {}).get("error", {})
                raise AgentDeadError("response_failed", str(error))

        await asyncio.wait_for(proc.wait(), timeout=10)
        # flush any remaining buffered text
        if text_buffer:
            emit_step("output", text_buffer)
            final_text += text_buffer

        # curl exited non-zero without producing any text (e.g. connection dropped,
        # --max-time cut mid-stream, server gone): the agent gave us nothing usable.
        if proc.returncode not in (0, None) and not final_text:
            raise AgentDeadError("curl_failed", f"rc={proc.returncode}")

        return final_text, input_tokens, output_tokens, response_id

    async def _wait_ready(self, container_id: str, timeout: int = 90, step_cb=None) -> None:
        if step_cb:
            step_cb("status", "Waiting for agent to become ready")
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                proc = await asyncio.create_subprocess_exec(
                    "docker", "exec", container_id,
                    "curl", "-sf", f"http://localhost:{_HERMES_INTERNAL_PORT}/health",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                out, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
                if proc.returncode == 0:
                    if step_cb:
                        step_cb("status", "Agent is ready")
                    return
            except Exception:
                pass
            await asyncio.sleep(2)
        logs = ""
        try:
            lp = await asyncio.create_subprocess_exec(
                "docker", "logs", "--tail", "30", container_id,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            )
            lo, _ = await asyncio.wait_for(lp.communicate(), timeout=5)
            logs = lo.decode(errors="replace")[-800:]
        except Exception:
            pass
        raise RuntimeError(f"Hermes API not ready after {timeout}s\nlogs:\n{logs}")

    async def _stop_container(self, container_id: str) -> None:
        proc = await asyncio.create_subprocess_exec(
            "docker", "stop", container_id,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(proc.communicate(), timeout=900)
        except asyncio.TimeoutError:
            pass

