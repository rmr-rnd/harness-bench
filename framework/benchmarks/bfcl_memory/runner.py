"""BFCLMemoryRunner — two-session memory protocol for BFCL memory benchmark."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from framework.harnesses.base import AgentDeadError
from framework.runners.base import Runner

if TYPE_CHECKING:
    from framework.models import AgentTrace, Sample
    from framework.context import ExecutionContext
    from framework.runners.base import SendTurnFn


class BFCLMemoryRunner(Runner):
    """Manages the two-session memory protocol (prereq writes + eval questions).

    Session 1 (prereq_turns): fresh /v1/responses calls per turn (no continuation);
    each call has store=True so the agent's built-in memory accumulates.
    Session 2 (test_questions): similar fresh calls with eval_system_prompt.
    The harness container is started lazily on the first send_turn call.
    """

    async def run(
        self,
        task: "Sample",
        send_turn: "SendTurnFn",
        ctx: "ExecutionContext",
    ) -> "AgentTrace":
        from framework.benchmarks.bfcl._shared.agentic_checker import agentic_checker
        from framework.benchmarks.bfcl_memory.prompts import (
            MEMORY_AGENT_SETTINGS,
            ADDITIONAL_SYSTEM_PROMPT_FOR_AGENTIC_RESPONSE_FORMAT,
            WRITE_SYSTEM_PROMPT,
            EVAL_SYSTEM_PROMPT,
        )
        from framework.models import AgentTrace, Step
        import time

        scenario: str = task.metadata["scenario"]
        prereq_turns: list = task.metadata.get("prereq_turns", [])
        test_questions: list = task.metadata.get("test_questions", [])
        start = time.time()
        step_cb = ctx.step_cb

        steps: list[Step] = []
        total_input_tokens = 0
        total_output_tokens = 0

        def _step(stype: str, content: Any) -> None:
            steps.append(Step(type=stype, content=content))
            if step_cb:
                step_cb(stype, content)

        scenario_setting = MEMORY_AGENT_SETTINGS.get(scenario, "")
        write_system_prompt = WRITE_SYSTEM_PROMPT.format(
            scenario_setting=scenario_setting,
        )
        eval_system_prompt = EVAL_SYSTEM_PROMPT.format(
            scenario_setting=scenario_setting,
            answer_format=ADDITIONAL_SYSTEM_PROMPT_FOR_AGENTIC_RESPONSE_FORMAT,
        )

        prereq_turn_timeout = 60
        eval_turn_timeout = ctx.timeout

        try:
            # Session 1: feed prereq turns — each is a fresh session (no previous_response_id)
            for turn_idx, turn in enumerate(prereq_turns):
                user_msg = turn[-1]["content"] if turn else ""
                if not user_msg:
                    continue
                _step("input", [
                    {"role": "system", "content": write_system_prompt},
                    {"role": "user", "content": user_msg},
                ])
                # Pass continue_session=False so the harness does NOT chain previous_response_id
                try:
                    response = await send_turn(
                        messages=[
                            {"role": "system", "content": write_system_prompt},
                            {"role": "user", "content": user_msg},
                        ],
                        tools=[],
                        system_prompt=write_system_prompt,
                        ctx=ctx,
                        timeout=prereq_turn_timeout,
                        continue_session=False,
                    )
                    total_input_tokens += response.input_tokens
                    total_output_tokens += response.output_tokens
                    for s in response.steps:
                        steps.append(s)
                except AgentDeadError:
                    # The agent is dead, not just bad at this prereq turn. Don't skip
                    # and keep re-hitting it (×N prereq turns); let it propagate so the
                    # outer handler returns a trace the orchestrator grades AGENT_DEAD.
                    raise
                except Exception as e:
                    _step("status", f"prereq turn {turn_idx} error (skipped): {e}")

            _step("status", "prereq session complete")

            # Session 2: test questions — also fresh calls (no chained session)
            per_question_results = []
            for tq in test_questions:
                question = tq["question"]
                gt = tq["ground_truth"]

                _step("input", [
                    {"role": "system", "content": eval_system_prompt},
                    {"role": "user", "content": question},
                ])
                response = await send_turn(
                    messages=[
                        {"role": "system", "content": eval_system_prompt},
                        {"role": "user", "content": question},
                    ],
                    tools=[],
                    system_prompt=eval_system_prompt,
                    ctx=ctx,
                    timeout=eval_turn_timeout,
                    continue_session=False,
                )
                answer = response.text
                total_input_tokens += response.input_tokens
                total_output_tokens += response.output_tokens
                for s in response.steps:
                    steps.append(s)

                check = agentic_checker(answer, [str(a) for a in gt])
                per_question_results.append({
                    "question": question,
                    "ground_truth": gt,
                    "answer": answer,
                    "correct": check["valid"],
                })

        except Exception as exc:
            trace = AgentTrace(
                task_id=task.id, final_output="", error=str(exc),
                steps=steps,
                input_tokens=total_input_tokens,
                output_tokens=total_output_tokens,
                duration_sec=round(time.time() - start, 2),
            )
            trace._memory_per_question = []  # type: ignore[attr-defined]
            return trace
        else:
            n_correct = sum(1 for r in per_question_results if r["correct"])
            answers_text = "\n".join(
                f"Q{i+1}: {r['question']}\nA: {r['answer']}\nGT: {r['ground_truth']}\n"
                f"{'✓' if r['correct'] else '✗'}"
                for i, r in enumerate(per_question_results)
            )
            trace = AgentTrace(
                task_id=task.id,
                final_output=f"{n_correct}/{len(per_question_results)} correct\n\n{answers_text}",
                steps=steps,
                input_tokens=total_input_tokens,
                output_tokens=total_output_tokens,
                duration_sec=round(time.time() - start, 2),
            )
            trace._memory_per_question = per_question_results  # type: ignore[attr-defined]
            return trace
        finally:
            # Run any cleanup_fns registered by send_turn
            for fn in list(ctx.cleanup_fns):
                try:
                    await fn()
                except Exception:
                    pass
            ctx.cleanup_fns.clear()
