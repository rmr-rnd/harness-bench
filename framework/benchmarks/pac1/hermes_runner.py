"""Launch and manage Hermes in Docker for one PAC1 task."""
from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from framework.benchmarks.pac1._utils import ANSWER_FILENAME, _normalize_refs, _read_answer
from framework.utils.work_dir import make_work_dir

logger = logging.getLogger(__name__)

# Hook scripts injected into the container via volume mount.
# They write structured log entries to /tmp/hermes-home/logs/{tools,llm}.log.
_HOOK_PRE = """\
import json, sys
d = json.load(sys.stdin)
name = d.get("tool_name", "?")
inp = d.get("tool_input") or {}
cmd = inp.get("command", "") if isinstance(inp, dict) else str(inp)
with open("/tmp/hermes-home/logs/tools.log", "a") as f:
    f.write(f"[TOOL CALL] {name}\\nINPUT: {cmd[:600]}\\n---\\n")
"""

_HOOK_POST = """\
import json, sys
d = json.load(sys.stdin)
name = d.get("tool_name", "?")
out = (d.get("extra") or {}).get("result") or ""
with open("/tmp/hermes-home/logs/tools.log", "a") as f:
    f.write(f"[TOOL RESULT] {name}\\nOUTPUT: {str(out)[:800]}\\n===\\n")
"""

_HOOK_POST_LLM = """\
import json, sys
d = json.load(sys.stdin)
ex = d.get("extra") or {}
response = str(ex.get("assistant_response") or "").strip()
reasoning = ""
history = ex.get("conversation_history") or []
for msg in reversed(history):
    if msg.get("role") == "user":
        break
    if msg.get("role") == "assistant":
        r = msg.get("reasoning") or msg.get("reasoning_content") or ""
        if r:
            reasoning = str(r).strip()
            break
with open("/tmp/hermes-home/logs/llm.log", "a") as f:
    if reasoning:
        f.write(f"[THINKING]\\n{reasoning[:2000]}\\n---\\n")
    if response:
        f.write(f"[RESPONSE]\\n{response[:1000]}\\n===\\n")
usage = ex.get("usage") or {}
inp = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
out = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
if inp or out:
    with open("/tmp/hermes-home/logs/tokens.log", "a") as f:
        f.write(f"{inp} {out}\\n")
"""



def _write_hook_scripts(logs_dir: Path) -> None:
    (logs_dir / "hook_pre.py").write_text(_HOOK_PRE, encoding="utf-8")
    (logs_dir / "hook_post.py").write_text(_HOOK_POST, encoding="utf-8")
    (logs_dir / "hook_post_llm.py").write_text(_HOOK_POST_LLM, encoding="utf-8")


def _make_hermes_config(model_name: str, base_url: str) -> str:
    """Generate hermes config.yaml content from model parameters."""
    return f"""\
model:
  default: "{model_name}"
  provider: "custom"
  base_url: "{base_url}"

logging:
  level: "WARNING"

hooks:
  pre_tool_call:
    - command: "python3 /tmp/hermes-home/logs/hook_pre.py"
  post_tool_call:
    - command: "python3 /tmp/hermes-home/logs/hook_post.py"
  post_llm_call:
    - command: "python3 /tmp/hermes-home/logs/hook_post_llm.py"
"""


def _tail_file(
    log_path: Path,
    stop_event: threading.Event,
    formatter: Callable[[str], str],
    seek_end: bool = True,
    wait_timeout: float = 30.0,
) -> None:
    deadline = time.monotonic() + wait_timeout
    while not log_path.exists():
        if stop_event.is_set() or time.monotonic() > deadline:
            return
        time.sleep(0.3)

    with open(log_path, encoding="utf-8", errors="replace") as f:
        if seek_end:
            f.seek(0, 2)
        while not stop_event.is_set():
            line = f.readline()
            if not line:
                time.sleep(0.1)
                continue
            out = formatter(line)
            if out:
                sys.stdout.write(out)
                sys.stdout.flush()


def _tail_agent_log(log_path: Path, stop_event: threading.Event) -> None:
    _DIM = "\x1b[2m"; _RST = "\x1b[0m"; _CYAN = "\x1b[36m"; _YELLOW = "\x1b[33m"

    def fmt(line: str) -> str:
        # Skip DEBUG and INFO lines from internal modules — only show WARNING+
        if " DEBUG " in line or " INFO " in line:
            return ""
        low = line.lower()
        if "tool_call" in low or "calling tool" in low or " tool " in low:
            return f"  {_CYAN}[tool]{_RST} {line.rstrip()}\n"
        elif "llm" in low or "completion" in low or "response" in low:
            return f"  {_YELLOW}[llm]{_RST}  {line.rstrip()}\n"
        return f"  {_DIM}[log]{_RST}  {line.rstrip()}\n"

    _tail_file(log_path, stop_event, fmt, seek_end=True)


