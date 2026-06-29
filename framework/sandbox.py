"""Unified sandbox abstraction.

Sandbox implementations register themselves via @register_sandbox("type").
The orchestrator creates the right one via make_sandbox(spec).

Built-in implementations:
  docker_run      — DockerRunSandbox (single container)
  docker_compose  — DockerComposeSandbox (multi-service stack)
  swe_bench       — SWEbenchSandbox (registered in swe_bench/sandbox.py)
"""
from __future__ import annotations

import asyncio
import fnmatch
import io
import json
import logging
import tarfile
import uuid
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from framework.models import Checkpoint, EvalResult, SandboxSpec, SandboxTool

logger = logging.getLogger(__name__)

# Directory inside every sandbox where tool code is injected.
_TOOLS_ROOT = "/.sandbox_tools"


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_SANDBOX_REGISTRY: dict[str, type["Sandbox"]] = {}


def register_sandbox(name: str):
    """Class decorator: register a Sandbox implementation under a type name."""
    def decorator(cls):
        _SANDBOX_REGISTRY[name] = cls
        return cls
    return decorator


# ---------------------------------------------------------------------------
# ABC
# ---------------------------------------------------------------------------

class Sandbox(ABC):
    """Minimal interface the orchestrator and MCP bridge interact with."""

    @abstractmethod
    async def start(self) -> None:
        """Start the container(s). Pull image if needed."""

    @abstractmethod
    async def exec_bash(self, cmd: str, timeout: int = 60) -> tuple[str, str, int]:
        """Run a bash command. Returns (stdout, stderr, exit_code)."""

    @abstractmethod
    async def exec_stdin(
        self,
        runner: str,
        path: str,
        stdin_data: bytes,
        timeout: int = 60,
    ) -> tuple[str, str, int]:
        """Run `runner path` with stdin_data piped to stdin.
        Returns (stdout, stderr, exit_code).
        """

    @abstractmethod
    async def read_file(self, path: str) -> bytes:
        """Read a file from inside the sandbox. Returns raw bytes."""

    @abstractmethod
    async def write_file(self, content: bytes, path: str) -> None:
        """Write bytes to path inside the sandbox."""

    @abstractmethod
    async def exec_cmd(self, args: list[str], timeout: int = 60) -> tuple[str, str, int]:
        """Run `docker exec container args[0] args[1] ...`. Returns (stdout, stderr, exit_code)."""

    async def inject_tools(self, tools: list["SandboxTool"]) -> None:
        """Copy tool directories into /.sandbox_tools/<name>/ and run install_cmd."""
        if not tools:
            return
        await self.exec_bash(f"mkdir -p {_TOOLS_ROOT}", timeout=15)
        for tool in tools:
            dest = f"{_TOOLS_ROOT}/{tool.name}"
            await self.exec_bash(f"mkdir -p {dest}", timeout=10)
            tar_data = _make_tar(tool.source_dir, tool.exclude)
            await self._inject_tar(tar_data, dest)
            if tool.install_cmd:
                _, stderr, rc = await self.exec_bash(
                    f"cd {dest} && {tool.install_cmd}", timeout=120
                )
                if rc != 0:
                    logger.warning(
                        "install_cmd for tool %r exited %d: %s", tool.name, rc, stderr[:200]
                    )

    @abstractmethod
    async def _inject_tar(self, tar_data: bytes, dest_path: str) -> None:
        """Extract tar_data (bytes of a tar archive) into dest_path inside the sandbox."""

    @abstractmethod
    async def stop(self) -> None:
        """Stop and remove container(s)."""


# ---------------------------------------------------------------------------
# DockerRunSandbox
# ---------------------------------------------------------------------------

