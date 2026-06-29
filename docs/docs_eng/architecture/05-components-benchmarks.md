# 05 — Components: Benchmark Subsystem

> Diagram: [diagrams/components-benchmarks.md](../diagrams/components-benchmarks.md)

## Benchmark ABC

```python
class Benchmark(ABC):
    name: str

    def load_samples(self) -> list[Sample]: ...

    def make_runner(self, model_cfg=None) -> Runner:
        return SingleTurnRunner()   # overridden for multi-turn

    def make_scorer(self) -> Scorer: ...

    def format_prompt(self, sample: Sample) -> list[dict]:
        return [system_prompt_msg] + messages  # default
```

**Important:** sandbox config (`SandboxSpec`, `SandboxTool`, `Checkpoint`, `mcp_tool_groups`) now lives in `Sample`, not in the Benchmark. The Benchmark only loads data and assigns the Runner/Scorer.

---

## Benchmark table

> Every scorer below is each benchmark's own `Scorer` subclass, not a base class from `framework/scorers/` (see the note in [03](03-components-framework.md#scorer-frameworkscorersbasepy)).

### Without Docker

| Benchmark | Runner | Scorer | Data |
|-----------|--------|--------|------|
| **SimpleQA** | SingleTurn | `_SimpleQAScorer` (LLM-judge) | `benchmarks_data/simpleqa/` |
| **BFCL** | BFCLRunner (multi-turn) | `_BFCLScorer` (AST checker) | `benchmarks_data/bfcl/` |
| **BFCL Memory** | BFCLMemoryRunner (multi-session) | `_BFCLMemoryScorer` (AST checker) | `benchmarks_data/bfcl/` |
| **HumanEval+** | SingleTurn | `_HumanEvalScorer` (tests in a local subprocess) | `benchmarks_data/humaneval_plus/` |
| **PersistBench** | SingleTurn | `_PersistBenchScorer` (LLM-judge) | `benchmarks_data/persistbench/` |
| **NIAH** | SingleTurn | `_NIAHScorer` (LLM-judge, rubric 1/3/5/7/10) | `benchmarks_data/niah/` |

> HumanEval+ does **not** use a Docker sandbox: the generated code runs via `subprocess.run([sys.executable, ...])` directly on the host.

### With Docker (sample.sandbox is set)

| Benchmark | Sandbox type | Scorer | Data |
|-----------|--------------|--------|------|
| **SWE-bench** | `swe_bench`: per-repo image | `SWEBenchScorer` (eval.sh) | `benchmarks_data/swe_bench/swe_bench_12tasks.json` |
| **SWE-bench Multilingual** | `swe_bench`: per-repo image | `SWEBenchScorer` (eval.sh) | `benchmarks_data/swe_bench/swe_bench_multilingual_8tasks.json` |
| **TheAgentCompany** | `docker_compose` | `SandboxEvalScorer` (per-task evaluator, weighted) | `benchmarks_data/theagentcompany/` |
| **PAC1** | Remote PCM workspace (BitGN) | `_Pac1Scorer` (run-level grade) | BitGN API (not local files) |

---

## Details of key benchmarks

### BFCL — Berkeley Function Calling

