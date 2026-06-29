"""Docker exec/cp bridge — all sandbox I/O goes through here, no SSH."""
from __future__ import annotations

import asyncio
import json
import os
import tempfile


class DockerBridge:
    """Executes commands and file operations in a target Docker container via the Docker socket."""

    def __init__(self, container_id: str) -> None:
        self.container_id = container_id
        self._rpc_id = 0
        self._browser_session_id: str | None = None

    # ------------------------------------------------------------------
    # Shell
    # ------------------------------------------------------------------

    async def exec_bash(self, cmd: str, timeout: int = 120) -> str:
        proc = await asyncio.create_subprocess_exec(
            "docker", "exec", self.container_id,
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
        tmp = f"/tmp/_mcp_{_hex8()}.py"
        write_res = await self.write_file(tmp, code)
        if write_res.startswith("["):
            return write_res
        return await self.exec_bash(f"python3 {tmp}; rm -f {tmp}", timeout=timeout)

    # ------------------------------------------------------------------
    # Filesystem
    # ------------------------------------------------------------------

    async def read_file(self, path: str, timeout: int = 30) -> str:
        proc = await asyncio.create_subprocess_exec(
            "docker", "exec", self.container_id,
            "cat", path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            return "[read_file timeout]"
        if proc.returncode != 0:
            return f"[read_file error: {err.decode(errors='replace')[:200]}]"
        return out.decode(errors="replace")

    async def write_file(self, path: str, content: str, timeout: int = 30) -> str:
        # Write to a host temp file then docker cp into the container.
        # This avoids shell escaping issues with arbitrary content.
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".tmp", delete=False) as f:
            f.write(content)
            tmp_host = f.name
        try:
            parent = os.path.dirname(path)
            if parent and parent != "/":
                await self.exec_bash(f"mkdir -p '{parent}'", timeout=10)

            proc = await asyncio.create_subprocess_exec(
                "docker", "cp", tmp_host, f"{self.container_id}:{path}",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                return "[write_file timeout]"
            if proc.returncode != 0:
                return f"[write_file error: {err.decode()[:200]}]"
            return "ok"
        finally:
            os.unlink(tmp_host)

    async def list_dir(self, path: str = ".", timeout: int = 30) -> str:
        return await self.exec_bash(f"ls -la '{path}'", timeout=timeout)

    # ------------------------------------------------------------------
    # Browser (via inspect-tool-support inside the container)
    # ------------------------------------------------------------------

    async def exec_jsonrpc(self, method: str, params: dict, timeout: int = 180) -> dict:
        self._rpc_id += 1
        request = json.dumps({
            "jsonrpc": "2.0",
            "id": self._rpc_id,
            "method": method,
            "params": params,
        }).encode()
        proc = await asyncio.create_subprocess_exec(
            "docker", "exec", "-i", self.container_id,
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
            return {"error": {"message": f"bad JSON-RPC response: {raw[:300]} stderr={stderr.decode()[:100]}"}}

    async def web_browser(self, method: str, params: dict, timeout: int = 180) -> str:
        if not self._browser_session_id:
            resp = await self.exec_jsonrpc("web_new_session", {"headful": False})
            err = resp.get("error")
            if err:
                return f"[browser error: {err.get('message', err)}]"
            self._browser_session_id = resp.get("result", {}).get("session_name", "")

        params["session_name"] = self._browser_session_id
        resp = await self.exec_jsonrpc(method, params, timeout=timeout)
        result = resp.get("result", {})
        err = resp.get("error")
        if err:
            return f"[browser error: {err.get('message', err)}]"
        if result.get("error"):
            return f"[browser error: {result['error']}]"
        web_at = result.get("web_at", "") or ""
        main_content = result.get("main_content") or ""
        web_at_lines = [line.partition("data:image/png;base64")[0] for line in web_at.splitlines()]
        web_at = "\n".join(web_at_lines)
        if main_content:
            return f"main content:\n{main_content}\n\naccessibility tree:\n{web_at}"
        return web_at or "(no content)"


def _hex8() -> str:
    import uuid
    return uuid.uuid4().hex[:8]
