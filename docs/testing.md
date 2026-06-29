# Тесты

Покрывают оркестратор, харнессы, scorers, runners и конфиг. Не требуют Docker, сети или базы данных — все внешние зависимости замоканы.

## Установка

```bash
pip install -e ".[test]"
# или если основной пакет уже установлен:
pip install pytest pytest-asyncio
```

## Запуск

```bash
# Все тесты
pytest tests/ -v

# Только unit-тесты
pytest tests/unit/ -v

# Только интеграционные тесты
pytest tests/integration/ -v

# Конкретный файл или тест
pytest tests/integration/test_orchestrator.py::test_happy_path -v
```

## Структура

```
tests/
├── conftest.py                          # Общие fixtures: EchoHarness, ExactScorer, FakeBenchmark, mock_db
├── unit/
│   ├── test_models.py
│   ├── test_config.py
│   ├── test_benchmark_config.py
│   ├── test_benchmark_registry.py
│   ├── test_harness_base.py
│   ├── test_turn_response.py
│   ├── test_single_turn_runner.py
│   ├── test_exact_match_scorer.py
│   ├── test_db_retry.py
│   ├── test_niah.py
│   └── test_theagentcompany_scoring.py
└── integration/
    ├── test_orchestrator.py             # _run_task: happy path, timeout, resume, teardown
    ├── test_orchestrator_summary.py     # run(): агрегация, finalize, progress_cb, run_id
    └── test_web_picker.py               # Web UI: выбор/загрузка конфига
```
