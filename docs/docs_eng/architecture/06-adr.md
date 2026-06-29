# 06 — Architecture Decision Records

ADRs capture **why** the system is built the way it is. Used together with the C4 diagrams.

---

## ADR-001: The agent runs in Docker, not in-process

**Status:** Accepted

**Context:**
Agent frameworks (Hermes, OpenClaw, OpenCode) are standalone processes with their own dependencies. Running several agents in one Python process creates conflicts.

**Decision:**
Each agent is a `docker run --rm`. The orchestrator drives it via `docker exec` (curl to the internal API) and `docker stop`. The container lives for one task (or several turns of one task).

**Consequences:**
- `+` Full isolation; N tasks = N independent containers
- `+` The agent version changes via a single `hermes_image` parameter
- `−` 10–30 s overhead per task for pull/start/stop

---

## ADR-002: MCP HTTP Bridge between agent and sandbox

**Status:** Accepted

**Context:**
An agent in Docker must call tools inside a sandbox container. HTTP via `host.docker.internal` is the standard way that agent frameworks support out of the box. The stdio alternative would require an extra proxy process per task.

**Decision:**
A FastAPI server (`MCPHttpServer`) is brought up on the host on a random port. The agent reaches it via `http://host.docker.internal:{port}/mcp` over the MCP 2025-11-25 protocol (JSON-RPC 2.0). The server translates calls into `docker exec`.

**Consequences:**
- `+` The agent uses standard MCP — no need to modify the agent
- `+` Custom `SandboxTool` scripts are added without changing the harness
- `−` One port per task; an extra network hop

---

## ADR-003: Resume via task status in the DB

**Status:** Accepted (updated — FS storage removed)

**Context:**
Long runs get interrupted. Restarting from scratch is expensive in tokens.

**Decision:**
Before running a task, `db.fetch_task_output(run_id, task_id)` is called. If a record exists and `status == "done"` — the task is skipped (`event="skip"`). If no DB is configured — resume doesn't work and the task runs again.

**Consequences:**
- `+` Resume works correctly even across parallel runs
- `+` No need to track files on disk
- `−` Without PostgreSQL configured, resume is unavailable

---

## ADR-004: Scorer runs while the sandbox is alive

**Status:** Accepted

**Context:**
For sandbox tasks, scoring needs to read files and run commands inside the container. After `sandbox.stop()` the data is destroyed.

**Decision:**
`scorer(sample, trace, judge, sandbox)` is called **before** `_teardown_ctx()`. The sandbox is passed straight into the Scorer as an argument.

**Consequences:**
- `+` The Scorer has full access to the container FS
- `−` Teardown can't be moved before the Scorer call

---

## ADR-005: Benchmark and Harness are independent plugins

**Status:** Accepted

**Context:**
We need the ability to combine any agent with any benchmark.

**Decision:**
Benchmark and Harness are independent hierarchies. The Orchestrator links them via `ExecutionContext` and `Runner`. The only compatibility contract: `harness.supports_sandbox` must be `True` for tasks with `sample.sandbox`.

**Consequences:**
- `+` Adding a new Harness requires no Benchmark changes
- `−` Incompatibility (a harness without sandbox + a sandbox benchmark) shows up at runtime

---

## ADR-006: Parallelism via asyncio + Semaphore

**Status:** Accepted

**Context:**
Tasks should run in parallel, but with a cap.

**Decision:**
`asyncio.Semaphore(workers)` + `asyncio.gather(*tasks, return_exceptions=True)`.

**Consequences:**
- `+` A single `workers` parameter controls parallelism
- `+` Graceful stop: `Orchestrator.stop()` cancels everything and stops the sandbox
- `−` CPU-bound operations block the event loop → `asyncio.to_thread()`

---

## ADR-007: BFCL uses in-process mock backends

**Status:** Accepted

**Context:**
BFCL multi-turn requires real feedback from "services" (music, weather, …). Running real services is expensive and flaky.

**Decision:**
`BFCLMCPServer` runs in-process on the host with mock backends as Python classes. The agent interacts over standard MCP, unaware they're mocks.

**Consequences:**
- `+` Deterministic testing, no network dependencies
- `+` Memory snapshot between sessions via the FS
- `−` Mock backends must be kept in sync with the function specs

---

## ADR-008: PostgreSQL as the single result store

**Status:** Accepted (updated — FS storage removed)

**Context:**
Originally a dual store was used (FS + PostgreSQL). FS files created a second source of truth and complicated the resume logic.

**Decision:**
The single store is PostgreSQL (asyncpg). If the DB isn't configured or is unavailable — all `Database` methods are no-ops and results are stored nowhere. The schema auto-migrates on start via SQL files from `db/migrations/` (advisory lock to protect against races on parallel runs).

Tables: `runs`, `benchmark_runs`, `task_outputs` (status running/done/error/timeout), `eval_results`, `schema_migrations`.

**Consequences:**
- `+` A single source of truth
- `+` SQL analysis and run comparison out of the box
- `+` Transactional resume via `status == "done"`
- `−` Without PostgreSQL, results aren't saved and resume is unavailable

---

## ADR-009: Runner and Scorer as separate abstractions

**Status:** Accepted

**Context:**
Originally the harness drove both the conversation and the scoring. That made it impossible to reuse scoring logic across harnesses and to do multi-turn orchestration without duplication.

**Decision:**
Split into three roles:
- **Harness** — transport only: send messages, get a reply (`send_turn → TurnResponse`)
- **Runner** — drives the turns: calls `send_turn()` as many times as needed, aggregates into `AgentTrace`
- **Scorer** — evaluates the result: takes `Sample + AgentTrace + sandbox → Score`

The Benchmark selects the Runner and Scorer via `make_runner()` / `make_scorer()`.

**Consequences:**
- `+` The harness doesn't know about the benchmark's conversation structure
- `+` The BFCL Runner works with any harness unchanged
- `+` The Scorer is reused across benchmarks
- `−` PAC1 doesn't fit — left on the legacy `run_task()` protocol

---

## ADR-010: Lazy container start and ctx.cleanup_fns

**Status:** Accepted

**Context:**
Under the modern `send_turn()` protocol the Runner calls the harness several times. The container must be started once on the first call and stopped after the last.

**Decision:**
The harness starts the container lazily on the first `send_turn()` and stores `{container_id, previous_response_id}` in `ctx.extras["harness_session"]`. A cleanup callback (`docker stop`) is registered in `ctx.cleanup_fns`. The Orchestrator calls all `cleanup_fns` in `_teardown_ctx()`.

**Alternatives:**
- A context manager at the Orchestrator level — would complicate the harness interface
- One container per task with explicit start/stop — would break the abstraction (the Runner shouldn't know about containers)

**Consequences:**
- `+` The harness fully encapsulates container management
- `+` Runner and Scorer don't know about Docker
- `−` Cleanup must be registered correctly — otherwise the container won't stop (a leak)
