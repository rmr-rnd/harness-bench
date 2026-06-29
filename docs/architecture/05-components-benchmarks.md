# 05 — Components: Benchmark Subsystem

> Диаграмма: [diagrams/components-benchmarks.md](../diagrams/components-benchmarks.md)

## Benchmark ABC

```python
class Benchmark(ABC):
    name: str

    def load_samples(self) -> list[Sample]: ...

    def make_runner(self, model_cfg=None) -> Runner:
        return SingleTurnRunner()   # переопределяется для multi-turn

    def make_scorer(self) -> Scorer: ...

    def format_prompt(self, sample: Sample) -> list[dict]:
        return [system_prompt_msg] + messages  # default
```

**Важно:** sandbox-конфигурация (`SandboxSpec`, `SandboxTool`, `Checkpoint`, `mcp_tool_groups`) теперь хранится в `Sample`, а не в Benchmark. Benchmark только загружает данные и назначает Runner/Scorer.

---

## Таблица бенчмарков

> Все scorer'ы ниже — собственные подклассы `Scorer` каждого бенчмарка, а не базовые классы из `framework/scorers/` (см. примечание в [03](03-components-framework.md#scorer-frameworkscorersbasepy)).

### Без Docker

| Бенчмарк | Runner | Scorer | Данные |
|----------|--------|--------|--------|
| **SimpleQA** | SingleTurn | `_SimpleQAScorer` (LLM-judge) | `benchmarks_data/simpleqa/` |
| **BFCL** | BFCLRunner (multi-turn) | `_BFCLScorer` (AST checker) | `benchmarks_data/bfcl/` |
| **BFCL Memory** | BFCLMemoryRunner (multi-session) | `_BFCLMemoryScorer` (AST checker) | `benchmarks_data/bfcl/` |
| **HumanEval+** | SingleTurn | `_HumanEvalScorer` (тесты в локальном subprocess) | `benchmarks_data/humaneval_plus/` |
| **PersistBench** | SingleTurn | `_PersistBenchScorer` (LLM-judge) | `benchmarks_data/persistbench/` |
| **NIAH** | SingleTurn | `_NIAHScorer` (LLM-judge, рубрика 1/3/5/7/10) | `benchmarks_data/niah/` |

> HumanEval+ **не** использует Docker-sandbox: сгенерированный код исполняется через `subprocess.run([sys.executable, ...])` прямо на хосте.

### С Docker (sample.sandbox задан)

| Бенчмарк | Sandbox-тип | Scorer | Данные |
|----------|------------|--------|--------|
| **SWE-bench** | `swe_bench`: per-repo образ | `SWEBenchScorer` (eval.sh) | `benchmarks_data/swe_bench/swe_bench_12tasks.json` |
| **SWE-bench Multilingual** | `swe_bench`: per-repo образ | `SWEBenchScorer` (eval.sh) | `benchmarks_data/swe_bench/swe_bench_multilingual_8tasks.json` |
| **TheAgentCompany** | `docker_compose` | `SandboxEvalScorer` (per-task evaluator, weighted) | `benchmarks_data/theagentcompany/` |
| **PAC1** | Удалённый PCM-workspace (BitGN) | `_Pac1Scorer` (run-level grade) | BitGN API (не локальные файлы) |

---

## Детали ключевых бенчмарков

### BFCL — Berkeley Function Calling

