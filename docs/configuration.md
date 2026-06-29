# Конфигурация

Все строковые поля поддерживают `${VAR}` и `${VAR:-default}`.

Стартовый шаблон: `configs/example.yaml`.

## Полный пример конфига

```yaml
model:
  base_url: https://api.openai.com/v1   # любой OpenAI-совместимый endpoint
  api_key: ${OPENAI_API_KEY}
  model_name: gpt-4o
  temperature: 0.0
  max_tokens: 4096
  reasoning_effort: null                # none / low / medium / high / xhigh

judge_model:                            # модель для LLM-as-judge (опционально)
  base_url: https://api.openai.com/v1
  api_key: ${OPENAI_API_KEY}
  model_name: gpt-4o-mini
  temperature: 0.0
  max_tokens: 4096

harness:
  type: hermes                          # см. ниже

benchmarks:
  - name: simpleqa
    limit: 50                           # null = все задачи
  - name: bfcl
    limit: null

parallelism:
  workers: 2                            # кол-во параллельных задач
  timeout_per_task: 300                 # секунд на одну задачу
  eval_timeout: 120                     # секунд на оценку

search:
  tavily_api_key: ${TAVILY_API_KEY:-}   # для web_search: true в simpleqa

docker:
  network: bridge
  cleanup_after: true

database:                               # опционально
  host: localhost                       # или postgres в Docker
  port: 5432
  name: harness_results
  user: harness
  password: harness_dev
```

## Инфраструктурные vs UI-managed секции

Конфиг делится на две группы по тому, кто ими владеет:

| Группа | Секции | Кто редактирует |
|--------|--------|-----------------|
| **UI-managed** (параметры прогона) | `model`, `judge_model`, `search`, `harness`, `benchmarks`, `parallelism` | Web UI (форма настроек) **и** YAML |
| **Инфраструктура** (config-only) | `database`, `docker`, `swe_bench`, `analysis_model` | **Только** YAML-файл |

Правила:

- **Сохранение из Web UI** («Save YAML») перезаписывает только UI-managed секции. Инфраструктура остаётся ровно как в файле — с `${ENV}`-плейсхолдерами и паролями. Так настройки БД нельзя случайно затереть из интерфейса.
- **Запуск из Web UI** берёт инфраструктуру из загруженного конфига, а не из дефолтов (`docker`, `swe_bench` применяются как в файле).
- `database` не редактируется в UI; её состояние (подключена / не настроена → история не сохранится) показывается в логе **Activity** при запуске прогона.
- `analysis_model` редактируется отдельной кнопкой ⚙ в истории, а не в форме прогона.
- `run_id` — служебное поле, в сохраняемый конфиг не пишется.

> **`harness` и кастомные поля.** Секция `harness` допускает произвольные дополнительные поля (`extra=allow`). При «Save YAML» она перезаписывается целиком из формы, поэтому **вручную дописанные** в YAML поля harness, которых нет в форме, будут потеряны. Если правишь такие поля — правь YAML напрямую и не используй «Save YAML», либо держи их вне UI-цикла.

## Harness-специфичные параметры

```yaml
harness:
  type: hermes
  hermes_image: nousresearch/hermes-agent:latest
  hermes_api_key: ''                    # генерируется автоматически если пусто
  hermes_approvals_off: true

  # opencode
  opencode_image: ghcr.io/anomalyco/opencode:latest
  opencode_token: ''

  # openclaw
  openclaw_image: ghcr.io/openclaw/openclaw:latest
  openclaw_token: ''
  openclaw_approvals_off: true

  # PAC1
  bitgn_api_key: ${BITGN_API_KEY}
  bitgn_benchmark_host: https://api.bitgn.com
  bitgn_benchmark_id: bitgn/pac1-dev
  bitgn_run_name: harness-bench
```

## Per-benchmark harness

Разные harness для разных бенчмарков в одном запуске:

```yaml
harness:
  type: hermes                          # дефолтный harness
  benchmark_harness:
    pac1: pac1_opencode                 # для pac1 — использовать pac1_opencode
    bfcl: opencode                      # для bfcl — использовать opencode
```

## База данных

PostgreSQL используется для хранения истории запусков, шагов агента и результатов оценки. Без БД история **не сохраняется** — результаты доступны только в реальном времени (CLI-вывод и Web UI текущего прогона).

### Локально

Запусти только postgres из docker-compose:

```bash
docker compose -f docker/docker-compose.yml --env-file docker/.env up postgres -d
```

Миграции применяются автоматически при старте postgres-контейнера через `db/migrations/`. Для ручного применения:

```bash
framework db-init configs/my_run.yaml
```

В Docker-режиме `database.host` должен быть `postgres` (имя сервиса).

## Переменные окружения (Docker)

Задаются в `docker/.env`:

| Переменная | Описание |
|-----------|---------|
| `HARNESS_HOST_WORK_DIR` | Абсолютный путь к репозиторию на хосте |
| `DB_PASSWORD` | Пароль PostgreSQL (дефолт: `harness_dev`) |

Задаются автоматически в `docker/docker-compose.yml`:

| Переменная | Описание |
|-----------|---------|
| `HARNESS_WORK_DIR` | Путь для временных файлов (совпадает внутри контейнера и на хосте) |
| `HARNESS_DOCKER_NETWORK` | Имя Docker-сети (`harness-net`) |
| `HARNESS_MCP_HOST` | Hostname framework-контейнера для агентов (значение: `framework`) |
| `HARNESS_BENCHMARKS_DATA_DIR` | Хост-путь к `benchmarks_data` для sandbox-compose файлов |

## Запуск на Windows (Docker Desktop)

Windows-пути вида `D:\harness_bench` (и `C:/...`) нельзя использовать в `HARNESS_HOST_WORK_DIR` напрямую: двоеточие буквы диска конфликтует с синтаксисом `src:dst` в volume'ах docker-compose, а демон Docker Desktop резолвит хостовые пути через WSL2-mount. Используй WSL2-путь:

```
HARNESS_HOST_WORK_DIR=/run/desktop/mnt/host/d/harness_bench
```

Шаблон: `D:\foo\bar` → `/run/desktop/mnt/host/d/foo/bar` (буква диска — строчная, `\` → `/`).
