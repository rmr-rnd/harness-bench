"""TheAgentCompany benchmark adapter."""
from __future__ import annotations

import importlib
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

from framework.benchmarks.base import Benchmark, register_benchmark
from framework.models import AgentTrace, EvalResult, Message, Sample, SandboxSpec, Score, Task, Tool
from framework.scorers.base import Scorer
from framework.benchmarks.theagentcompany.scorer import SandboxEvalScorer

if TYPE_CHECKING:
    from framework.evaluators.llm_judge import LLMJudge
    from framework.sandbox import Sandbox

logger = logging.getLogger(__name__)

_host_benchmarks = os.environ.get("HARNESS_BENCHMARKS_DATA_DIR", "")
_DATA_DIR = (Path(_host_benchmarks) / "theagentcompany") if _host_benchmarks else (
    Path(__file__).parent.parent.parent.parent / "benchmarks_data" / "theagentcompany"
)
TASKS_DIR = _DATA_DIR / "tasks"
PROMPTS_DATA_DIR = _DATA_DIR / "data"

SYSTEM_PROMPT = "Complete the task provided."

# Mirrors original TheAgentCompany DEFAULT_INCORRECT_MESSAGE exactly
INCORRECT_MESSAGE = (
    "Please continue working on the task. "
    "If you think you have solved the task, submit your final answer by calling the submit() tool. "
    "IMPORTANT: YOU SHOULD NEVER ASK FOR HUMAN HELP. "
    "If you are stuck and cannot make progress, call the submit() tool to give up."
)

MAX_ATTEMPTS = 3

BASH_TOOL = Tool(
    name="bash",
    description="Run a bash command in the sandbox environment. Returns stdout+stderr.",
    parameters={
        "type": "object",
        "properties": {
            "cmd": {"type": "string", "description": "The bash command to run"},
        },
        "required": ["cmd"],
    },
)

PYTHON_TOOL = Tool(
    name="python",
    description="Run Python code in the sandbox environment. Returns stdout+stderr.",
    parameters={
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "Python code to execute"},
        },
        "required": ["code"],
    },
)

WEB_BROWSER_GO = Tool(
    name="web_browser_go",
    description=(
        "Navigate the web browser to a URL.\n\n"
        "Once you have navigated to a page, you will be presented with a web accessibility tree "
        "of the elements on the page. Each element has an ID, which is displayed in brackets at "
        "the beginning of its line.\n\n"
        "You should only attempt to navigate the web browser one page at a time."
    ),
    parameters={
        "type": "object",
        "properties": {"url": {"type": "string", "description": "URL to navigate to."}},
        "required": ["url"],
    },
)

WEB_BROWSER_CLICK = Tool(
    name="web_browser_click",
    description="Click an element on the page currently displayed by the web browser.",
    parameters={
        "type": "object",
        "properties": {"element_id": {"type": "integer", "description": "ID of the element to click."}},
        "required": ["element_id"],
    },
)

WEB_BROWSER_TYPE = Tool(
    name="web_browser_type",
    description="Type text into an input on a web browser page.",
    parameters={
        "type": "object",
        "properties": {
            "element_id": {"type": "integer", "description": "ID of the element to type text into."},
            "text": {"type": "string", "description": "Text to type."},
        },
        "required": ["element_id", "text"],
    },
)

WEB_BROWSER_TYPE_SUBMIT = Tool(
    name="web_browser_type_submit",
    description="Type text into a form input on a web browser page and press ENTER to submit the form.",
    parameters={
        "type": "object",
        "properties": {
            "element_id": {"type": "integer", "description": "ID of the element to type text into."},
            "text": {"type": "string", "description": "Text to type."},
        },
        "required": ["element_id", "text"],
    },
)

WEB_BROWSER_SCROLL = Tool(
    name="web_browser_scroll",
    description='Scroll the web browser up or down by one page.',
    parameters={
        "type": "object",
        "properties": {"direction": {"type": "string", "description": '"up" or "down"'}},
        "required": ["direction"],
    },
)

WEB_BROWSER_BACK = Tool(
    name="web_browser_back",
    description="Navigate the web browser back in the browser history.",
    parameters={"type": "object", "properties": {}, "required": []},
)

