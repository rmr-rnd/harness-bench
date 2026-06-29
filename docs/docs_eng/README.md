# harness_bench Architecture Docs (C4)

Documentation follows the C4 model (Context → Containers → Components + ADR).
Diagrams are written in Mermaid.js and render natively in GitHub/GitLab.

## Structure

```
docs_eng/
├── architecture/        # Prose: descriptions, decisions, tables
│   ├── 01-system-context.md
│   ├── 02-containers.md
│   ├── 03-components-framework.md
│   ├── 04-components-harnesses.md
│   ├── 05-components-benchmarks.md
│   └── 06-adr.md
└── diagrams/            # Diagram source (Mermaid flowchart)
    ├── context.md
    ├── containers.md
    ├── components-framework.md       ← 3 diagrams: core, Docker/MCP, storage
    ├── components-harnesses.md       ← 3 diagrams: protocols, hierarchy, lazy start
    ├── components-benchmarks.md      ← 3 diagrams: taxonomy, scorers, Sample
    └── runtime-flow.md               ← dynamic view: end-to-end flow of one task (steps 1–7)
```

## Navigation

| Document | Level | Audience | Diagram |
|----------|-------|----------|---------|
| [01-system-context.md](architecture/01-system-context.md) | L1 | Everyone | [context.md](diagrams/context.md) |
| [02-containers.md](architecture/02-containers.md) | L2 | Architects, DevOps | [containers.md](diagrams/containers.md) |
| [03-components-framework.md](architecture/03-components-framework.md) | L3 | Developers | [components-framework.md](diagrams/components-framework.md) |
| [04-components-harnesses.md](architecture/04-components-harnesses.md) | L3 | Developers | [components-harnesses.md](diagrams/components-harnesses.md) |
| [05-components-benchmarks.md](architecture/05-components-benchmarks.md) | L3 | Developers | [components-benchmarks.md](diagrams/components-benchmarks.md) |
| [06-adr.md](architecture/06-adr.md) | ADR | Everyone | — |
| Runtime flow | Dynamic | Everyone | [runtime-flow.md](diagrams/runtime-flow.md) |

## Key concepts (quick cheat sheet)

| Concept | Where to look |
|---------|---------------|
| How one task runs (end-to-end flow) | [runtime-flow.md](diagrams/runtime-flow.md) |
| Task lifecycle at the class level | [03 — core diagram 1](diagrams/components-framework.md) |
| Two protocols: send_turn vs run_task | [04 — harnesses diagram 1](diagrams/components-harnesses.md) |
| Lazy container start | [04 — harnesses diagram 3](diagrams/components-harnesses.md) |
| How a Sample carries its sandbox config | [05 — benchmarks diagram 3](diagrams/components-benchmarks.md) |
| Why Runner and Scorer are separate | [ADR-009](architecture/06-adr.md#adr-009-runner-and-scorer-as-separate-abstractions) |
| Why MCP, not stdio | [ADR-002](architecture/06-adr.md#adr-002-mcp-http-bridge-between-agent-and-sandbox) |
| How to add a new harness | [04 — end of file](architecture/04-components-harnesses.md#how-to-add-a-new-harness) |
| How to add a new benchmark | [05 — end of file](architecture/05-components-benchmarks.md#how-to-add-a-new-benchmark) |
