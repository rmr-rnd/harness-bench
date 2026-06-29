# Harness Components

Три диаграммы: два протокола, иерархия классов, жизненный цикл lazy-контейнера.

---

## 1. Два протокола выполнения

```mermaid
flowchart LR
    Orch["**Orchestrator**"]

    subgraph modern["Современный протокол\n(все harness кроме PAC1)"]
        Runner["**Runner**"]
        SendTurn["**send_turn()**\n→ TurnResponse"]
        TR["**TurnResponse**"]
    end

    subgraph legacy["Legacy протокол\n(только PAC1)"]
        RunTask["**run_task()**\n→ AgentTrace"]
    end

    Orch -->|"runner.run(…)"| Runner
    Runner -->|"один ход"| SendTurn
    SendTurn --> TR
    TR -->|"агрегирует"| Runner

    Orch -->|"напрямую"| RunTask

    style modern fill:#d5e8d4,stroke:#82b366
    style legacy fill:#fff2cc,stroke:#d6b656
```

**Сигнатуры:**

| Вызов | Аргументы | Возврат |
|-------|-----------|---------|
| `runner.run(...)` | sample · send_turn · ctx | `AgentTrace` |
| `send_turn(...)` | messages · tools · system_prompt · ctx · timeout | `TurnResponse` (text · tool_calls · finish_reason · tokens · steps) |
| `run_task(...)` | task · ctx | `AgentTrace` |

---

## 2. Иерархия harness-классов

```mermaid
flowchart TD
    ABC["**Harness ABC**\nsend_turn() ИЛИ run_task()"]

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

**Файлы и протоколы:** все современные harness — `supports_sandbox=True`.

| Класс | Файл | Образ / API |
|-------|------|-------------|
| HermesHarness | `hermes.py` | `nousresearch/hermes-agent` · `/v1/responses` SSE |
| OpenClawHarness | `openclaw.py` | `ghcr.io/openclaw/openclaw` · `/v1/responses` SSE |
| OpenCodeHarness | `opencode.py` | `ghcr.io/anomalyco/opencode` · `/session/{id}/message` SSE |
| Pac1Harness ABC | `pac1_base.py` | `SUPPORTS_RUNNER_PROTOCOL=False`; `run_task()` + `_run_agent()`; PcmMirror; run-level `submit_run` |
| Pac1HermesHarness | `pac1_hermes.py` | Hermes + PcmMirror |
| Pac1OpenClawHarness | `pac1_openclaw.py` | OpenClaw + PcmMirror |
| Pac1OpenCodeHarness | `pac1_opencode.py` | OpenCode + PcmMirror |

---

## 3. Lazy-старт контейнера (Hermes / OpenClaw / OpenCode)

```mermaid
flowchart TD
    Call1(["send_turn() — первый вызов"])
    Call2(["send_turn() — повторный"])
    Cleanup(["teardown задачи"])

    Check{{"session\nсуществует?"}}

    Start["**Старт контейнера**\ndocker run -d --rm\n+ регистрация cleanup"]
    Reuse["**Переиспользовать**\ncontainer_id +\nprevious_response_id"]
    Post["**POST на endpoint**\nstream=True\nпарсить SSE"]
    Return["**TurnResponse**"]
    Stop["docker stop"]

    Call1 --> Check
    Check -->|"нет"| Start
    Check -->|"да"| Reuse
    Start --> Post
    Reuse --> Post
    Post --> Return

    Call2 --> Check
    Cleanup -->|"cleanup_fns"| Stop
```

**Детали шагов:**

| Шаг | Что происходит |
|-----|----------------|
| Старт | configure model + MCP URL → ждать готовности (`/health` · `/healthz` · `/global/health`) → сохранить session в `ctx.extras["harness_session"]`; `ctx.cleanup_fns.append(docker stop)` |
| Переиспользовать | взять `container_id` и `previous_response_id` из `ctx.extras` |
| POST | endpoint `/v1/responses` (Hermes/OpenClaw) или `/session/{id}/message` (OpenCode); парсинг SSE → text delta · function_calls · finish_reason |
| Teardown | оркестратор вызывает все `ctx.cleanup_fns` → `docker stop container_id` |
