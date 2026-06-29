# 03 — Components: Framework (Orchestrator Container)

> Diagram: [diagrams/components-framework.md](../diagrams/components-framework.md)

## Overview

The framework's Python process is built around three core abstractions: **Runner** (drives the conversation turns), **Scorer** (evaluates the result), **TurnResponse** (a unified harness reply). This lets you combine any benchmark with any agent without changing code.

## Components

### CLI (`framework/cli.py`)

A wrapper over Click. Loads `RunConfig` from YAML, starts the Orchestrator.

| Command | Purpose |
|---------|---------|
| `serve [--config config.yaml] [--open-browser]` | Bring up the Web UI as a service (pick/edit a config in the browser) |
| `run config.yaml` | Headless run (no UI) |
| `results run_id --config config.yaml` | View a run's results |
| `compare run_a run_b --config config.yaml` | Compare two runs |
| `runs config.yaml` / `db-runs config.yaml` | List runs from the DB |
| `db-init config.yaml` | Initialize the DB schema |

---

### RunConfig / Models (`framework/config.py` + `framework/models.py`)

**Config models:**

| Class | Purpose |
|-------|---------|
| `RunConfig` | Root object. Holds all other models |
| `ModelConfig` | API params: base_url, api_key, model_name, temperature, max_tokens |
| `HarnessConfig` | Harness type + `benchmark_harness: dict` (per-benchmark overrides) |
| `BenchmarkConfig` | Name, data paths, limit, filters |
| `ParallelismConfig` | workers, timeout_per_task, eval_timeout |
| `SearchConfig` | tavily_api_key for web search |
| `DockerConfig` | network, cleanup_after |
| `DatabaseConfig` | PostgreSQL connection |
| `SWEBenchConfig` | namespace, image_mode |

**Data models:**

| Class | Purpose |
|-------|---------|
| `Sample` | The primary data unit (formerly `Task`). Holds messages, ground_truth, tools, metadata, **sandbox, sandbox_tools, checkpoints, mcp_tool_groups** |
| `SandboxSpec` | Docker environment spec: type, image, compose_file, config dict |
| `SandboxTool` | Custom tool: source_dir, entrypoint, runner, install_cmd, timeout, max_output_bytes |
| `Checkpoint` | Bash check: cmd, weight, target_exit_code, timeout |
| `AgentTrace` | Execution result: final_output, steps, tokens, error |
| `Score` | Evaluation (formerly `EvalResult`): score 0–1, grade, explanation, judge_model |
| `Step` | One agent step: type, content, ts |
| `TurnResponse` | Unified harness reply: text, tool_calls, finish_reason, input_tokens, output_tokens, steps |

> **Renames in code:** `Task = Sample`, `EvalResult = Score` — aliases for backward compatibility. Properties: `Sample.target` ← `ground_truth`, `Sample.needs_web_search` ← `web_search`.

---

### Orchestrator (`framework/orchestrator.py`)

