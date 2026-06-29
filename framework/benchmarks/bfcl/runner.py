"""BFCLRunner — manages BFCLMCPServer lifecycle + multi-turn loop for BFCL tasks."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from framework.runners.base import Runner

if TYPE_CHECKING:
    from framework.models import AgentTrace, Sample
    from framework.context import ExecutionContext
    from framework.runners.base import SendTurnFn


def _call_strings_to_dicts(calls: list[str]) -> list[dict]:
    """Convert ['func(a=1, b="x")'] → [{'func': {'a': 1, 'b': 'x'}}]."""
    import ast
    result = []
    for call in calls:
        try:
            tree = ast.parse(call, mode="eval")
            node = tree.body
            if not isinstance(node, ast.Call):
                continue
            parts: list[str] = []
            fn = node.func
            while isinstance(fn, ast.Attribute):
                parts.append(fn.attr)
                fn = fn.value
            if isinstance(fn, ast.Name):
                parts.append(fn.id)
            func_name = ".".join(reversed(parts))
            kwargs: dict = {}
            for kw in node.keywords:
                try:
                    kwargs[kw.arg] = ast.literal_eval(kw.value)
                except Exception:
                    if isinstance(kw.value, ast.Name):
                        kwargs[kw.arg] = kw.value.id
            result.append({func_name: kwargs})
        except Exception:
            continue
    return result


class BFCLRunner(Runner):
    """Manages BFCLMCPServer lifecycle and multi-turn loop for BFCL tasks.

    Works with all harnesses that implement send_turn().
    Starts BFCLMCPServer and signals the harness via ctx.extras["bfcl_mcp_port"].
    """

    def __init__(self, model_cfg=None) -> None:
        self.model_cfg = model_cfg

    async def run(
        self,
        task: "Sample",
        send_turn: "SendTurnFn",
        ctx: "ExecutionContext",
    ) -> "AgentTrace":
        return await self._run_mcp_harness(task, send_turn, ctx)

    # ------------------------------------------------------------------
    # MCP harnesses (hermes / openclaw / opencode)
    # ------------------------------------------------------------------

    async def _run_mcp_harness(
        self,
        task: "Sample",
        send_turn: "SendTurnFn",
        ctx: "ExecutionContext",
    ) -> "AgentTrace":
        from framework.mcp.bfcl_server import BFCLMCPServer
        from framework.models import AgentTrace, Step
        import time

        start = time.time()
        step_cb = ctx.step_cb

        is_multi_turn: bool = task.metadata.get("multi_turn", False)
        func_docs: list[dict] = task.metadata.get("functions", [])
        involved_classes: list[str] = task.metadata.get("involved_classes", [])
        initial_config: dict = task.metadata.get("initial_config", {})
        long_context: bool = task.metadata.get("long_context", False)
        question_turns: list = task.metadata.get("question_turns", [])
        missed_function: dict = task.metadata.get("missed_function", {})

        withheld: set[str] = set()
        for names in missed_function.values():
            withheld.update(names)

        steps: list[Step] = []
        all_outputs: list[str] = []
        total_input_tokens = 0
        total_output_tokens = 0
        per_turn_model_calls: list[list[str]] = []
        mcp_server: BFCLMCPServer | None = None

        def _step(stype: str, content: Any) -> None:
            steps.append(Step(type=stype, content=content))
            if step_cb:
                step_cb(stype, content)

        # Build llm_client for BFCLMCPServer (long-context uses LLM for summarisation)
        llm_client = None
        llm_model = ""
        if self.model_cfg is not None:
            from openai import AsyncOpenAI
            llm_client = AsyncOpenAI(
                base_url=self.model_cfg.base_url,
                api_key=self.model_cfg.api_key or "sk-none",
            )
            llm_model = self.model_cfg.model_name

        try:
            mcp_server = BFCLMCPServer(
                func_docs=func_docs,
                involved_classes=involved_classes,
                initial_config=initial_config,
                long_context=long_context,
                task_id=task.id,
                withheld=withheld,
                step_cb=_step,
                llm_client=llm_client,
                llm_model=llm_model,
            )
            mcp_port = await mcp_server.start()
            _step("status", f"BFCL MCP server started on port {mcp_port}")
            ctx.extras["bfcl_mcp_port"] = mcp_port

            system_prompt = task.system_prompt or (
                "You are an expert assistant. Use the provided tools to fulfill "
                "the user's requests. Call all necessary tools."
            )

            # Determine turns to run
            if is_multi_turn:
                turns_to_run = question_turns
            else:
                turns_to_run = [[{"role": "user", "content": task.messages[-1].content}]]

            # Run turns
            turn_timeout = ctx.timeout
            accumulated_messages: list[dict] = []

            for turn_idx, turn in enumerate(turns_to_run):
                revealed = missed_function.get(str(turn_idx), [])
                if revealed and mcp_server:
                    mcp_server.reveal_functions(revealed)

                if is_multi_turn and not turn:
                    if revealed:
                        user_msg = (
                            "New tools are now available. "
                            "Please retry your previous request using them."
                        )
                    else:
                        per_turn_model_calls.append([])
                        all_outputs.append("")
                        continue
                else:
                    user_msg = turn[-1]["content"] if turn else ""

                if not accumulated_messages:
                    accumulated_messages.append({"role": "system", "content": system_prompt})

                accumulated_messages.append({"role": "user", "content": user_msg})

                mcp_server.begin_turn(n_expected=0)

                response = await send_turn(
                    messages=accumulated_messages,
                    tools=[],
                    system_prompt=system_prompt,
                    ctx=ctx,
                    timeout=turn_timeout,
                )

                steps.extend(response.steps)
                final_output = response.text
                total_input_tokens += response.input_tokens
                total_output_tokens += response.output_tokens
                all_outputs.append(final_output)

                if final_output:
                    accumulated_messages.append({"role": "assistant", "content": final_output})

                turn_calls = mcp_server.end_turn()
                per_turn_model_calls.append(turn_calls)

        except Exception as exc:
            _step("output", "")
            trace = AgentTrace(
                task_id=task.id, final_output="", error=str(exc),
                steps=steps,
                duration_sec=round(time.time() - start, 2),
            )
            trace._bfcl_tool_calls = []  # type: ignore[attr-defined]
            trace._bfcl_per_turn_calls = []  # type: ignore[attr-defined]
            return trace
        else:
            final_output = all_outputs[-1] if all_outputs else ""
            trace = AgentTrace(
                task_id=task.id,
                final_output=final_output,
                steps=steps,
                input_tokens=total_input_tokens,
                output_tokens=total_output_tokens,
                duration_sec=round(time.time() - start, 2),
            )
            if is_multi_turn:
                trace._bfcl_per_turn_calls = per_turn_model_calls  # type: ignore[attr-defined]
            else:
                calls = per_turn_model_calls[0] if per_turn_model_calls else []
                trace._bfcl_tool_calls = _call_strings_to_dicts(calls)  # type: ignore[attr-defined]
            return trace
        finally:
            if mcp_server:
                await mcp_server.stop()
            # Run any cleanup_fns registered by send_turn (e.g. stop container)
            for fn in list(ctx.cleanup_fns):
                try:
                    await fn()
                except Exception:
                    pass
            ctx.cleanup_fns.clear()
