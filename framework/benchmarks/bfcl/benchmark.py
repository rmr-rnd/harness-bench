"""BFCL (Berkeley Function Calling Leaderboard) benchmark adapter.

BFCL uses plaintext function-call format, NOT OpenAI tool_calls API.
Function definitions are injected into the user prompt as JSON.
Model must respond with: [func_name(param=value, ...)]

Multi-turn categories use a simulated execution sandbox (GorillaFileSystem,
MathAPI, TwitterAPI, etc.) — see _shared/multi_turn.py.
"""
from __future__ import annotations

import json
from collections import defaultdict
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

from framework.benchmarks.base import Benchmark, register_benchmark
from framework.benchmarks.bfcl._shared.ast_checker import check as ast_check
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

EXCLUDED_CATEGORIES = {
    "live_simple",
    "live_multiple",
    "live_parallel",
    "live_parallel_multiple",
    "live_irrelevance",
    "live_relevance",
    "irrelevance",
    "simple",
    "multiple",
    "parallel",
    "parallel_multiple",
    "simple_python",
    "simple_java",
    "simple_javascript",
    # Hermes caches the tool list at container startup and has no API to reload it,
    # so composite and miss_func tasks (which require revealing tools mid-conversation)
    # will always fail — exclude them until a fix is available.
    # miss_param is NOT excluded: tools are fixed from the start, only certain functions
    # are forbidden (excluded_function), which the agent must work around.
    "composite",
    "miss_func",
}

MULTI_TURN_FUNC_DOC_FILE_MAPPING = {
    "GorillaFileSystem": "gorilla_file_system.json",
    "MathAPI": "math_api.json",
    "MessageAPI": "message_api.json",
    "TwitterAPI": "posting_api.json",
    "TicketAPI": "ticket_api.json",
    "TradingBot": "trading_bot.json",
    "TravelAPI": "travel_booking.json",
    "VehicleControlAPI": "vehicle_control.json",
}


def _is_excluded(filename: str) -> bool:
    return any(cat in filename.lower() for cat in EXCLUDED_CATEGORIES)


def _funcs_to_prompt_text(functions: list[dict]) -> str:
    """Embed function definitions as JSON in the user message (BFCL classic style)."""
    return json.dumps(functions, ensure_ascii=False, indent=2)


@lru_cache(maxsize=None)
def _load_func_docs_for_class(func_doc_dir: Path, class_name: str) -> tuple[dict, ...]:
    """Load all function docs for a backend class from JSONL file."""
    filename = MULTI_TURN_FUNC_DOC_FILE_MAPPING.get(class_name)
    if not filename:
        return ()
    path = func_doc_dir / filename
    if not path.exists():
        return ()
    docs = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            docs.append(json.loads(line))
    return tuple(docs)


def _load_multi_turn_func_docs(
    func_doc_dir: Path,
    involved_classes: list[str],
) -> list[dict]:
    """Load all function docs for the involved classes of a multi-turn task.

    The `path` field in BFCL is metadata about which functions the model SHOULD
    call (used for evaluation), not a filter on which tools the model can SEE.
    The model gets access to ALL functions of the involved classes.
    """
    docs = []
    for class_name in involved_classes:
        docs.extend(_load_func_docs_for_class(func_doc_dir, class_name))
    return docs


