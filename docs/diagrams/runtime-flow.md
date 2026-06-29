# Runtime Flow (Dynamic)

Сквозной поток выполнения одной задачи — что C4-уровни L1–L3 не показывают единой картинкой. Нумерация рёбер = порядок шагов.

```mermaid
flowchart LR
    classDef control fill:#f0f4f8,stroke:#102a43,stroke-width:2px;
    classDef agent fill:#fff3e0,stroke:#e65100,stroke-width:2px;
    classDef env fill:#e8f5e9,stroke:#1b5e20,stroke-width:2px;

    Bench["Benchmark"]
    DB[("PostgreSQL")]
    Orch{"Orchestrator"}:::control

    subgraph run["Выполнение"]
        Runner["Runner"]:::control
        Agent["Агент"]:::agent
    end

    subgraph box["Песочница"]
        MCP["MCP Bridge"]:::env
        Sandbox["Sandbox"]:::env
    end

    Scorer["Scorer"]:::control

    Bench -->|"задачи"| Orch
    DB <-->|"resume"| Orch

    Orch -->|"1 · поднять среду"| MCP
    Orch -->|"2 · старт цикла"| Runner

    Runner <-->|"send_turn"| Agent
    Agent <-->|"tools (JSON-RPC)"| MCP
    MCP <-->|"docker exec"| Sandbox

    Runner -->|"3 · AgentTrace"| Orch
    Orch -->|"4 · оценка"| Scorer
    Scorer -.->|"5 · чтение (до остановки)"| Sandbox
    Scorer -->|"6 · Score"| Orch
    Orch -->|"7 · сохранение"| DB
```

| Шаг | Действие |
|-----|----------|
| 1 | Поднять sandbox + MCP Bridge (если `sample.sandbox` задан) |
| 2 | `runner.run(...)` — цикл ходов с агентом через `send_turn` |
| 3 | Runner возвращает `AgentTrace` |
| 4–6 | Scorer оценивает результат **пока sandbox жив** → `Score` |
| 7 | Сохранить `task_output` + `eval_result` в БД (no-op без PostgreSQL) |

> Связанные виды: [context](context.md) (L1) · [containers](containers.md) (L2) · [components-framework](components-framework.md) (L3, тот же жизненный цикл на уровне классов).
