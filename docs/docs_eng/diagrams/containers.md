# Containers

```mermaid
graph TD
    Researcher(["👤 ML engineer"])

    subgraph host["Host machine"]
        CLI["**Framework CLI**"]
        WebUI["**Web UI**\nlocalhost:8765"]
        Orch["**Orchestrator**"]
        MCP["**MCP HTTP Bridge**\nport per task"]

        subgraph docker["Docker Engine"]
            AgentC["**Agent Container**"]
            SandboxC["**Benchmark Sandbox**"]
        end

        PG[("**PostgreSQL**\noptional")]
    end

    LLM(["☁️ LLM Provider"])
    BitGN(["☁️ BitGN / PAC1 API"])

    Researcher -->|"starts a run"| CLI
    Researcher -->|"watches"| WebUI

    CLI -->|"run (headless)"| Orch
    CLI -->|"serve"| WebUI
    WebUI -->|"async task"| Orch

    Orch -->|"steps (WebSocket)"| WebUI
    Orch -->|"brings up"| MCP
    Orch -->|"docker run + SSE"| AgentC
    Orch -->|"docker run/compose"| SandboxC
    Orch -->|"INSERT (asyncpg)"| PG
    Orch -->|"LLM scoring"| LLM
    Orch -->|"tasks / submit_run"| BitGN

    AgentC -->|"tool calls (JSON-RPC)"| MCP
    AgentC -->|"completions"| LLM
    MCP -->|"docker exec"| SandboxC
```

**Technologies and lifecycle:**

| Container | Technologies | Lives |
|-----------|--------------|-------|
| Framework CLI | Python · Click · asyncio | whole run |
| Web UI | FastAPI · WebSocket · SPA | whole run |
| Orchestrator | asyncio · semaphore | whole run |
| MCP HTTP Bridge | FastAPI · JSON-RPC 2.0 | per task |
| Agent Container | `hermes-agent` (etc.) · `docker run --rm` | per task |
| Benchmark Sandbox | `docker run` / `compose up` | per task |
| PostgreSQL | asyncpg · single store | persistent |
