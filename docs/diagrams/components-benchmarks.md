# Benchmark Components

Три диаграммы: таксономия, как работают Scorers, как Sample несёт sandbox-конфиг.

---

## 1. Таксономия бенчмарков

```mermaid
flowchart TD
    ABC["**Benchmark ABC**\nload_samples · make_runner · make_scorer"]

    subgraph simple["Без Docker-sandbox — любой harness"]
        SimpleQA["**SimpleQA**\nLLM-судья"]
        BFCL["**BFCL**\nAST + свой Runner"]
        BFCLMem["**BFCL Memory**\nAST + память"]
        PersistBench["**PersistBench**\nLLM-судья"]
        NIAH["**NIAH**\nLLM-судья"]
        HumanEval["**HumanEval+**\nтесты в subprocess"]
    end

    subgraph sandbox["С Docker или удалённым workspace"]
        SWEBench["**SWE-bench**\n(+ multilingual)"]
        TAC["**TheAgentCompany**\nDocker Compose"]
        PAC1["**PAC1**\nBitGN API · run-level"]
    end

    ABC --> simple & sandbox

    style simple fill:#f9f9f9,stroke:#ccc
    style sandbox fill:#fff2cc,stroke:#d6b656
```

---

## 2. Scorers — как оцениваются ответы

```mermaid
flowchart LR
    Inputs["**AgentTrace + Sample**\nfinal_output · steps\nground_truth · checkpoints"]

    Base["**Базовые scorer'ы**\nкирпичики для\nкастомных бенчмарков"]

    subgraph scorers["Свой подкласс Scorer у каждого встроенного бенчмарка"]
        LLMJudge["**LLM-судья**\nSimpleQA · NIAH · PersistBench"]
        AST["**AST-сравнение**\nBFCL · BFCL Memory"]
        Local["**Subprocess**\nHumanEval+"]
        Eval["**eval.sh в sandbox**\nSWE-bench"]
        TACScorer["**Weighted checkpoints**\nTheAgentCompany"]
        Pac1Scorer["**Run-level grade**\nPAC1"]
    end

    Score["**Score**\nscore 0.0–1.0 · grade\nexplanation · judge_model"]

    Base -.->|"переиспользуются"| scorers
    Inputs --> scorers
    scorers --> Score
```

**Классы scorer'ов:**

| Группа | Класс(ы) | Результат |
|--------|----------|-----------|
| Базовые (`scorers/base.py`) | ExactMatch · LLMJudge · Checkpoint · Subprocess | для кастомных бенчмарков |
| LLM-судья | `_SimpleQAScorer` · `_NIAHScorer` (рубрика 1/3/5/7/10) · `_PersistBenchScorer` | CORRECT / INCORRECT / NOT_ATTEMPTED |
| AST | `_BFCLScorer` · `_BFCLMemoryScorer` | CORRECT / INCORRECT |
| Subprocess | `_HumanEvalScorer` (без Docker) | CORRECT / INCORRECT |
| Sandbox | `SWEBenchScorer` (FAIL_TO_PASS) | CORRECT / INCORRECT |
| Sandbox | `SandboxEvalScorer` | score = Σearned / Σmax |
| Run-level | `_Pac1Scorer` (`_apply_run_grades`) | EVALUATING → реальная оценка |

---

## 3. Sample несёт sandbox-конфигурацию

```mermaid
flowchart TD
    Bench["**Benchmark.load_samples()**"]

    Sample["**Sample**\nmessages · ground_truth · tools\n📦 sandbox · 🔧 sandbox_tools\n✅ checkpoints · 🔌 mcp_tool_groups"]

    Orch["**Orchestrator**\n_build_ctx()"]

    subgraph infra["Поднимается при наличии sample.sandbox"]
        SandboxC["Docker контейнер"]
        MCPS["MCP HTTP Bridge"]
        Injected["Инжектированные\nинструменты"]
    end

    Ctx["**ExecutionContext**\nsandbox · mcp_url · mcp_server"]

    Bench --> Sample
    Sample --> Orch
    Orch -->|"sample.sandbox"| SandboxC
    Orch -->|"inject_tools()"| Injected
    Orch -->|"mcp_tool_groups"| MCPS

    SandboxC & Injected & MCPS -->|"в ctx"| Ctx
```

**Поля Sample:** `id` · `benchmark` · `messages` · `ground_truth` · `system_prompt` · `tools` · `metadata` · `epochs` · `sandbox: SandboxSpec | None` · `sandbox_tools: list[SandboxTool]` · `checkpoints: list[Checkpoint]` · `mcp_tool_groups: list[str]`. Тип контейнера берётся из `SandboxSpec`; инжект — в `/.sandbox_tools/{name}/`.
