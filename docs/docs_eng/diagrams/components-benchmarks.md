# Benchmark Components

Three diagrams: taxonomy, how Scorers work, how a Sample carries its sandbox config.

---

## 1. Benchmark taxonomy

```mermaid
flowchart TD
    ABC["**Benchmark ABC**\nload_samples · make_runner · make_scorer"]

    subgraph simple["No Docker sandbox — any harness"]
        SimpleQA["**SimpleQA**\nLLM judge"]
        BFCL["**BFCL**\nAST + own Runner"]
        BFCLMem["**BFCL Memory**\nAST + memory"]
        PersistBench["**PersistBench**\nLLM judge"]
        NIAH["**NIAH**\nLLM judge"]
        HumanEval["**HumanEval+**\ntests in subprocess"]
    end

    subgraph sandbox["With Docker or a remote workspace"]
        SWEBench["**SWE-bench**\n(+ multilingual)"]
        TAC["**TheAgentCompany**\nDocker Compose"]
        PAC1["**PAC1**\nBitGN API · run-level"]
    end

    ABC --> simple & sandbox

    style simple fill:#f9f9f9,stroke:#ccc
    style sandbox fill:#fff2cc,stroke:#d6b656
```

---

## 2. Scorers — how answers are evaluated

```mermaid
flowchart LR
    Inputs["**AgentTrace + Sample**\nfinal_output · steps\nground_truth · checkpoints"]

    Base["**Base scorers**\nbricks for\ncustom benchmarks"]

    subgraph scorers["Each built-in benchmark has its own Scorer subclass"]
        LLMJudge["**LLM judge**\nSimpleQA · NIAH · PersistBench"]
        AST["**AST comparison**\nBFCL · BFCL Memory"]
        Local["**Subprocess**\nHumanEval+"]
        Eval["**eval.sh in sandbox**\nSWE-bench"]
        TACScorer["**Weighted checkpoints**\nTheAgentCompany"]
        Pac1Scorer["**Run-level grade**\nPAC1"]
    end

    Score["**Score**\nscore 0.0–1.0 · grade\nexplanation · judge_model"]

    Base -.->|"reused"| scorers
    Inputs --> scorers
    scorers --> Score
```

**Scorer classes:**

| Group | Class(es) | Result |
|-------|-----------|--------|
| Base (`scorers/base.py`) | ExactMatch · LLMJudge · Checkpoint · Subprocess | for custom benchmarks |
| LLM judge | `_SimpleQAScorer` · `_NIAHScorer` (rubric 1/3/5/7/10) · `_PersistBenchScorer` | CORRECT / INCORRECT / NOT_ATTEMPTED |
| AST | `_BFCLScorer` · `_BFCLMemoryScorer` | CORRECT / INCORRECT |
| Subprocess | `_HumanEvalScorer` (no Docker) | CORRECT / INCORRECT |
| Sandbox | `SWEBenchScorer` (FAIL_TO_PASS) | CORRECT / INCORRECT |
| Sandbox | `SandboxEvalScorer` | score = Σearned / Σmax |
| Run-level | `_Pac1Scorer` (`_apply_run_grades`) | EVALUATING → real score |

---

## 3. Sample carries its sandbox config

```mermaid
flowchart TD
    Bench["**Benchmark.load_samples()**"]

    Sample["**Sample**\nmessages · ground_truth · tools\n📦 sandbox · 🔧 sandbox_tools\n✅ checkpoints · 🔌 mcp_tool_groups"]

    Orch["**Orchestrator**\n_build_ctx()"]

    subgraph infra["Brought up when sample.sandbox is set"]
        SandboxC["Docker container"]
        MCPS["MCP HTTP Bridge"]
        Injected["Injected\ntools"]
    end

    Ctx["**ExecutionContext**\nsandbox · mcp_url · mcp_server"]

    Bench --> Sample
    Sample --> Orch
    Orch -->|"sample.sandbox"| SandboxC
    Orch -->|"inject_tools()"| Injected
    Orch -->|"mcp_tool_groups"| MCPS

    SandboxC & Injected & MCPS -->|"into ctx"| Ctx
```

**Sample fields:** `id` · `benchmark` · `messages` · `ground_truth` · `system_prompt` · `tools` · `metadata` · `epochs` · `sandbox: SandboxSpec | None` · `sandbox_tools: list[SandboxTool]` · `checkpoints: list[Checkpoint]` · `mcp_tool_groups: list[str]`. The container type comes from `SandboxSpec`; injection goes to `/.sandbox_tools/{name}/`.
