# Tests

Cover the orchestrator, harnesses, scorers, runners, and config. They need no Docker, network, or database — all external dependencies are mocked.

## Install

```bash
pip install -e ".[test]"
# or, if the main package is already installed:
pip install pytest pytest-asyncio
```

## Run

```bash
# All tests
pytest tests/ -v

# Unit tests only
pytest tests/unit/ -v

# Integration tests only
pytest tests/integration/ -v

# A specific file or test
pytest tests/integration/test_orchestrator.py::test_happy_path -v
```

## Structure

```
tests/
├── conftest.py                          # Shared fixtures: EchoHarness, ExactScorer, FakeBenchmark, mock_db
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
    ├── test_orchestrator_summary.py     # run(): aggregation, finalize, progress_cb, run_id
    └── test_web_picker.py               # Web UI: pick/load a config
```