class _BFCLScorer(Scorer):
    async def __call__(
        self,
        sample: Sample,
        trace: AgentTrace,
        judge: "LLMJudge",
        sandbox: "Sandbox | None" = None,
    ) -> Score:
        task = sample
        cat = task.metadata.get("category", "")
        is_multi_turn = task.metadata.get("multi_turn", False)
        gt = task.ground_truth
        output = trace.final_output or ""

        # Irrelevance: model should NOT make a function call
        if "irrelevance" in cat:
            tool_calls = getattr(trace, "_bfcl_tool_calls", None)
            if tool_calls is not None:
                made_call = len(tool_calls) > 0
            else:
                made_call = output.strip().startswith("[") and "(" in output
            correct = not made_call
            return Score(
                sample_id=task.id,
                score=1.0 if correct else 0.0,
                grade="CORRECT" if correct else "INCORRECT",
                explanation="Correctly abstained" if correct else "Incorrectly produced a function call",
            )

        # Relevance: model SHOULD make a function call
        if "live_relevance" in cat:
            tool_calls = getattr(trace, "_bfcl_tool_calls", None)
            if tool_calls is not None:
                made_call = len(tool_calls) > 0
            else:
                made_call = output.strip().startswith("[") and "(" in output
            correct = made_call
            return Score(
                sample_id=task.id,
                score=1.0 if correct else 0.0,
                grade="CORRECT" if correct else "INCORRECT",
                explanation="Correctly produced a function call" if correct else "Failed to produce a function call",
            )

        # Multi-turn: per-turn results stored in trace metadata by orchestrator
        if is_multi_turn:
            per_turn_calls = getattr(trace, "_bfcl_per_turn_calls", None)
            if per_turn_calls is not None:
                from framework.benchmarks.bfcl._shared.multi_turn import multi_turn_check
                correct, explanation = multi_turn_check(
                    per_turn_model_calls=per_turn_calls,
                    per_turn_gt_calls=gt,
                    initial_config=task.metadata.get("initial_config", {}),
                    involved_classes=task.metadata.get("involved_classes", []),
                    task_id=task.id,
                    long_context=task.metadata.get("long_context", False),
                )
                if not correct and judge is not None:
                    from framework.benchmarks.bfcl._shared.judge import grade_multi_turn
                    question_turns = task.metadata.get("question_turns", [])
                    task_description = " | ".join(
                        turn[-1].get("content", "") for turn in question_turns if turn
                    )
                    judge_passed, judge_raw = grade_multi_turn(
                        judge,
                        task_description=task_description,
                        ground_truth=gt,
                        agent_calls=per_turn_calls,
                    )
                    if judge_passed:
                        return Score(
                            sample_id=task.id,
                            score=1.0,
                            grade="CORRECT",
                            explanation=f"[multi_turn][llm_judge] AST failed ({explanation}), but LLM judge: equivalent",
                            judge_output=judge_raw,
                        )
                    return Score(
                        sample_id=task.id,
                        score=0.0,
                        grade="INCORRECT",
                        explanation=f"[multi_turn][llm_judge] AST failed ({explanation}), LLM judge: not equivalent",
                        judge_output=judge_raw,
                    )
                return Score(
                    sample_id=task.id,
                    score=1.0 if correct else 0.0,
                    grade="CORRECT" if correct else "INCORRECT",
                    explanation=f"[multi_turn] {explanation}",
                )
            return Score(
                sample_id=task.id,
                score=0.0,
                grade="INCORRECT",
                explanation="[multi_turn] no execution results available",
            )

        # Single-turn via tools= API
        tool_calls = getattr(trace, "_bfcl_tool_calls", None)
        if tool_calls is None:
            # Legacy fallback (e.g. task has no functions defined)
            correct, explanation = ast_check(output, gt)
            return Score(
                sample_id=task.id,
                score=1.0 if correct else 0.0,
                grade="CORRECT" if correct else "INCORRECT",
                explanation=explanation,
            )

        from framework.benchmarks.bfcl._shared.ast_checker import _call_matches
        if not tool_calls:
            return Score(
                sample_id=task.id,
                score=0.0,
                grade="INCORRECT",
                explanation="Model made no function calls",
            )

        for acceptable in gt:
            if len(tool_calls) != len(acceptable):
                continue
            if all(_call_matches(p, g) for p, g in zip(tool_calls, acceptable)):
                return Score(
                    sample_id=task.id, score=1.0, grade="CORRECT",
                    explanation="Matches ground truth",
                )
        return Score(
            sample_id=task.id,
            score=0.0,
            grade="INCORRECT",
            explanation=f"No match. Tool calls: {tool_calls}",
        )


