"""SWE-bench sandbox — wraps a single docker run container per instance."""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from framework.sandbox import register_sandbox

if TYPE_CHECKING:
    from framework.models import SandboxSpec

logger = logging.getLogger(__name__)


@register_sandbox("swe_bench")
class SWEbenchSandbox:
    """
    Manages one Docker container based on a SWE-bench instance image.
    Can be constructed either via SandboxSpec (registry path) or directly with
    instance_id/namespace (legacy path used by orchestrator before Phase 5).
    """

    def __init__(self, spec_or_instance_id: "SandboxSpec | str", namespace: str = "swebench") -> None:
        from framework.models import SandboxSpec as _SandboxSpec
        if isinstance(spec_or_instance_id, _SandboxSpec):
            spec = spec_or_instance_id
            self.instance_id = spec.config["instance_id"]
            self.namespace = spec.config.get("namespace", "swebench")
        else:
            self.instance_id = spec_or_instance_id
            self.namespace = namespace
        self.container_id: str | None = None
        # Image naming: {namespace}/sweb.eval.x86_64.{instance_id_lower}:latest
        # Note: double underscores are replaced with _1776_ in remote image names
        sanitized = self.instance_id.lower().replace("__", "_1776_")
        self._image_tag = f"{self.namespace}/sweb.eval.x86_64.{sanitized}:latest"

    async def start(self) -> None:
        logger.info("Checking SWE-bench image %s", self._image_tag)

        check = await asyncio.create_subprocess_exec(
            "docker", "image", "inspect", self._image_tag,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await check.communicate()

        if check.returncode != 0:
            logger.info("Pulling image %s", self._image_tag)
            proc = await asyncio.create_subprocess_exec(
                "docker", "pull", self._image_tag,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=600)
            if proc.returncode != 0:
                raise RuntimeError(
                    f"docker pull {self._image_tag} failed: {stderr.decode()[:300]}"
                )

        proc = await asyncio.create_subprocess_exec(
            "docker", "run", "-d",
            "--name", f"sweb_sandbox_{self.instance_id.lower().replace('__', '_')}_{id(self)}",
            self._image_tag, "sleep", "infinity",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        if proc.returncode != 0:
            raise RuntimeError(
                f"docker run for {self._image_tag} failed: {stderr.decode()[:300]}"
            )
        self.container_id = stdout.decode().strip()
        logger.info("SWE-bench sandbox started: %s", self.container_id[:12])

    async def exec_bash(self, cmd: str, timeout: int = 120) -> tuple[str, str]:
        """Run a bash command inside the container. Returns (stdout, stderr)."""
        if not self.container_id:
            return "", "[sandbox not started]"
        proc = await asyncio.create_subprocess_exec(
            "docker", "exec", self.container_id,
            "bash", "-c", cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return stdout.decode(errors="replace"), stderr.decode(errors="replace")
        except asyncio.TimeoutError:
            try:
                proc.kill()
                await proc.communicate()
            except Exception:
                pass
            return "", f"[bash timeout after {timeout}s]"

    async def get_patch(self) -> str:
        """Return unified diff of changes the agent made relative to HEAD."""
        stdout, stderr = await self.exec_bash("git -C /testbed diff HEAD", timeout=30)
        return stdout

    async def write_file(self, content: str, dst_path: str) -> None:
        """Write content to dst_path inside the container via stdin."""
        proc = await asyncio.create_subprocess_exec(
            "docker", "exec", "-i", self.container_id,
            "bash", "-c", f"cat > {dst_path}",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(
            proc.communicate(input=content.encode()), timeout=30
        )
        if proc.returncode != 0:
            raise RuntimeError(f"write_file to {dst_path} failed: {stderr.decode()[:200]}")

    async def run_eval_script(self, eval_script: str, timeout: int = 1800) -> str:
        """
        Write eval_script to /eval.sh, make executable, run it, return combined output.
        This is the SWE-bench eval script — output is parsed by log_parsers.
        """
        await self.write_file(eval_script, "/eval.sh")
        chmod_out, chmod_err = await self.exec_bash("chmod +x /eval.sh", timeout=10)
        stdout, stderr = await self.exec_bash("/bin/bash /eval.sh", timeout=timeout)
        return stdout + stderr

    async def get_networks(self) -> list[str]:
        """Return list of network names this container is connected to."""
        if not self.container_id:
            return []
        proc = await asyncio.create_subprocess_exec(
            "docker", "inspect",
            "--format", "{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}",
            self.container_id,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        return [n for n in out.decode().split() if n]

    async def stop(self) -> None:
        if not self.container_id:
            return
        proc = await asyncio.create_subprocess_exec(
            "docker", "rm", "-f", self.container_id,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(proc.communicate(), timeout=30)
        except asyncio.TimeoutError:
            pass
        self.container_id = None
