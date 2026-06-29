# Containers

```mermaid
graph TD
    Researcher(["👤 ML-инженер"])

    subgraph host["Хост-машина"]
        CLI["**Framework CLI**"]
        WebUI["**Web UI**\nlocalhost:8765"]
        Orch["**Orchestrator**"]
        MCP["**MCP HTTP Bridge**\nпорт на задачу"]

        subgraph docker["Docker Engine"]
            AgentC["**Agent Container**"]
            SandboxC["**Benchmark Sandbox**"]
        end

        PG[("**PostgreSQL**\nопционально")]
    end

    LLM(["☁️ LLM Provider"])
    BitGN(["☁️ BitGN / PAC1 API"])

    Researcher -->|"запускает"| CLI
    Researcher -->|"наблюдает"| WebUI

    CLI -->|"run (headless)"| Orch
    CLI -->|"serve"| WebUI
    WebUI -->|"async task"| Orch

    Orch -->|"шаги (WebSocket)"| WebUI
    Orch -->|"поднимает"| MCP
    Orch -->|"docker run + SSE"| AgentC
    Orch -->|"docker run/compose"| SandboxC
    Orch -->|"INSERT (asyncpg)"| PG
    Orch -->|"LLM-оценка"| LLM
    Orch -->|"задачи / submit_run"| BitGN

    AgentC -->|"tool calls (JSON-RPC)"| MCP
    AgentC -->|"completions"| LLM
    MCP -->|"docker exec"| SandboxC
```

**Технологии и жизненный цикл:**

| Контейнер | Технологии | Живёт |
|-----------|-----------|-------|
| Framework CLI | Python · Click · asyncio | весь прогон |
| Web UI | FastAPI · WebSocket · SPA | весь прогон |
| Orchestrator | asyncio · semaphore | весь прогон |
| MCP HTTP Bridge | FastAPI · JSON-RPC 2.0 | на задачу |
| Agent Container | `hermes-agent` (и др.) · `docker run --rm` | на задачу |
| Benchmark Sandbox | `docker run` / `compose up` | на задачу |
| PostgreSQL | asyncpg · единственное хранилище | постоянно |