@register_benchmark('bfcl')
class BFCLBenchmark(Benchmark):
    name = "bfcl"
    display_name  = "BFCL"
    description   = (
        "Berkeley Function Calling Leaderboard — multi-turn. Агент получает описание набора API-функций "
        "и многоходовой диалог с пользователем. На каждом ходу должен вызвать правильную функцию с правильными параметрами. "
        "Оценка: AST-сравнение вызовов с эталоном. Категории: base (стандартные сценарии), "
        "long_context (длинный контекст), miss_param (часть параметров отсутствует в подсказке)."
    )
    category      = "Tool Calling"
    default_paths = (
        "benchmarks_data/bfcl/questions",
        "benchmarks_data/bfcl/answers",
    )

    def make_scorer(self) -> Scorer:
        return _BFCLScorer()

    def make_runner(self, model_cfg=None):
        from framework.benchmarks.bfcl.runner import BFCLRunner
        return BFCLRunner(model_cfg=model_cfg)

    def load_samples(self) -> list[Sample]:
        tasks_dir = Path(self.cfg.tasks_dir)
        answers_dir = Path(self.cfg.answers_dir)
        func_doc_dir = tasks_dir.parent / "multi_turn_func_doc"
        tasks: list[Task] = []

        allowed_cats = set(self.cfg.categories) if self.cfg.categories else None

        for q_file in sorted(tasks_dir.glob("*.json")):
            if _is_excluded(q_file.name):
                continue

            cat = q_file.stem.replace("BFCL_v4_", "").replace("BFCL_v3_", "")
            if allowed_cats and cat not in allowed_cats:
                continue

            a_file = answers_dir / q_file.name
            if not a_file.exists():
                continue

            if "memory" in cat and "multi_turn" not in cat:
                continue  # memory tasks handled by BFCLMemoryBenchmark
            is_multi_turn = "multi_turn" in cat

            gt_map: dict[str, list] = {}
            with open(a_file) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    obj = json.loads(line)
                    gt = obj.get("ground_truth", [])
                    # Normalize single-turn: ground_truth is either list[dict] or list[list[dict]]
                    if gt and not isinstance(gt[0], list):
                        gt = [gt]
                    gt_map[obj["id"]] = gt

            if is_multi_turn:
                with open(q_file) as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        obj = json.loads(line)
                        qid = obj["id"]
                        if qid not in gt_map:
                            continue

                        question_turns = obj.get("question", [[]])
                        involved_classes = obj.get("involved_classes", [])
                        func_docs = _load_multi_turn_func_docs(func_doc_dir, involved_classes)

                        first_turn_content = ""
                        for turn in question_turns:
                            if turn:
                                first_turn_content = turn[-1].get("content", "")
                                break

                        messages = [Message(role="user", content=first_turn_content)]
                        gt = gt_map[qid]

                        task = Task(
                            id=f"bfcl_{qid}",
                            benchmark=self.name,
                            system_prompt=SYSTEM_PROMPT,
                            messages=messages,
                            ground_truth=gt,
                            metadata={
                                "category": cat,
                                "file": q_file.name,
                                "multi_turn": True,
                                "question_turns": question_turns,
                                "functions": func_docs,
                                "initial_config": obj.get("initial_config", {}),
                                "involved_classes": involved_classes,
                                "missed_function": obj.get("missed_function", {}),
                                "long_context": "long_context" in cat or "composite" in cat,
                            },
                        )
                        tasks.append(task)

            else:
                with open(q_file) as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        obj = json.loads(line)
                        qid = obj["id"]
                        if qid not in gt_map:
                            continue

                        question_turns = obj.get("question", [[]])
                        functions = obj.get("function", [])
                        last_turn = question_turns[-1] if question_turns else []
                        user_content = last_turn[-1]["content"] if last_turn else ""
                        messages = [Message(role="user", content=user_content)]
                        gt = gt_map[qid]

                        task = Task(
                            id=f"bfcl_{qid}",
                            benchmark=self.name,
                            system_prompt=SYSTEM_PROMPT,
                            messages=messages,
                            ground_truth=gt,
                            metadata={
                                "category": cat,
                                "file": q_file.name,
                                "multi_turn": False,
                                "functions": functions,
                            },
                        )
                        tasks.append(task)

        limit = self.cfg.limit
        return tasks[:limit] if limit else tasks


