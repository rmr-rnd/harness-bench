# 03 — Components: Framework (Orchestrator Container)

> Диаграмма: [diagrams/components-framework.md](../diagrams/components-framework.md)

## Обзор

Python-процесс фреймворка строится вокруг трёх новых абстракций: **Runner** (управляет ходами диалога), **Scorer** (оценивает результат), **TurnResponse** (унифицированный ответ харнеса). Это позволяет комбинировать любой бенчмарк с любым агентом без изменения кода.

## Компоненты

### CLI (`framework/cli.py`)

Обёртка над Click. Загружает `RunConfig` из YAML, запускает Orchestrator.

| Команда | Назначение |
|---------|-----------|
| `serve [--config config.yaml] [--open-browser]` | Поднять Web UI как сервис (выбор/правка конфига в браузере) |
| `run config.yaml` | Headless-прогон (без UI) |
| `results run_id --config config.yaml` | Просмотреть результаты прогона |
| `compare run_a run_b --config config.yaml` | Сравнить два прогона |
| `runs config.yaml` / `db-runs config.yaml` | Список прогонов из БД |
| `db-init config.yaml` | Инициализировать схему БД |

---

### RunConfig / Models (`framework/config.py` + `framework/models.py`)

**Конфигурационные модели:**

| Класс | Назначение |
|-------|-----------|
| `RunConfig` | Корневой объект. Содержит все остальные модели |
| `ModelConfig` | API-параметры: base_url, api_key, model_name, temperature, max_tokens |
| `HarnessConfig` | Тип harness + `benchmark_harness: dict` (per-benchmark переопределения) |
| `BenchmarkConfig` | Имя, пути к данным, limit, фильтры |
| `ParallelismConfig` | workers, timeout_per_task, eval_timeout |
| `SearchConfig` | tavily_api_key для веб-поиска |
| `DockerConfig` | network, cleanup_after |
| `DatabaseConfig` | PostgreSQL connection |
| `SWEBenchConfig` | namespace, image_mode |

**Модели данных:**

| Класс | Назначение |
|-------|-----------|
| `Sample` | Основная единица данных (раньше `Task`). Содержит messages, ground_truth, tools, metadata, **sandbox, sandbox_tools, checkpoints, mcp_tool_groups** |
| `SandboxSpec` | Описание Docker-окружения: type, image, compose_file, config dict |
| `SandboxTool` | Кастомный инструмент: source_dir, entrypoint, runner, install_cmd, timeout, max_output_bytes |
| `Checkpoint` | Bash-проверка: cmd, weight, target_exit_code, timeout |
| `AgentTrace` | Результат выполнения: final_output, steps, tokens, error |
| `Score` | Оценка (раньше `EvalResult`): score 0–1, grade, explanation, judge_model |
| `Step` | Один шаг агента: type, content, ts |
| `TurnResponse` | Унифицированный ответ харнеса: text, tool_calls, finish_reason, input_tokens, output_tokens, steps |

> **Переименования в коде:** `Task = Sample`, `EvalResult = Score` — алиасы для обратной совместимости. Свойства: `Sample.target` ← `ground_truth`, `Sample.needs_web_search` ← `web_search`.

---

### Orchestrator (`framework/orchestrator.py`)

