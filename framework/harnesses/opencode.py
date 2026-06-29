"""OpenCode agent harness — runs ghcr.io/anomalyco/opencode in Docker, communicates via HTTP + SSE."""
from __future__ import annotations

import asyncio
import base64
import json
import os
import secrets
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

_OPENCODE_PORT = 4096


class OpenCodeConfig(BaseModel):
    opencode_image: str = "ghcr.io/anomalyco/opencode:latest"
    opencode_token: str = ""  # server password; auto-generated if empty
    web_search: bool = False


class OpenCodeHarness(Harness):
    type = "opencode"
    config_model = OpenCodeConfig
    supports_sandbox = True

    def __init__(
        self,
        model_cfg: "ModelConfig",
        opencode_image: str = "ghcr.io/anomalyco/opencode:latest",
        opencode_token: str = "",
        web_search: bool = False,
        **_: Any,
    ) -> None:
        super().__init__(model_cfg)
        self.opencode_image = opencode_image
        self.opencode_token = opencode_token or secrets.token_hex(24)
        self.web_search = web_search
        auth_b64 = base64.b64encode(f"opencode:{self.opencode_token}".encode()).decode()
        self._auth_header = f"Basic {auth_b64}"

    async def send_turn(
        self,
        messages: list[dict],
        tools: list[dict],
        system_prompt: str,
        ctx: "ExecutionContext",
        timeout: int = 120,
        continue_session: bool = True,
    ) -> "TurnResponse":
        """Send one turn to OpenCode.

        Lazily starts the container on the first call. Uses a single session
        for turn chaining (continue_session=True) or a fresh session per call (False).
        """
        from framework.runners.base import TurnResponse
        from framework.models import Step

        session = ctx.extras.setdefault("harness_session", {})
        container_id = session.get("container_id")

        if container_id is None:
            _status = ctx.step_cb
            await self._ensure_image(step_cb=_status)
            tmpdir = str(make_work_dir(prefix="opencode_turn_"))
            mcp_port = ctx.extras.get("bfcl_mcp_port")
            mcp_url = (
                ctx.mcp_url or
                (f"http://{get_mcp_host()}:{mcp_port}" if mcp_port else None)
            )

            _status and _status("status", "Starting container")
            container_id, base_url = await self._start_container(tmpdir, mcp_url=mcp_url)
            _status and _status("status", f"Container started ({container_id[:12]})")
            await self._wait_ready(base_url, timeout=900, step_cb=_status)

            session["container_id"] = container_id
            session["base_url"] = base_url
            session["tmpdir"] = tmpdir
            session["session_id"] = None
            ctx.extras["harness_session"] = session

            _cid = container_id
            _td = tmpdir

            async def _cleanup() -> None:
                await self._stop_container(_cid)
                shutil.rmtree(_td, ignore_errors=True)

            ctx.cleanup_fns.append(_cleanup)

        base_url: str = session["base_url"]

        if not continue_session or session.get("session_id") is None:
            session["session_id"] = await self._create_session(base_url)
            ctx.extras["harness_session"] = session

        session_id: str = session["session_id"]

        user_msg = next(
            (m["content"] for m in reversed(messages) if m["role"] == "user"), ""
        )
        prompt = f"[System]\n{system_prompt}\n\n{user_msg}" if system_prompt else user_msg

        step_cb = ctx.step_cb
        step_list: list[Step] = []

        step_list.append(Step(type="input", content=messages))
        if step_cb:
            step_cb("input", messages)

        def _emit_step(stype: str, content: Any) -> None:
            step_list.append(Step(type=stype, content=content))
            if step_cb:
                step_cb(stype, content)

        stream_task = asyncio.create_task(self._stream_events(base_url, _emit_step))
        try:
            resp_data, in_tok, out_tok = await asyncio.wait_for(
                self._send_message(base_url, session_id, prompt),
                timeout=timeout + 30,
            )
        finally:
            stream_task.cancel()
            try:
                await stream_task
            except asyncio.CancelledError:
                pass

        text = self._parse_response(resp_data, _emit_step)
        return TurnResponse(
            text=text,
            tool_calls=[],
            finish_reason="stop",
            input_tokens=in_tok,
            output_tokens=out_tok,
            steps=step_list,
        )

    @staticmethod
    def _parse_response(resp: dict, step_fn: Any) -> str:
        final_text = ""
        for part in resp.get("parts", []):
            if part.get("type") == "text":
                text = part.get("text", "")
                if text:
                    step_fn("output", text)
                    final_text += text
        return final_text

    @staticmethod
    def _build_prompt(task: "Task") -> str:
        parts = []
        if task.system_prompt:
            parts.append(f"[System]\n{task.system_prompt}")
        for m in task.messages:
            parts.append(m.content)
        return "\n\n".join(parts)

    def _make_opencode_config(self, mcp_url: str | None = None) -> dict:
        cfg: dict = {
            "provider": {
                "bench": {
                    "name": "bench",
                    "type": "openai",
                    "options": {
                        "baseURL": self.model_cfg.base_url,
                        "apiKey": self.model_cfg.api_key or "sk-none",
                    },
                    "models": {
                        self.model_cfg.model_name: (
                            {"options": {"reasoningEffort": self.model_cfg.reasoning_effort}}
                            if self.model_cfg.reasoning_effort else {}
                        ),
                    },
                }
            },
            "autoshare": False,
            "agent": {"title": {"disable": True}},
        }
        if mcp_url:
            cfg["mcp"] = {
                "sandbox-bridge": {
                    "type": "remote",
                    "url": mcp_url,
                }
            }
            cfg["permission"] = {
                "*": "deny",
                "sandbox-bridge_*": "allow",
            }
        elif not self.web_search:
            cfg["permission"] = {
                "websearch": "deny",
                "webfetch": "deny",
            }
        return cfg

    async def _ensure_image(self, step_cb=None) -> None:
        def _s(msg: str) -> None:
            if step_cb:
                step_cb("status", msg)
        _s(f"Inspecting image {self.opencode_image}")
        check = await asyncio.create_subprocess_exec(
            "docker", "image", "inspect", self.opencode_image,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await check.communicate()
        if check.returncode == 0:
            return
        _s(f"Image not found locally, pulling {self.opencode_image}")
        proc = await asyncio.create_subprocess_exec(
            "docker", "pull", self.opencode_image,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=900)
        if proc.returncode != 0:
            err = stderr.decode(errors="replace").strip()
            if is_daemon_down(err):
                raise RuntimeError("Docker daemon is not running. Start Docker and retry.")
            raise RuntimeError(f"Image pull failed for {self.opencode_image}: {err[:300]}")
        _s("Image pulled")

    async def _start_container(
        self,
        tmpdir: str,
        mcp_url: str | None = None,
    ) -> tuple[str, str]:
        """Start container; returns (container_id, base_url)."""
        import os
        cfg_dir = os.path.join(tmpdir, "opencode_cfg")
        workspace_dir = os.path.join(tmpdir, "workspace")
        os.makedirs(cfg_dir)
        os.makedirs(workspace_dir)

        cfg_path = os.path.join(cfg_dir, "opencode.jsonc")
        with open(cfg_path, "w") as f:
            json.dump(self._make_opencode_config(mcp_url=mcp_url), f, indent=2)

        import platform as _platform
        add_host_args = (
            [] if _platform.system() == "Darwin"
            else ["--add-host=host.docker.internal:host-gateway"]
        )
        network = os.environ.get("HARNESS_DOCKER_NETWORK", "")
        network_args = ["--network", network] if network else []

        # In Docker mode: connect via container IP on the shared network (no host port mapping).
        # Locally: publish to 127.0.0.1 and use docker port to find the mapped port.
        port_args = [] if network else ["-p", f"127.0.0.1::{_OPENCODE_PORT}"]

        proc = await asyncio.create_subprocess_exec(
            "docker", "run", "-d", "--rm",
            *add_host_args,
            *network_args,
            *port_args,
            "-v", f"{workspace_dir}:/workspace",
            "-v", f"{cfg_dir}:/workspace/.opencode",
            "-e", f"OPENCODE_SERVER_PASSWORD={self.opencode_token}",
            self.opencode_image,
            "serve", "--hostname", "0.0.0.0", "--port", str(_OPENCODE_PORT),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=900)
        if proc.returncode != 0:
            err = stderr.decode(errors="replace")[:400]
            if is_daemon_down(err):
                raise RuntimeError("Docker daemon is not running. Start Docker and retry.")
            raise RuntimeError(f"Container failed to start: {err}")
        container_id = stdout.decode().strip()

        if network:
            container_ip = await self._get_container_ip(container_id, network)
            return container_id, f"http://{container_ip}:{_OPENCODE_PORT}"
        else:
            host_port = await self._get_host_port(container_id)
            return container_id, f"http://127.0.0.1:{host_port}"

    @staticmethod
    async def _get_container_ip(container_id: str, network: str) -> str:
        proc = await asyncio.create_subprocess_exec(
            "docker", "inspect",
            "--format", f'{{{{(index .NetworkSettings.Networks "{network}").IPAddress}}}}',
            container_id,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
        return stdout.decode().strip()

    @staticmethod
    async def _get_host_port(container_id: str) -> int:
        proc = await asyncio.create_subprocess_exec(
            "docker", "port", container_id, str(_OPENCODE_PORT),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=900)
        line = stdout.decode().strip().splitlines()[0]
        return int(line.rsplit(":", 1)[-1])

    async def _wait_ready(self, base_url: str, timeout: int = 120, step_cb=None) -> None:
        import httpx
        if step_cb:
            step_cb("status", "Waiting for agent to become ready")
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                async with httpx.AsyncClient(timeout=5) as client:
                    resp = await client.get(
                        f"{base_url}/global/health",
                        headers={"Authorization": self._auth_header},
                    )
                    if resp.status_code in (200, 204):
                        if step_cb:
                            step_cb("status", "Agent is ready")
                        return
                    # 401 with www-authenticate means server is up but auth failed;
                    # shouldn't happen with correct token but still means server is ready
                    if resp.status_code == 401:
                        if step_cb:
                            step_cb("status", "Agent is ready")
                        return
            except Exception:
                pass
            await asyncio.sleep(2)
        raise RuntimeError(f"OpenCode API not ready after {timeout}s")

    async def _create_session(self, base_url: str) -> str:
        import httpx
        async with httpx.AsyncClient(timeout=900) as client:
            resp = await client.post(
                f"{base_url}/session",
                json={},
                headers={
                    "Authorization": self._auth_header,
                    "x-opencode-directory": "/workspace",
                },
            )
            resp.raise_for_status()
            return resp.json()["id"]

    async def _send_message(
        self,
        base_url: str,
        session_id: str,
        prompt: str,
    ) -> tuple[dict, int, int]:
        import httpx
        async with httpx.AsyncClient(timeout=None) as client:
            resp = await client.post(
                f"{base_url}/session/{session_id}/message",
                json={
                    "providerID": "bench",
                    "modelID": self.model_cfg.model_name,
                    "parts": [{"type": "text", "text": prompt}],
                },
                headers={
                    "Authorization": self._auth_header,
                    "x-opencode-directory": "/workspace",
                },
            )
            # OpenCode serve-mode returns the assistant message even when the LLM
            # call died — death is signalled by info.error (HTTP 200), not an HTTP
            # status. A catastrophic server error surfaces as 5xx instead. Treat both
            # as agent death so the orchestrator aborts the task (grade AGENT_DEAD)
            # rather than scoring an empty/partial answer as a wrong response.
            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as e:
                raise AgentDeadError("http_error", f"{e.response.status_code} from /message")
            data = resp.json()
            err = (data.get("info") or {}).get("error")
            if err:
                name = err.get("name", "error") if isinstance(err, dict) else str(err)
                msg = ""
                if isinstance(err, dict):
                    msg = (err.get("data") or {}).get("message", "") if isinstance(err.get("data"), dict) else ""
                raise AgentDeadError(name, msg)
            tokens = data.get("info", {}).get("tokens", {})
            return data, tokens.get("input", 0), tokens.get("output", 0)

    async def _stream_events(self, base_url: str, step_fn: Any) -> None:
        """Stream SSE from /event; call step_fn for tool/text events."""
        import httpx
        text_buf = ""

        def flush_text() -> None:
            nonlocal text_buf
            if text_buf.strip():
                step_fn("thinking", text_buf)
            text_buf = ""

        try:
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream(
                    "GET",
                    f"{base_url}/event?directory=/workspace",
                    headers={
                        "Authorization": self._auth_header,
                        "Accept": "text/event-stream",
                    },
                ) as response:
                    buf = ""
                    async for chunk in response.aiter_text():
                        buf += chunk
                        while "\n\n" in buf:
                            block, buf = buf.split("\n\n", 1)
                            data_line = ""
                            for line in block.splitlines():
                                if line.startswith("data:"):
                                    data_line = line[5:].strip()
                            if not data_line:
                                continue
                            try:
                                ev = json.loads(data_line)
                            except Exception:
                                continue

                            ev_type = ev.get("type", "")
                            props = ev.get("properties", {})

                            if ev_type == "message.part.updated":
                                part = props.get("part", {})
                                if part.get("type") == "tool":
                                    state = part.get("state", {})
                                    status = state.get("status", "")
                                    tool_name = part.get("tool", "")
                                    if status == "running":
                                        flush_text()
                                        step_fn("tool_call", {
                                            "name": tool_name,
                                            "args": state.get("input", ""),
                                        })
                                    elif status in ("completed", "error"):
                                        out = state.get("output", "") or state.get("error", "")
                                        step_fn("tool_result",
                                                f"[{tool_name}: {status}] {str(out)[:500]}")

                            elif ev_type == "message.part.delta":
                                delta = props.get("delta", "")
                                if delta:
                                    text_buf += delta

                            elif ev_type == "session.error":
                                flush_text()
                                step_fn("status", f"Session error: {props.get('error', 'unknown')}")

                    flush_text()

        except asyncio.CancelledError:
            flush_text()
        except Exception:
            flush_text()

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
