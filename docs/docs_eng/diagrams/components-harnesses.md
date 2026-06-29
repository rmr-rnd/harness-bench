# Harness Components

Three diagrams: two protocols, class hierarchy, lazy-container lifecycle.

---

## 1. Two execution protocols

```mermaid
flowchart LR
    Orch["**Orchestrator**"]

    subgraph modern["Modern protocol\n(all harnesses except PAC1)"]
        Runner["**Runner**"]
        SendTurn["**send_turn()**\n→ TurnResponse"]
        TR["**TurnResponse**"]
    end

    subgraph legacy["Legacy protocol\n(PAC1 only)"]
        RunTask["**run_task()**\n→ AgentTrace"]
    end

    Orch -->|"runner.run(…)"| Runner
    Runner -->|"one turn"| SendTurn
    SendTurn --> TR
    TR -->|"aggregates"| Runner

    Orch -->|"directly"| RunTask

    style modern fill:#d5e8d4,stroke:#82b366
    style legacy fill:#fff2cc,stroke:#d6b656
```

**Signatures:**

| Call | Arguments | Returns |
|------|-----------|---------|
| `runner.run(...)` | sample · send_turn · ctx | `AgentTrace` |
| `send_turn(...)` | messages · tools · system_prompt · ctx · timeout | `TurnResponse` (text · tool_calls · finish_reason · tokens · steps) |
| `run_task(...)` | task · ctx | `AgentTrace` |

---

## 2. Harness class hierarchy

```mermaid
flowchart TD
    ABC["**Harness ABC**\nsend_turn() OR run_task()"]

    Hermes["**HermesHarness**"]
    OpenClaw["**OpenClawHarness**"]
    OpenCode["**OpenCodeHarness**"]

    Pac1ABC["**Pac1Harness ABC**\nrun_task()"]

    Pac1Hermes["**Pac1HermesHarness**"]
    Pac1Claw["**Pac1OpenClawHarness**"]
    Pac1Code["**Pac1OpenCodeHarness**"]

    ABC --> Hermes & OpenClaw & OpenCode & Pac1ABC
    Pac1ABC --> Pac1Hermes & Pac1Claw & Pac1Code

    style ABC fill:#dae8fc,stroke:#6c8ebf
    style Pac1ABC fill:#fff2cc,stroke:#d6b656
```

**Files and protocols:** all modern harnesses are `supports_sandbox=True`.

| Class | File | Image / API |
|-------|------|-------------|
| HermesHarness | `hermes.py` | `nousresearch/hermes-agent` · `/v1/responses` SSE |
| OpenClawHarness | `openclaw.py` | `ghcr.io/openclaw/openclaw` · `/v1/responses` SSE |
| OpenCodeHarness | `opencode.py` | `ghcr.io/anomalyco/opencode` · `/session/{id}/message` SSE |
| Pac1Harness ABC | `pac1_base.py` | `SUPPORTS_RUNNER_PROTOCOL=False`; `run_task()` + `_run_agent()`; PcmMirror; run-level `submit_run` |
| Pac1HermesHarness | `pac1_hermes.py` | Hermes + PcmMirror |
| Pac1OpenClawHarness | `pac1_openclaw.py` | OpenClaw + PcmMirror |
| Pac1OpenCodeHarness | `pac1_opencode.py` | OpenCode + PcmMirror |

---

## 3. Lazy container start (Hermes / OpenClaw / OpenCode)

```mermaid
flowchart TD
    Call1(["send_turn() — first call"])
    Call2(["send_turn() — subsequent"])
    Cleanup(["task teardown"])

    Check{{"session\nexists?"}}

    Start["**Start container**\ndocker run -d --rm\n+ register cleanup"]
    Reuse["**Reuse**\ncontainer_id +\nprevious_response_id"]
    Post["**POST to endpoint**\nstream=True\nparse SSE"]
    Return["**TurnResponse**"]
    Stop["docker stop"]

    Call1 --> Check
    Check -->|"no"| Start
    Check -->|"yes"| Reuse
    Start --> Post
    Reuse --> Post
    Post --> Return

    Call2 --> Check
    Cleanup -->|"cleanup_fns"| Stop
```

**Step details:**

| Step | What happens |
|------|--------------|
| Start | configure model + MCP URL → wait for readiness (`/health` · `/healthz` · `/global/health`) → save session in `ctx.extras["harness_session"]`; `ctx.cleanup_fns.append(docker stop)` |
| Reuse | take `container_id` and `previous_response_id` from `ctx.extras` |
| POST | endpoint `/v1/responses` (Hermes/OpenClaw) or `/session/{id}/message` (OpenCode); parse SSE → text delta · function_calls · finish_reason |
| Teardown | the orchestrator calls all `ctx.cleanup_fns` → `docker stop container_id` |
