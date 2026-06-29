"""HumanEval+ benchmark — code generation evaluated by execution."""
from __future__ import annotations

import json
import re
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import TYPE_CHECKING

from framework.benchmarks.base import Benchmark, register_benchmark
from framework.utils.work_dir import make_work_file
from framework.models import AgentTrace, Message, Sample, Score
from framework.scorers.base import Scorer

if TYPE_CHECKING:
    from framework.config import BenchmarkConfig
    from framework.evaluators.llm_judge import LLMJudge
    from framework.sandbox import Sandbox

INSTRUCTION_PREFIX = (
    "Complete the following Python function. "
    "Return ONLY the function implementation inside a ```python code block. "
    "Do not add any explanation, imports outside the function, or extra code.\n\n"
)

EXEC_TIMEOUT = 30


def _extract_code(text: str) -> str:
    m = re.search(r"```python\n(.*?)```", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    m = re.search(r"```\w*\n(.*?)```", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    return text.strip()


def _run_tests(code: str, test_code: str, entry_point: str, timeout: int = EXEC_TIMEOUT) -> tuple[bool, str]:
    """Run code + tests in a subprocess. Returns (passed, message)."""
    full_code = textwrap.dedent(f"""
import sys
import math
import re
import collections
from typing import List, Dict, Tuple, Optional, Set, Any, Union
from collections import defaultdict, Counter

{code}

{test_code}

try:
    check({entry_point})
    print("PASSED")
except Exception as e:
    print(f"FAILED: {{e}}")
    sys.exit(1)
""")
    tmp_path = make_work_file(prefix="humaneval-", suffix=".py")
    tmp_path.write_text(full_code)

    try:
        result = subprocess.run(
            [sys.executable, tmp_path],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        passed = result.returncode == 0 and "PASSED" in result.stdout
        msg = result.stdout.strip() or result.stderr.strip()
        return passed, msg
    except subprocess.TimeoutExpired:
        return False, f"Timeout after {timeout}s"
    except Exception as e:
        return False, str(e)
    finally:
        Path(tmp_path).unlink(missing_ok=True)


class _HumanEvalScorer(Scorer):
    async def __call__(
        self,
        sample: Sample,
        trace: AgentTrace,
        judge: "LLMJudge",
        sandbox: "Sandbox | None" = None,
    ) -> Score:
        gt = sample.ground_truth
        code = _extract_code(trace.final_output)
        entry_point = sample.metadata.get("entry_point", "")
        test_code = gt.get("test", "")
        passed, msg = _run_tests(code, test_code, entry_point)
        return Score(
            sample_id=sample.id,
            score=1.0 if passed else 0.0,
            grade="CORRECT" if passed else "INCORRECT",
            explanation=msg[:500],
        )


@register_benchmark('humaneval_plus')
class HumanEvalPlusBenchmark(Benchmark):
    name = "humaneval_plus"
    display_name  = "HumanEval+"
    description   = (
        "Генерация кода на Python. Агент получает сигнатуру функции и docstring, должен написать реализацию. "
        "Оценка: запуск тест-кейсов (включая дополнительные из HumanEval+). CORRECT = все тесты прошли."
    )
    category      = "Программирование"
    default_paths = (
        "benchmarks_data/humaneval_plus/questions",
        "benchmarks_data/humaneval_plus/answers",
    )

    def load_samples(self) -> list[Sample]:
        tasks_dir = Path(self.cfg.tasks_dir)
        answers_dir = Path(self.cfg.answers_dir)

        gt_map: dict[str, dict] = {}
        gt_file = answers_dir / "ground_truth.jsonl"
        if gt_file.exists():
            with open(gt_file) as f:
                for line in f:
                    if line.strip():
                        obj = json.loads(line)
                        gt_map[obj["task_id"]] = obj

        samples: list[Sample] = []
        q_file = tasks_dir / "humaneval_plus.jsonl"
        if not q_file.exists():
            return []

        with open(q_file) as f:
            for line in f:
                if not line.strip():
                    continue
                obj = json.loads(line)
                tid = obj["task_id"]
                if tid not in gt_map:
                    continue
                prompt = INSTRUCTION_PREFIX + obj["prompt"]
                samples.append(Sample(
                    id=f"humaneval_{tid.replace('/', '_')}",
                    benchmark=self.name,
                    messages=[Message(role="user", content=prompt)],
                    ground_truth=gt_map[tid],
                    metadata={"entry_point": obj.get("entry_point", ""), "task_id": tid},
                ))

        limit = self.cfg.limit
        return samples[:limit] if limit else samples

    def format_prompt(self, sample: Sample) -> list[dict]:
        return [{"role": m.role, "content": m.content} for m in sample.messages]

    def make_scorer(self) -> Scorer:
        return _HumanEvalScorer()