> Key decisions: [ADR-003 Resume](06-adr.md#adr-003-resume-via-task-status-in-the-db) · [ADR-004 Evaluate while alive](06-adr.md#adr-004-scorer-runs-while-the-sandbox-is-alive) · [ADR-006 Parallelism](06-adr.md#adr-006-parallelism-via-asyncio--semaphore)

Key fields: `run_id`, `harness`, `_benchmark_harnesses`, `judge`, `db`.

Lifecycle of one task in `_run_task()`:

```
# Resume: check the DB (only when PostgreSQL is present)   # → ADR-003
existing = await db.fetch_task_output(run_id, task_id)
if existing and existing["status"] == "done": skip

await db.save_task_start(run_id, task_id)   # status = "running"

# Scorer is built before ctx (some scorers need log_dir)
scorer = benchmark.make_scorer()

ctx = ExecutionContext(timeout, step_cb, web_search=task.web_search)
ctx = await _build_ctx(task, benchmark, ctx)
  ├── if sample.sandbox → docker + MCP Bridge (get_mcp_host())
  └── sets ctx.sandbox, ctx.mcp_url
ctx.harness_type = harness.type

# Modern protocol (not PAC1):
runner = benchmark.make_runner(model_cfg=cfg.model)
trace  = await runner.run(sample, harness.send_turn, ctx)
score  = await scorer(sample, trace, judge, sandbox=ctx.sandbox)

# Legacy (PAC1):
trace  = await harness.run_task(sample, ctx)
score  = await scorer(sample, trace, judge)

await _teardown_ctx(ctx)                    # cleanup_fns + MCP + sandbox → ADR-004

# Persist to the DB (no-op if PostgreSQL is not configured)
await db.save_task_output(run_id, benchmark, trace)
await db.save_eval_result(run_id, benchmark, result)
```

---

### Benchmark Registry

`@register_benchmark("name")` — a decorator that registers the class. `resolve_benchmark(name)` (in `benchmarks/base.py`) returns the class; the orchestrator calls it through an internal wrapper `_resolve_benchmark()`, which first runs `discover_all()`. 10 registered benchmarks: simpleqa, bfcl, bfcl_memory, humaneval_plus, niah, persistbench, swe_bench, swe_bench_multilingual, theagentcompany, pac1.

---

### Harness Registry (`framework/harnesses/__init__.py`)

> Key decision: [ADR-005 Independent plugins](06-adr.md#adr-005-benchmark-and-harness-are-independent-plugins)

`load_harness_class(type_name)` — looks up the module `framework.harnesses.{type_name}` via `importlib`. Auto-convention: for benchmark `bfcl` + harness `hermes` it automatically tries `bfcl_hermes`.

---

### Runner (`framework/runners/base.py`)

> Key decision: [ADR-009 Runner/Scorer split](06-adr.md#adr-009-runner-and-scorer-as-separate-abstractions)

```python
class Runner(ABC):
    async def run(
        sample: Sample,
        send_turn: SendTurnFn,  # harness.send_turn — passed as a callback
        ctx: ExecutionContext
    ) -> AgentTrace: ...
```

**Implementations:**
- `SingleTurnRunner` — calls `send_turn()` once. Default for most benchmarks
- `BFCLRunner` (in `benchmarks/bfcl/`) — multi-turn: agent calls functions → mock backend → next turn

A benchmark selects a Runner via `make_runner()`. This lets the benchmark control the conversation logic without touching the harness.

---

### Scorer (`framework/scorers/base.py`)

> Key decision: [ADR-009 Runner/Scorer split](06-adr.md#adr-009-runner-and-scorer-as-separate-abstractions)

```python
class Scorer(ABC):
    async def __call__(
        sample: Sample,
        trace: AgentTrace,
        judge: LLMJudge,
        sandbox: Sandbox | None = None
    ) -> Score: ...
```

**Base scorer classes** (`framework/scorers/`) — reusable "bricks" for custom benchmarks:

| Scorer | Logic |
|--------|-------|
| `ExactMatchScorer` | Normalized string comparison |
| `LLMJudgeScorer` | Question + reference + answer → LLM → CORRECT/INCORRECT |
| `CheckpointScorer` | Bash commands in the sandbox, weighted pass/fail |
| `SubprocessScorer` | External script runner |

> **Important:** no built-in benchmark instantiates these base classes directly — each has its own `Scorer` subclass (see the table in [05](05-components-benchmarks.md)). The base scorers exist as ready-made components for **custom** benchmarks.

**Built-in benchmarks' own scorers** (each subclasses `Scorer`):

| Scorer | Logic | Benchmark |
|--------|-------|-----------|
| `_SimpleQAScorer` | LLM-judge over question+reference+answer | SimpleQA |
| `_NIAHScorer` | LLM-judge with a numeric rubric (1/3/5/7/10) | NIAH |
| `_PersistBenchScorer` | LLM-judge with per-type prompts | PersistBench |
| `_BFCLScorer` / `_BFCLMemoryScorer` | AST comparison of function calls | BFCL, BFCL Memory |
| `_HumanEvalScorer` | Run tests in a local subprocess (no Docker) | HumanEval+ |
| `SWEBenchScorer` | `eval.sh` in the sandbox → RESOLVED/APPLIED/NO_GENERATION | SWE-bench (+ multilingual) |
| `SandboxEvalScorer` | Per-task evaluator modules → weighted CheckpointResult | TheAgentCompany |
| `_Pac1Scorer` | grade=`EVALUATING`, the real score arrives run-level (inline in pac1/benchmark.py) | PAC1 |

A benchmark selects a Scorer via `make_scorer()`.

---

### Benchmark (ABC) (`framework/benchmarks/base.py`)

> Key decision: [ADR-005 Independent plugins](06-adr.md#adr-005-benchmark-and-harness-are-independent-plugins)

```python
class Benchmark(ABC):
    name: str

    def load_samples(self) -> list[Sample]: ...

    def make_runner(self, model_cfg=None) -> Runner:
        return SingleTurnRunner()  # default

    def make_scorer(self) -> Scorer: ...

    def format_prompt(self, sample: Sample) -> list[dict]:
        return [system_prompt] + messages  # default
```

**Important:** sandbox config now lives in `Sample`, not in the Benchmark. The Benchmark only loads data and assigns the Runner/Scorer.

---

### Harness (ABC) (`framework/harnesses/base.py`)

> Key decisions: [ADR-001 Agent in Docker](06-adr.md#adr-001-the-agent-runs-in-docker-not-in-process) · [ADR-010 Lazy container start](06-adr.md#adr-010-lazy-container-start-and-ctxcleanup_fns)

| Attribute | Value |
|-----------|-------|
| `type: str` | Name in the registry |
| `supports_sandbox: bool` | True → can work with the MCP Bridge (connects to the address from `get_mcp_host()`) |
| `SUPPORTS_RUNNER_PROTOCOL: bool` | True → implements `send_turn()`, False → implements `run_task()` (PAC1) |

**Modern protocol:**

```python
async def send_turn(
    messages: list[dict],
    tools: list[dict],
    system_prompt: str,
    ctx: ExecutionContext,
    timeout: int,
    **kwargs
) -> TurnResponse:
    # Lazily start the container (if not running)
    # POST /v1/responses with stream=True
    # Parse SSE → TurnResponse
```

The container starts on the first `send_turn()` call and is stored in `ctx.extras["harness_session"]`. A cleanup callback to stop it is registered in `ctx.cleanup_fns`.

---

### ExecutionContext (`framework/context.py`)

| Field | Type | Purpose |
|-------|------|---------|
| `timeout` | `int` | Agent time limit (sec) |
| `step_cb` | `Callable` | Callback for streaming steps to the UI |
| `web_search` | `bool` | Whether web search is allowed for this task (from `task.web_search`) |
| `harness_type` | `str` | Harness type — set by the Orchestrator before `runner.run()` |
| `sandbox` | `Sandbox \| None` | The started sandbox container |
| `mcp_url` | `str \| None` | MCP Bridge URL for the agent |
| `mcp_server` | `MCPHttpServer \| None` | The server, for teardown |
| `cleanup_fns` | `list[Callable]` | Async callbacks for teardown (docker stop, …) |
| `extras` | `ContextExtras` | A typed TypedDict with known keys |

**ContextExtras (TypedDict):**

| Key | Type | Set by |
|-----|------|--------|
| `harness_session` | `dict` | Harness on the first `send_turn()` — container_id, previous_response_id |
| `bfcl_mcp_server` | `BFCLMCPServer` | Orchestrator for BFCL |
| `bfcl_snapshot_dir` | `str` | Orchestrator for memory scenarios |
| `bfcl_func_doc_dir` | `str` | Orchestrator for BFCL |
| `swe_instance_id` | `str` | Orchestrator for SWE-bench |
| `swe_namespace` | `str` | Orchestrator for SWE-bench |
| `pac1_runtime_url` | `str` | PAC1 harness |
| `bfcl_mcp_port` | `int` | Orchestrator for BFCL |

---

### Sandbox Manager (`framework/sandbox.py`)

> Key decision: [ADR-001 Agent in Docker](06-adr.md#adr-001-the-agent-runs-in-docker-not-in-process)

`@register_sandbox("docker_run")` / `@register_sandbox("docker_compose")` — register implementations.
`make_sandbox(SandboxSpec) → Sandbox` — the factory.

| Sandbox method | Purpose |
|----------------|---------|
| `start()` | docker run / compose up |
| `stop()` | docker stop / compose down |
| `exec_bash(cmd, timeout)` | run bash in the container |
| `exec_stdin(runner, path, data)` | run a SandboxTool script with JSON on stdin |
| `read_file(path)` | read a file from the container |
| `write_file(content, path)` | write a file into the container |
| `inject_tools(tools)` | tar → /.sandbox_tools/{name}/ + install_cmd |

---

### MCP HTTP Bridge (`framework/mcp/http_server.py`)

> Key decision: [ADR-002 MCP as the broker](06-adr.md#adr-002-mcp-http-bridge-between-agent-and-sandbox)

FastAPI on a random port. POST `/mcp` — JSON-RPC 2.0 / MCP 2025-11-25.

Routing for `tools/call`:
1. Custom tool (from `sample.sandbox_tools`) → `sandbox.exec_stdin()`
2. Standard group (shell/filesystem/browser) → `DockerBridge` → `docker exec`

---

### LLM Judge (`framework/evaluators/llm_judge.py`)

OpenAI API. Prompt: question + reference + answer → a single letter → CORRECT/INCORRECT/NOT_ATTEMPTED.

---

### Utils (`framework/utils/`)

| Module | Function | Purpose |
|--------|----------|---------|
| `network.py` | `get_mcp_host()` | Address for the MCP Bridge: `host.docker.internal` by default, or the container's own IP under `HARNESS_DOCKER_NETWORK` (Docker-in-Docker). Overridable via `HARNESS_MCP_HOST`. |
| `work_dir.py` | `make_work_dir(prefix)` | Create a temp directory. If `HARNESS_WORK_DIR` is set — create it inside there (for mounting into Docker). |
| `work_dir.py` | `make_work_file(prefix, suffix)` | Create a temp file with the same behavior. |

`HARNESS_DOCKER_NETWORK` — Docker network name: when present, the harness adds `--network {name}` when starting agent containers and uses the IP instead of `host.docker.internal`.

---

### Database (`framework/db.py`)

> Key decision: [ADR-008 PostgreSQL as the single store](06-adr.md#adr-008-postgresql-as-the-single-result-store)

Async asyncpg. No-op if the DB isn't configured or asyncpg isn't installed. Auto-migration on `connect()` — SQL files from `db/migrations/` under an advisory lock (race protection). Secrets redaction in config_yaml before saving.

Tables: `runs`, `benchmark_runs`, `task_outputs` (status: running/done/error/timeout), `eval_results`, `schema_migrations`.
