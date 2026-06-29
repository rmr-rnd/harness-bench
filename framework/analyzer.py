"""AI-powered benchmark analysis via OpenAI-compatible API."""
from __future__ import annotations

import asyncio
import json
from typing import Awaitable, Callable

from pydantic import BaseModel


class RunAnalysis(BaseModel):
    label: str
    strengths: str
    weaknesses: str
    characteristic_error: str


class BenchmarkAnalysis(BaseModel):
    runs: list[RunAnalysis]
    comparison_a_advantage: str | None = None
    comparison_b_advantage: str | None = None
    conclusion: str

def _get_display(name: str) -> str:
    from framework.benchmarks.base import _BENCHMARK_REGISTRY
    cls = _BENCHMARK_REGISTRY.get(name)
    return cls.display_name if cls and cls.display_name else name


def _get_description(name: str) -> str:
    from framework.benchmarks.base import _BENCHMARK_REGISTRY
    cls = _BENCHMARK_REGISTRY.get(name)
    return cls.description if cls else ""

BAD_GRADES  = {"INCORRECT", "FAIL", "NOT_ATTEMPTED", "ERROR", "AGENT_DEAD"}
GOOD_GRADES = {"CORRECT", "PASS"}

ProgressCb = Callable[[str], Awaitable[None]] | None


# ──────────────────────────────────────────────────────────────────────────────
# Sample collection
# ──────────────────────────────────────────────────────────────────────────────

def _format_steps(steps: list, limits: dict) -> str:
    lines = []
    first_input_seen = False

    for s in steps:
        t = s.get("type", "")
        content = s.get("content", "")

        if t == "status":
            continue

        if t == "input":
            if first_input_seen:
                continue
            first_input_seen = True
            lim = limits["task"]
            if isinstance(content, list):
                for msg in content:
                    if isinstance(msg, dict) and msg.get("role") == "user":
                        lines.append(f"[TASK] {str(msg.get('content', ''))[:lim]}")
            else:
                lines.append(f"[TASK] {str(content)[:lim]}")

        elif t == "thinking":
            text = str(content).strip()
            if text:
                lines.append(f"[THINKING] {text[:limits['thinking']]}")

        elif t == "tool_call":
            lines.append(f"[TOOL_CALL] {str(content).strip()[:limits['tool_call']]}")

        elif t == "tool_result":
            text = str(content).strip()
            lim = limits["tool_result"]
            suffix = f"… [+{len(text)-lim} chars]" if len(text) > lim else ""
            lines.append(f"[TOOL_RESULT] {text[:lim]}{suffix}")

        elif t == "output":
            text = str(content).strip()
            if text:
                lines.append(f"[OUTPUT] {text[:limits['output']]}")

    return "\n".join(lines)


async def _collect_samples(bench: str, run_id: str, db, cfg) -> dict:
    """Return wrong/correct sample lists with full agent steps from DB."""
    if not (db and db._enabled):
        return {"bad": [], "good": []}

    limits = {
        "task":        cfg.limit_task,
        "thinking":    cfg.limit_thinking,
        "tool_call":   cfg.limit_tool_call,
        "tool_result": cfg.limit_tool_result,
        "output":      cfg.limit_output,
    }
    max_bad  = cfg.max_bad_samples
    max_good = cfg.max_good_samples

    all_tasks = await db.fetch_run_tasks(run_id)
    tasks = all_tasks.get(bench, [])

    bad, good = [], []
    for d in tasks:
        if len(bad) >= max_bad and len(good) >= max_good:
            break

        grade = d.get("grade", "")
        task_id = d.get("task_id", "")

        sample = {
            "task_id": task_id,
            "grade":   grade,
            "score":   d.get("score", 0),
            "expl":    d.get("explanation", "")[:300],
            "steps_text": "",
        }

        ji = d.get("judge_output", "")
        if ji and len(ji) > 20:
            sample["judge_context"] = ji[:500]

        try:
            steps = await db.fetch_task_steps(run_id, task_id)
            if steps:
                sample["steps_text"] = _format_steps(steps, limits)
                sample["n_steps"] = len([s for s in steps if s.get("type") not in ("status",)])
            trace = await db.fetch_task_output(run_id, task_id)
            if trace:
                sample["final_output"] = str(trace.get("final_output", ""))[:300]
        except Exception:
            pass

        if grade in BAD_GRADES and len(bad) < max_bad:
            bad.append(sample)
        elif grade in GOOD_GRADES and len(good) < max_good:
            good.append(sample)

    return {"bad": bad, "good": good}