> Ключевые решения: [ADR-003 Resume](06-adr.md#adr-003-resume-через-статус-задачи-в-бд) · [ADR-004 Evaluate while alive](06-adr.md#adr-004-scorer-вызывается-пока-sandbox-жив) · [ADR-006 Параллелизм](06-adr.md#adr-006-параллелизм-через-asyncio--semaphore)

Ключевые поля: `run_id`, `harness`, `_benchmark_harnesses`, `judge`, `db`.

Жизненный цикл одной задачи в `_run_task()`:

```
# Resume: проверить DB (только при наличии PostgreSQL)   # → ADR-003
existing = await db.fetch_task_output(run_id, task_id)
if existing and existing["status"] == "done": skip

await db.save_task_start(run_id, task_id)   # status = "running"

# Scorer строится до ctx (нужен log_dir у некоторых scorers)
scorer = benchmark.make_scorer()

ctx = ExecutionContext(timeout, step_cb, web_search=task.web_search)
ctx = await _build_ctx(task, benchmark, ctx)
  ├── если sample.sandbox → docker + MCP Bridge (get_mcp_host())
  └── устанавливает ctx.sandbox, ctx.mcp_url
ctx.harness_type = harness.type

# Современный протокол (не PAC1):
runner = benchmark.make_runner(model_cfg=cfg.model)
trace  = await runner.run(sample, harness.send_turn, ctx)
score  = await scorer(sample, trace, judge, sandbox=ctx.sandbox)

# Legacy (PAC1):
trace  = await harness.run_task(sample, ctx)
score  = await scorer(sample, trace, judge)

await _teardown_ctx(ctx)                    # cleanup_fns + MCP + sandbox → ADR-004

# Сохранить в БД (no-op если PostgreSQL не настроен)
await db.save_task_output(run_id, benchmark, trace)
await db.save_eval_result(run_id, benchmark, result)
```

---

### Benchmark Registry

`@register_benchmark("name")` — декоратор, регистрирует класс. `resolve_benchmark(name)` (в `benchmarks/base.py`) — возвращает класс; оркестратор вызывает его через внутренний враппер `_resolve_benchmark()`, который предварительно запускает `discover_all()`. 10 зарегистрированных бенчмарков: simpleqa, bfcl, bfcl_memory, humaneval_plus, niah, persistbench, swe_bench, swe_bench_multilingual, theagentcompany, pac1.

---

### Harness Registry (`framework/harnesses/__init__.py`)

> Ключевое решение: [ADR-005 Независимые плагины](06-adr.md#adr-005-benchmark-и-harness--независимые-плагины)

`load_harness_class(type_name)` — ищет модуль `framework.harnesses.{type_name}` через `importlib`. Авто-конвенция: для бенчмарка `bfcl` + harness `hermes` автоматически пробует `bfcl_hermes`.

---

### Runner (`framework/runners/base.py`)

> Ключевое решение: [ADR-009 Runner/Scorer разделение](06-adr.md#adr-009-runner-и-scorer-как-отдельные-абстракции)

```python
class Runner(ABC):
    async def run(
        sample: Sample,
        send_turn: SendTurnFn,  # harness.send_turn — передаётся как колбэк
        ctx: ExecutionContext
    ) -> AgentTrace: ...
```

**Реализации:**
- `SingleTurnRunner` — вызывает `send_turn()` один раз. Default для большинства бенчмарков
- `BFCLRunner` (в `benchmarks/bfcl/`) — многоходовой: агент вызывает функции → mock-бэкенд → следующий ход

Benchmark выбирает Runner через `make_runner()`. Это позволяет бенчмарку контролировать логику диалога, не трогая харнес.

---

### Scorer (`framework/scorers/base.py`)

> Ключевое решение: [ADR-009 Runner/Scorer разделение](06-adr.md#adr-009-runner-и-scorer-как-отдельные-абстракции)

```python
class Scorer(ABC):
    async def __call__(
        sample: Sample,
        trace: AgentTrace,
        judge: LLMJudge,
        sandbox: Sandbox | None = None
    ) -> Score: ...
```

**Базовые scorer-классы** (`framework/scorers/`) — переиспользуемые «кирпичики» для кастомных бенчмарков:

| Scorer | Логика |
|--------|--------|
| `ExactMatchScorer` | Нормализованное строковое сравнение |
| `LLMJudgeScorer` | Вопрос + эталон + ответ → LLM → CORRECT/INCORRECT |
| `CheckpointScorer` | Bash-команды в sandbox, weighted pass/fail |
| `SubprocessScorer` | Внешний скрипт-раннер |

> **Важно:** ни один встроенный бенчмарк не инстанцирует эти базовые классы напрямую — у каждого свой собственный подкласс `Scorer` (см. таблицу в [05](05-components-benchmarks.md)). Базовые scorer'ы существуют как готовые компоненты для **кастомных** бенчмарков.

**Собственные scorer'ы встроенных бенчмарков** (каждый наследует `Scorer`):

| Scorer | Логика | Бенчмарк |
|--------|--------|----------|
| `_SimpleQAScorer` | LLM-judge поверх вопрос+эталон+ответ | SimpleQA |
| `_NIAHScorer` | LLM-judge с числовой рубрикой (1/3/5/7/10) | NIAH |
| `_PersistBenchScorer` | LLM-judge с per-type промптами | PersistBench |
| `_BFCLScorer` / `_BFCLMemoryScorer` | AST-сравнение вызовов функций | BFCL, BFCL Memory |
| `_HumanEvalScorer` | Запуск тестов в локальном subprocess (без Docker) | HumanEval+ |
| `SWEBenchScorer` | `eval.sh` в sandbox → RESOLVED/APPLIED/NO_GENERATION | SWE-bench (+ multilingual) |
| `SandboxEvalScorer` | Per-task evaluator-модули → weighted CheckpointResult | TheAgentCompany |
| `_Pac1Scorer` | grade=`EVALUATING`, реальная оценка приходит run-level (inline в pac1/benchmark.py) | PAC1 |

Benchmark выбирает Scorer через `make_scorer()`.

---

### Benchmark (ABC) (`framework/benchmarks/base.py`)

> Ключевое решение: [ADR-005 Независимые плагины](06-adr.md#adr-005-benchmark-и-harness--независимые-плагины)

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

**Важно:** sandbox-конфигурация теперь живёт в `Sample`, а не в Benchmark. Benchmark только загружает данные и назначает Runner/Scorer.

---

### Harness (ABC) (`framework/harnesses/base.py`)

> Ключевое решение: [ADR-001 Агент в Docker](06-adr.md#adr-001-агент-запускается-в-docker-а-не-in-process) · [ADR-010 Lazy-старт контейнера](06-adr.md#adr-010-lazy-старт-контейнера-и-ctxcleanup_fns)

| Атрибут | Значение |
|---------|---------|
| `type: str` | Имя в registry |
| `supports_sandbox: bool` | True → может работать с MCP Bridge (подключается к адресу из `get_mcp_host()`) |
| `SUPPORTS_RUNNER_PROTOCOL: bool` | True → реализует `send_turn()`, False → реализует `run_task()` (PAC1) |

**Современный протокол:**

```python
async def send_turn(
    messages: list[dict],
    tools: list[dict],
    system_prompt: str,
    ctx: ExecutionContext,
    timeout: int,
    **kwargs
) -> TurnResponse:
    # Запустить контейнер лениво (если не запущен)
    # POST /v1/responses с stream=True
    # Парсить SSE → TurnResponse
```

Контейнер запускается при первом вызове `send_turn()` и хранится в `ctx.extras["harness_session"]`. Cleanup-колбэк для остановки регистрируется в `ctx.cleanup_fns`.

---

### ExecutionContext (`framework/context.py`)

| Поле | Тип | Назначение |
|------|-----|-----------|
| `timeout` | `int` | Лимит времени на агента (сек) |
| `step_cb` | `Callable` | Callback для стриминга шагов в UI |
| `web_search` | `bool` | Разрешён ли веб-поиск для этой задачи (из `task.web_search`) |
| `harness_type` | `str` | Тип харнеса — устанавливается Orchestrator перед `runner.run()` |
| `sandbox` | `Sandbox \| None` | Запущенный sandbox-контейнер |
| `mcp_url` | `str \| None` | URL MCP Bridge для агента |
| `mcp_server` | `MCPHttpServer \| None` | Сервер для teardown |
| `cleanup_fns` | `list[Callable]` | Async-колбэки для teardown (docker stop, …) |
| `extras` | `ContextExtras` | Типизированный TypedDict с известными ключами |

**ContextExtras (TypedDict):**

| Ключ | Тип | Устанавливается |
|------|-----|----------------|
| `harness_session` | `dict` | Harness при первом `send_turn()` — container_id, previous_response_id |
| `bfcl_mcp_server` | `BFCLMCPServer` | Orchestrator для BFCL |
| `bfcl_snapshot_dir` | `str` | Orchestrator для memory-сценариев |
| `bfcl_func_doc_dir` | `str` | Orchestrator для BFCL |
| `swe_instance_id` | `str` | Orchestrator для SWE-bench |
| `swe_namespace` | `str` | Orchestrator для SWE-bench |
| `pac1_runtime_url` | `str` | PAC1 harness |
| `bfcl_mcp_port` | `int` | Orchestrator для BFCL |

---

### Sandbox Manager (`framework/sandbox.py`)

> Ключевое решение: [ADR-001 Агент в Docker](06-adr.md#adr-001-агент-запускается-в-docker-а-не-in-process)

`@register_sandbox("docker_run")` / `@register_sandbox("docker_compose")` — регистрирует реализации.  
`make_sandbox(SandboxSpec) → Sandbox` — фабрика.

| Метод Sandbox | Назначение |
|---------------|-----------|
| `start()` | docker run / compose up |
| `stop()` | docker stop / compose down |
| `exec_bash(cmd, timeout)` | выполнить bash в контейнере |
| `exec_stdin(runner, path, data)` | запустить SandboxTool скрипт с JSON на stdin |
| `read_file(path)` | прочитать файл из контейнера |
| `write_file(content, path)` | записать файл в контейнер |
| `inject_tools(tools)` | tar → /.sandbox_tools/{name}/ + install_cmd |

---

### MCP HTTP Bridge (`framework/mcp/http_server.py`)

> Ключевое решение: [ADR-002 MCP как посредник](06-adr.md#adr-002-mcp-http-bridge-между-агентом-и-sandbox)

FastAPI на случайном порту. POST `/mcp` — JSON-RPC 2.0 / MCP 2025-11-25.

Маршрутизация `tools/call`:
1. Кастомный инструмент (из `sample.sandbox_tools`) → `sandbox.exec_stdin()`
2. Стандартная группа (shell/filesystem/browser) → `DockerBridge` → `docker exec`

---

### LLM Judge (`framework/evaluators/llm_judge.py`)

OpenAI API. Промпт: вопрос + эталон + ответ → одна буква → CORRECT/INCORRECT/NOT_ATTEMPTED.

---

### Utils (`framework/utils/`)

| Модуль | Функция | Назначение |
|--------|---------|-----------|
| `network.py` | `get_mcp_host()` | Адрес для MCP Bridge: `host.docker.internal` по умолчанию, или собственный IP при `HARNESS_DOCKER_NETWORK` (Docker-in-Docker). Переопределяется `HARNESS_MCP_HOST`. |
| `work_dir.py` | `make_work_dir(prefix)` | Создать временную директорию. Если задан `HARNESS_WORK_DIR` — создаёт внутри него (для монтирования в Docker). |
| `work_dir.py` | `make_work_file(prefix, suffix)` | Создать временный файл с тем же поведением. |

`HARNESS_DOCKER_NETWORK` — имя Docker-сети: при наличии харнес добавляет `--network {name}` при запуске агент-контейнеров и использует IP вместо `host.docker.internal`.

---

### Database (`framework/db.py`)

> Ключевое решение: [ADR-008 PostgreSQL как единственное хранилище](06-adr.md#adr-008-postgresql-как-единственное-хранилище-результатов)

Async asyncpg. No-op если БД не настроена или asyncpg не установлен. Авто-миграция при `connect()` — SQL-файлы из `db/migrations/` под advisory lock (защита от гонок). Secrets-redaction в config_yaml перед сохранением.

Таблицы: `runs`, `benchmark_runs`, `task_outputs` (status: running/done/error/timeout), `eval_results`, `schema_migrations`.