> Key decision: [ADR-007 In-process mock backends](06-adr.md#adr-007-bfcl-uses-in-process-mock-backends)

**Function-encoding quirk:** functions are passed as JSON in the message text, **not** via the OpenAI tools API. The agent replies in the format `[function_name(param=value)]`. An AST parser extracts the calls and compares them with the reference.

**BFCLRunner (in `benchmarks/bfcl/`):**
- Starts `BFCLMCPServer` with in-process mock backends (GorillaFileSystem, MathAPI, TwitterAPI, …)
- Each turn: agent calls a function → mock returns a result → next turn
- All conversation logic lives in the Runner; the harness knows nothing about BFCL

**AST Checker** (`bfcl/_shared/ast_checker.py`):
- Compares calls via Python AST
- Supports: nested types, optional params, partial match, `miss_func` (functions revealed along the way)

---

### SWE-bench

**SWEbenchSandbox** (type=`swe_bench`, registered in `benchmarks/swe_bench/sandbox.py`):
- A Docker image with a repo clone and dependencies
- `get_patch()` → `git diff HEAD` — what the agent changed
- `run_eval_script(script)` → run eval.sh, return the log

**`SWEBenchScorer`** (while the sandbox is alive):
```
sandbox.get_patch()        → trace._agent_patch
sandbox.run_eval_script()  → trace._eval_log
parse FAIL_TO_PASS / PASS_TO_PASS from the log → score 1.0 / 0.0
grade = CORRECT (all tests passed) | INCORRECT
```
The patch and eval log are saved to the DB in `task_outputs.agent_patch` and `task_outputs.eval_log`.

---

### TheAgentCompany

A Docker Compose stack: Gitea, MariaDB, Redis, web server, mail server.

**Tools in the sandbox** (via the MCP Bridge):
- `bash` — shell commands
- `python` — Python scripts
- `web_browser_go/click/type/type_submit/scroll` — browser control

**Scoring — `SandboxEvalScorer`:**
- Dynamically imports a per-task evaluator module from `theagentcompany/evaluators/` (name in `sample.metadata["evaluator_module"]`)
- The module returns a list of weighted `CheckpointResult` (`{id, value, max_value}`)
- `score = sum(value) / sum(max_value)`, `min(1.0)`
- Grade: CORRECT (≥1.0), PARTIAL (>0), INCORRECT (0)
- An LLM judge is available to the evaluator for fuzzy checks

---

### PAC1

```python
class Pac1Benchmark(Benchmark):
    def load_samples(self):
        # 1. client.get_benchmark(id) → task definitions
        # 2. client.start_run(id, name, api_key) → run_id + trial_ids
        # 3. Creates a Sample per trial_id with trial_id in metadata
```

Pac1Scorer reads `trace._pac1_result` (outcome) and immediately sets `grade="ERROR"` only for `OUTCOME_ERR_INTERNAL`; otherwise — `grade="EVALUATING"`, `score=0.0`. Real scores arrive run-level: after all tasks the orchestrator calls `harness.await_run_grades()` (→ `submit_run()` → `SubmitRunResponse.trials[]`) and rewrites score/grade via `_apply_run_grades()`.

---

## How to add a new Benchmark

### A simple benchmark

```python
# framework/benchmarks/my_bench.py
from framework.benchmarks.base import Benchmark
from framework.scorers.llm_judge import LLMJudgeScorer

@register_benchmark("my_bench")
class MyBenchmark(Benchmark):
    name = "my_bench"

    def load_samples(self) -> list[Sample]:
        # reads self.cfg.tasks_dir
        ...

    def make_scorer(self):
        return LLMJudgeScorer()
```

### A sandbox benchmark

The sandbox config is now set right in the `Sample` at `load_samples()`:

```python
def load_samples(self) -> list[Sample]:
    samples = []
    for item in load_json(self.cfg.tasks_dir):
        samples.append(Sample(
            id=item["id"],
            benchmark=self.name,
            messages=[Message(role="user", content=item["prompt"])],
            ground_truth=item["answer"],
            # Sandbox config right in the Sample:
            sandbox=SandboxSpec(type="docker_run", image="ubuntu:22.04"),
            mcp_tool_groups=["shell", "filesystem"],
            checkpoints=[
                Checkpoint(name="result_exists", cmd="test -f /result.txt", weight=1.0),
            ],
            sandbox_tools=[],  # custom tools if needed
        ))
    return samples

def make_scorer(self):
    return CheckpointScorer()
```

### Registration

The `@register_benchmark("my_bench")` decorator does everything automatically. Add the data:

```yaml
benchmarks:
  - name: my_bench
    tasks_dir: benchmarks_data/my_bench/questions
    limit: 50
```
