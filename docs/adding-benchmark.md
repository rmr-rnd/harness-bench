# Добавление нового бенчмарка

## Архитектура

Каждый бенчмарк — класс, унаследованный от `Benchmark` (`framework/benchmarks/base.py`) с декоратором `@register_benchmark('name')`. Оркестратор вызывает `load_samples()` → запускает агента → передаёт результат в `make_scorer()`.

Все бенчмарки из `framework/benchmarks/` подхватываются **автоматически** при старте.

## Шаг 1 — Создать файл бенчмарка

`framework/benchmarks/my_bench.py` (или `framework/benchmarks/my_bench/benchmark.py`):

```python
from framework.benchmarks.base import Benchmark, register_benchmark

@register_benchmark("my_bench")
class MyBench(Benchmark):
    display_name  = "My Bench"
    description   = "Описание бенчмарка для AI-анализатора."
    category      = "Программирование"
    default_paths = (
        "benchmarks_data/my_bench/questions",
        "benchmarks_data/my_bench/answers",
    )

    def load_samples(self): ...
    def make_scorer(self): ...
```

### `load_samples()`

Возвращает список `Sample`. Каждый `Sample` описывает одну задачу: сообщения агенту, эталонный ответ, метаданные.

### `make_scorer()`

Возвращает `Scorer`, который оценивает ответ агента и возвращает `Score`:

- `grade` — строка: `CORRECT`, `INCORRECT`, `NOT_ATTEMPTED`, `PARTIAL`, `ERROR`, `TIMEOUT` (можно свои)
- `score` — float от 0.0 до 1.0
- `explanation` — текст для Web UI

В Web UI `✓` считает `CORRECT + PASS`, `✗` считает `INCORRECT + FAIL`. Остальные grades идут в n, но не в ✓/✗.

### `make_runner()` (опционально)

По умолчанию `SingleTurnRunner` — один вызов `send_turn`. Если нужна многоходовая логика (tool calls, итерации) — создай свой `Runner` и переопредели этот метод.

## Шаг 2 — Положить данные

```
benchmarks_data/my_bench/
  questions/
  answers/
```

## Итог

| Действие | Файл |
|----------|------|
| Создать класс с метаданными | `framework/benchmarks/my_bench.py` |
| Реализовать `load_samples()` и `make_scorer()` | там же |
| Положить данные | `benchmarks_data/my_bench/` |

Больше ничего не нужно — бенчмарк появится в Web UI и отчётах автоматически.

## Sandbox-бенчмарки

Если задача требует Docker-среды, в `Sample` указывается `sandbox`:

```python
Sample(
    ...
    sandbox=SandboxSpec(
        type="docker_compose",          # или "docker_run"
        compose_file="/abs/path/to/compose.yaml",
        target_service="default",
    ),
    mcp_tool_groups=["shell", "filesystem", "browser"],
)
```

Доступные группы инструментов MCP (модули в `framework/mcp/tools/`, подключаются HTTP-мостом `framework/mcp/http_server.py`):
- `shell` — bash-команды в sandbox
- `filesystem` — чтение/запись файлов в sandbox
- `browser` — веб-браузер (accessibility tree)

Harness должен иметь `supports_sandbox = True` (у `hermes`, `opencode`, `openclaw` — есть).

> В Docker-режиме путь к `compose_file` внутри контейнера должен совпадать с хост-путём. Используй `HARNESS_BENCHMARKS_DATA_DIR` (пример: `framework/benchmarks/theagentcompany/benchmark.py`).
