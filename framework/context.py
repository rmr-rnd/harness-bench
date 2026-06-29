from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from typing_extensions import TypedDict

if TYPE_CHECKING:
    from framework.mcp.bfcl_server import BFCLMCPServer
    from framework.mcp.http_server import MCPHttpServer


class ContextExtras(TypedDict, total=False):
    """Typed extras for ExecutionContext.

    All known keys must be declared here so IDE and mypy catch typos.
    Never use ctx.extras["raw_string"] directly — add the key here first.
    """
    bfcl_mcp_server: "BFCLMCPServer"   # set by orchestrator._setup_bfcl_ctx
    bfcl_snapshot_dir: str              # bfcl_memory: path to memory snapshot dir
    bfcl_func_doc_dir: str              # bfcl: path to function doc dir for MCP server
    swe_instance_id: str                # set by orchestrator._setup_swe_ctx
    swe_namespace: str                  # set by orchestrator._setup_swe_ctx
    pac1_runtime_url: str               # used internally by Pac1Harness
    harness_session: dict               # per-task harness container state (for send_turn lazy start)
    bfcl_mcp_port: int                  # port where BFCLRunner started the MCP server


@dataclass
class ExecutionContext:
    """Carries per-task execution state from orchestrator to harness.

    The orchestrator builds this in _build_ctx(), populates infrastructure
    fields (sandbox, mcp_url, mcp_server), then passes it to harness.run_task().
    The harness treats it as read-only.
    """
    timeout: int = 180
    step_cb: Callable[..., None] | None = None
    web_search: bool = False
    # Max seconds of stream inactivity before a harness declares the agent dead
    # (see ParallelismConfig.stream_idle_timeout). 0 disables the idle watchdog.
    stream_idle_timeout: int = 60

    # Set by orchestrator._build_ctx when the benchmark needs a sandbox:
    sandbox: Any | None = None                      # live Sandbox instance
    mcp_url: str = ""                               # URL of pre-started MCP bridge
    mcp_server: "MCPHttpServer | None" = None       # kept for _teardown_ctx cleanup

    # Set by orchestrator before runner.run() to let runners know harness identity
    harness_type: str = ""

    # Async cleanup callbacks registered by harness.send_turn (e.g. stop container)
    cleanup_fns: list = field(default_factory=list)

    # Benchmark-specific extras — must be declared in ContextExtras above
    extras: ContextExtras = field(default_factory=ContextExtras)
