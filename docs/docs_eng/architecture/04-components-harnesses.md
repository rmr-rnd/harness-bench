# 04 — Components: Harness Subsystem

> Diagram: [diagrams/components-harnesses.md](../diagrams/components-harnesses.md)

## Two protocols

> Key decisions: [ADR-001 Agent in Docker](06-adr.md#adr-001-the-agent-runs-in-docker-not-in-process) · [ADR-010 Lazy container start](06-adr.md#adr-010-lazy-container-start-and-ctxcleanup_fns)

Two harness↔orchestrator protocols coexist in the system:

| | Modern (`send_turn`) | Legacy (`run_task`) |
|--|----------------------|---------------------|
| **Who uses it** | Hermes, OpenClaw, OpenCode, OMP | PAC1 only |
| **Flag** | `SUPPORTS_RUNNER_PROTOCOL = True` | `SUPPORTS_RUNNER_PROTOCOL = False` |
| **Engine** | Runner drives the turns | Harness drives the conversation itself |
| **Container** | Lazy start on the first `send_turn()` | Started inside `run_task()` |
| **Multi-turn** | Runner calls `send_turn()` several times | Harness iterates itself |

---

## Components

### Harness (ABC) — the base contract

`type: str`, `supports_sandbox: bool`, `SUPPORTS_RUNNER_PROTOCOL: bool`, `config_model: type[BaseModel] | None`

---

### HermesHarness
`supports_sandbox = True`

> Key decision: [ADR-002 MCP HTTP Bridge](06-adr.md#adr-002-mcp-http-bridge-between-agent-and-sandbox)

Runs `nousresearch/hermes-agent` in Docker. Protocol: `/v1/responses` with `stream=True`, an SSE event stream.

**Lazy start and session management:**
- On the first `send_turn()` it starts the container → `container_id` + `previous_response_id` are stored in `ctx.extras["harness_session"]`
- Subsequent calls use `previous_response_id` to continue the session
- `ctx.cleanup_fns.append(docker_stop)` — the container stops at task teardown

**Config (HermesConfig):**
- `hermes_image` — Docker image
- `hermes_api_key` — auth token
- `hermes_approvals_off` — disable human-in-the-loop
- `tavily_api_key` — web search

**MCP behavior:** if `ctx.mcp_url` holds a URL, Hermes receives it in `config.yaml` via volume-mount. All agent tool calls go to the MCP Bridge, not the local terminal.

---

### OpenClawHarness
`supports_sandbox = True`

Runs `ghcr.io/openclaw/openclaw` in Docker. Protocol: `/v1/responses` SSE (like Hermes).

**Specifics:**
- `ctx.web_search` is passed into `_start_container` — enables/disables web search in the OpenClaw config
- With an external MCP Bridge present — Pi tools are blocked via the allow-list `["sandbox-bridge_*"]`
- During `send_turn`, `_stream_events()` runs in parallel as a background asyncio task for real-time event streaming; it's cancelled after the answer arrives

---

### OpenCodeHarness
`supports_sandbox = True`

Runs `ghcr.io/anomalyco/opencode` in Docker. The port is published to the host via a random ephemeral port (`docker port`). Protocol over httpx (not docker exec):
1. `_create_session(base_url)` → POST `/session` → `session_id`
2. `_send_message(base_url, session_id, prompt)` → POST `/session/{id}/message` → full JSON reply with `parts` and tokens
3. `_parse_response(resp_data, emit_step)` → extracts text from `parts[].type=="text"`

With an MCP Bridge present — built-in tools are blocked via `permission: {"*": "deny", "sandbox-bridge_*": "allow"}`.

---

### OmpHarness
`supports_sandbox = True`

Runs Oh My Pi (`omp`) in Docker in RPC mode (`--mode rpc`) and communicates through JSONL stdio. The harness generates a temporary OMP profile from `model.*`, mounts it with the working directory, and removes it after the task.

When an MCP Bridge is present, the harness writes an OMP-native `mcp.json` with `sandbox-bridge` (`type: "http"`) so sandbox benchmarks use the shared MCP bridge.

---

### Pac1Harness (ABC)
`SUPPORTS_RUNNER_PROTOCOL = False`

Abstract base for PAC1. Uses the **legacy `run_task()` protocol** — it manages the full task lifecycle itself.

**Task lifecycle (in `run_task()`):**
1. `start_trial(trial_id)` → get the `runtime_url` and instruction from BitGN; the instruction is written into `task.messages[0]`
2. `PcmMirror.download(workspace_dir)` → download the workspace via PCM
3. `await _run_agent(workspace_dir, task, step_cb)` → run the agent (overridden in subclasses)
4. `mirror.sync_back(workspace_dir)` → upload changes back
5. `_submit_and_end()`: `pcm.answer()` + `harness_client.end_trial()` — closes the trial (lifecycle only). Returns a placeholder score `0.0`; the real score is **unavailable** at this stage.

**Scoring is run-level, not per-trial.** `end_trial()` gives no scores. After all benchmark tasks finish, the orchestrator calls `await_run_grades()`, which does `harness_client.submit_run(force=True)` and pulls per-trial scores from `SubmitRunResponse.trials[]`; `orchestrator._apply_run_grades()` rewrites score/grade in the DB and re-emits UI events. Until then the scorer holds tasks in `EVALUATING`. BLIND benchmarks return `score_available=False` — scores are sealed until reveal.

**Finalize:** `finalize()` for PAC1 is a **no-op** (`submit_run()` happens inside `await_run_grades()`).

**Runner-function architecture:** the agent-launch logic is moved out of the harness into separate modules `benchmarks/pac1/{hermes,openclaw,opencode}_runner.py`. The Hermes runner is blocking — the harness calls it via `asyncio.to_thread(run_hermes, ...)`; the openclaw and opencode runners are async and called directly via `await`. The Hermes runner uses `subprocess.Popen` + threading to stream log files in parallel and injects hook scripts (pre_tool_call, post_tool_call, post_llm_call) to collect tokens and tool calls.

**Subclasses:**

| Class | Agent | Runner function |
|-------|-------|-----------------|
| `Pac1HermesHarness` | Hermes | `hermes_runner.run_hermes()` — `subprocess.Popen`, `--yolo --toolsets terminal`, hooks |
| `Pac1OpenClawHarness` | OpenClaw | `openclaw_runner.run_openclaw()` — Pi tools on `/workspace` |
| `Pac1OpenCodeHarness` | OpenCode | `opencode_runner.run_opencode()` — `opencode serve`, SSE events |
| `Pac1OmpHarness` | OMP | `omp_runner.run_omp()` — `omp --mode rpc`, `.pac1_answer.json` |

---

## How to add a new Harness

```python
# framework/harnesses/my_harness.py
from pydantic import BaseModel
from framework.harnesses.base import Harness, TurnResponse

class MyHarnessConfig(BaseModel):
    my_image: str = "myagent:latest"

class MyHarness(Harness):
    type = "my_harness"
    config_model = MyHarnessConfig
    supports_sandbox = True
    SUPPORTS_RUNNER_PROTOCOL = True

    def __init__(self, model_cfg, my_image="myagent:latest"):
        super().__init__(model_cfg)
        self.my_image = my_image

    async def send_turn(self, messages, tools, system_prompt, ctx, timeout, **kw) -> TurnResponse:
        # 1. Lazily start container (check ctx.extras["harness_session"])
        # 2. POST API request
        # 3. Parse response
        # 4. Register cleanup: ctx.cleanup_fns.append(stop_fn)
        return TurnResponse(text="...", tool_calls=[], finish_reason="stop", ...)
```

No registration needed — `load_harness_class("my_harness")` finds the module automatically.
