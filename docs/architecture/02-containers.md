# 02 — Containers

> Диаграмма: [diagrams/containers.md](../diagrams/containers.md)

## Обзор

При запуске прогона поднимаются следующие развёртываемые единицы. Часть из них живёт на протяжении всего прогона, часть — только на время одной задачи.

## Контейнеры

### Framework CLI
**Технология:** Python 3.12+, Click, asyncio  
**Жизненный цикл:** весь прогон

Две точки входа: `framework serve` поднимает Web UI как сервис (конфиг выбирается в браузере; БД — из окружения деплоя); `framework run config.yaml` — headless-прогон: читает YAML в `RunConfig`, инициализирует `Orchestrator`, `asyncio.run(orchestrator.run())`.

Другие команды: `compare` (сравнение двух прогонов), `results` (просмотр результатов), `db-init`, `db-runs`.

---

### Web UI
**Технология:** FastAPI, uvicorn, WebSocket, встроенный HTML/JS SPA  
**Жизненный цикл:** весь прогон  
**URL:** `http://localhost:8765` (предпочтительный; занятый порт — следующий свободный)

Даёт возможность наблюдать за прогоном в браузере. Frontend получает события через WebSocket `/ws`. REST-эндпоинты: `POST /api/run`, `POST /api/stop`, `GET /api/configs`, `POST /api/load-config`, `GET /api/report/{run_id}`. Запускается командой `framework serve` (а в Docker — это дефолтный `CMD`).

---

### Orchestrator
**Технология:** Python asyncio  
**Жизненный цикл:** весь прогон

Ядро системы. Один экземпляр на прогон. Итерируется по бенчмаркам из конфига, для каждого запускает задачи параллельно через `asyncio.Semaphore(workers)`.

Жизненный цикл одной задачи:
1. Проверить resume: `db.fetch_task_output(run_id, task_id)` — если `status == "done"`, пропустить
2. `db.save_task_start()` — пометить задачу как `running`
3. `_build_ctx()` — поднять sandbox и MCP Bridge (если `sample.sandbox` задан)
4. `runner.run(task, harness.send_turn, ctx)` → `AgentTrace`
5. `scorer(task, trace, judge, sandbox)` — оценить пока sandbox жив
6. `_teardown_ctx()` — остановить harness-контейнер, MCP Bridge, sandbox
7. `db.save_task_output()` + `db.save_eval_result()` — сохранить в БД

Resume работает только при наличии PostgreSQL.

---

### MCP HTTP Bridge
**Технология:** FastAPI, uvicorn, JSON-RPC 2.0  
**Жизненный цикл:** на время одной задачи  
**Протокол:** MCP 2025-11-25

Поднимается на случайном свободном порту хост-машины для каждой sandbox-задачи. Агент (в Docker) обращается к нему через адрес, который возвращает `get_mcp_host()`: `host.docker.internal:{port}` по умолчанию, или собственный IP контейнера при запуске фреймворка внутри Docker (`HARNESS_DOCKER_NETWORK`).

Маршрутизация входящих вызовов:
- **Стандартные инструменты** (shell, filesystem, browser) → `DockerBridge` → `docker exec` в sandbox
- **Кастомные SandboxTools** → `sandbox.exec_stdin()` (скрипт, инжектированный в sandbox при старте)

---

### Agent Container
**Технология:** Docker, `nousresearch/hermes-agent` (или openclaw, opencode)  
**Жизненный цикл:** на время одной задачи

Запускается через `docker run -d --rm`. Конфиг модели и URL MCP-сервера передаются через volume-mount config.yaml. Hermes/OpenClaw управляются через `docker exec curl` к внутреннему HTTP API:
- `POST /v1/responses` со `stream=True` — отправить ход, получить SSE-поток шагов агента (мульти-turn через `previous_response_id`)
- `GET /health` — проверить готовность

OpenCode общается по-другому: контейнер публикует порт на хост (random ephemeral, `docker port`), а харнес ходит туда напрямую через `httpx` (`POST /session`, `POST /session/{id}/message`, SSE `/event`).

---

### Benchmark Sandbox
**Технология:** Docker run / Docker Compose  
**Жизненный цикл:** на время одной задачи

Изолированная среда выполнения задачи. Тип зависит от бенчмарка:

| Бенчмарк | Sandbox-тип | Образ |
|----------|------------|-------|
| SWE-bench (+ multilingual) | `SWEbenchSandbox` (`type=swe_bench`) | per-repo образ с зависимостями |
| TheAgentCompany | `DockerComposeSandbox` (`type=docker_compose`) | Multi-service стек (Gitea, web, БД) |
| PAC1 | Удалённый PCM-workspace (BitGN), не локальный Docker | Определяется BitGN API |

> HumanEval+ исполняет код агента в **локальном subprocess** на хосте (без Docker-sandbox), поэтому здесь не значится. Sandbox-задачами являются только SWE-bench и TheAgentCompany.

---

### PostgreSQL
**Технология:** PostgreSQL 16, asyncpg  
**Жизненный цикл:** постоянное хранилище (опциональный)

Единственное хранилище результатов. Включается через `RunConfig.database`. Если не настроен — все методы `Database` работают как no-op, результаты нигде не сохраняются и resume недоступен.

Схема авто-мигрируется при подключении: SQL-файлы из `db/migrations/` применяются под advisory lock (защита от гонок при параллельном запуске).

Таблицы: `runs`, `benchmark_runs`, `task_outputs` (статус: running/done/error/timeout), `eval_results`, `schema_migrations`.

## Топология сети при выполнении sandbox-задачи

```
┌─────────────── Хост-машина ───────────────────────────────┐
│                                                            │
│  Framework CLI / Orchestrator                              │
│         │                                                  │
│         ├── [Docker SDK] ──────────────────────────────┐  │
│         │                                              │  │
│         ├── MCP HTTP Bridge :RANDOM_PORT               │  │
│                │                                       │  │
│                │ ◄──── http://host.docker.internal:PORT │  │
│                │                                       │  │
│  ┌─────────────────────────┐  ┌────────────────────┐  │  │
│  │  Agent Container        │  │  Benchmark Sandbox  │  │  │
│  │  (Hermes / OpenClaw)    │  │  (task environment) │  │  │
│  │                         │  │                     │  │  │
│  │  MCP calls ────────────>│  │<── docker exec ─────│  │  │
│  └─────────────────────────┘  └────────────────────┘  │  │
│                                                        │  │
└────────────────────────────────────────────────────────┘  │
                                                             │
                LLM Provider API ◄───────────────────────────┘
                                    HTTPS / OpenAI API
```
