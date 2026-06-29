# Framework Components

Три диаграммы: ядро, инфраструктура Docker/MCP, хранилище.

---

## 1. Ядро — жизненный цикл задачи

```mermaid
flowchart TD
    Config["**RunConfig**\nконфиг прогона"]
    CLI["**CLI**\nточка входа"]
    Orch["**Orchestrator**\nдвижок прогона"]
    BReg["**Benchmark Registry**"]
    HReg["**Harness Registry**"]
    Ctx["**ExecutionContext**\nсостояние задачи"]

    Bench["**Benchmark**\nданные + выбор Runner/Scorer"]
    Runner["**Runner**\nуправляет ходами"]
    Harness["**Harness**\nтранспорт к агенту"]
    Scorer["**Scorer**\nоценка результата"]

    CLI -->|"загружает"| Config
    CLI -->|"создаёт"| Orch

    Orch -->|"имя → класс"| BReg
    Orch -->|"тип → класс"| HReg
    Orch -->|"на каждую задачу"| Ctx

    Orch -->|"load_samples()"| Bench
    Bench -->|"make_runner()"| Runner
    Bench -->|"make_scorer()"| Scorer

    Runner -->|"send_turn()"| Harness
    Runner -->|"AgentTrace"| Orch

    Orch -->|"scorer(…)"| Scorer
    Scorer -->|"Score"| Orch

    Harness -->|"читает"| Ctx

    style Runner fill:#d5e8d4,stroke:#82b366
    style Scorer fill:#d5e8d4,stroke:#82b366
```

**Файлы и детали:**

| Компонент | Файл | Детали |
|-----------|------|--------|
| RunConfig | `config.py` | Pydantic-модели |
| CLI | `cli.py` | Click |
| Orchestrator | `orchestrator.py` | — |
| Benchmark | `benchmarks/base.py` | `load_samples()` · `make_runner()` · `make_scorer()` |
| Runner | `runners/base.py` | `SingleTurnRunner` (default) · `BFCLRunner` |
| Harness | `harnesses/base.py` | `send_turn()` (современный) / `run_task()` (PAC1-legacy) |
| Scorer | `scorers/base.py` | ExactMatch · LLMJudge · Checkpoint · Subprocess |
| ExecutionContext | `context.py` | timeout · sandbox · mcp_url · cleanup_fns · extras |

---

## 2. Инфраструктура Docker и MCP

```mermaid
flowchart LR
    Orch["**Orchestrator**"]

    subgraph infra["Инфраструктура (per-task)"]
        MCP["**MCP HTTP Bridge**\nпорт :RANDOM"]
        DockerBridge["**Docker Bridge**\ndocker exec → sandbox"]
        SandboxMgr["**Sandbox Manager**"]
    end

    subgraph groups["Группы инструментов MCP"]
        Shell["shell"]
        FS["filesystem"]
        Browser["browser"]
        Custom["SandboxTool\nкастомные скрипты"]
    end

    AgentC["**Agent Container**\nHermes / OpenClaw / OpenCode"]
    SandboxC["**Benchmark Sandbox**"]

    Orch -->|"start() / stop()"| MCP
    Orch -->|"start() / stop()"| SandboxMgr
    SandboxMgr -->|"docker run / compose up"| SandboxC
    SandboxMgr -->|"inject_tools()"| SandboxC

    AgentC -->|"POST /mcp"| MCP
    MCP -->|"стандартные группы"| DockerBridge
    MCP -->|"кастомные"| Custom
    DockerBridge -->|"docker exec"| SandboxC

    Shell & FS & Browser --> DockerBridge
    Custom -.->|"инжектирован при старте"| SandboxC
```

**Файлы и детали:**

| Компонент | Файл | Детали |
|-----------|------|--------|
| MCP HTTP Bridge | `mcp/http_server.py` | FastAPI, JSON-RPC 2.0, MCP 2025-11-25 |
| Docker Bridge | `mcp/docker_bridge.py` | `docker exec {container_id} {cmd}` |
| Sandbox Manager | `sandbox.py` | `@register_sandbox()` |
| Группы инструментов | `mcp/tools/` | `shell` · `filesystem` · `browser` |
| SandboxTool | — | из `Sample.sandbox_tools`, через `sandbox.exec_stdin()` |

---

## 3. Хранилище результатов

```mermaid
flowchart LR
    Orch["**Orchestrator**"]
    DB["**Database**\nno-op если не настроен"]
    PG[("**PostgreSQL**")]
    Resume["**Resume check**\nstatus=='done' → skip"]

    Orch -->|"save_task_start()"| DB
    Orch -->|"save_task_output()"| DB
    Orch -->|"save_eval_result()"| DB
    Orch -->|"save_benchmark_summary()"| DB
    DB -->|"INSERT / ON CONFLICT"| PG

    Orch -->|"перед задачей"| Resume
    Resume -->|"fetch_task_output()"| DB
```

**Файлы и детали:**

| Компонент | Файл | Детали |
|-----------|------|--------|
| Database | `db.py` | asyncpg; no-op без настройки |
| PostgreSQL | — | таблицы: `runs` · `benchmark_runs` · `task_outputs` · `eval_results` · `schema_migrations` |
| save_task_output | — | пишет `status = done/error/timeout` |
