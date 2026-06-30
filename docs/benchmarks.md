# Бенчмарки

Данные хранятся в `benchmarks_data/`. Добавление нового бенчмарка — см. [adding-benchmark.md](adding-benchmark.md).

## simpleqa

Фактические вопросы-ответы. Оценка через LLM-judge.

```
benchmarks_data/simpleqa/
  questions/
  answers/
```

```yaml
- name: simpleqa
  limit: 100
  web_search: false   # true — разрешить агенту веб-поиск через Tavily
```

## bfcl

Berkeley Function-Calling Leaderboard — вызов функций агентом.

```
benchmarks_data/bfcl/
  questions/
  answers/
```

```yaml
- name: bfcl
  limit: null
```

## bfcl_memory

Многоходовая версия BFCL с историей разговора.

```yaml
- name: bfcl_memory
  limit: null
```

## humaneval_plus

Задачи на генерацию кода (HumanEval). Оценка — запуск тестов в **локальном subprocess** на хосте (`subprocess.run([sys.executable, ...])`), **без Docker-sandbox**. Harness с поддержкой sandbox не требуется.

```
benchmarks_data/humaneval_plus/
  questions/    ← humaneval_plus.jsonl
  answers/      ← ground_truth.jsonl (тест-кейсы)
```

```yaml
- name: humaneval_plus
  limit: null
```

## persistbench

Долгосрочная память агента. Оценка через LLM-judge.

```
benchmarks_data/persistbench/
  data/
  answers/
```

```yaml
- name: persistbench
  limit: null
```

## niah

Needle-in-a-Haystack — поиск информации в длинном контексте.

```
benchmarks_data/niah/
  data/       ← образцы (sample_*.json), уже в репозитории
  answers/
```

> Данные уже сгенерированы и закоммичены в `benchmarks_data/niah/data/` — отдельный шаг подготовки не требуется.

```yaml
- name: niah
  limit: null
```

## swe_bench / swe_bench_multilingual

Исправление реальных GitHub-issues. Требует harness с поддержкой sandbox (`hermes`, `opencode`, `openclaw`).

```
benchmarks_data/swe_bench/
  swe_bench_5tasks.json
  swe_bench_12tasks.json          ← дефолт для swe_bench
  swe_bench_16tasks.json
  swe_bench_multilingual_8tasks.json   ← дефолт для swe_bench_multilingual
```

```yaml
- name: swe_bench
  limit: 5
- name: swe_bench_multilingual
  limit: null
```

## theagentcompany

Агентские задачи в корпоративной среде (SQL, HR, ML и др.). Каждая задача запускает свой Docker-compose sandbox. Требует harness с поддержкой sandbox.

```
benchmarks_data/theagentcompany/
  data/       ← промпты задач (.md)
  tasks/      ← docker-compose окружения
    ds_sql_exercise/compose.yaml
    hr_salary_analysis/compose.yaml
    ...
```

```yaml
- name: theagentcompany
  limit: 2
```

Доступные задачи: `ds_sql_exercise`, `ds_answer_numerical_data_question`, `hr_salary_analysis`, `hr_resume_categorization`, `hr_populate_salary_increase_memo`, `ml_grade_exam`, `research_answer_questions_on_paper`, `sde_copy_table_from_pdf_to_xlsx`, `sde_create_sqlite_database`, `sde_run_rising_wave_locally`.

> В Docker-режиме требует монтирования `benchmarks_data` по тому же хост-пути через `HARNESS_BENCHMARKS_DATA_DIR` в `docker/.env`.

## pac1

Задачи от BitGN. Данные загружаются через API. Требует `pac1`-extras и ключ.

```bash
pip install -e ".[pac1]"
```

```yaml
harness:
  type: pac1_opencode          # или pac1_hermes, pac1_openclaw, pac1_omp
  benchmark_harness:
    pac1: pac1_opencode        # для OMP: pac1_omp
  bitgn_api_key: ${BITGN_API_KEY}
  bitgn_benchmark_host: https://api.bitgn.com
  bitgn_benchmark_id: bitgn/pac1-dev
  bitgn_run_name: my-run

benchmarks:
  - name: pac1
    limit: 10
```