WEB_BROWSER_FORWARD = Tool(
    name="web_browser_forward",
    description="Navigate the web browser forward in the browser history.",
    parameters={"type": "object", "properties": {}, "required": []},
)

WEB_BROWSER_REFRESH = Tool(
    name="web_browser_refresh",
    description="Refresh the current page of the web browser.",
    parameters={"type": "object", "properties": {}, "required": []},
)

SUBMIT_TOOL = Tool(
    name="submit",
    description=(
        "Submit your final answer to complete the task, or call with no arguments to give up. "
        "Call this when you believe the task is complete or you cannot make further progress."
    ),
    parameters={"type": "object", "properties": {}, "required": []},
)

WEB_BROWSER_TOOLS = [
    WEB_BROWSER_GO, WEB_BROWSER_CLICK, WEB_BROWSER_TYPE_SUBMIT,
    WEB_BROWSER_TYPE, WEB_BROWSER_SCROLL, WEB_BROWSER_BACK,
    WEB_BROWSER_FORWARD, WEB_BROWSER_REFRESH,
]

# Map task names to their evaluator module names in benchmarks/theagentcompany/evaluators/
TASK_EVALUATORS: dict[str, str] = {
    "ds_sql_exercise": "ds_sql_exercise",
    "ds_answer_numerical_data_question": "ds_answer_numerical_data_question",
    "hr_salary_analysis": "hr_salary_analysis",
    "hr_resume_categorization": "hr_resume_categorization",
    "hr_populate_salary_increase_memo": "hr_populate_salary_increase_memo",
    "ml_grade_exam": "ml_grade_exam",
    "research_answer_questions_on_paper": "research_answer_questions_on_paper",
    "sde_copy_table_from_pdf_to_xlsx": "sde_copy_table_from_pdf_to_xlsx",
    "sde_create_sqlite_database": "sde_create_sqlite_database",
    "sde_run_rising_wave_locally": "sde_run_rising_wave_locally",
}

REAL_TASKS = [
    "ds_sql_exercise",
    "ds_answer_numerical_data_question",
    "hr_salary_analysis",
    "hr_resume_categorization",
    "hr_populate_salary_increase_memo",
    "ml_grade_exam",
    "research_answer_questions_on_paper",
    "sde_copy_table_from_pdf_to_xlsx",
    "sde_create_sqlite_database",
    "sde_run_rising_wave_locally",
]


class _ExecResult:
    """Result object returned by _EvalSandboxCompat.exec(), matching inspect_ai sandbox API."""

    def __init__(self, stdout: str, stderr: str, returncode: int) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.success = returncode == 0


class _EvalSandboxCompat:
    """Adapts the new Sandbox interface to what TAC evaluators expect.

    Old DockerSandbox.exec_bash() returned str; new Sandbox.exec_bash() returns
    (stdout, stderr, exit_code). This wrapper makes evaluators work unchanged.
    """

    def __init__(self, sandbox) -> None:
        self._s = sandbox

    async def read_file(self, path: str) -> bytes:
        return await self._s.read_file(path)

    async def exec_bash(self, cmd: str, timeout: int = 120) -> str:
        stdout, stderr, _ = await self._s.exec_bash(cmd, timeout=timeout)
        return stdout or stderr

    async def exec(self, cmd: list[str], timeout: int = 120) -> "_ExecResult":
        """Run a command in the sandbox. Returns object with .returncode, .stdout, .stderr."""
        stdout, stderr, returncode = await self._s.exec_cmd(cmd, timeout=timeout)
        return _ExecResult(stdout=stdout, stderr=stderr, returncode=returncode)


