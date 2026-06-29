"""BFCL Memory benchmark — separate benchmark for memory scenarios.

5 scenarios (customer, finance, healthcare, notetaker, student).
Each scenario: feed prereq dialogues → agent stores facts → ask test questions.

Scoring:
  score  = correct_answers / total_questions  (across all scenarios)
  grade  = CORRECT (all), PARTIAL (some), INCORRECT (none)
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING

from framework.benchmarks.base import Benchmark, register_benchmark
from framework.models import AgentTrace, Message, Sample, Score, Task
from framework.scorers.base import Scorer

if TYPE_CHECKING:
    from framework.evaluators.llm_judge import LLMJudge
    from framework.sandbox import Sandbox

SYSTEM_PROMPT = (
    "You are an expert assistant. Use the provided tools to answer the user's request. "
    "Call all necessary tools to fulfill the request. "
    "If the required parameters are missing or no tool applies, say so."
)


class _BFCLMemoryScorer(Scorer):
    async def __call__(
        self,
        sample: Sample,
        trace: AgentTrace,
        judge: "LLMJudge",
        sandbox: "Sandbox | None" = None,
    ) -> Score:
        per_q = getattr(trace, "_memory_per_question", None)
        if not per_q:
            return Score(
                sample_id=sample.id,
                score=0.0,
                grade="INCORRECT",
                explanation="No memory question results recorded",
            )

        correct = sum(1 for r in per_q if r["correct"])
        total = len(per_q)
        score = correct / total if total else 0.0

        if correct == total:
            grade = "CORRECT"
        elif correct == 0:
            grade = "INCORRECT"
        else:
            grade = "PARTIAL"

        details = "; ".join(
            f"Q{i+1}({'✓' if r['correct'] else '✗'}): {r['question'][:40]} → {r['answer'][:40]}"
            for i, r in enumerate(per_q)
        )
        return Score(
            sample_id=sample.id,
            score=score,
            grade=grade,
            explanation=f"{correct}/{total} correct. {details}",
        )


@register_benchmark('bfcl_memory')
class BFCLMemoryBenchmark(Benchmark):
    name = "bfcl_memory"
    display_name  = "BFCL Memory"
    description   = (
        "BFCL со сценариями памяти. Агент должен запоминать информацию из предыдущих ходов диалога "
        "и использовать её в последующих вызовах функций. PARTIAL означает частичное выполнение."
    )
    category      = "Память"
    default_paths = (
        "benchmarks_data/bfcl/questions",   # shares data dir with bfcl
        "benchmarks_data/bfcl/answers",
    )

    def make_scorer(self) -> Scorer:
        return _BFCLMemoryScorer()

    def make_runner(self, model_cfg=None):
        from framework.benchmarks.bfcl_memory.runner import BFCLMemoryRunner
        return BFCLMemoryRunner()

    def load_samples(self) -> list[Sample]:
        tasks_dir = Path(self.cfg.tasks_dir)
        answers_dir = Path(self.cfg.answers_dir)
        prereq_dir = tasks_dir.parent / "memory_prereq_conversation"

        # Find memory question file
        memory_file = tasks_dir / "BFCL_v4_memory.json"
        answer_file = answers_dir / "BFCL_v4_memory.json"
        if not memory_file.exists() or not answer_file.exists():
            return []

        # Load ground truths
        gt_map: dict[str, list] = {}
        with open(answer_file) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                gt_map[obj["id"]] = obj.get("ground_truth", [])

        # Group test questions by scenario
        scenario_tests: dict[str, list] = defaultdict(list)
        with open(memory_file) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                qid = obj["id"]
                if qid not in gt_map:
                    continue
                scenario = obj.get("scenario", "")
                question_turns = obj.get("question", [[]])
                question_text = (
                    question_turns[-1][-1]["content"]
                    if question_turns and question_turns[-1]
                    else ""
                )
                scenario_tests[scenario].append({
                    "id": qid,
                    "question": question_text,
                    "ground_truth": gt_map[qid],
                })

        tasks: list[Task] = []
        for scenario, test_questions in scenario_tests.items():
            prereq_file = prereq_dir / f"memory_{scenario}.json"
            prereq_turns: list[list[dict]] = []
            if prereq_file.exists():
                with open(prereq_file) as pf:
                    for pl in pf:
                        pl = pl.strip()
                        if pl:
                            entry = json.loads(pl)
                            prereq_turns.extend(entry.get("question", []))

            task = Task(
                id=f"bfcl_memory_{scenario}",
                benchmark=self.name,
                system_prompt=SYSTEM_PROMPT,
                messages=[Message(role="user", content="")],
                ground_truth=[],
                metadata={
                    "category": "memory",
                    "file": memory_file.name,
                    "multi_turn": False,
                    "is_memory_scenario": True,
                    "scenario": scenario,
                    "prereq_turns": prereq_turns,
                    "test_questions": test_questions,
                },
            )
            tasks.append(task)

        limit = self.cfg.limit
        return tasks[:limit] if limit else tasks


