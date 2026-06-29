"""SWE-bench benchmark adapter."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from framework.benchmarks.base import Benchmark, register_benchmark
from framework.models import AgentTrace, Message, Sample, SandboxSpec, Score
from framework.benchmarks.swe_bench.engine.constants import ResolvedStatus
from framework.scorers.base import Scorer

if TYPE_CHECKING:
    from framework.config import BenchmarkConfig
    from framework.evaluators.llm_judge import LLMJudge
    from framework.sandbox import Sandbox

logger = logging.getLogger(__name__)

SWE_BENCH_SYSTEM_PROMPT = """\
You are an expert software engineer tasked with resolving a GitHub issue.

The repository is cloned at /testbed. Use bash to explore the codebase, \
understand the issue, and implement a fix by editing files directly.

Guidelines:
- Do NOT run `git commit` or `git add` — just edit files
- You may run tests to verify your fix: cd /testbed && python -m pytest <test_file> -x
- When you are done with your changes, say "I have finished my changes."

The tests will be run automatically after you finish.\
"""


class SWEBenchScorer(Scorer):
    """Extract agent patch, run eval.sh, grade the result.

    Requires SWEbenchSandbox (created from SandboxSpec type="swe_bench").
    """

    async def __call__(
        self,
        sample: Sample,
        trace: AgentTrace,
        judge: "LLMJudge",
        sandbox: "Sandbox | None" = None,
    ) -> Score:
        if sandbox is None or trace.error:
            return self._grade_from_trace(sample, trace)

        from framework.benchmarks.swe_bench.grader import grade_swe_instance
        from framework.benchmarks.swe_bench.engine.test_spec.test_spec import make_test_spec

        instance_id = sample.metadata.get("instance_id", sample.id)
        namespace = sample.sandbox.config.get("namespace", "swebench") if sample.sandbox else "swebench"
        instance_dict = sample.metadata.get("_instance") or {
            **sample.metadata,
            "problem_statement": sample.messages[0].content if sample.messages else "",
        }

        try:
            agent_patch = await sandbox.get_patch()
            trace._agent_patch = agent_patch

            test_spec = make_test_spec(instance_dict, namespace=namespace)
            eval_output = await sandbox.run_eval_script(test_spec.eval_script)
            trace._eval_log = eval_output

            eval_result_raw = grade_swe_instance(
                instance=instance_dict,
                model_patch=agent_patch,
                eval_log_content=eval_output,
                namespace=namespace,
            )
            trace._swe_eval_result = eval_result_raw
        except Exception as exc:
            logger.error("SWEBenchScorer failed for %s: %s", instance_id, exc)
            trace._swe_eval_result = None

        return self._grade_from_trace(sample, trace)

    def _grade_from_trace(self, sample: Sample, trace: AgentTrace) -> Score:
        eval_result = getattr(trace, "_swe_eval_result", None)
        if eval_result is None:
            return Score(
                sample_id=sample.id,
                score=0.0,
                grade="INCORRECT",
                explanation="No eval result attached to trace.",
            )

        resolution_status = eval_result.get("resolution_status", ResolvedStatus.NO.value)
        patch_applied = eval_result.get("patch_applied", False)
        f2p_success = eval_result.get("f2p_success", [])
        f2p_failure = eval_result.get("f2p_failure", [])
        p2p_success = eval_result.get("p2p_success", [])
        p2p_failure = eval_result.get("p2p_failure", [])
        agent_patch = getattr(trace, "_agent_patch", "") or ""

        f2p_total = len(f2p_success) + len(f2p_failure)
        p2p_total = len(p2p_success) + len(p2p_failure)
        patch_lines = len(agent_patch.splitlines()) if agent_patch else 0

        score = 1.0 if resolution_status == ResolvedStatus.FULL.value else 0.0
        grade = "CORRECT" if score == 1.0 else "INCORRECT"
        explanation = (
            f"{resolution_status}. "
            f"F2P: {len(f2p_success)}/{f2p_total}. "
            f"P2P: {len(p2p_success)}/{p2p_total}. "
            f"Patch: {patch_lines} lines."
        )
        if not patch_applied:
            explanation += " Patch not applied."
        return Score(sample_id=sample.id, score=score, grade=grade, explanation=explanation)


@register_benchmark('swe_bench')
class SWEBenchBenchmark(Benchmark):
    name = "swe_bench"
    display_name  = "SWE-bench"
    description   = (
        "Реальные GitHub-issues из популярных Python-репозиториев (django, flask, pytest и др.). "
        "Агент получает описание бага/фичи и должен внести изменения в код репозитория в Docker-среде. "
        "Оценка: запуск тест-сьюта репозитория. CORRECT = все failing тесты стали passing."
    )
    category      = "Программирование"
    default_paths = (
        "benchmarks_data/swe_bench/swe_bench_12tasks.json",
        "benchmarks_data/swe_bench/answers",
    )

    def load_samples(self) -> list[Sample]:
        data_path = Path(self.cfg.tasks_dir)
        instances = []
        with open(data_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                instances.append(json.loads(line))

        if self.cfg.limit:
            instances = instances[: self.cfg.limit]

        samples = []
        for inst in instances:
            instance_id = inst["instance_id"]
            swe_cfg = getattr(self.cfg, "swe_bench", None)
            namespace = (swe_cfg.namespace if swe_cfg else None) or "swebench"
            samples.append(Sample(
                id=instance_id,
                benchmark=self.name,
                messages=[Message(role="user", content=inst["problem_statement"])],
                ground_truth=instance_id,
                system_prompt=SWE_BENCH_SYSTEM_PROMPT,
                sandbox=SandboxSpec(
                    type="swe_bench",
                    config={"instance_id": instance_id, "namespace": namespace},
                ),
                mcp_tool_groups=["shell", "filesystem"],
                metadata={
                    "instance_id": instance_id,
                    "repo": inst.get("repo", ""),
                    "base_commit": inst.get("base_commit", ""),
                    "test_patch": inst.get("test_patch", ""),
                    "version": inst.get("version", ""),
                    "fail_to_pass": (
                        json.loads(inst["FAIL_TO_PASS"])
                        if isinstance(inst.get("FAIL_TO_PASS"), str)
                        else inst.get("FAIL_TO_PASS", [])
                    ),
                    "pass_to_pass": (
                        json.loads(inst["PASS_TO_PASS"])
                        if isinstance(inst.get("PASS_TO_PASS"), str)
                        else inst.get("PASS_TO_PASS", [])
                    ),
                    "_instance": inst,
                },
            ))
        return samples

    def make_scorer(self) -> Scorer:
        return SWEBenchScorer()



@register_benchmark('swe_bench_multilingual')
class SWEBenchMultilingualBenchmark(SWEBenchBenchmark):
    name = "swe_bench_multilingual"
    display_name  = "SWE-bench Multilingual"
    description   = (
        "SWE-bench на многоязычных репозиториях: Go, Rust, Java, Ruby, C++, PHP, TypeScript. "
        "Та же механика что SWE-bench, но код на разных языках."
    )
    category      = "Программирование"
    default_paths = (
        "benchmarks_data/swe_bench/swe_bench_multilingual_8tasks.json",
        "benchmarks_data/swe_bench/answers",
    )