> Ключевое решение: [ADR-007 In-process mock-бэкенды](06-adr.md#adr-007-bfcl-использует-in-process-mock-бэкенды)

**Особенность кодирования функций:** функции передаются как JSON в тексте сообщения, **не** как OpenAI tools API. Агент отвечает в формате `[function_name(param=value)]`. AST-парсер извлекает вызовы и сравнивает с эталоном.

**BFCLRunner (в `benchmarks/bfcl/`):**
- Запускает `BFCLMCPServer` с in-process mock-бэкендами (GorillaFileSystem, MathAPI, TwitterAPI, …)
- Каждый ход: агент вызывает функцию → mock возвращает результат → следующий ход
- Вся логика диалога в Runner, харнес не знает о BFCL

**AST Checker** (`bfcl/_shared/ast_checker.py`):
- Сравнивает вызовы через Python AST
- Поддерживает: nested types, optional params, partial match, `miss_func` (функции открываются по ходу)

---

### SWE-bench

**SWEbenchSandbox** (type=`swe_bench`, зарегистрирован в `benchmarks/swe_bench/sandbox.py`):
- Docker-образ с клоном репозитория и зависимостями
- `get_patch()` → `git diff HEAD` — что изменил агент
- `run_eval_script(script)` → запустить eval.sh, вернуть лог

**`SWEBenchScorer`** (пока sandbox жив):
```
sandbox.get_patch()        → trace._agent_patch
sandbox.run_eval_script()  → trace._eval_log
парсинг FAIL_TO_PASS / PASS_TO_PASS из лога → score 1.0 / 0.0
grade = CORRECT (все тесты прошли) | INCORRECT
```
Патч и eval-лог сохраняются в БД в `task_outputs.agent_patch` и `task_outputs.eval_log`.

---

### TheAgentCompany

Docker Compose стек: Gitea, MariaDB, Redis, веб-сервер, почтовый сервер.

**Инструменты в sandbox** (через MCP Bridge):
- `bash` — shell-команды
- `python` — Python-скрипты
- `web_browser_go/click/type/type_submit/scroll` — управление браузером

**Оценка — `SandboxEvalScorer`:**
- Динамически импортирует per-task evaluator-модуль из `theagentcompany/evaluators/` (имя — в `sample.metadata["evaluator_module"]`)
- Модуль возвращает список взвешенных `CheckpointResult` (`{id, value, max_value}`)
- `score = sum(value) / sum(max_value)`, `min(1.0)`
- Grade: CORRECT (≥1.0), PARTIAL (>0), INCORRECT (0)
- LLM-судья доступен evaluator'у для нечётких проверок

---

### PAC1

```python
class Pac1Benchmark(Benchmark):
    def load_samples(self):
        # 1. client.get_benchmark(id) → task definitions
        # 2. client.start_run(id, name, api_key) → run_id + trial_ids
        # 3. Создаёт Sample per trial_id с trial_id в metadata
```

Pac1Scorer читает `trace._pac1_result` (outcome) и сразу выставляет `grade="ERROR"` только для `OUTCOME_ERR_INTERNAL`; в остальных случаях — `grade="EVALUATING"`, `score=0.0`. Реальные оценки приходят run-level: оркестратор после всех задач вызывает `harness.await_run_grades()` (→ `submit_run()` → `SubmitRunResponse.trials[]`) и переписывает score/grade через `_apply_run_grades()`.

---

## Как добавить новый Benchmark

### Простой бенчмарк

```python
# framework/benchmarks/my_bench.py
from framework.benchmarks.base import Benchmark
from framework.scorers.llm_judge import LLMJudgeScorer

@register_benchmark("my_bench")
class MyBenchmark(Benchmark):
    name = "my_bench"

    def load_samples(self) -> list[Sample]:
        # читает self.cfg.tasks_dir
        ...

    def make_scorer(self):
        return LLMJudgeScorer()
```

### Sandbox-бенчмарк

Sandbox-конфиг теперь задаётся прямо в `Sample` при `load_samples()`:

```python
def load_samples(self) -> list[Sample]:
    samples = []
    for item in load_json(self.cfg.tasks_dir):
        samples.append(Sample(
            id=item["id"],
            benchmark=self.name,
            messages=[Message(role="user", content=item["prompt"])],
            ground_truth=item["answer"],
            # Sandbox-конфигурация прямо в Sample:
            sandbox=SandboxSpec(type="docker_run", image="ubuntu:22.04"),
            mcp_tool_groups=["shell", "filesystem"],
            checkpoints=[
                Checkpoint(name="result_exists", cmd="test -f /result.txt", weight=1.0),
            ],
            sandbox_tools=[],  # кастомные инструменты если нужны
        ))
    return samples

def make_scorer(self):
    return CheckpointScorer()
```

### Регистрация

Декоратор `@register_benchmark("my_bench")` делает всё автоматически. Добавить данные:

```yaml
benchmarks:
  - name: my_bench
    tasks_dir: benchmarks_data/my_bench/questions
    limit: 50
```
