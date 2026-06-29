# Документация архитектуры harness_bench (C4)

Документация построена по модели C4 (Context → Containers → Components + ADR).  
Диаграммы написаны в Mermaid.js и рендерятся нативно в GitHub/GitLab.

## Структура

```
docs/
├── architecture/        # Текст: описание, решения, таблицы
│   ├── 01-system-context.md
│   ├── 02-containers.md
│   ├── 03-components-framework.md
│   ├── 04-components-harnesses.md
│   ├── 05-components-benchmarks.md
│   └── 06-adr.md
└── diagrams/            # Код диаграмм (Mermaid flowchart)
    ├── context.md
    ├── containers.md
    ├── components-framework.md       ← 3 диаграммы: ядро, Docker/MCP, хранилище
    ├── components-harnesses.md       ← 3 диаграммы: протоколы, иерархия, lazy-старт
    ├── components-benchmarks.md      ← 3 диаграммы: таксономия, scorers, Sample
    └── runtime-flow.md               ← dynamic-вид: сквозной поток одной задачи (шаги 1–7)
```

## Навигация

| Документ | Уровень | Аудитория | Диаграмма |
|----------|---------|-----------|-----------|
| [01-system-context.md](architecture/01-system-context.md) | L1 | Все | [context.md](diagrams/context.md) |
| [02-containers.md](architecture/02-containers.md) | L2 | Архитекторы, DevOps | [containers.md](diagrams/containers.md) |
| [03-components-framework.md](architecture/03-components-framework.md) | L3 | Разработчики | [components-framework.md](diagrams/components-framework.md) |
| [04-components-harnesses.md](architecture/04-components-harnesses.md) | L3 | Разработчики | [components-harnesses.md](diagrams/components-harnesses.md) |
| [05-components-benchmarks.md](architecture/05-components-benchmarks.md) | L3 | Разработчики | [components-benchmarks.md](diagrams/components-benchmarks.md) |
| [06-adr.md](architecture/06-adr.md) | ADR | Все | — |
| Runtime flow | Dynamic | Все | [runtime-flow.md](diagrams/runtime-flow.md) |

## Ключевые концепции (быстрая шпаргалка)

| Концепция | Где смотреть |
|-----------|-------------|
| Как выполняется одна задача (сквозной поток) | [runtime-flow.md](diagrams/runtime-flow.md) |
| Жизненный цикл задачи на уровне классов | [03 — ядро диаграмма 1](diagrams/components-framework.md) |
| Два протокола: send_turn vs run_task | [04 — харнесы диаграмма 1](diagrams/components-harnesses.md) |
| Lazy-старт контейнера | [04 — харнесы диаграмма 3](diagrams/components-harnesses.md) |
| Как Sample несёт sandbox-конфиг | [05 — бенчмарки диаграмма 3](diagrams/components-benchmarks.md) |
| Почему Runner и Scorer отдельные | [ADR-009](architecture/06-adr.md#adr-009-runner-и-scorer-как-отдельные-абстракции) |
| Почему MCP, а не stdio | [ADR-002](architecture/06-adr.md#adr-002-mcp-http-bridge-между-агентом-и-sandbox) |
| Как добавить новый харнес | [04 — конец файла](architecture/04-components-harnesses.md#как-добавить-новый-harness) |
| Как добавить новый бенчмарк | [05 — конец файла](architecture/05-components-benchmarks.md#как-добавить-новый-benchmark) |
