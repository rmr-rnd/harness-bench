# 02 — Containers

> Diagram: [diagrams/containers.md](../diagrams/containers.md)

## Overview

When a run starts, the following deployable units come up. Some live for the whole run, some only for the duration of a single task.

## Containers

### Framework CLI
**Technology:** Python 3.12+, Click, asyncio
**Lifecycle:** whole run

Two entry points: `framework serve` brings up the Web UI as a service (config is picked in the browser; DB — from the deployment environment); `framework run config.yaml` — a headless run: reads the YAML into `RunConfig`, initializes the `Orchestrator`, `asyncio.run(orchestrator.run())`.

Other commands: `compare` (compare two runs), `results` (view results), `db-init`, `db-runs`.

---

### Web UI
**Technology:** FastAPI, uvicorn, WebSocket, built-in HTML/JS SPA
**Lifecycle:** whole run
**URL:** `http://localhost:8765` (preferred; if busy, the next free port)

Lets you watch a run in the browser. The frontend receives events over WebSocket `/ws`. REST endpoints: `POST /api/run`, `POST /api/stop`, `GET /api/configs`, `POST /api/load-config`, `GET /api/report/{run_id}`. Started by `framework serve` (and in Docker it's the default `CMD`).

---

### Orchestrator
**Technology:** Python asyncio
**Lifecycle:** whole run

The system core. One instance per run. Iterates over the benchmarks in the config; for each one it runs tasks in parallel via `asyncio.Semaphore(workers)`.

Lifecycle of one task:
1. Check resume: `db.fetch_task_output(run_id, task_id)` — if `status == "done"`, skip
2. `db.save_task_start()` — mark the task as `running`
3. `_build_ctx()` — bring up the sandbox and MCP Bridge (if `sample.sandbox` is set)
4. `runner.run(task, harness.send_turn, ctx)` → `AgentTrace`
5. `scorer(task, trace, judge, sandbox)` — score while the sandbox is alive
6. `_teardown_ctx()` — stop the harness container, MCP Bridge, sandbox
7. `db.save_task_output()` + `db.save_eval_result()` — persist to the DB

Resume works only when PostgreSQL is present.

---

### MCP HTTP Bridge
**Technology:** FastAPI, uvicorn, JSON-RPC 2.0
**Lifecycle:** duration of one task
**Protocol:** MCP 2025-11-25

Brought up on a random free port on the host for each sandbox task. The agent (in Docker) reaches it via the address returned by `get_mcp_host()`: `host.docker.internal:{port}` by default, or the container's own IP when the framework runs inside Docker (`HARNESS_DOCKER_NETWORK`).

Routing of incoming calls:
- **Standard tools** (shell, filesystem, browser) → `DockerBridge` → `docker exec` into the sandbox
- **Custom SandboxTools** → `sandbox.exec_stdin()` (a script injected into the sandbox at start)

---

### Agent Container
**Technology:** Docker, `nousresearch/hermes-agent` (or openclaw, opencode)
**Lifecycle:** duration of one task

Started with `docker run -d --rm`. The model config and MCP server URL are passed via a volume-mounted config.yaml. Hermes/OpenClaw are driven through `docker exec curl` to an internal HTTP API:
- `POST /v1/responses` with `stream=True` — send a turn, receive an SSE stream of agent steps (multi-turn via `previous_response_id`)
- `GET /health` — check readiness

OpenCode communicates differently: the container publishes a port to the host (random ephemeral, `docker port`), and the harness talks to it directly over `httpx` (`POST /session`, `POST /session/{id}/message`, SSE `/event`).

---

### Benchmark Sandbox
**Technology:** Docker run / Docker Compose
**Lifecycle:** duration of one task

An isolated task execution environment. The type depends on the benchmark:

| Benchmark | Sandbox type | Image |
|-----------|--------------|-------|
| SWE-bench (+ multilingual) | `SWEbenchSandbox` (`type=swe_bench`) | per-repo image with dependencies |
| TheAgentCompany | `DockerComposeSandbox` (`type=docker_compose`) | Multi-service stack (Gitea, web, DB) |
| PAC1 | Remote PCM workspace (BitGN), not local Docker | Defined by the BitGN API |

> HumanEval+ runs the agent's code in a **local subprocess** on the host (no Docker sandbox), so it isn't listed here. The only sandbox tasks are SWE-bench and TheAgentCompany.

---

### PostgreSQL
**Technology:** PostgreSQL 16, asyncpg
**Lifecycle:** persistent storage (optional)

The single result store. Enabled via `RunConfig.database`. If not configured — all `Database` methods are no-ops, results are stored nowhere, and resume is unavailable.

The schema auto-migrates on connect: SQL files from `db/migrations/` are applied under an advisory lock (race protection for parallel runs).

Tables: `runs`, `benchmark_runs`, `task_outputs` (status: running/done/error/timeout), `eval_results`, `schema_migrations`.

## Network topology during a sandbox task

```
┌─────────────── Host machine ───────────────────────────────┐
│                                                            │
│  Framework CLI / Orchestrator                              │
│         │                                                  │
│         ├── [Docker SDK] ──────────────────────────────┐  │
│         │                                              │  │
│         ├── MCP HTTP Bridge :RANDOM_PORT               │  │
│                │                                       │  │
│                │ ◄──── http://host.docker.internal:PORT │  │
│                │                                       │  │
│  ┌─────────────────────────┐  ┌────────────────────┐  │  │
│  │  Agent Container        │  │  Benchmark Sandbox  │  │  │
│  │  (Hermes / OpenClaw)    │  │  (task environment) │  │  │
│  │                         │  │                     │  │  │
│  │  MCP calls ────────────>│  │<── docker exec ─────│  │  │
│  └─────────────────────────┘  └────────────────────┘  │  │
│                                                        │  │
└────────────────────────────────────────────────────────┘  │
                                                             │
                LLM Provider API ◄───────────────────────────┘
                                    HTTPS / OpenAI API
```
