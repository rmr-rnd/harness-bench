"""Download Phase + Sync Phase: mirror BitGN PCM sandbox ↔ local directory."""
from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_SKIP_DIRS = {"__pycache__", "node_modules", ".venv", "venv", ".tox"}
# .git intentionally NOT skipped — PAC1 has git tasks.
# Set skip_git=True to opt out if .git causes slowness on specific benchmarks.


def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()


class PcmMirror:
    """Handles download from PCM sandbox and sync back after agent finishes."""

    def __init__(self, runtime_url: str, skip_git: bool = False) -> None:
        from vendor.bitgn.vm.pcm_connect import PcmRuntimeClientSync
        self._client = PcmRuntimeClientSync(runtime_url)
        self._skip_git = skip_git
        self._snapshot: dict[str, str] = {}  # rel_path → sha256

    def close(self) -> None:
        if hasattr(self._client, "close"):
            self._client.close()

    # ------------------------------------------------------------------
    # Download Phase
    # ------------------------------------------------------------------

    def download(self, workspace_dir: Path) -> None:
        """Download entire sandbox into workspace_dir and build snapshot."""
        workspace_dir.mkdir(parents=True, exist_ok=True)
        self._snapshot = {}

        skip = set(_SKIP_DIRS)
        if self._skip_git:
            skip.add(".git")

        paths = self._collect_file_paths("/", skip)
        logger.info("Download: %d files to fetch", len(paths))
        asyncio.run(self._download_all(paths, workspace_dir))

    def _collect_file_paths(self, root: str, skip_dirs: set[str]) -> list[str]:
        result: list[str] = []
        self._walk(root, skip_dirs, result)
        return result

    def _walk(self, path: str, skip_dirs: set[str], result: list[str]) -> None:
        from vendor.bitgn.vm.pcm_pb2 import ListRequest
        from connectrpc.errors import ConnectError
        try:
            resp = self._client.list(ListRequest(name=path))
        except ConnectError as e:
            logger.warning("list(%s) failed: %s", path, e)
            return
        for entry in resp.entries:
            name = entry.name
            full = f"{path.rstrip('/')}/{name}"
            if entry.is_dir:
                if name in skip_dirs:
                    continue
                self._walk(full, skip_dirs, result)
            else:
                result.append(full)

    async def _download_all(self, paths: list[str], workspace_dir: Path) -> None:
        sem = asyncio.Semaphore(20)

        async def fetch(path: str) -> None:
            async with sem:
                await asyncio.to_thread(self._fetch_one, path, workspace_dir)

        await asyncio.gather(*[fetch(p) for p in paths])

    def _fetch_one(self, sandbox_path: str, workspace_dir: Path) -> None:
        from vendor.bitgn.vm.pcm_pb2 import ReadRequest
        from connectrpc.errors import ConnectError
        try:
            resp = self._client.read(ReadRequest(path=sandbox_path))
        except ConnectError as e:
            logger.warning("read(%s) failed: %s", sandbox_path, e)
            return
        content = resp.content
        rel = sandbox_path.lstrip("/")
        local = workspace_dir / rel
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_text(content, encoding="utf-8", errors="replace")
        self._snapshot[rel] = _sha256(content)

    # ------------------------------------------------------------------
    # Sync Phase
    # ------------------------------------------------------------------

    def sync_back(self, workspace_dir: Path) -> None:
        """Push changes from workspace_dir back to sandbox."""
        from vendor.bitgn.vm.pcm_pb2 import DeleteRequest, MkDirRequest, WriteRequest
        from connectrpc.errors import ConnectError

        current = self._scan_workspace(workspace_dir)

        added_or_changed = {
            p for p, h in current.items()
            if self._snapshot.get(p) != h and p != ".pac1_answer.json"
        }
        deleted = {p for p in self._snapshot if p not in current}

        logger.info(
            "Sync: %d changed/added, %d deleted",
            len(added_or_changed),
            len(deleted),
        )

        # 1. mkdir for new directories (topological order by depth)
        new_dirs = sorted(
            {str(Path(p).parent) for p in added_or_changed if Path(p).parent != Path(".")},
            key=lambda d: d.count("/"),
        )
        for rel_dir in new_dirs:
            try:
                self._client.mk_dir(MkDirRequest(path="/" + rel_dir))
            except ConnectError:
                pass  # may already exist

        # 2. write changed/new files
        for rel in added_or_changed:
            local = workspace_dir / rel
            try:
                content = local.read_text(encoding="utf-8", errors="replace")
                self._client.write(WriteRequest(path="/" + rel, content=content))
            except (OSError, ConnectError) as e:
                logger.warning("write /%s failed: %s", rel, e)

        # 3. delete removed files
        for rel in deleted:
            try:
                self._client.delete(DeleteRequest(path="/" + rel))
            except ConnectError as e:
                logger.warning("delete /%s failed: %s", rel, e)

    @staticmethod
    def _scan_workspace(workspace_dir: Path) -> dict[str, str]:
        result: dict[str, str] = {}
        for fpath in workspace_dir.rglob("*"):
            if fpath.is_file():
                rel = str(fpath.relative_to(workspace_dir))
                try:
                    content = fpath.read_text(encoding="utf-8", errors="replace")
                    result[rel] = _sha256(content)
                except OSError:
                    pass
        return result
