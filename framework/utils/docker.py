"""Docker availability check used as a run preflight.

Surfaces a clear, actionable message when the Docker CLI is missing or the
daemon is not running, instead of letting every task fail with a cryptic
"docker pull ... failed". Messages are English and emoji-free by convention.
"""
from __future__ import annotations

import asyncio


class DockerUnavailableError(RuntimeError):
    """Raised by the preflight when Docker is required but unavailable."""


def is_daemon_down(text: str) -> bool:
    """Heuristic: does this docker stderr indicate the daemon is not running?"""
    t = (text or "").lower()
    return (
        "cannot connect to the docker daemon" in t
        or "is the docker daemon running" in t
        or "error during connect" in t          # Windows named-pipe variant
        or "docker daemon is not running" in t
    )


async def docker_status(timeout: int = 10) -> tuple[bool, str]:
    """Return (ok, message).

    ok=True  -> Docker daemon is reachable; message is a short info string.
    ok=False -> message explains the problem (CLI missing / daemon down).
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "info", "--format", "{{.ServerVersion}}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return False, "Docker CLI not found on PATH. Install Docker and retry."

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return False, f"Docker did not respond within {timeout}s. Is the Docker daemon running?"

    if proc.returncode == 0:
        version = stdout.decode(errors="replace").strip()
        return True, f"Docker daemon is running (server {version})" if version else "Docker daemon is running"

    err = stderr.decode(errors="replace").strip()
    if is_daemon_down(err):
        return False, "Docker daemon is not running. Start Docker and retry."
    return False, f"Docker is not available: {err[:200]}" if err else "Docker is not available."
