# Harness Bench

Framework for benchmarking AI harnesses. Runs agents over task sets, scores answer quality, and shows results in a Web UI with run history.

Supports built-in benchmarks and **custom** ones — write a single Python class and run any agent on it.

Supported harnesses: hermes, openclaw, opencode

> [!IMPORTANT]
> On the first benchmark run, the agent-harness image (`nousresearch/hermes-agent` and the like) is pulled automatically. Some benchmarks (e.g. **SWE-bench** and **theagentcompany**) likewise pull images on first run. Please wait for the download to finish.
> Don't forget to fill in the database in the `.yaml` config, otherwise run results won't be saved!

## Requirements

- Python 3.12+
- Docker Desktop (to run harnesses)
- PostgreSQL (optional; without it, history is not persisted)

## Quick start — local

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e .

cp configs/example.yaml configs/my_run.yaml
# edit api_key, base_url, model_name

framework serve                            # Web UI as a service at http://localhost:8765 — pick a config in the UI
framework serve --config configs/my_run.yaml --open-browser  # preload a config and open the browser
framework run configs/my_run.yaml          # headless run (no UI)
```

PAC1 support: `pip install -e ".[pac1]"`

## Quick start — Docker

```bash
cp docker/.env.example docker/.env
echo "HARNESS_HOST_WORK_DIR=$(pwd)" >> docker/.env

docker compose -f docker/docker-compose.yml --env-file docker/.env up --build -d
```

Then open **http://localhost:8765** — the Web UI as a service. Pick a config from the dropdown (or "New (defaults)"), edit it, and start a run right in the browser.

> `HARNESS_HOST_WORK_DIR` — absolute path to the repo on the host. Needed so agent containers find temporary files under the same path.

> **Windows (Docker Desktop + WSL2):** `$(pwd)` won't work here. The path is read by the **Docker daemon**, which lives in the WSL2 VM, not in Windows — it sees drive `D:` as `/run/desktop/mnt/host/d/...`. Also, the drive-letter colon (`D:\…`) clashes with the `src:dst` volume syntax. So set the WSL2 path explicitly:
> ```
> HARNESS_HOST_WORK_DIR=/run/desktop/mnt/host/d/harness_bench/new_iteration/harness_bench
> ```
> See `docs_eng/configuration.md` for details.

The DB for the service is set by the **deployment environment** (docker-compose injects `DB_HOST=postgres`, etc.), not by the `database` section of the selected config — it is fixed for the lifetime of the service. For a headless console run: `docker compose … run --rm framework framework run /app/configs/my_run.yaml`.

## Harnesses (agents)

| Type | Description | Sandbox |
|------|-------------|---------|
| `hermes` | Hermes Agent in Docker | Yes |
| `opencode` | OpenCode in Docker | Yes |
| `openclaw` | OpenClaw in Docker | Yes |
| `pac1_hermes` | Hermes + PAC1 | — |
| `pac1_opencode` | OpenCode + PAC1 | — |
| `pac1_openclaw` | OpenClaw + PAC1 | — |

## Benchmarks

| Name | Description | Source |
|------|-------------|--------|
| `simpleqa` | Factual Q&A, LLM-judge | [SimpleQA](https://github.com/openai/simple-evals) |
| `bfcl` | Function calling (Berkeley FCL) | [BFCL](https://gorilla.cs.berkeley.edu/leaderboard.html) |
| `bfcl_memory` | Multi-turn BFCL | [BFCL](https://gorilla.cs.berkeley.edu/leaderboard.html) |
| `humaneval_plus` | Code generation, test execution | [HumanEval+](https://github.com/evalplus/evalplus) |
| `persistbench` | Long-term memory, LLM-judge | [PersistBench](https://github.com/ivaxi0s/PersistBench) |
| `niah` | Needle-in-a-Haystack | [NIAH (inspect_evals)](https://github.com/UKGovernmentBEIS/inspect_evals/tree/main/src/inspect_evals/niah) |
| `swe_bench` | Fixing GitHub issues (sandbox) | [SWE-bench](https://www.swebench.com/) |
| `swe_bench_multilingual` | SWE-Bench, multilingual (sandbox) | [SWE-bench](https://www.swebench.com/) |
| `theagentcompany` | Enterprise tasks (sandbox) | [TheAgentCompany](https://github.com/TheAgentCompany/TheAgentCompany) |
| `pac1` | BitGN PAC1 (API, key required) | [BitGN PAC1](https://bitgn.com/challenge/PAC) |

More: [docs_eng/benchmarks.md](docs_eng/benchmarks.md)

## CLI

```bash
framework serve                            # Web UI as a service (pick/edit a config in the browser)
framework run configs/my_run.yaml          # headless benchmark run
framework runs configs/my_run.yaml         # list runs from the DB
framework results RUN_ID --config configs/my_run.yaml
framework compare RUN_ID_A RUN_ID_B --config configs/my_run.yaml
framework db-init configs/my_run.yaml      # apply DB migrations manually
framework db-runs configs/my_run.yaml      # last 20 runs straight from the DB
```

## Your own benchmark

You can test on your own benchmarks. Create `framework/benchmarks/my_bench.py` with a class:

```python
from framework.benchmarks.base import Benchmark, register_benchmark

@register_benchmark("my_bench")
class MyBench(Benchmark):
    def load_samples(self): ...   # list of tasks
    def make_scorer(self): ...    # scoring logic
```

The benchmark shows up in the Web UI and reports automatically. Full guide — [docs_eng/adding-benchmark.md](docs_eng/adding-benchmark.md).

## Documentation

- [Configuration](docs_eng/configuration.md) — all config fields, harness params, DB, Docker env
- [Benchmarks](docs_eng/benchmarks.md) — detailed description of each benchmark
- [Adding a benchmark](docs_eng/adding-benchmark.md) — how to write your own benchmark
- [Tests](docs_eng/testing.md) — running and structure of the tests
- [Architecture](docs_eng/README.md) — C4 diagrams, ADRs, internals
