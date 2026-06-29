FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir openai httpx pyyaml

# Framework code is mounted at runtime:
#   -v ./framework:/app/framework
# Task JSON is passed via TASK_FILE env var
# Output JSON is written to OUTPUT_FILE env var

CMD ["python", "-m", "framework.harnesses.docker_runner"]
