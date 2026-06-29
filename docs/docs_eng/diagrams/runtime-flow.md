# Runtime Flow (Dynamic)

The end-to-end flow of executing one task — what the C4 levels L1–L3 don't show in a single picture. Edge numbers = step order.

```mermaid
flowchart LR
    classDef control fill:#f0f4f8,stroke:#102a43,stroke-width:2px;
    classDef agent fill:#fff3e0,stroke:#e65100,stroke-width:2px;
    classDef env fill:#e8f5e9,stroke:#1b5e20,stroke-width:2px;

    Bench["Benchmark"]
    DB[("PostgreSQL")]
    Orch{"Orchestrator"}:::control

    subgraph run["Execution"]
        Runner["Runner"]:::control
        Agent["Agent"]:::agent
    end

    subgraph box["Sandbox"]
        MCP["MCP Bridge"]:::env
        Sandbox["Sandbox"]:::env
    end

    Scorer["Scorer"]:::control

    Bench -->|"tasks"| Orch
    DB <-->|"resume"| Orch

    Orch -->|"1 · bring up env"| MCP
    Orch -->|"2 · start loop"| Runner

    Runner <-->|"send_turn"| Agent
    Agent <-->|"tools (JSON-RPC)"| MCP
    MCP <-->|"docker exec"| Sandbox

    Runner -->|"3 · AgentTrace"| Orch
    Orch -->|"4 · scoring"| Scorer
    Scorer -.->|"5 · read (before stop)"| Sandbox
    Scorer -->|"6 · Score"| Orch
    Orch -->|"7 · persist"| DB
```

| Step | Action |
|------|--------|
| 1 | Bring up the sandbox + MCP Bridge (if `sample.sandbox` is set) |
| 2 | `runner.run(...)` — turn loop with the agent via `send_turn` |
| 3 | Runner returns an `AgentTrace` |
| 4–6 | The Scorer evaluates the result **while the sandbox is alive** → `Score` |
| 7 | Persist `task_output` + `eval_result` to the DB (no-op without PostgreSQL) |

> Related views: [context](context.md) (L1) · [containers](containers.md) (L2) · [components-framework](components-framework.md) (L3, the same lifecycle at the class level).