# ──────────────────────────────────────────────────────────────────────────────
# Prompt builder
# ──────────────────────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
Ты — аналитик AI-бенчмарков. Анализируешь результаты прогонов агентов.
Правила:
- Только русский язык
- Конкретные факты из данных трейсов, никаких общих фраз
- Каждый пункт — 1-3 предложения
- Отвечай строго в формате JSON согласно предоставленной JSON-схеме\
"""


def _build_messages(bench: str, runs_data: list[dict]) -> tuple[str, str]:
    """Return (system_prompt, user_message) for the analysis request."""
    display = _get_display(bench)
    description = _get_description(bench)

    # ── User message: data ────────────────────────────────────────────────────
    lines = [
        f"Бенчмарк: {display}",
        f"Описание: {description}",
        "",
        "РЕЗУЛЬТАТЫ МОДЕЛЕЙ:",
        "=" * 60,
    ]

    for rd in runs_data:
        summary = rd["summary"]
        bdata   = summary.get("benchmarks", {}).get(bench, {})
        samples = rd["samples"]
        label   = f"{summary.get('model','?')} [{summary.get('harness','?')}]"
        acc     = bdata.get("accuracy", 0)
        grades  = bdata.get("grades", {})
        cats    = bdata.get("categories", {})
        total   = bdata.get("total", 0)

        lines += [
            "",
            f"Модель: {label}",
            f"Итог: {acc*100:.1f}% из {total} задач | {grades}",
        ]

        if cats:
            lines.append("Категории (точность):")
            for cat, cv in sorted(cats.items(), key=lambda x: x[1].get("accuracy", 0)):
                lines.append(f"  {cat}: {cv.get('accuracy',0)*100:.0f}% (n={cv.get('n',0)})")

        if samples["bad"]:
            n_total_bad = sum(1 for g in grades if g in BAD_GRADES for _ in range(grades[g]))
            lines.append(f"\nПримеры неудачных задач ({len(samples['bad'])} из {n_total_bad}):")
            for s in samples["bad"]:
                lines += ["", f"  --- [{s['task_id']}] grade={s['grade']} score={s['score']} ---"]
                lines.append(f"  Evaluator: {s['expl']}")
                if s.get("judge_context"):
                    lines.append(f"  Judge context: {s['judge_context']}")
                if s.get("steps_text"):
                    lines.append(f"  Agent trace ({s.get('n_steps', '?')} steps):")
                    for step_line in s["steps_text"].splitlines():
                        lines.append(f"    {step_line}")
                if s.get("final_output"):
                    lines.append(f"  Final output: {s['final_output']}")

        if samples["good"]:
            lines.append(f"\nПримеры успешных задач ({len(samples['good'])}):")
            for s in samples["good"]:
                lines += ["", f"  --- [{s['task_id']}] grade={s['grade']} score={s['score']} ---"]
                lines.append(f"  Evaluator: {s['expl']}")
                if s.get("steps_text"):
                    lines.append(f"  Agent trace ({s.get('n_steps', '?')} steps):")
                    for step_line in s["steps_text"].splitlines():
                        lines.append(f"    {step_line}")

        lines.append("\n" + "-" * 60)

    # ── JSON schema instruction ───────────────────────────────────────────────
    schema = BenchmarkAnalysis.model_json_schema()
    model_names = [rd["summary"].get("model", "?") for rd in runs_data]
    labels = [f"{rd['summary'].get('model','?')} [{rd['summary'].get('harness','?')}]" for rd in runs_data]

    lines += [
        "",
        "Верни JSON строго по схеме:",
        json.dumps(schema, ensure_ascii=False, indent=2),
        "",
        "Заполни поле runs для каждой модели:",
    ]
    for lbl in labels:
        lines.append(f"  - label={lbl!r}")

    if len(runs_data) > 1:
        lines += [
            f"Заполни comparison_a_advantage (преимущество {model_names[0]}),",
            f"comparison_b_advantage (преимущество {model_names[1] if len(model_names) > 1 else 'другой модели'}),",
            "conclusion (итог: какую выбрать и почему).",
        ]
    else:
        lines += [
            "comparison_a_advantage и comparison_b_advantage оставь null.",
            "conclusion — ключевые выводы по модели.",
        ]

    return _SYSTEM_PROMPT, "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────────
# OpenAI-compatible API call
# ──────────────────────────────────────────────────────────────────────────────

async def _call_llm(
    system: str,
    user: str,
    base_url: str,
    api_key: str,
    model_name: str,
    temperature: float = 0.3,
    timeout: int = 120,
) -> BenchmarkAnalysis | str:
    """Call OpenAI-compatible API with structured output. Returns BenchmarkAnalysis or error str."""
    try:
        from openai import AsyncOpenAI
    except ImportError:
        return "[Analysis unavailable — install openai: pip install openai]"

    if not api_key:
        return "[Analysis unavailable — API key not configured. Set it in ⚙ Analysis Model settings.]"

    client = AsyncOpenAI(base_url=base_url, api_key=api_key)
    messages = [
        {"role": "system", "content": system},
        {"role": "user",   "content": user},
    ]
    try:
        response = await asyncio.wait_for(
            client.chat.completions.create(
                model=model_name,
                messages=messages,
                temperature=temperature,
                max_tokens=2048,
                response_format={"type": "json_object"},
            ),
            timeout=timeout,
        )
        content = response.choices[0].message.content or "{}"
        return BenchmarkAnalysis.model_validate_json(content)
    except asyncio.TimeoutError:
        return "[Analysis timed out]"
    except Exception as e:
        return f"[Analysis error: {e}]"


# ──────────────────────────────────────────────────────────────────────────────
# Public entry point
# ──────────────────────────────────────────────────────────────────────────────

async def analyze_runs(
    summaries: list[dict],
    db=None,
    progress_cb: ProgressCb = None,
    analysis_model_cfg=None,
) -> dict[str, BenchmarkAnalysis | str]:
    """Analyze all benchmarks present in the given runs.

    analysis_model_cfg: AnalysisModelConfig instance (from config or web UI settings).
    Returns {bench_name: BenchmarkAnalysis} or {bench_name: error_str}.
    """
    from framework.config import AnalysisModelConfig
    cfg: AnalysisModelConfig = analysis_model_cfg or AnalysisModelConfig()

    all_benches: list[str] = []
    seen: set[str] = set()
    for s in summaries:
        for b in s.get("benchmarks", {}):
            if b not in seen:
                all_benches.append(b)
                seen.add(b)

    results: dict[str, BenchmarkAnalysis | str] = {}
    total = len(all_benches)

    for i, bench in enumerate(all_benches, 1):
        display = _get_display(bench)
        if progress_cb:
            await progress_cb(f"Analyzing {display} ({i}/{total})…")

        runs_data = []
        for s in summaries:
            if bench not in s.get("benchmarks", {}):
                continue
            run_id  = s.get("run_id", "")
            samples = await _collect_samples(bench, run_id, db, cfg)
            runs_data.append({"summary": s, "samples": samples})

        if not runs_data:
            continue

        system, user = _build_messages(bench, runs_data)
        results[bench] = await _call_llm(
            system, user,
            base_url=cfg.base_url,
            api_key=cfg.api_key,
            model_name=cfg.model_name,
            temperature=cfg.temperature,
        )

    if progress_cb:
        await progress_cb("Analysis complete.")

    return results
