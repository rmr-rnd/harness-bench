"""NIAH (Needle in a Haystack) benchmark.

Faithful port of inspect_evals/niah. The dataset is generated once from the
OpenCompass NeedleBench corpus and frozen as JSON (see
benchmarks_data/niah/extract_data.py); this module only reads that artifact.

Fidelity contract (mirrors inspect_evals/tests/niah/):
  - prompts use the upstream MAIN_PROMPT / QUESTION_PROMPT wording;
  - each Sample carries `needle_question`, `target_context_length`,
    `target_position` in metadata, plus a `category` = grid cell so the
    orchestrator emits per-cell accuracy (the NIAH grid / heatmap);
  - the judge is model-graded QA with the upstream 1/3/5/7/10 rubric, sees the
    needle *question* as history, and the resulting Score carries `target_*`
    metadata so the grid can be aggregated.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

log = logging.getLogger(__name__)

from framework.benchmarks.base import Benchmark, register_benchmark
from framework.models import AgentTrace, Message, Sample, Score
from framework.scorers.base import Scorer

if TYPE_CHECKING:
    from framework.evaluators.llm_judge import LLMJudge
    from framework.sandbox import Sandbox

# Upstream prompting (inspect_evals/niah/utils/prompting.py).
MAIN_PROMPT = "Please read the context and then answer the question:"


def _question_prompt(context: str, question: str) -> str:
    """Mirror of inspect_evals' create_question_prompt()."""
    return (
        f"Context: {context}\n\n"
        f"Question: {question}\n\n"
        "Don't give information outside the context or repeat your findings."
    )


# Upstream model_graded_qa rubric (inspect_evals/niah/utils/scoring.py).
JUDGE_INSTRUCTIONS = """Evaluate the answer based on the following criteria:
Score 1: The answer is completely unrelated to the reference.
Score 3: The answer has minor relevance but does not align with the reference.
Score 5: The answer has moderate relevance but contains inaccuracies.
Score 7: The answer aligns with the reference but has minor omissions.
Score 10: The answer is completely accurate and aligns perfectly with the reference.
Only respond with a numerical score.

Question: {question}
Reference answer: {reference}
Candidate answer: {candidate}"""


def _niah_grade(sample: Sample, trace: AgentTrace, judge: "LLMJudge") -> Score:
    # The judge sees the needle *question* (upstream include_history contract),
    # the reference answer and the candidate answer.
    prompt = JUDGE_INSTRUCTIONS.format(
        question=sample.metadata.get("needle_question", ""),
        reference=sample.ground_truth,
        candidate=trace.final_output,
    )
    raw = judge._call(prompt)

    # Upstream grade_pattern = r"(\d+)" — take the first integer the judge emits.
    m = re.search(r"\d+", raw or "")
    if m is None:
        numeric = 1
        log.warning("NIAH judge returned no numeric score for %s: %r", sample.id, raw)
    else:
        numeric = max(1, min(10, int(m.group())))

    score = numeric / 10.0
    grade = "CORRECT" if numeric >= 7 else ("PARTIAL" if numeric >= 5 else "INCORRECT")
    return Score(
        sample_id=sample.id,
        score=score,
        grade=grade,
        explanation=f"judge_score={numeric}/10",
        judge_model=judge.cfg.model_name,
        judge_input=prompt[:500],
        judge_output=raw,
        # Carry grid coordinates so accuracy can be grouped by cell (upstream
        # subset_accuracy_combinations contract: Score.metadata holds target_*).
        metadata={
            "target_context_length": sample.metadata.get("target_context_length"),
            "target_position": sample.metadata.get("target_position"),
        },
    )


class _NIAHScorer(Scorer):
    async def __call__(
        self,
        sample: Sample,
        trace: AgentTrace,
        judge: "LLMJudge",
        sandbox: "Sandbox | None" = None,
    ) -> Score:
        return _niah_grade(sample, trace, judge)


@register_benchmark('niah')
class NIAHBenchmark(Benchmark):
    name = "niah"
    display_name  = "NIAH"
    description   = (
        "Needle In A Haystack — поиск в длинном контексте. Агент получает очень длинный документ "
        "со скрытым фактом ('иголкой') и должен его найти и воспроизвести. "
        "Проверяет способность работать с большим контекстом. "
        "Данные сгенерированы из корпуса OpenCompass NeedleBench (профиль Reduced: ~40 ячеек "
        "сетки длина×позиция, до 60k токенов) — это сабсет сетки оригинала (225), "
        "поэтому абсолютные числа несопоставимы с полным NIAH."
    )
    category      = "Память"
    default_paths = (
        "benchmarks_data/niah/data",
        "benchmarks_data/niah/answers",
    )

    def load_samples(self) -> list[Sample]:
        data_dir = Path(self.cfg.tasks_dir)
        answers_dir = Path(self.cfg.answers_dir) if self.cfg.answers_dir else data_dir.parent / "answers"
        samples: list[Sample] = []

        if not data_dir.exists() or not any(data_dir.glob("*.json")):
            log.warning(
                "NIAH data not found at %s. "
                "Generate it once with: python benchmarks_data/niah/extract_data.py "
                "(requires `pip install '.[niah-gen]'`).",
                data_dir,
            )
            return []

        gt_map: dict[str, str] = {}
        if answers_dir.exists():
            for f in answers_dir.glob("*.json"):
                obj = json.loads(f.read_text())
                gt_map[obj["id"]] = obj["answer"]

        for data_file in sorted(data_dir.glob("*.json")):
            obj = json.loads(data_file.read_text())
            sid = obj["id"]
            if sid not in gt_map:
                continue

            context = obj["context"]
            question = obj["question"]
            tcl = obj.get("target_context_length")
            tpos = obj.get("target_position")
            samples.append(Sample(
                id=f"niah_{sid}",
                benchmark=self.name,
                system_prompt=MAIN_PROMPT,
                messages=[Message(role="user", content=_question_prompt(context, question))],
                ground_truth=gt_map[sid],
                metadata={
                    "target_context_length": tcl,
                    "target_position": tpos,
                    "needle_question": question,
                    # Grid cell — drives per-cell accuracy in the run summary
                    # (orchestrator groups by metadata["category"]).
                    "category": f"ctx{tcl}_pos{tpos}",
                },
            ))

        limit = self.cfg.limit
        return samples[:limit] if limit else samples

    def make_scorer(self) -> Scorer:
        return _NIAHScorer()