@register_sandbox("docker_run")
class DockerRunSandbox(Sandbox):
    """Single container started with `docker run -d image sleep infinity`."""

    def __init__(self, spec: "SandboxSpec") -> None:
        self._spec = spec
        self._container_id: str | None = None

    async def start(self) -> None:
        image = self._spec.image
        if not image:
            raise ValueError("SandboxSpec.image must be set for docker_run")

        if self._spec.pull:
            check = await asyncio.create_subprocess_exec(
                "docker", "image", "inspect", image,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await check.communicate()
            if check.returncode != 0:
                logger.info("Pulling sandbox image %s", image)
                pull = await asyncio.create_subprocess_exec(
                    "docker", "pull", image,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, stderr = await asyncio.wait_for(pull.communicate(), timeout=600)
                if pull.returncode != 0:
                    raise RuntimeError(f"docker pull {image} failed: {stderr.decode()[:300]}")

        proc = await asyncio.create_subprocess_exec(
            "docker", "run", "-d", "--rm",
            f"--name=sandbox_{uuid.uuid4().hex[:8]}",
            image, "sleep", "infinity",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        if proc.returncode != 0:
            raise RuntimeError(f"docker run failed: {stderr.decode()[:300]}")
        self._container_id = stdout.decode().strip()
        logger.info("DockerRunSandbox started: %s", self._container_id[:12])

    async def exec_bash(self, cmd: str, timeout: int = 60) -> tuple[str, str, int]:
        if not self._container_id:
            return "", "[sandbox not started]", -1
        proc = await asyncio.create_subprocess_exec(
            "docker", "exec", self._container_id,
            "bash", "-c", cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return (
                stdout.decode(errors="replace"),
                stderr.decode(errors="replace"),
                proc.returncode or 0,
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
                await proc.communicate()
            except Exception:
                pass
            return "", f"[bash timeout after {timeout}s]", -1

    async def exec_stdin(
        self,
        runner: str,
        path: str,
        stdin_data: bytes,
        timeout: int = 60,
    ) -> tuple[str, str, int]:
        if not self._container_id:
            return "", "[sandbox not started]", -1
        proc = await asyncio.create_subprocess_exec(
            "docker", "exec", "-i", self._container_id,
            runner, path,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=stdin_data), timeout=timeout
            )
            return (
                stdout.decode(errors="replace"),
                stderr.decode(errors="replace"),
                proc.returncode or 0,
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
                await proc.communicate()
            except Exception:
                pass
            return "", f"[exec_stdin timeout after {timeout}s]", -1

    async def read_file(self, path: str) -> bytes:
        if not self._container_id:
            return b""
        proc = await asyncio.create_subprocess_exec(
            "docker", "exec", self._container_id, "cat", path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
            return out if proc.returncode == 0 else b""
        except asyncio.TimeoutError:
            return b""

    async def exec_cmd(self, args: list[str], timeout: int = 60) -> tuple[str, str, int]:
        if not self._container_id:
            return "", "[sandbox not started]", -1
        proc = await asyncio.create_subprocess_exec(
            "docker", "exec", self._container_id, *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return (
                stdout.decode(errors="replace"),
                stderr.decode(errors="replace"),
                proc.returncode or 0,
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
                await proc.communicate()
            except Exception:
                pass
            return "", f"[exec_cmd timeout after {timeout}s]", -1

    async def write_file(self, content: bytes, path: str) -> None:
        if not self._container_id:
            return
        parent = str(Path(path).parent)
        if parent and parent != "/":
            await self.exec_bash(f"mkdir -p '{parent}'", timeout=10)
        proc = await asyncio.create_subprocess_exec(
            "docker", "exec", "-i", self._container_id,
            "bash", "-c", f"cat > '{path}'",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.communicate(input=content), timeout=30)

    async def _inject_tar(self, tar_data: bytes, dest_path: str) -> None:
        proc = await asyncio.create_subprocess_exec(
            "docker", "exec", "-i", self._container_id,
            "tar", "-xf", "-", "-C", dest_path,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(input=tar_data), timeout=60)
        if proc.returncode != 0:
            raise RuntimeError(f"tar injection failed: {stderr.decode()[:200]}")

    async def stop(self) -> None:
        if not self._container_id:
            return
        proc = await asyncio.create_subprocess_exec(
            "docker", "rm", "-f", self._container_id,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(proc.communicate(), timeout=30)
        except asyncio.TimeoutError:
            pass
        self._container_id = None


# ---------------------------------------------------------------------------
# DockerComposeSandbox
# ---------------------------------------------------------------------------

@register_sandbox("docker_compose")
class DockerComposeSandbox(Sandbox):
    """Multi-service stack started with `docker compose up`."""

    def __init__(self, spec: "SandboxSpec") -> None:
        self._spec = spec
        self._project = f"sandbox_{uuid.uuid4().hex[:8]}"
        self._container_id: str | None = None

    async def start(self) -> None:
        compose_file = self._spec.compose_file
        if not compose_file:
            raise ValueError("SandboxSpec.compose_file must be set for docker_compose")

        proc = await asyncio.create_subprocess_exec(
            "docker", "compose", "-f", compose_file, "-p", self._project,
            "up", "-d", "--build",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
        if proc.returncode != 0:
            raise RuntimeError(f"docker compose up failed: {stderr.decode()[:400]}")

        # Resolve target service → container ID
        target = self._spec.target_service
        if not target:
            target = await self._auto_detect_service(compose_file)

        ps = await asyncio.create_subprocess_exec(
            "docker", "compose", "-f", compose_file, "-p", self._project,
            "ps", "-q", target,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await asyncio.wait_for(ps.communicate(), timeout=30)
        cid = out.decode().strip().split("\n")[0].strip()
        if not cid:
            raise RuntimeError(
                f"Could not find container for service '{target}'. "
                "Set SandboxSpec.target_service to the service name that receives tool calls."
            )
        self._container_id = cid
        logger.info("DockerComposeSandbox started, target service '%s': %s", target, cid[:12])

    async def _auto_detect_service(self, compose_file: str) -> str:
        """Return the single service name, or raise if there are multiple."""
        proc = await asyncio.create_subprocess_exec(
            "docker", "compose", "-f", compose_file,
            "config", "--services",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        services = [s for s in out.decode().splitlines() if s.strip()]
        if len(services) == 1:
            return services[0]
        raise ValueError(
            f"compose file has {len(services)} services {services} but "
            "SandboxSpec.target_service is not set. "
            "Specify which service should receive tool calls and checkpoints."
        )

    # Delegate exec methods to DockerRunSandbox logic (same docker exec commands)
    async def exec_bash(self, cmd: str, timeout: int = 60) -> tuple[str, str, int]:
        _tmp = DockerRunSandbox.__new__(DockerRunSandbox)
        _tmp._container_id = self._container_id
        return await DockerRunSandbox.exec_bash(_tmp, cmd, timeout)

    async def exec_stdin(self, runner, path, stdin_data, timeout=60):
        _tmp = DockerRunSandbox.__new__(DockerRunSandbox)
        _tmp._container_id = self._container_id
        return await DockerRunSandbox.exec_stdin(_tmp, runner, path, stdin_data, timeout)

    async def read_file(self, path: str) -> bytes:
        _tmp = DockerRunSandbox.__new__(DockerRunSandbox)
        _tmp._container_id = self._container_id
        return await DockerRunSandbox.read_file(_tmp, path)

    async def write_file(self, content: bytes, path: str) -> None:
        _tmp = DockerRunSandbox.__new__(DockerRunSandbox)
        _tmp._container_id = self._container_id
        await DockerRunSandbox.write_file(_tmp, content, path)

    async def exec_cmd(self, args: list[str], timeout: int = 60) -> tuple[str, str, int]:
        _tmp = DockerRunSandbox.__new__(DockerRunSandbox)
        _tmp._container_id = self._container_id
        return await DockerRunSandbox.exec_cmd(_tmp, args, timeout)

    async def _inject_tar(self, tar_data: bytes, dest_path: str) -> None:
        _tmp = DockerRunSandbox.__new__(DockerRunSandbox)
        _tmp._container_id = self._container_id
        await DockerRunSandbox._inject_tar(_tmp, tar_data, dest_path)

    async def stop(self) -> None:
        compose_file = self._spec.compose_file
        proc = await asyncio.create_subprocess_exec(
            "docker", "compose", "-f", compose_file, "-p", self._project,
            "down", "-v", "--remove-orphans",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(proc.communicate(), timeout=60)
        except asyncio.TimeoutError:
            logger.warning("docker compose down timed out for project %s", self._project)
        self._container_id = None


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def make_sandbox(spec: "SandboxSpec") -> Sandbox:
    # Ensure swe_bench sandbox is registered (import triggers @register_sandbox decorator)
    if spec.type == "swe_bench" and "swe_bench" not in _SANDBOX_REGISTRY:
        import framework.benchmarks.swe_bench.sandbox  # noqa: F401

    cls = _SANDBOX_REGISTRY.get(spec.type)
    if cls is None:
        raise ValueError(
            f"Unknown sandbox type: {spec.type!r}. "
            f"Registered types: {sorted(_SANDBOX_REGISTRY)}"
        )
    return cls(spec)


# ---------------------------------------------------------------------------
# Checkpoint runner
# ---------------------------------------------------------------------------

async def run_checkpoints(
    sandbox: Sandbox,
    task_id: str,
    checkpoints: list["Checkpoint"],
) -> "EvalResult":
    """Run all checkpoints and return a weighted EvalResult."""
    from framework.models import EvalResult

    passed_weight = 0.0
    details: list[str] = []

    for cp in checkpoints:
        _, _, exit_code = await sandbox.exec_bash(cp.cmd, timeout=cp.timeout)
        passed = (exit_code == cp.target_exit_code)
        if passed:
            passed_weight += cp.weight
        details.append(f"{'✓' if passed else '✗'} {cp.name}")

    score = round(min(passed_weight, 1.0), 4)
    if score >= 1.0:
        grade = "CORRECT"
    elif score > 0:
        grade = "PARTIAL"
    else:
        grade = "INCORRECT"

    return EvalResult(
        sample_id=task_id,
        score=score,
        grade=grade,
        explanation="; ".join(details),
    )


# ---------------------------------------------------------------------------
# Tar helper
# ---------------------------------------------------------------------------

def _make_tar(source_dir: str, exclude: list[str]) -> bytes:
    """Create an in-memory tar archive of source_dir, respecting exclude patterns."""
    src = Path(source_dir)
    patterns = _resolve_exclude_patterns(src, exclude)

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for item in sorted(src.rglob("*")):
            rel = item.relative_to(src)
            rel_str = str(rel)
            if _is_excluded(rel_str, patterns):
                continue
            tar.add(str(item), arcname=rel_str)
    buf.seek(0)
    return buf.read()


def _resolve_exclude_patterns(src: Path, explicit: list[str]) -> list[str]:
    """Return the effective list of exclude patterns for a tool directory."""
    if explicit:
        return explicit
    ignore_file = src / ".sandboxignore"
    if ignore_file.exists():
        lines = ignore_file.read_text(encoding="utf-8").splitlines()
        return [l.strip() for l in lines if l.strip() and not l.startswith("#")]
    return [".git", "__pycache__", "*.pyc"]


def _is_excluded(rel_path: str, patterns: list[str]) -> bool:
    parts = Path(rel_path).parts
    for pattern in patterns:
        # Match against the full relative path and each path component
        if fnmatch.fnmatch(rel_path, pattern):
            return True
        for part in parts:
            if fnmatch.fnmatch(part, pattern):
                return True
    return False
