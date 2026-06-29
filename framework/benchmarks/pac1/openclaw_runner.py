"""Launch and manage OpenClaw in Docker for one PAC1 task."""
from __future__ import annotations

import asyncio
import json
import secrets
import shutil
import time
from pathlib import Path
from typing import Callable

from framework.benchmarks.pac1._utils import _read_answer
from framework.utils.work_dir import make_work_dir

_OPENCLAW_PORT = 18789
_SYSTEM_PROMPT_PATH = Path(__file__).parent / "system_prompt.md"


def _build_input(instruction: str) -> str:
    template = _SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
    return template.replace("{instruction}", instruction)


async def run_openclaw(
    *,
    task_id: str,
    instruction: str,
    workspace_dir: Path,
    openclaw_image: str,
    openclaw_token: str,
    openclaw_approvals_off: bool = True,
    openai_base_url: str,
    openai_api_key: str,
    model_id: str,
    agent_max_seconds: int,
    step_cb: Callable | None = None,
) -> dict:
    """
    Run OpenClaw in Docker against workspace_dir.
    Returns dict: {message, outcome, refs, input_tokens, output_tokens}.
    """
    token = openclaw_token or secrets.token_hex(24)
    input_tokens = 0
    output_tokens = 0

    tmpdir = make_work_dir(prefix="pac1-openclaw-")
    cfg_dir = tmpdir / "openclaw_state"
    cfg_dir.mkdir(parents=True)
    cfg_dir.chmod(0o777)

    container_id: str | None = None
    try:
        cfg = _make_openclaw_config(
            model_id=model_id,
            base_url=openai_base_url,
            api_key=openai_api_key,
            token=token,
            approvals_off=openclaw_approvals_off,
        )
        (cfg_dir / "openclaw.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")

        import platform as _platform
        add_host_args = (
            [] if _platform.system() == "Darwin"
            else ["--add-host=host.docker.internal:host-gateway"]
        )

        proc = await asyncio.create_subprocess_exec(
            "docker", "run", "-d", "--rm",
            *add_host_args,
            "-v", f"{workspace_dir}:/workspace",
            "-v", f"{cfg_dir}:/home/node/.openclaw",
            "-e", "HOME=/home/node",
            "-e", "OPENCLAW_HOME=/home/node",
            "-e", "OPENCLAW_STATE_DIR=/home/node/.openclaw",
            "-e", "OPENCLAW_CONFIG_PATH=/home/node/.openclaw/openclaw.json",
            "-e", f"OPENCLAW_GATEWAY_TOKEN={token}",
            "-e", "OPENCLAW_ALLOW_INSECURE_PRIVATE_WS=1",
            "-e", "OPENCLAW_DISABLE_BONJOUR=1",
            openclaw_image,
            "node", "dist/index.js", "gateway", "run",
            "--bind", "lan", "--port", str(_OPENCLAW_PORT),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        if proc.returncode != 0:
            raise RuntimeError(f"docker run openclaw failed: {stderr.decode()[:400]}")
        container_id = stdout.decode().strip()

        if step_cb:
            step_cb("status", f"OpenClaw container {container_id[:12]} starting...")

        await _wait_ready(container_id, timeout=120)

        req = {
            "model": "openclaw",
            "input": _build_input(instruction),
            "store": False,
        }

        if step_cb:
            step_cb("status", "OpenClaw running task...")

        log_task = asyncio.create_task(_stream_events(container_id, token, step_cb))
        try:
            raw = await asyncio.wait_for(
                _exec_curl(container_id, [
                    "-X", "POST",
                    f"http://localhost:{_OPENCLAW_PORT}/v1/responses",
                    "-H", "Content-Type: application/json",
                    "-H", f"Authorization: Bearer {token}",
                    "--data-binary", "@-",
                ], stdin_data=json.dumps(req).encode(), timeout=agent_max_seconds),
                timeout=agent_max_seconds + 30,
            )
        finally:
            log_task.cancel()
            try:
                await log_task
            except asyncio.CancelledError:
                pass

        resp = json.loads(raw)
        usage = resp.get("usage", {})
        input_tokens = usage.get("input_tokens", 0) or usage.get("prompt_tokens", 0)
        output_tokens = usage.get("output_tokens", 0) or usage.get("completion_tokens", 0)

        final_text = ""
        for item in resp.get("output", []):
            if item.get("type") == "message":
                for part in item.get("content", []):
                    if part.get("type") == "output_text":
                        final_text += part.get("text", "")

        if step_cb and final_text:
            step_cb("output", final_text)

    except asyncio.TimeoutError:
        if step_cb:
            step_cb("status", f"Timeout after {agent_max_seconds}s")
        answer = {
            "message": f"Agent timed out after {agent_max_seconds}s",
            "outcome": "OUTCOME_ERR_INTERNAL",
            "refs": [],
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        }
        return answer
    finally:
        if container_id:
            await _stop_container(container_id)
        shutil.rmtree(tmpdir, ignore_errors=True)

    # Remove OpenClaw internal state dir from workspace before sync_back
    openclaw_state = workspace_dir / ".openclaw"
    if openclaw_state.exists():
        shutil.rmtree(openclaw_state, ignore_errors=True)

    answer = _read_answer(workspace_dir, task_id)
    answer["input_tokens"] = input_tokens
    answer["output_tokens"] = output_tokens
    if step_cb:
        step_cb("output", answer["message"])
    return answer


def _make_openclaw_config(
    model_id: str,
    base_url: str,
    api_key: str,
    token: str,
    approvals_off: bool = True,
) -> dict:
    gateway: dict = {
        "mode": "local",
        "bind": "lan",
        "port": _OPENCLAW_PORT,
        "auth": {
            "mode": "token",
            "token": token,
        },
        "controlUi": {"enabled": False},
        "http": {
            "endpoints": {
                "responses": {"enabled": True},
            }
        },
    }
    return {
        "gateway": gateway,
        "models": {
            "providers": {
                "bench": {
                    "baseUrl": base_url,
                    "apiKey": api_key or "sk-none",
                    "api": "openai-completions",
                    "models": [{"id": model_id, "name": model_id}],
                }
            }
        },
        "agents": {
            "defaults": {
                "model": f"bench/{model_id}",
                "workspace": "/workspace",
                "contextInjection": "never",
                "skipBootstrap": True,
                "timeoutSeconds": 900,
            },
        },
        "tools": {
            "exec": {
                "ask": "off" if approvals_off else "on-miss",
            },
            "deny": [
                "browser", "canvas", "cron", "file_fetch", "file_write",
                "gateway", "message", "nodes", "process", "session_status",
                "sessions_history", "sessions_list", "sessions_send",
                "sessions_spawn", "sessions_yield", "subagents", "tts",
                "web_fetch", "web_search", "dir_fetch", "dir_list",
                "memory_get", "memory_search",
            ],
        },
    }


async def _wait_ready(container_id: str, timeout: int = 120) -> None:
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
                return
        except Exception:
            pass
        await asyncio.sleep(2)
    raise RuntimeError(f"OpenClaw API not ready after {timeout}s ({container_id[:12]})")


async def _exec_curl(
    container_id: str,
    args: list[str],
    stdin_data: bytes | None = None,
    timeout: int = 60,
) -> bytes:
    proc = await asyncio.create_subprocess_exec(
        "docker", "exec", "-i", container_id,
        "curl", "-s", "--fail-with-body", *args,
        stdin=asyncio.subprocess.PIPE if stdin_data is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(
        proc.communicate(input=stdin_data), timeout=timeout
    )
    if proc.returncode != 0:
        body = stdout.decode(errors="replace")[:400]
        err = stderr.decode(errors="replace")[:200]
        raise RuntimeError(f"curl failed (rc={proc.returncode}): {body or err}")
    return stdout


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
    container_id: str,
    token: str,
    step_cb: Callable | None,
) -> None:
    """Stream tool/text events via WebSocket (Node.js client inside container)."""
    if not step_cb:
        return
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
        .replace("__TOKEN__", token)
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
            step_cb("thinking", text_buf)
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
                step_cb("tool_call", {"name": name, "args": ev.get("args", "")})
            elif phase == "result":
                status = "error" if ev.get("isError") else "done"
                aggregated = ev.get("result", {}).get("details", {}).get("aggregated", "")
                step_cb("tool_result", f"[{name}: {status}] {aggregated}".strip())
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