def _tail_tools_log(
    log_path: Path,
    stop_event: threading.Event,
    step_cb: Callable | None = None,
) -> None:
    """Stream tools.log; call step_cb only for key events (tool_call / tool_result)."""
    _CYAN = "\x1b[36m"; _GREEN = "\x1b[32m"; _DIM = "\x1b[2m"; _RST = "\x1b[0m"

    # Accumulate current call/result for step_cb
    current_name: str = ""
    input_buf: str = ""
    output_buf: str = ""
    in_section: str = ""  # "call" | "result" | ""

    def flush_call() -> None:
        nonlocal current_name, input_buf, in_section
        if step_cb and current_name:
            step_cb("tool_call", {"name": current_name, "args": input_buf[:300]})
        current_name = input_buf = ""
        in_section = ""

    def flush_result() -> None:
        nonlocal current_name, output_buf, in_section
        if step_cb and output_buf:
            step_cb("tool_result", output_buf[:500])
        current_name = output_buf = ""
        in_section = ""

    def fmt(line: str) -> str:
        nonlocal current_name, input_buf, output_buf, in_section
        stripped = line.rstrip()
        if stripped.startswith("[TOOL CALL]"):
            flush_result()
            current_name = stripped.removeprefix("[TOOL CALL]").strip()
            in_section = "call"
            return f"\n  {_CYAN}>>> TOOL: {current_name}{_RST}\n"
        if stripped.startswith("[TOOL RESULT]"):
            flush_call()
            current_name = stripped.removeprefix("[TOOL RESULT]").strip()
            in_section = "result"
            return f"  {_GREEN}<<< RESULT: {current_name}{_RST}\n"
        if stripped == "---":
            flush_call()
            return ""
        if stripped == "===":
            flush_result()
            return ""
        if stripped.startswith("INPUT:"):
            val = stripped.removeprefix("INPUT:").strip()
            input_buf += val
            return f"  {_DIM}  in : {val[:200]}{_RST}\n"
        if stripped.startswith("OUTPUT:"):
            val = stripped.removeprefix("OUTPUT:").strip()
            output_buf += val
            return f"  {_DIM}  out: {val[:300]}{_RST}\n"
        return f"  {_DIM}{stripped}{_RST}\n" if stripped else ""

    _tail_file(log_path, stop_event, fmt, seek_end=False, wait_timeout=30.0)


def _tail_llm_log(log_path: Path, stop_event: threading.Event) -> None:
    _MAGENTA = "\x1b[35m"; _YELLOW = "\x1b[33m"; _DIM = "\x1b[2m"; _RST = "\x1b[0m"
    block_type: str = ""

    def fmt(line: str) -> str:
        nonlocal block_type
        s = line.rstrip()
        if s == "[THINKING]":
            block_type = "thinking"
            return f"\n  {_MAGENTA}💭 THINKING:{_RST}\n"
        if s == "[RESPONSE]":
            block_type = "response"
            return f"\n  {_YELLOW}🤖 RESPONSE:{_RST}\n"
        if s in ("---", "==="):
            block_type = ""
            return ""
        if block_type == "thinking":
            return f"  {_DIM}{s}{_RST}\n"
        if block_type == "response":
            return f"  {_YELLOW}{s}{_RST}\n"
        return ""

    _tail_file(log_path, stop_event, fmt, seek_end=False, wait_timeout=30.0)


def _sum_tokens(tokens_log: Path) -> tuple[int, int]:
    """Sum input/output tokens from tokens.log written by hook_post_llm.py."""
    if not tokens_log.exists():
        return 0, 0
    inp = out = 0
    for line in tokens_log.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) == 2:
            try:
                inp += int(parts[0])
                out += int(parts[1])
            except ValueError:
                pass
    return inp, out


