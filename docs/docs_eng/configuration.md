# Configuration

All string fields support `${VAR}` and `${VAR:-default}`.

Starter template: `configs/example.yaml`.

## Full config example

```yaml
model:
  base_url: https://api.openai.com/v1   # any OpenAI-compatible endpoint
  api_key: ${OPENAI_API_KEY}
  model_name: gpt-4o
  temperature: 0.0
  max_tokens: 4096
  reasoning_effort: null                # none / low / medium / high / xhigh

judge_model:                            # model for LLM-as-judge (optional)
  base_url: https://api.openai.com/v1
  api_key: ${OPENAI_API_KEY}
  model_name: gpt-4o-mini
  temperature: 0.0
  max_tokens: 4096

harness:
  type: hermes                          # see below

benchmarks:
  - name: simpleqa
    limit: 50                           # null = all tasks
  - name: bfcl
    limit: null

parallelism:
  workers: 2                            # number of parallel tasks
  timeout_per_task: 300                 # seconds per task
  eval_timeout: 120                     # seconds for scoring

search:
  tavily_api_key: ${TAVILY_API_KEY:-}   # for web_search: true in simpleqa

docker:
  network: bridge
  cleanup_after: true

database:                               # optional
  host: localhost                       # or postgres in Docker
  port: 5432
  name: harness_results
  user: harness
  password: harness_dev
```

## Infrastructure vs UI-managed sections

The config splits into two groups by ownership:

| Group | Sections | Who edits |
|-------|----------|-----------|
| **UI-managed** (run parameters) | `model`, `judge_model`, `search`, `harness`, `benchmarks`, `parallelism` | Web UI (settings form) **and** YAML |
| **Infrastructure** (config-only) | `database`, `docker`, `swe_bench`, `analysis_model` | **YAML file only** |

Rules:

- **Save from the Web UI** ("Save YAML") overwrites only the UI-managed sections. Infrastructure stays exactly as in the file — with `${ENV}` placeholders and passwords. So DB settings can't be accidentally wiped from the UI.
- **Run from the Web UI** takes infrastructure from the loaded config, not from defaults (`docker`, `swe_bench` apply as in the file).
- `database` is not editable in the UI; its state (connected / not configured → history won't be saved) is shown in the **Activity** log when a run starts.
- `analysis_model` is edited via a separate ⚙ button in the history, not in the run form.
- `run_id` is a service field, not written to the saved config.

> **`harness` and custom fields.** The `harness` section allows arbitrary extra fields (`extra=allow`). On "Save YAML" it is overwritten entirely from the form, so **manually added** harness fields in YAML that aren't in the form will be lost. If you edit such fields — edit the YAML directly and don't use "Save YAML", or keep them out of the UI loop.

## Harness-specific parameters

```yaml
harness:
  type: hermes
  hermes_image: nousresearch/hermes-agent:latest
  hermes_api_key: ''                    # auto-generated if empty
  hermes_approvals_off: true

  # opencode
  opencode_image: ghcr.io/anomalyco/opencode:latest
  opencode_token: ''

  # openclaw
  openclaw_image: ghcr.io/openclaw/openclaw:latest
  openclaw_token: ''
  openclaw_approvals_off: true

  # omp / Oh My Pi
  omp_image: harness-bench/omp:16.2.8    # local Docker image
  omp_approval_mode: yolo
  omp_agent_max_seconds: 300

  # PAC1
  bitgn_api_key: ${BITGN_API_KEY}
  bitgn_benchmark_host: https://api.bitgn.com
  bitgn_benchmark_id: bitgn/pac1-dev
  bitgn_run_name: harness-bench
```

## Per-benchmark harness

Different harnesses for different benchmarks in one run:

```yaml
harness:
  type: hermes                          # default harness
  benchmark_harness:
    pac1: pac1_opencode                 # for pac1 — use pac1_opencode
    bfcl: opencode                      # for bfcl — use opencode
```

For OMP, the PAC1 variant is `pac1_omp`, so when `harness.type: omp` is
selected the Web UI and orchestrator can auto-fill `benchmark_harness: {pac1: pac1_omp}`.

## Harness Smoke Test

Real harness verification uses the same path as a normal run. For OMP, build the
local image first, then run your config with a small task limit:

```bash
docker build -t harness-bench/omp:16.2.8 -f docker/omp/Dockerfile docker/omp
framework run configs/my_run.yaml
```

## Database

PostgreSQL stores run history, agent steps, and evaluation results. Without a DB, history is **not saved** — results are available only live (CLI output and the Web UI for the current run).

### Locally

Start just postgres from docker-compose:

```bash
docker compose -f docker/docker-compose.yml --env-file docker/.env up postgres -d
```

Migrations are applied automatically on postgres container start via `db/migrations/`. To apply manually:

```bash
framework db-init configs/my_run.yaml
```

In Docker mode, `database.host` must be `postgres` (the service name).

## Environment variables (Docker)

Set in `docker/.env`:

| Variable | Description |
|----------|-------------|
| `HARNESS_HOST_WORK_DIR` | Absolute path to the repo on the host |
| `DB_PASSWORD` | PostgreSQL password (default: `harness_dev`) |

Set automatically in `docker/docker-compose.yml`:

| Variable | Description |
|----------|-------------|
| `HARNESS_WORK_DIR` | Path for temporary files (same inside the container and on the host) |
| `HARNESS_DOCKER_NETWORK` | Docker network name (`harness-net`) |
| `HARNESS_MCP_HOST` | Hostname of the framework container for agents (value: `framework`) |
| `HARNESS_BENCHMARKS_DATA_DIR` | Host path to `benchmarks_data` for sandbox-compose files |

## Running on Windows (Docker Desktop)

Windows paths like `D:\harness_bench` (and `C:/...`) can't be used in `HARNESS_HOST_WORK_DIR` directly: the drive-letter colon clashes with the `src:dst` volume syntax of docker-compose, and the Docker Desktop daemon resolves host paths through a WSL2 mount. Use the WSL2 path:

```
HARNESS_HOST_WORK_DIR=/run/desktop/mnt/host/d/harness_bench
```

Template: `D:\foo\bar` → `/run/desktop/mnt/host/d/foo/bar` (drive letter lowercased, `\` → `/`).
