"""Launch and manage OpenCode in Docker for one PAC1 task."""
from __future__ import annotations

import asyncio
import base64
import json
import os
import secrets
import shutil
import time
from pathlib import Path
from typing import Callable

from framework.benchmarks.pac1._utils import _read_answer
from framework.utils.work_dir import make_work_dir

_OPENCODE_PORT = 4096
_SYSTEM_PROMPT_PATH = Path(__file__).parent / "system_prompt.md"


def _build_prompt(instruction: str) -> str:
    return _SYSTEM_PROMPT_PATH.read_text(encoding="utf-8").replace("{instruction}", instruction)


def _make_opencode_config(model_id: str, base_url: str, api_key: str, approvals_off: bool = True) -> dict:
    cfg: dict = {
        "provider": {
            "bench": {
                "name": "bench",
                "type": "openai",
                "options": {
                    "baseURL": base_url,
                    "apiKey": api_key or "sk-none",
                },
                "models": {
                    model_id: {},
                },
            }
        },
        "autoshare": False,
        "agent": {"title": {"disable": True}},
    }
    if approvals_off:
        cfg["permission"] = {"*": "allow"}
    return cfg


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


async def _get_host_port(container_id: str) -> int:
    """Return the host port mapped to _OPENCODE_PORT inside the container."""
    proc = await asyncio.create_subprocess_exec(
        "docker", "port", container_id, str(_OPENCODE_PORT),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
    # output: "0.0.0.0:XXXXX" or "127.0.0.1:XXXXX"
    line = stdout.decode().strip().splitlines()[0]
    return int(line.rsplit(":", 1)[-1])


async def run_opencode(
    *,
    task_id: str,
    instruction: str,
    workspace_dir: Path,
    opencode_image: str,
    opencode_token: str = "",
    opencode_approvals_off: bool = True,
    openai_base_url: str,
    openai_api_key: str,
    model_id: str,
    agent_max_seconds: int,
    step_cb: Callable | None = None,
) -> dict:
    """Run OpenCode in Docker against workspace_dir.
    Returns dict: {message, outcome, refs, input_tokens, output_tokens}.
    """
    token = opencode_token or secrets.token_hex(24)
    auth_b64 = base64.b64encode(f"opencode:{token}".encode()).decode()
    auth_header = f"Basic {auth_b64}"

    tmpdir = make_work_dir(prefix="pac1-opencode-")
    cfg_dir = tmpdir / "opencode_cfg"
    cfg_dir.mkdir(parents=True)

    container_id: str | None = None
    input_tokens = 0
    output_tokens = 0

    try:
        cfg = _make_opencode_config(model_id, openai_base_url, openai_api_key, approvals_off=opencode_approvals_off)
        (cfg_dir / "opencode.jsonc").write_text(
            json.dumps(cfg, indent=2), encoding="utf-8"
        )

        import platform as _platform
        add_host_args = (
            [] if _platform.system() == "Darwin"
            else ["--add-host=host.docker.internal:host-gateway"]
        )
        network = os.environ.get("HARNESS_DOCKER_NETWORK", "")
        network_args = ["--network", network] if network else []
        port_args = [] if network else ["-p", f"127.0.0.1::{_OPENCODE_PORT}"]

        proc = await asyncio.create_subprocess_exec(
            "docker", "run", "-d", "--rm",
            *add_host_args,
            *network_args,
            *port_args,
            "-v", f"{workspace_dir}:/workspace",
            "-v", f"{cfg_dir}:/workspace/.opencode",
            "-e", f"OPENCODE_SERVER_PASSWORD={token}",
            opencode_image,
            "serve", "--hostname", "0.0.0.0", "--port", str(_OPENCODE_PORT),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        if proc.returncode != 0:
            raise RuntimeError(f"docker run opencode failed: {stderr.decode()[:400]}")
        container_id = stdout.decode().strip()

        if network:
            container_ip = await _get_container_ip(container_id, network)
            base_url = f"http://{container_ip}:{_OPENCODE_PORT}"
        else:
            host_port = await _get_host_port(container_id)
            base_url = f"http://127.0.0.1:{host_port}"

        if step_cb:
            step_cb("status", f"OpenCode container {container_id[:12]} starting…")

        await _wait_ready(base_url, auth_header, timeout=120)

        import httpx
        async with httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": auth_header},
            timeout=30,
        ) as client:
            resp = await client.post(
                "/session",
                json={},
                headers={"x-opencode-directory": "/workspace"},
            )
            resp.raise_for_status()
            session_id = resp.json()["id"]

        if step_cb:
            step_cb("status", "OpenCode running task…")

        stream_task = asyncio.create_task(
            _stream_events(base_url, auth_header, step_cb)
        )

        prompt = _build_prompt(instruction)
        try:
            async with httpx.AsyncClient(
                base_url=base_url,
                headers={"Authorization": auth_header},
                timeout=agent_max_seconds + 30,
            ) as client:
                raw = await asyncio.wait_for(
                    client.post(
                        f"/session/{session_id}/message",
                        json={
                            "providerID": "bench",
                            "modelID": model_id,
                            "parts": [{"type": "text", "text": prompt}],
                        },
                        headers={"x-opencode-directory": "/workspace"},
                    ),
                    timeout=agent_max_seconds + 30,
                )
            raw.raise_for_status()
            resp_data = raw.json()
            tokens = resp_data.get("info", {}).get("tokens", {})
            input_tokens = tokens.get("input", 0)
            output_tokens = tokens.get("output", 0)
        except asyncio.TimeoutError:
            if step_cb:
                step_cb("status", f"⏱ Timeout after {agent_max_seconds}s")
            answer = _read_answer(workspace_dir, task_id)
            answer["input_tokens"] = input_tokens
            answer["output_tokens"] = output_tokens
            return answer
        finally:
            stream_task.cancel()
            try:
                await stream_task
            except asyncio.CancelledError:
                pass

    except asyncio.TimeoutError:
        if step_cb:
            step_cb("status", f"⏱ Timeout after {agent_max_seconds}s")
        return {
            "message": f"Agent timed out after {agent_max_seconds}s",
            "outcome": "OUTCOME_ERR_INTERNAL",
            "refs": [],
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        }
    finally:
        if container_id:
            await _stop_container(container_id)
        shutil.rmtree(tmpdir, ignore_errors=True)

    answer = _read_answer(workspace_dir, task_id)
    answer["input_tokens"] = input_tokens
    answer["output_tokens"] = output_tokens
    if step_cb:
        step_cb("output", answer["message"])
    return answer


async def _wait_ready(base_url: str, auth_header: str, timeout: int = 120) -> None:
    import httpx
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(
                    f"{base_url}/global/health",
                    headers={"Authorization": auth_header},
                )
                if resp.status_code in (200, 204, 401):
                    return
        except Exception:
            pass
        await asyncio.sleep(2)
    raise RuntimeError(f"OpenCode API not ready after {timeout}s")


async def _stop_container(container_id: str) -> None:
    proc = await asyncio.create_subprocess_exec(
        "docker", "stop", container_id,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        await asyncio.wait_for(proc.communicate(), timeout=15)
    except asyncio.TimeoutError:
        pass


async def _stream_events(
    base_url: str,
    auth_header: str,
    step_cb: Callable | None,
) -> None:
    """Stream SSE from /event; call step_cb for tool/text events."""
    if not step_cb:
        return

    import httpx
    text_buf = ""

    def flush_text() -> None:
        nonlocal text_buf
        if text_buf.strip():
            step_cb("thinking", text_buf)
        text_buf = ""

    try:
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream(
                "GET",
                f"{base_url}/event?directory=/workspace",
                headers={
                    "Authorization": auth_header,
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
                                    step_cb("tool_call", {
                                        "name": tool_name,
                                        "args": state.get("input", ""),
                                    })
                                elif status in ("completed", "error"):
                                    out = state.get("output", "") or state.get("error", "")
                                    step_cb("tool_result",
                                            f"[{tool_name}: {status}] {str(out)[:500]}")

                        elif ev_type == "message.part.delta":
                            text_buf += props.get("delta", "")

                        elif ev_type == "session.error":
                            flush_text()
                            step_cb("status", f"Session error: {props.get('error', 'unknown')}")

                flush_text()

    except asyncio.CancelledError:
        flush_text()
    except Exception:
        flush_text()
