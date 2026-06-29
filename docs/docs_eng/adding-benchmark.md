# Adding a new benchmark

## Architecture

Every benchmark is a class derived from `Benchmark` (`framework/benchmarks/base.py`) with the `@register_benchmark('name')` decorator. The orchestrator calls `load_samples()` → runs the agent → passes the result to `make_scorer()`.

All benchmarks under `framework/benchmarks/` are picked up **automatically** at startup.

## Step 1 — Create the benchmark file

`framework/benchmarks/my_bench.py` (or `framework/benchmarks/my_bench/benchmark.py`):

```python
from framework.benchmarks.base import Benchmark, register_benchmark

@register_benchmark("my_bench")
class MyBench(Benchmark):
    display_name  = "My Bench"
    description   = "Benchmark description for the AI analyzer."
    category      = "Programming"
    default_paths = (
        "benchmarks_data/my_bench/questions",
        "benchmarks_data/my_bench/answers",
    )

    def load_samples(self): ...
    def make_scorer(self): ...
```

### `load_samples()`

Returns a list of `Sample`. Each `Sample` describes one task: messages to the agent, the reference answer, metadata.

### `make_scorer()`

Returns a `Scorer` that evaluates the agent's answer and returns a `Score`:

- `grade` — string: `CORRECT`, `INCORRECT`, `NOT_ATTEMPTED`, `PARTIAL`, `ERROR`, `TIMEOUT` (custom values allowed)
- `score` — float from 0.0 to 1.0
- `explanation` — text for the Web UI

In the Web UI, `✓` counts `CORRECT + PASS`, `✗` counts `INCORRECT + FAIL`. Other grades go into n but not into ✓/✗.

### `make_runner()` (optional)

Defaults to `SingleTurnRunner` — a single `send_turn` call. If you need multi-turn logic (tool calls, iterations) — create your own `Runner` and override this method.

## Step 2 — Add the data

```
benchmarks_data/my_bench/
  questions/
  answers/
```

## Summary

| Action | File |
|--------|------|
| Create the class with metadata | `framework/benchmarks/my_bench.py` |
| Implement `load_samples()` and `make_scorer()` | same file |
| Add the data | `benchmarks_data/my_bench/` |

Nothing else is needed — the benchmark shows up in the Web UI and reports automatically.

## Sandbox benchmarks

If a task needs a Docker environment, set `sandbox` in the `Sample`:

```python
Sample(
    ...
    sandbox=SandboxSpec(
        type="docker_compose",          # or "docker_run"
        compose_file="/abs/path/to/compose.yaml",
        target_service="default",
    ),
    mcp_tool_groups=["shell", "filesystem", "browser"],
)
```

Available MCP tool groups (modules under `framework/mcp/tools/`, wired up by the HTTP bridge `framework/mcp/http_server.py`):
- `shell` — bash commands in the sandbox
- `filesystem` — read/write files in the sandbox
- `browser` — web browser (accessibility tree)

The harness must have `supports_sandbox = True` (`hermes`, `opencode`, `openclaw` do).

> In Docker mode, the `compose_file` path inside the container must match the host path. Use `HARNESS_BENCHMARKS_DATA_DIR` (example: `framework/benchmarks/theagentcompany/benchmark.py`).
