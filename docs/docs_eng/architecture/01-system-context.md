# 01 — System Context

> Diagram: [diagrams/context.md](../diagrams/context.md)

## What harness_bench is

harness_bench is a framework for automated comparative benchmarking of LLM agents. It lets you run the same task sets across different language models and agent frameworks, collect results, and compare quality.

Typical use case: an ML engineer wants to understand how well a new model version handles function calling, code writing, and complex agentic tasks — compared with a previous version or a reference model.

## Actors and external systems

| Actor | Type | Role |
|-------|------|------|
| **ML engineer / Researcher** | User | Prepares the YAML config, starts runs, reads `summary.json` or the Web UI, makes model decisions |
| **LLM Provider** | External system | The language model under test. Any OpenAI-compatible endpoint: OpenAI, vLLM, OpenRouter, a local server |
| **LLM Judge Provider** | External system | A judge model for scoring open-ended answers. Can be the same model or a different (e.g. stronger) one. Configured via `judge_model` in the config |
| **Tavily Search API** | External system | Web search for tasks with the `web_search: true` flag. Optional — if no key is set, search is unavailable |
| **BitGN / PAC1 API** | External system | The platform for the PAC1 benchmark. Serves tasks via API and accepts run results. Used only when the `pac1` benchmark is enabled |
| **Docker Hub / Registry** | External system | The Docker image registry. Used to pull the agent image (Hermes) and sandbox-environment images (SWE-bench, TheAgentCompany) |

## System boundaries

Within harness_bench:
- The framework (CLI, orchestrator, Web UI)
- Agent and sandbox Docker containers (managed by the framework)
- Result storage (file system, PostgreSQL)

Outside the system:
- The language models themselves
- Agent frameworks (Hermes, OpenClaw) — used as Docker images
- External APIs (Tavily, BitGN)
- The CI/CD infrastructure the framework may run in
