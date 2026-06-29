"""Docker sandbox manager for TheAgentCompany tasks."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import uuid

logger = logging.getLogger(__name__)


class DockerSandbox:
    def __init__(self, compose_file: str, task_name: str) -> None:
        self.compose_file = compose_file
        self.task_name = task_name
        self.project_name = f"tac_{task_name}_{uuid.uuid4().hex[:8]}"
        self._container_id: str | None = None
        self._browser_session_id: str | None = None
        self._rpc_id = 0

    async def start(self) -> None:
        logger.info("Starting sandbox %s (compose: %s)", self.project_name, self.compose_file)
        proc = await asyncio.create_subprocess_exec(
            "docker", "compose",
            "-f", self.compose_file,
            "-p", self.project_name,
            "up", "-d", "--build",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
        if proc.returncode != 0:
            raise RuntimeError(
                f"docker compose up failed: {stderr.decode()[:500]}"
            )

        # Get container ID of the 'default' service
        proc2 = await asyncio.create_subprocess_exec(
            "docker", "compose",
            "-f", self.compose_file,
            "-p", self.project_name,
            "ps", "-q", "default",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await asyncio.wait_for(proc2.communicate(), timeout=30)
        cid = out.decode().strip().split("\n")[0].strip()
        if not cid:
            raise RuntimeError("Could not find 'default' container in compose stack")
        self._container_id = cid
        logger.info("Sandbox started, container: %s", cid[:12])

    async def exec_bash(self, cmd: str, timeout: int = 120) -> str:
        if not self._container_id:
            return "[sandbox not started]"
        proc = await asyncio.create_subprocess_exec(
            "docker", "exec", self._container_id,
            "bash", "-c", cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return out.decode(errors="replace")
        except asyncio.TimeoutError:
            proc.kill()
            return f"[bash timeout after {timeout}s]"

    async def exec_python(self, code: str, timeout: int = 120) -> str:
        if not self._container_id:
            return "[sandbox not started]"
        # Write code to a temp file inside the container via stdin
        escaped = code.replace("'", "'\\''")
        cmd = f"python3 -c '{escaped}'"
        # Use bash to run python to handle multiline code better
        tmp_name = f"/tmp/tac_code_{uuid.uuid4().hex[:8]}.py"
        write_cmd = f"cat > {tmp_name} << 'PYEOF'\n{code}\nPYEOF"
        await self.exec_bash(write_cmd, timeout=10)
        result = await self.exec_bash(f"python3 {tmp_name}; rm -f {tmp_name}", timeout=timeout)
        return result

    async def exec_jsonrpc(self, method: str, params: dict, timeout: int = 180) -> dict:
        """Send a JSON-RPC request to inspect-tool-support running in the container via stdin."""
        if not self._container_id:
            return {"error": {"message": "sandbox not started"}}
        self._rpc_id += 1
        request = json.dumps({"jsonrpc": "2.0", "id": self._rpc_id, "method": method, "params": params}).encode()
        proc = await asyncio.create_subprocess_exec(
            "docker", "exec", "-i", self._container_id,
            "inspect-tool-support", "exec",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(input=request), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return {"error": {"message": f"inspect-tool-support timeout after {timeout}s"}}
        raw = stdout.decode(errors="replace").strip()
        try:
            return json.loads(raw)
        except Exception:
            return {"error": {"message": f"bad JSON-RPC response: {raw[:300]} stderr={stderr.decode()[:200]}"}}

    async def web_browser(self, tool_name: str, params: dict, timeout: int = 180) -> str:
        """Execute a web browser JSON-RPC command via inspect-tool-support."""
        if not self._browser_session_id:
            resp = await self.exec_jsonrpc("web_new_session", {"headful": False}, timeout=timeout)
            result = resp.get("result", {})
            err = resp.get("error")
            if err:
                return f"[web_browser error: {err.get('message', err)}]"
            self._browser_session_id = result.get("session_name", "")

        params["session_name"] = self._browser_session_id
        resp = await self.exec_jsonrpc(tool_name, params, timeout=timeout)
        result = resp.get("result", {})
        err = resp.get("error")
        if err:
            return f"[web_browser error: {err.get('message', err)}]"
        if result.get("error"):
            return f"[web_browser error: {result['error']}]"
        web_at = result.get("web_at", "") or ""
        main_content = result.get("main_content") or ""
        # Strip base64 image data
        web_at_lines = [line.partition("data:image/png;base64")[0] for line in web_at.splitlines()]
        web_at = "\n".join(web_at_lines)
        if main_content:
            return f"main content:\n{main_content}\n\naccessibility tree:\n{web_at}"
        return web_at or "(no content)"

    async def read_file(self, path: str) -> bytes:
        if not self._container_id:
            return b""
        proc = await asyncio.create_subprocess_exec(
            "docker", "exec", self._container_id,
            "cat", path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=30)
            if proc.returncode != 0:
                logger.debug("read_file %s failed: %s", path, err.decode()[:200])
                return b""
            return out
        except asyncio.TimeoutError:
            return b""

    async def get_all_networks(self) -> list[tuple[str, str]]:
        """Return [(network_name, ip), ...] with non-internal networks first.

        Non-internal networks have internet access (needed for LLM API calls from Hermes).
        Internal networks are isolated (used for owncloud/gitea service-to-service comms).
        """
        tpl = "{{range $k,$v := .NetworkSettings.Networks}}{{$k}}={{$v.IPAddress}} {{end}}"
        proc = await asyncio.create_subprocess_exec(
            "docker", "inspect", "-f", tpl, self._container_id,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        networks = [(k, v) for part in out.decode().split()
                    for k, _, v in [part.partition("=")] if k and v]

        # Check each network's Internal flag; sort non-internal first
        internal_flags: dict[str, bool] = {}
        for name, _ in networks:
            try:
                p = await asyncio.create_subprocess_exec(
                    "docker", "network", "inspect", "-f", "{{.Internal}}", name,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                )
                o, _ = await asyncio.wait_for(p.communicate(), timeout=5)
                internal_flags[name] = o.decode().strip().lower() == "true"
            except Exception:
                internal_flags[name] = False

        networks.sort(key=lambda kv: internal_flags.get(kv[0], False))
        return networks

    async def get_network_info(self) -> tuple[str, str]:
        """Return (network_name, container_ip) for the first non-host network of this container."""
        tpl = "{{range $k,$v := .NetworkSettings.Networks}}{{$k}}={{$v.IPAddress}} {{end}}"
        proc = await asyncio.create_subprocess_exec(
            "docker", "inspect", "-f", tpl, self._container_id,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        for part in out.decode().split():
            k, _, v = part.partition("=")
            if v and k and "bridge" not in k.lower():
                return k, v
        # Fallback: take any network with an IP
        for part in out.decode().split():
            k, _, v = part.partition("=")
            if v:
                return k, v
        raise RuntimeError(f"Container {self._container_id[:12]} has no usable network")

    async def setup_ssh(self) -> tuple[str, str]:
        """Install sshd in the TAC container, inject a one-time SSH key.

        Returns (host_key_path, tac_ip_on_eval_net).
        The caller is responsible for deleting the key tmpdir.
        """
        # Generate ed25519 key pair on the host
        tmpdir = tempfile.mkdtemp(prefix="tac_ssh_")
        key_path = os.path.join(tmpdir, "id_ed25519")
        gen = await asyncio.create_subprocess_exec(
            "ssh-keygen", "-t", "ed25519", "-N", "", "-f", key_path,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(gen.communicate(), timeout=15)

        pub_key = open(key_path + ".pub").read().strip()

        # Step 1: install openssh-server and configure SSH keys (may take ~60s for apt)
        setup_sh = (
            "apt-get update -qq 2>/dev/null; "
            "DEBIAN_FRONTEND=noninteractive apt-get install -y -q openssh-server 2>/dev/null; "
            "mkdir -p /root/.ssh /run/sshd; "
            f"echo '{pub_key}' >> /root/.ssh/authorized_keys; "
            "chmod 700 /root/.ssh; chmod 600 /root/.ssh/authorized_keys"
        )
        proc = await asyncio.create_subprocess_exec(
            "docker", "exec", self._container_id, "bash", "-c", setup_sh,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.communicate(), timeout=120)

        # Step 2: start sshd as a detached background process
        sshd_proc = await asyncio.create_subprocess_exec(
            "docker", "exec", "-d", self._container_id,
            "/usr/sbin/sshd", "-D",
            "-o", "StrictModes=no",
            "-o", "PermitRootLogin=yes",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(sshd_proc.communicate(), timeout=10)
        await asyncio.sleep(2)  # give sshd a moment to bind

        network_name, tac_ip = await self.get_network_info()
        logger.info("TAC SSH ready at %s (network: %s)", tac_ip, network_name)
        return key_path, tac_ip, tmpdir

    async def stop(self) -> None:
        if not self.compose_file:
            return
        logger.info("Stopping sandbox %s", self.project_name)
        proc = await asyncio.create_subprocess_exec(
            "docker", "compose",
            "-f", self.compose_file,
            "-p", self.project_name,
            "down", "-v", "--remove-orphans",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            await asyncio.wait_for(proc.communicate(), timeout=60)
        except asyncio.TimeoutError:
            logger.warning("sandbox stop timed out for %s", self.project_name)
