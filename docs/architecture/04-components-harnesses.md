# 04 — Components: Harness Subsystem

> Диаграмма: [diagrams/components-harnesses.md](../diagrams/components-harnesses.md)

## Два протокола

> Ключевые решения: [ADR-001 Агент в Docker](06-adr.md#adr-001-агент-запускается-в-docker-а-не-in-process) · [ADR-010 Lazy-старт контейнера](06-adr.md#adr-010-lazy-старт-контейнера-и-ctxcleanup_fns)

В системе сосуществуют два протокола взаимодействия харнеса с оркестратором:

| | Современный (`send_turn`) | Legacy (`run_task`) |
|--|--------------------------|---------------------|
| **Кто использует** | Hermes, OpenClaw, OpenCode | PAC1 только |
| **Флаг** | `SUPPORTS_RUNNER_PROTOCOL = True` | `SUPPORTS_RUNNER_PROTOCOL = False` |
| **Движок** | Runner управляет ходами | Харнес сам управляет диалогом |
| **Контейнер** | Lazy-старт при первом `send_turn()` | Стартует внутри `run_task()` |
| **Multi-turn** | Runner вызывает `send_turn()` несколько раз | Харнес итерирует сам |

---

## Компоненты

### Harness (ABC) — базовый контракт

`type: str`, `supports_sandbox: bool`, `SUPPORTS_RUNNER_PROTOCOL: bool`, `config_model: type[BaseModel] | None`

---

### HermesHarness
`supports_sandbox = True`

> Ключевое решение: [ADR-002 MCP HTTP Bridge](06-adr.md#adr-002-mcp-http-bridge-между-агентом-и-sandbox)

Запускает `nousresearch/hermes-agent` в Docker. Протокол: `/v1/responses` с `stream=True`, SSE-поток событий.

**Lazy-старт и session management:**
- При первом `send_turn()` стартует контейнер → `container_id` + `previous_response_id` сохраняются в `ctx.extras["harness_session"]`
- Повторные вызовы используют `previous_response_id` для продолжения сессии
- `ctx.cleanup_fns.append(docker_stop)` — контейнер остановится при teardown задачи

**Конфиг (HermesConfig):**
- `hermes_image` — Docker-образ
- `hermes_api_key` — токен авторизации
- `hermes_approvals_off` — отключить human-in-the-loop
- `tavily_api_key` — веб-поиск

**Поведение с MCP:** если в `ctx.mcp_url` задан URL, Hermes получает его в `config.yaml` через volume-mount. Все tool calls агента уходят в MCP Bridge, а не в локальный терминал.

---

### OpenClawHarness
`supports_sandbox = True`

Запускает `ghcr.io/openclaw/openclaw` в Docker. Протокол: `/v1/responses` SSE (аналогично Hermes).

**Особенности:**
- `ctx.web_search` передаётся в `_start_container` — включает/отключает веб-поиск в конфиге OpenClaw
- При наличии внешнего MCP Bridge — Pi-инструменты блокируются через allow-list `["sandbox-bridge_*"]`
- Во время `send_turn` параллельно запускается `_stream_events()` как фоновая asyncio-задача для стриминга событий в реальном времени; отменяется после получения ответа

---

### OpenCodeHarness
`supports_sandbox = True`

Запускает `ghcr.io/anomalyco/opencode` в Docker. Порт публикуется на хост через случайный ephemeral-порт (`docker port`). Протокол через httpx (не docker exec):
1. `_create_session(base_url)` → POST `/session` → `session_id`
2. `_send_message(base_url, session_id, prompt)` → POST `/session/{id}/message` → полный JSON-ответ с `parts` и токенами
3. `_parse_response(resp_data, emit_step)` → извлекает текст из `parts[].type=="text"`

При наличии MCP Bridge — встроенные инструменты блокируются через `permission: {"*": "deny", "sandbox-bridge_*": "allow"}`.

---

### Pac1Harness (ABC)
`SUPPORTS_RUNNER_PROTOCOL = False`

Абстрактный базовый класс для PAC1. Использует **legacy `run_task()` протокол** — управляет жизненным циклом задачи полностью самостоятельно.

**Жизненный цикл задачи (в `run_task()`):**
1. `start_trial(trial_id)` → получить `runtime_url` и инструкцию от BitGN; инструкция записывается в `task.messages[0]`
2. `PcmMirror.download(workspace_dir)` → скачать workspace через PCM
3. `await _run_agent(workspace_dir, task, step_cb)` → запустить агента (переопределяется в подклассах)
4. `mirror.sync_back(workspace_dir)` → загрузить изменения обратно
5. `_submit_and_end()`: `pcm.answer()` + `harness_client.end_trial()` — закрывает trial (только lifecycle). Возвращает placeholder-score `0.0`; реальная оценка на этом этапе **недоступна**.

**Оценка — run-level, а не per-trial.** `end_trial()` оценок не даёт. После завершения всех задач бенчмарка оркестратор вызывает `await_run_grades()`, которая делает `harness_client.submit_run(force=True)` и достаёт per-trial оценки из `SubmitRunResponse.trials[]`; `orchestrator._apply_run_grades()` переписывает score/grade в БД и ре-эмитит UI-события. До этого момента скорер держит задачи в состоянии `EVALUATING`. BLIND-бенчмарки возвращают `score_available=False` — оценки запечатаны до reveal.

**Finalize:** `finalize()` для PAC1 — **no-op** (`submit_run()` выполняется внутри `await_run_grades()`).

**Архитектура runner-функций:** логика запуска агента вынесена из харнеса в отдельные модули `benchmarks/pac1/{hermes,openclaw,opencode}_runner.py`. Hermes-runner блокирующий — харнес вызывает его через `asyncio.to_thread(run_hermes, ...)`; openclaw- и opencode-runner'ы асинхронные и вызываются напрямую через `await`. Hermes-runner использует `subprocess.Popen` + threading для параллельного стриминга лог-файлов и инжектирует hook-скрипты (pre_tool_call, post_tool_call, post_llm_call) для сбора токенов и tool calls.

**Подклассы:**

| Класс | Агент | Runner-функция |
|-------|-------|---------------|
| `Pac1HermesHarness` | Hermes | `hermes_runner.run_hermes()` — `subprocess.Popen`, `--yolo --toolsets terminal`, hooks |
| `Pac1OpenClawHarness` | OpenClaw | `openclaw_runner.run_openclaw()` — Pi-инструменты на `/workspace` |
| `Pac1OpenCodeHarness` | OpenCode | `opencode_runner.run_opencode()` — `opencode serve`, SSE-события |

---

## Как добавить новый Harness

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

Никакой регистрации не нужно — `load_harness_class("my_harness")` найдёт модуль автоматически.