@register_benchmark('theagentcompany')
class TheAgentCompanyBenchmark(Benchmark):
    name = "theagentcompany"
    display_name  = "TheAgentCompany"
    description   = (
        "Реальные рабочие задачи в симулированной компании. Агент работает в Docker-среде с Gitea, "
        "базами данных, файловой системой. Задачи: анализ данных, написание кода, работа с репозиториями. "
        "PARTIAL означает частичное выполнение (набрано X из Y контрольных точек)."
    )
    category      = "Анализ данных"
    default_paths = (
        "benchmarks_data/theagentcompany/data",
        "benchmarks_data/theagentcompany/answers",
    )

    def make_scorer(self) -> Scorer:
        # Scoring always matches the inspect_evals reference ("original"): no UI/config
        # knob. The evaluators retain internal modes for tests/programmatic use.
        return SandboxEvalScorer(
            module_prefix="framework.benchmarks.theagentcompany.evaluators",
        )

    def load_samples(self) -> list[Sample]:
        if not TASKS_DIR.exists():
            raise FileNotFoundError(f"TAC tasks dir not found: {TASKS_DIR}")

        tasks = []
        for task_name in REAL_TASKS:
            prompt_file = PROMPTS_DATA_DIR / f"{task_name}.md"
            compose_yaml = TASKS_DIR / task_name / "compose.yaml"

            if not prompt_file.exists() or not compose_yaml.exists():
                continue

            prompt = prompt_file.read_text().strip()
            if not prompt:
                continue

            evaluator_module = TASK_EVALUATORS.get(task_name)

            # Load checkpoint criteria from evaluator module as ground truth
            criteria: list[str] = []
            if evaluator_module:
                try:
                    import importlib
                    mod = importlib.import_module(
                        f"framework.benchmarks.theagentcompany.evaluators.{evaluator_module}"
                    )
                    criteria = getattr(mod, "CRITERIA", [])
                except Exception:
                    pass
            ground_truth = "\n".join(f"{i+1}. {c}" for i, c in enumerate(criteria)) if criteria else task_name

            tasks.append(Task(
                id=f"tac_{task_name}",
                benchmark=self.name,
                system_prompt=SYSTEM_PROMPT,
                messages=[Message(role="user", content=prompt)],
                tools=[BASH_TOOL, PYTHON_TOOL, SUBMIT_TOOL] + WEB_BROWSER_TOOLS,
                ground_truth=ground_truth,
                sandbox=SandboxSpec(
                    type="docker_compose",
                    compose_file=str(compose_yaml.absolute()),
                    target_service="default",
                ),
                mcp_tool_groups=["shell", "filesystem", "browser"],
                metadata={
                    "task_name": task_name,
                    "compose_file": str(compose_yaml.absolute()),
                    "evaluator_module": evaluator_module,
                    "incorrect_message": INCORRECT_MESSAGE,
                    "max_attempts": MAX_ATTEMPTS,
                },
            ))

        limit = self.cfg.limit
        return tasks[:limit] if limit else tasks

    def get_sandbox(self, task: Task) -> SandboxSpec:
        compose_file = task.metadata.get("compose_file", "")
        return SandboxSpec(
            type="docker_compose",
            compose_file=compose_file,
            target_service="default",
        )

    async def evaluate_sandbox(
        self,
        task: Task,
        sandbox: "Sandbox",
        trace: AgentTrace,
        judge: "LLMJudge",
    ) -> EvalResult:
        evaluator_module = task.metadata.get("evaluator_module")
        if not evaluator_module:
            return EvalResult(
                sample_id=task.id, score=0.0, grade="INCORRECT",
                explanation="No evaluator module configured for this task",
            )

        # Evaluators were written against the old DockerSandbox whose exec_bash()
        # returned a plain str. The new Sandbox.exec_bash() returns (stdout, stderr, rc).
        # Wrap so evaluators don't need to change.
        compat = _EvalSandboxCompat(sandbox)

        try:
            mod = importlib.import_module(
                f"framework.benchmarks.theagentcompany.evaluators.{evaluator_module}"
            )
            checkpoint_results = await mod.evaluate(compat, judge)  # always "original"
        except Exception as exc:
            logger.warning("TAC evaluator %s failed: %s", evaluator_module, exc)
            return EvalResult(
                sample_id=task.id, score=0.0, grade="ERROR",
                explanation=f"Evaluator error: {exc}",
            )

        from framework.benchmarks.theagentcompany.evaluators.base import summarize_checkpoints
        score, grade, details = summarize_checkpoints(checkpoint_results)
        return EvalResult(
            sample_id=task.id,
            score=score,
            grade=grade,
            explanation=f"score={score:.3f} | {details}",
        )
