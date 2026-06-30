# Benchmarks

Data lives in `benchmarks_data/`. Adding a new benchmark — see [adding-benchmark.md](adding-benchmark.md).

## simpleqa

Factual Q&A. Scored via LLM-judge.

```
benchmarks_data/simpleqa/
  questions/
  answers/
```

```yaml
- name: simpleqa
  limit: 100
  web_search: false   # true — allow the agent web search via Tavily
```

## bfcl

Berkeley Function-Calling Leaderboard — function calls by the agent.

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

Multi-turn version of BFCL with conversation history.

```yaml
- name: bfcl_memory
  limit: null
```

## humaneval_plus

Code-generation tasks (HumanEval). Scoring runs tests in a **local subprocess** on the host (`subprocess.run([sys.executable, ...])`), **without a Docker sandbox**. A sandbox-capable harness is not required.

```
benchmarks_data/humaneval_plus/
  questions/    ← humaneval_plus.jsonl
  answers/      ← ground_truth.jsonl (test cases)
```

```yaml
- name: humaneval_plus
  limit: null
```

## persistbench

Long-term agent memory. Scored via LLM-judge.

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

Needle-in-a-Haystack — finding information in a long context.

```
benchmarks_data/niah/
  data/       ← samples (sample_*.json), already in the repo
  answers/
```

> Data is already generated and committed to `benchmarks_data/niah/data/` — no separate prep step needed.

```yaml
- name: niah
  limit: null
```

## swe_bench / swe_bench_multilingual

Fixing real GitHub issues. Requires a sandbox-capable harness (`hermes`, `opencode`, `openclaw`).

```
benchmarks_data/swe_bench/
  swe_bench_5tasks.json
  swe_bench_12tasks.json          ← default for swe_bench
  swe_bench_16tasks.json
  swe_bench_multilingual_8tasks.json   ← default for swe_bench_multilingual
```

```yaml
- name: swe_bench
  limit: 5
- name: swe_bench_multilingual
  limit: null
```

## theagentcompany

Agentic tasks in an enterprise environment (SQL, HR, ML, etc.). Each task spins up its own Docker-compose sandbox. Requires a sandbox-capable harness.

```
benchmarks_data/theagentcompany/
  data/       ← task prompts (.md)
  tasks/      ← docker-compose environments
    ds_sql_exercise/compose.yaml
    hr_salary_analysis/compose.yaml
    ...
```

```yaml
- name: theagentcompany
  limit: 2
```

Available tasks: `ds_sql_exercise`, `ds_answer_numerical_data_question`, `hr_salary_analysis`, `hr_resume_categorization`, `hr_populate_salary_increase_memo`, `ml_grade_exam`, `research_answer_questions_on_paper`, `sde_copy_table_from_pdf_to_xlsx`, `sde_create_sqlite_database`, `sde_run_rising_wave_locally`.

> In Docker mode, requires mounting `benchmarks_data` at the same host path via `HARNESS_BENCHMARKS_DATA_DIR` in `docker/.env`.

## pac1

Tasks from BitGN. Data is loaded via API. Requires the `pac1` extras and a key.

```bash
pip install -e ".[pac1]"
```

```yaml
harness:
  type: pac1_opencode          # or pac1_hermes, pac1_openclaw, pac1_omp
  benchmark_harness:
    pac1: pac1_opencode        # for OMP: pac1_omp
  bitgn_api_key: ${BITGN_API_KEY}
  bitgn_benchmark_host: https://api.bitgn.com
  bitgn_benchmark_id: bitgn/pac1-dev
  bitgn_run_name: my-run

benchmarks:
  - name: pac1
    limit: 10
```
