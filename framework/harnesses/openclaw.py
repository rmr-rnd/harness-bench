"""OpenClaw agent harness — runs ghcr.io/openclaw/openclaw in Docker, communicates via /v1/responses HTTP API."""
from __future__ import annotations

import asyncio
import json
import secrets
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

_OPENCLAW_PORT = 18789


class OpenClawConfig(BaseModel):
    openclaw_image: str = "ghcr.io/openclaw/openclaw:latest"
    openclaw_token: str = ""          # gateway auth token; auto-generated if empty
    openclaw_approvals_off: bool = True  # disable human-in-the-loop approvals (yolo mode)
    tavily_api_key: str = ""


class OpenClawHarness(Harness):
    type = "openclaw"
    config_model = OpenClawConfig
    supports_sandbox = True

    def __init__(
        self,
        model_cfg: "ModelConfig",
        openclaw_image: str = "ghcr.io/openclaw/openclaw:latest",
        openclaw_token: str = "",
        openclaw_approvals_off: bool = True,
        tavily_api_key: str = "",
        **_: Any,
    ) -> None:
        super().__init__(model_cfg)
        self.openclaw_image = openclaw_image
        self.openclaw_token = openclaw_token or secrets.token_hex(24)
        self.openclaw_approvals_off = openclaw_approvals_off
        self.tavily_api_key = tavily_api_key

    async def send_turn(
        self,
        messages: list[dict],
        tools: list[dict],
        system_prompt: str,
        ctx: "ExecutionContext",
        timeout: int = 120,
        continue_session: bool = True,
    ) -> "TurnResponse":
        """Send one turn to OpenClaw via /v1/responses.

        Lazily starts the container on the first call. Supports previous_response_id
        for turn chaining (continue_session=True) or fresh sessions (False).
        """
        import os
        import platform as _platform
        from framework.runners.base import TurnResponse
        from framework.models import Step

        session = ctx.extras.setdefault("harness_session", {})
        container_id = session.get("container_id")

        if container_id is None:
            _status = ctx.step_cb
            await self._ensure_image(step_cb=_status)
            tmpdir = str(make_work_dir(prefix="openclaw_turn_"))
            mcp_port = ctx.extras.get("bfcl_mcp_port")

            _status and _status("status", "Starting container")
            container_id = await self._start_container(
                tmpdir,
                mcp_port=mcp_port,
                mcp_url=ctx.mcp_url or None,
                web_search=ctx.web_search,
            )
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

        user_msg = next(
            (m["content"] for m in reversed(messages) if m["role"] == "user"), ""
        )

        req: dict = {"model": "openclaw", "input": user_msg, "store": True, "stream": True}
        prev_id = session.get("previous_response_id") if continue_session else None
        if prev_id:
            req["previous_response_id"] = prev_id
        else:
            req["instructions"] = system_prompt
        if self.model_cfg.max_tokens:
            req["max_output_tokens"] = self.model_cfg.max_tokens
        if self.model_cfg.temperature is not None:
            req["temperature"] = self.model_cfg.temperature

        step_cb = ctx.step_cb
        step_list: list[Step] = []

        step_list.append(Step(type="input", content=messages))
        if step_cb:
            step_cb("input", messages)

        # Activity-based idle watchdog. OpenClaw's SSE /v1/responses has NO keepalive,
        # so an SSE-silence timer (like Hermes uses) would false-trip on a long LLM
        # call. Liveness is instead tracked via _emit_step, which is fed by BOTH the
        # WS stream (_stream_events: assistant deltas, tool events) and the SSE final
        # aggregation (_stream_responses). No activity from either for stream_idle_timeout
        # → the agent is dead.
        last_activity = time.monotonic()

        def _emit_step(stype: str, content: Any) -> None:
            nonlocal last_activity
            last_activity = time.monotonic()
            step_list.append(Step(type=stype, content=content))
            if step_cb:
                step_cb(stype, content)

        idle = ctx.stream_idle_timeout

        async def _idle_watchdog() -> None:
            while idle and idle > 0:
                await asyncio.sleep(min(idle, 5))
                if time.monotonic() - last_activity > idle:
                    raise AgentDeadError("stream_idle", f">{idle}s no stream activity")

        log_task = asyncio.create_task(self._stream_events(container_id, _emit_step))
        stream_task = asyncio.create_task(self._stream_responses(
            container_id=container_id,
            req=req,
            emit_step=_emit_step,
            timeout=timeout,
        ))
        wd_task = asyncio.create_task(_idle_watchdog())
        try:
            done, _pending = await asyncio.wait(
                {stream_task, wd_task}, return_when=asyncio.FIRST_COMPLETED,
            )
            # Watchdog tripped (raised AgentDeadError) before the stream finished.
            if wd_task in done and not wd_task.cancelled() and wd_task.exception() is not None:
                stream_task.cancel()
                try:
                    await stream_task
                except BaseException:
                    pass
                raise wd_task.exception()
            # Stream finished first (normally, or with its own AgentDeadError).
            text, in_tok, out_tok, response_id = await stream_task
        finally:
            for _t in (wd_task, log_task, stream_task):
                _t.cancel()
            for _t in (wd_task, log_task, stream_task):
                try:
                    await _t
                except BaseException:
                    pass
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


    def _make_openclaw_config(
        self,
        mcp_port: int | None = None,
        mcp_url: str | None = None,
        web_search: bool = False,
        tavily_api_key: str = "",
    ) -> dict:
        """Build openclaw.json passed to the container.

        mcp_port: wires OpenClaw to a BFCL MCP server we own on the host.
        mcp_url: wires OpenClaw to a pre-started MCP bridge (sandbox/SWE-bench).
        When neither is set, OpenClaw uses its built-in Pi tools.

        When mcp_url is provided (sandbox/SWE benchmarks), Pi built-in tools are
        disabled so the agent is forced to use sandbox MCP tools exclusively.
        When openclaw_approvals_off=True, human-in-the-loop approvals are disabled.
        """
        model_name = self.model_cfg.model_name
        url = mcp_url or (f"http://{get_mcp_host()}:{mcp_port}" if mcp_port is not None else None)
        # When using an external MCP bridge, disable Pi builtins so the agent
        # only uses the sandbox-provided tools (avoids Pi write/bash going to
        # the local container filesystem instead of the sandbox).
        has_external_bridge = url is not None

        gateway: dict = {
            "mode": "local",
            "bind": "lan",
            "port": _OPENCLAW_PORT,
            "auth": {
                "mode": "token",
                "token": self.openclaw_token,
            },
            "controlUi": {"enabled": False},
            "http": {
                "endpoints": {
                    "responses": {"enabled": True},
                }
            },
        }
        agents_defaults: dict = {"model": f"bench/{model_name}", "skipBootstrap": True, "timeoutSeconds": 900}
        if self.model_cfg.reasoning_effort:
            # OpenClaw uses thinkingDefault; "none" maps to "off", rest match directly
            thinking = "off" if self.model_cfg.reasoning_effort == "none" else self.model_cfg.reasoning_effort
            agents_defaults["thinkingDefault"] = thinking

        search_cfg: dict = {"enabled": web_search}
        if web_search and tavily_api_key:
            search_cfg["provider"] = "tavily"

        tools_cfg: dict = {
            "exec": {
                "ask": "off" if self.openclaw_approvals_off else "on-miss",
            },
            "web": {
                "fetch": {"enabled": web_search},
                "search": search_cfg,
            },
            "deny": ["browser"],
        }
        if has_external_bridge:
            # Allow only MCP bridge tools (prefixed "bridge_"); all built-ins are blocked.
            # OpenClaw's policy: deny is checked before allow, so allow-list alone is sufficient
            # to block everything that doesn't match "bridge_*".
            tools_cfg["allow"] = ["sandbox-bridge_*"]

        cfg: dict = {
            "gateway": gateway,
            "models": {
                "providers": {
                    "bench": {
                        "baseUrl": self.model_cfg.base_url,
                        "apiKey": self.model_cfg.api_key or "sk-none",
                        "api": "openai-completions",
                        "timeoutSeconds": 900,
                        "models": [{"id": model_name, "name": model_name}],
                    }
                }
            },
            "agents": {
                "defaults": agents_defaults,
            },
            "tools": tools_cfg,
        }
        if tavily_api_key and web_search:
            cfg["plugins"] = {
                "entries": {
                    "tavily": {
                        "config": {
                            "webSearch": {
                                "apiKey": tavily_api_key,
                            }
                        }
                    }
                }
            }
        if url:
            cfg["mcp"] = {
                "servers": {
                    "sandbox-bridge": {
                        "url": url,
                        "transport": "streamable-http",
                    }
                }
            }
        return cfg

    async def _ensure_image(self, step_cb=None) -> None:
        def _s(msg: str) -> None:
            if step_cb:
                step_cb("status", msg)
        _s(f"Inspecting image {self.openclaw_image}")
        check = await asyncio.create_subprocess_exec(
            "docker", "image", "inspect", self.openclaw_image,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await check.communicate()
        if check.returncode == 0:
            return
        _s(f"Image not found locally, pulling {self.openclaw_image}")
        proc = await asyncio.create_subprocess_exec(
            "docker", "pull", self.openclaw_image,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=900)
        if proc.returncode != 0:
            err = stderr.decode(errors="replace").strip()
            if is_daemon_down(err):
                raise RuntimeError("Docker daemon is not running. Start Docker and retry.")
            raise RuntimeError(f"Image pull failed for {self.openclaw_image}: {err[:300]}")
        _s("Image pulled")

    async def _start_container(
        self,
        tmpdir: str,
        mcp_port: int | None = None,
        mcp_url: str | None = None,
        web_search: bool = False,
    ) -> str:
        import os
        cfg_dir = os.path.join(tmpdir, "openclaw_state")
        os.makedirs(cfg_dir)
        os.chmod(cfg_dir, 0o777)
        cfg_path = os.path.join(cfg_dir, "openclaw.json")
        with open(cfg_path, "w") as f:
            json.dump(self._make_openclaw_config(mcp_port=mcp_port, mcp_url=mcp_url, web_search=web_search, tavily_api_key=self.tavily_api_key), f, indent=2)

        import platform as _platform
        add_host_args = (
            [] if _platform.system() == "Darwin"
            else ["--add-host=host.docker.internal:host-gateway"]
        )
        network = os.environ.get("HARNESS_DOCKER_NETWORK", "")
        network_args = ["--network", network] if network else []
        proc = await asyncio.create_subprocess_exec(
            "docker", "run", "-d", "--rm",
            *add_host_args,
            *network_args,
            "-v", f"{cfg_dir}:/home/node/.openclaw",
            "-e", "HOME=/home/node",
            "-e", "OPENCLAW_HOME=/home/node",
            "-e", "OPENCLAW_STATE_DIR=/home/node/.openclaw",
            "-e", "OPENCLAW_CONFIG_PATH=/home/node/.openclaw/openclaw.json",
            "-e", f"OPENCLAW_GATEWAY_TOKEN={self.openclaw_token}",
            "-e", "OPENCLAW_ALLOW_INSECURE_PRIVATE_WS=1",
            "-e", "OPENCLAW_DISABLE_BONJOUR=1",
            *(["-e", f"TAVILY_API_KEY={self.tavily_api_key}"] if web_search and self.tavily_api_key else []),
            self.openclaw_image,
            "node", "dist/index.js", "gateway", "run",
            "--bind", "lan", "--port", str(_OPENCLAW_PORT),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=900)
        if proc.returncode != 0:
            err = stderr.decode(errors="replace")[:400]
            if is_daemon_down(err):
                raise RuntimeError("Docker daemon is not running. Start Docker and retry.")
            raise RuntimeError(f"Container failed to start: {err}")
        return stdout.decode().strip()

    async def _wait_ready(self, container_id: str, timeout: int = 120, step_cb=None) -> None:
        if step_cb:
            step_cb("status", "Waiting for agent to become ready")
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                proc = await asyncio.create_subprocess_exec(
                    "docker", "exec", container_id,
                    "curl", "-sf", f"http://localhost:{_OPENCLAW_PORT}/healthz",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                _, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
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
        raise RuntimeError(f"OpenClaw API not ready after {timeout}s\nlogs:\n{logs}")

    async def _stream_events(self, container_id: str, step_fn: Any) -> None:
        """Stream real-time tool/text events via WebSocket (Node.js client inside container)."""
        script = (
            r"""
const WebSocket = require('ws');
const ws = new WebSocket('ws://localhost:18789/');
ws.on('message', (data) => {
  const msg = JSON.parse(data);
  if (msg.event === 'connect.challenge') {
    ws.send(JSON.stringify({
      type: 'req', id: '1', method: 'connect',
      params: {
        minProtocol: 4, maxProtocol: 4,
        client: {id: 'gateway-client', version: '1.0.0', platform: 'linux', mode: 'backend'},
        auth: {token: '__TOKEN__'},
        role: 'operator', scopes: ['operator.read'], caps: ['tool-events']
      }
    }));
  } else if (msg.id === '1' && msg.ok) {
    ws.send(JSON.stringify({type: 'req', id: '2', method: 'sessions.subscribe', params: {}}));
  } else if (msg.event === 'session.tool') {
    process.stdout.write(JSON.stringify(msg.payload.data) + '\n');
  } else if (msg.event === 'agent' && msg.payload && msg.payload.stream === 'assistant') {
    process.stdout.write(JSON.stringify({stream: 'assistant', delta: msg.payload.data.delta}) + '\n');
  }
});
ws.on('error', () => process.exit(1));
"""
            .replace("__TOKEN__", self.openclaw_token)
        )
        proc = await asyncio.create_subprocess_exec(
            "docker", "exec", "-i", container_id,
            "node", "-e", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        text_buf = ""

        def flush_text() -> None:
            nonlocal text_buf
            if text_buf.strip():
                step_fn("thinking", text_buf)
            text_buf = ""

        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                phase = ev.get("phase")
                name = ev.get("name", "")
                if phase == "start":
                    flush_text()
                    step_fn("tool_call", {"name": name, "args": ev.get("args", "")})
                elif phase == "result":
                    status = "error" if ev.get("isError") else "done"
                    aggregated = ev.get("result", {}).get("details", {}).get("aggregated", "")
                    step_fn("tool_result", f"[{name}: {status}] {aggregated}".strip())
                elif ev.get("stream") == "assistant" and ev.get("delta"):
                    text_buf += ev["delta"]
            flush_text()
        except asyncio.CancelledError:
            flush_text()
        finally:
            try:
                proc.kill()
            except Exception:
                pass

    async def _stream_responses(
        self,
        container_id: str,
        req: dict,
        emit_step: Any,
        timeout: int,
    ) -> tuple[str, int, int, str]:
        """POST /v1/responses with stream=True, collect full SSE response, extract final result.

        Real-time events are handled by the parallel _stream_events WebSocket task.
        Returns (final_text, input_tokens, output_tokens, response_id).
        """
        proc = await asyncio.create_subprocess_exec(
            "docker", "exec", "-i", container_id,
            "curl", "-sf", "--no-buffer",
            "-X", "POST",
            f"http://localhost:{_OPENCLAW_PORT}/v1/responses",
            "-H", "Content-Type: application/json",
            "-H", f"Authorization: Bearer {self.openclaw_token}",
            "-H", "Accept: text/event-stream",
            "--max-time", str(timeout),
            "--data-binary", "@-",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(
            proc.communicate(input=json.dumps(req).encode()),
            timeout=timeout + 10,
        )

        final_text = ""
        input_tokens = 0
        output_tokens = 0
        response_id = ""
        text_buffer = ""

        for line in stdout.decode(errors="replace").splitlines():
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
                text_buffer += ev.get("delta", "")

            elif etype == "response.output_item.done":
                item = ev.get("item", {})
                if item.get("type") == "message":
                    if text_buffer:
                        emit_step("output", text_buffer)
                        final_text += text_buffer
                        text_buffer = ""

            elif etype == "response.completed":
                resp = ev.get("response", {})
                response_id = resp.get("id", "")
                usage = resp.get("usage", {})
                input_tokens = usage.get("input_tokens", 0) or usage.get("prompt_tokens", 0)
                output_tokens = usage.get("output_tokens", 0) or usage.get("completion_tokens", 0)
                if text_buffer:
                    emit_step("output", text_buffer)
                    final_text += text_buffer
                    text_buffer = ""
                if not final_text:
                    for item in resp.get("output", []):
                        if item.get("type") == "message":
                            text = ""
                            for part in item.get("content", []):
                                if part.get("type") == "output_text":
                                    text += part.get("text", "")
                            if text:
                                emit_step("output", text)
                                final_text += text

            elif etype == "response.failed":
                error = ev.get("response", {}).get("error", {})
                raise AgentDeadError("response_failed", str(error))

        return final_text, input_tokens, output_tokens, response_id

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
