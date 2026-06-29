# Framework Components

Three diagrams: core, Docker/MCP infrastructure, storage.

---

## 1. Core — task lifecycle

```mermaid
flowchart TD
    Config["**RunConfig**\nrun config"]
    CLI["**CLI**\nentry point"]
    Orch["**Orchestrator**\nrun engine"]
    BReg["**Benchmark Registry**"]
    HReg["**Harness Registry**"]
    Ctx["**ExecutionContext**\ntask state"]

    Bench["**Benchmark**\ndata + Runner/Scorer choice"]
    Runner["**Runner**\ndrives the turns"]
    Harness["**Harness**\ntransport to the agent"]
    Scorer["**Scorer**\nresult scoring"]

    CLI -->|"loads"| Config
    CLI -->|"creates"| Orch

    Orch -->|"name → class"| BReg
    Orch -->|"type → class"| HReg
    Orch -->|"per task"| Ctx

    Orch -->|"load_samples()"| Bench
    Bench -->|"make_runner()"| Runner
    Bench -->|"make_scorer()"| Scorer

    Runner -->|"send_turn()"| Harness
    Runner -->|"AgentTrace"| Orch

    Orch -->|"scorer(…)"| Scorer
    Scorer -->|"Score"| Orch

    Harness -->|"reads"| Ctx

    style Runner fill:#d5e8d4,stroke:#82b366
    style Scorer fill:#d5e8d4,stroke:#82b366
```

**Files and details:**

| Component | File | Details |
|-----------|------|---------|
| RunConfig | `config.py` | Pydantic models |
| CLI | `cli.py` | Click |
| Orchestrator | `orchestrator.py` | — |
| Benchmark | `benchmarks/base.py` | `load_samples()` · `make_runner()` · `make_scorer()` |
| Runner | `runners/base.py` | `SingleTurnRunner` (default) · `BFCLRunner` |
| Harness | `harnesses/base.py` | `send_turn()` (modern) / `run_task()` (PAC1-legacy) |
| Scorer | `scorers/base.py` | ExactMatch · LLMJudge · Checkpoint · Subprocess |
| ExecutionContext | `context.py` | timeout · sandbox · mcp_url · cleanup_fns · extras |

---

## 2. Docker and MCP infrastructure

```mermaid
flowchart LR
    Orch["**Orchestrator**"]

    subgraph infra["Infrastructure (per-task)"]
        MCP["**MCP HTTP Bridge**\nport :RANDOM"]
        DockerBridge["**Docker Bridge**\ndocker exec → sandbox"]
        SandboxMgr["**Sandbox Manager**"]
    end

    subgraph groups["MCP tool groups"]
        Shell["shell"]
        FS["filesystem"]
        Browser["browser"]
        Custom["SandboxTool\ncustom scripts"]
    end

    AgentC["**Agent Container**\nHermes / OpenClaw / OpenCode"]
    SandboxC["**Benchmark Sandbox**"]

    Orch -->|"start() / stop()"| MCP
    Orch -->|"start() / stop()"| SandboxMgr
    SandboxMgr -->|"docker run / compose up"| SandboxC
    SandboxMgr -->|"inject_tools()"| SandboxC

    AgentC -->|"POST /mcp"| MCP
    MCP -->|"standard groups"| DockerBridge
    MCP -->|"custom"| Custom
    DockerBridge -->|"docker exec"| SandboxC

    Shell & FS & Browser --> DockerBridge
    Custom -.->|"injected at start"| SandboxC
```

**Files and details:**

| Component | File | Details |
|-----------|------|---------|
| MCP HTTP Bridge | `mcp/http_server.py` | FastAPI, JSON-RPC 2.0, MCP 2025-11-25 |
| Docker Bridge | `mcp/docker_bridge.py` | `docker exec {container_id} {cmd}` |
| Sandbox Manager | `sandbox.py` | `@register_sandbox()` |
| Tool groups | `mcp/tools/` | `shell` · `filesystem` · `browser` |
| SandboxTool | — | from `Sample.sandbox_tools`, via `sandbox.exec_stdin()` |

---

## 3. Result storage

```mermaid
flowchart LR
    Orch["**Orchestrator**"]
    DB["**Database**\nno-op if not configured"]
    PG[("**PostgreSQL**")]
    Resume["**Resume check**\nstatus=='done' → skip"]

    Orch -->|"save_task_start()"| DB
    Orch -->|"save_task_output()"| DB
    Orch -->|"save_eval_result()"| DB
    Orch -->|"save_benchmark_summary()"| DB
    DB -->|"INSERT / ON CONFLICT"| PG

    Orch -->|"before a task"| Resume
    Resume -->|"fetch_task_output()"| DB
```

**Files and details:**

| Component | File | Details |
|-----------|------|---------|
| Database | `db.py` | asyncpg; no-op without config |
| PostgreSQL | — | tables: `runs` · `benchmark_runs` · `task_outputs` · `eval_results` · `schema_migrations` |
| save_task_output | — | writes `status = done/error/timeout` |