def run_hermes(
    *,
    task_id: str,
    run_id: str,
    instruction: str,
    workspace_dir: Path,
    system_prompt_template: str,
    hermes_image: str,
    openai_api_key: str,
    openai_base_url: str,
    model_id: str,
    agent_max_seconds: int,
    step_cb: Callable | None = None,
) -> dict:
    """
    Run Hermes in Docker against workspace_dir.
    Returns dict: {message, outcome, refs}.
    """
    safe_task_id = re.sub(r"[^\w\-]", "_", task_id)
    container_name = f"pac1-{run_id[:8]}-{safe_task_id[:20]}"
    system_prompt = system_prompt_template.replace("{instruction}", instruction)

    # Temporary directory for prompt file, config and logs
    tmp_dir = make_work_dir(prefix="pac1-runner-")
    prompt_file = tmp_dir / "prompt.md"
    logs_dir = tmp_dir / "logs"
    config_file = tmp_dir / "config.yaml"

    try:
        prompt_file.write_text(system_prompt, encoding="utf-8")
        logs_dir.mkdir(parents=True, exist_ok=True)

        # Dynamic hermes config from model params
        config_file.write_text(
            _make_hermes_config(model_id, openai_base_url), encoding="utf-8"
        )
        _write_hook_scripts(logs_dir)

        agent_log = logs_dir / "agent.log"
        tools_log = logs_dir / "tools.log"
        llm_log = logs_dir / "llm.log"
        tokens_log = logs_dir / "tokens.log"
        tools_log.unlink(missing_ok=True)
        llm_log.unlink(missing_ok=True)
        tokens_log.unlink(missing_ok=True)

        cmd = [
            "docker", "run", "--rm",
            "--name", container_name,
            "--memory", "2g",
            "--workdir", "/workspace",
            "-v", f"{workspace_dir}:/workspace",
            "-v", f"{prompt_file}:/pac1_prompt.md:ro",
            "-v", f"{config_file}:/tmp/hermes-home/config.yaml:ro",
            "-v", f"{logs_dir}:/tmp/hermes-home/logs",
            "-e", f"OPENAI_API_KEY={openai_api_key}",
            "-e", f"OPENAI_BASE_URL={openai_base_url}",
            "-e", "HERMES_HOME=/tmp/hermes-home",
            "-e", "HERMES_ACCEPT_HOOKS=1",
            "-e", "HERMES_NONINTERACTIVE=1",
            hermes_image,
            "bash", "-c",
            'hermes -z "$(cat /pac1_prompt.md)" --yolo --toolsets terminal',
        ]

        if step_cb:
            step_cb("status", f"Container {container_name} starting")

        stop_event = threading.Event()
        t_agent = threading.Thread(
            target=_tail_agent_log, args=(agent_log, stop_event), daemon=True
        )
        t_tools = threading.Thread(
            target=_tail_tools_log, args=(tools_log, stop_event, step_cb), daemon=True
        )
        t_llm = threading.Thread(
            target=_tail_llm_log, args=(llm_log, stop_event), daemon=True
        )
        t_agent.start(); t_tools.start(); t_llm.start()

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

        def _drain_stdout() -> None:
            skills_done = False
            for line in proc.stdout:
                if not skills_done:
                    if line.strip().startswith("Done:") and "bundled" in line:
                        skills_done = True
                    continue
                sys.stdout.write(f"  \x1b[32m[answer]\x1b[0m {line}")
                sys.stdout.flush()

        t_drain = threading.Thread(target=_drain_stdout, daemon=True)
        t_drain.start()

        try:
            proc.wait(timeout=agent_max_seconds)
        except subprocess.TimeoutExpired:
            logger.warning("[%s] Hermes timed out after %ds", task_id, agent_max_seconds)
            subprocess.run(["docker", "stop", container_name], timeout=15, capture_output=True)
            proc.wait()
            if step_cb:
                step_cb("status", f"⏱ Timeout after {agent_max_seconds}s")
            return {
                "message": f"Agent timed out after {agent_max_seconds}s",
                "outcome": "OUTCOME_ERR_INTERNAL",
                "refs": [],
            }
        finally:
            stop_event.set()
            # Join tail threads before touching log files or cleanup
            for t in (t_drain, t_agent, t_tools, t_llm):
                t.join(timeout=2)

        logger.debug("[%s] docker exit code: %d", task_id, proc.returncode)

        input_tokens, output_tokens = _sum_tokens(tokens_log)
        answer = _read_answer(workspace_dir, task_id)
        answer["input_tokens"] = input_tokens
        answer["output_tokens"] = output_tokens
        if step_cb:
            step_cb("output", answer["message"])
        return answer

    finally:
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)


