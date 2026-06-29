"""Orchestrator: loads tasks, runs harness, evaluates, saves results."""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Callable

logger = logging.getLogger(__name__)

import yaml

from framework.config import RunConfig
from framework.context import ExecutionContext
from framework.db import Database
from framework.evaluators.llm_judge import LLMJudge
from framework.utils.network import get_mcp_host
from framework.utils.docker import docker_status, DockerUnavailableError
from framework.harnesses import load_harness_class
from framework.models import AgentTrace, EvalResult, Task


def _make_run_id() -> str:
    return f"run_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"


def _resolve_benchmark(cfg_name: str):
    from framework.benchmarks._discovery import discover_all
    from framework.benchmarks.base import resolve_benchmark
    discover_all()
    return resolve_benchmark(cfg_name)


class Orchestrator:
    def __init__(self, cfg: RunConfig, progress_cb: Callable | None = None) -> None:
        self.cfg = cfg
        self.run_id = cfg.run_id or _make_run_id()
        self.progress_cb = progress_cb or (lambda **kw: None)
        self._stop_event = asyncio.Event()
        self._active_sandboxes: list = []
        self._running_tasks: list[asyncio.Task] = []

        judge_cfg = cfg.judge_model or cfg.model
        self.judge = LLMJudge(judge_cfg)

        harness_cls = load_harness_class(cfg.harness.type)
        raw = {**(cfg.harness.model_extra or {}), "tavily_api_key": cfg.search.tavily_api_key}
        self.harness = harness_cls.from_config(cfg.model, raw)

        # Auto-detect per-benchmark harnesses by convention: if "{benchmark}_{harness_type}"
        # exists as a harness file, use it for that benchmark. Explicit benchmark_harness
        # in config takes precedence over auto-detection.
        self._benchmark_harnesses: dict[str, "Harness"] = {}
        htype = cfg.harness.type
        for bench_cfg in cfg.benchmarks:
            bname = bench_cfg.name
            variant = f"{bname}_{htype}"
            explicit = cfg.harness.benchmark_harness.get(bname)
            target = explicit or variant
            try:
                override_cls = load_harness_class(target)
                self._benchmark_harnesses[bname] = override_cls.from_config(cfg.model, raw)
            except ValueError:
                pass  # no variant exists, use default harness

        self.db = Database(cfg.database)

    def stop(self) -> None:
        self._stop_event.set()
        for t in list(self._running_tasks):
            t.cancel()
        for sandbox in list(self._active_sandboxes):
            asyncio.create_task(sandbox.stop())

    def _get_harness(self, benchmark) -> "Harness":
        """Return the harness for this benchmark, respecting per-benchmark overrides."""
        return self._benchmark_harnesses.get(benchmark.name, self.harness)

    async def _apply_run_grades(
        self,
        harness: "Harness",
        benchmark_name: str,
        results_pairs: list,
    ) -> None:
        """If the harness exposes run-level scores (e.g. BitGN PAC1), rewrite
        the per-task placeholders both in memory and in the DB."""
        task_ids: list[str] = []
        for pair in results_pairs:
            if isinstance(pair, Exception):
                continue
            _trace, er, _cat = pair
            task_ids.append(er.sample_id)
        if not task_ids:
            return

        try:
            score_map = await harness.await_run_grades(task_ids)
        except Exception as e:
            logger.warning("await_run_grades() failed: %s", e)
            return
        if not score_map:
            return

        regrade = None
        if benchmark_name == "pac1":
            from framework.benchmarks.pac1.benchmark import _outcome_to_grade
            regrade = _outcome_to_grade

        for pair in results_pairs:
            if isinstance(pair, Exception):
                continue
            _trace, er, cat = pair
            if er.sample_id not in score_map:
                continue
            new_score = score_map[er.sample_id]
            er.score = new_score
            if regrade:
                er.grade = regrade(er.explanation or "", new_score)
            await self.db.save_eval_result(self.run_id, benchmark_name, er)
            # Re-emit per-task event so the UI badge flips from EVALUATING
            # to the real grade.
            self.progress_cb(
                event="done",
                task_id=er.sample_id,
                benchmark=benchmark_name,
                grade=er.grade,
                score=er.score,
                ground_truth="",
                explanation=er.explanation or "",
                judge_model=er.judge_model or "",
                judge_output=er.judge_output or "",
                category=cat or "",
            )

    async def _build_ctx(
        self, task: Task, benchmark, base_ctx: ExecutionContext
    ) -> ExecutionContext:
        """Populate ctx with infrastructure declared by the sample.

        Routing is purely via sample.sandbox — no benchmark name checks, no isinstance.
        """
        if task.sandbox is not None:
            return await self._setup_sandbox_ctx(task, base_ctx)
        return base_ctx

    async def _setup_sandbox_ctx(
        self, task: Task, base_ctx: ExecutionContext
    ) -> ExecutionContext:
        from framework.mcp.http_server import MCPHttpServer
        from framework.sandbox import make_sandbox

        spec = task.sandbox
        base_ctx.step_cb and base_ctx.step_cb("status", f"Starting sandbox ({spec.type})…")
        sandbox = make_sandbox(spec)
        self._active_sandboxes.append(sandbox)
        await sandbox.start()

        tools = task.sandbox_tools
        if tools:
            base_ctx.step_cb and base_ctx.step_cb("status", f"Injecting {len(tools)} custom tool(s)…")
            await sandbox.inject_tools(tools)

        container_id = getattr(sandbox, "container_id", None) or getattr(sandbox, "_container_id", None) or ""
        mcp_server = MCPHttpServer(
            container_id=container_id,
            tool_groups=task.mcp_tool_groups or [],
            step_cb=base_ctx.step_cb,
            sandbox=sandbox,
            custom_tools=tools,
        )
        mcp_port = await mcp_server.start()
        tool_names = getattr(mcp_server, "_tool_names", [])
        base_ctx.step_cb and base_ctx.step_cb("status", f"MCP bridge on port {mcp_port}, tools: {tool_names}, starting agent…")

        base_ctx.sandbox = sandbox
        base_ctx.mcp_url = f"http://{get_mcp_host()}:{mcp_port}"
        base_ctx.mcp_server = mcp_server
        return base_ctx

    async def _teardown_ctx(self, ctx: ExecutionContext) -> None:
        # Run any cleanup_fns registered by harness.send_turn (e.g. stop lazy containers)
        for fn in list(getattr(ctx, 'cleanup_fns', [])):
            try:
                await fn()
            except Exception:
                pass
        if hasattr(ctx, 'cleanup_fns'):
            ctx.cleanup_fns.clear()

        if ctx.mcp_server:
            try:
                await ctx.mcp_server.stop()
            except Exception:
                pass
        if ctx.sandbox and ctx.sandbox in self._active_sandboxes:
            self._active_sandboxes.remove(ctx.sandbox)
            try:
                await ctx.sandbox.stop()
            except Exception:
                pass
        elif ctx.sandbox:
            try:
                await ctx.sandbox.stop()
            except Exception:
                pass

    async def _run_task(
        self, task: Task, benchmark, semaphore: asyncio.Semaphore
    ) -> tuple[AgentTrace, EvalResult, str]:
        if self._stop_event.is_set():
            raise asyncio.CancelledError("stopped")
        async with semaphore:
            if self._stop_event.is_set():
                raise asyncio.CancelledError("stopped")

            # Resume: skip already-completed tasks (check DB)
            existing = await self.db.fetch_task_output(self.run_id, task.id)
            if existing and existing.get("status") == "done":
                eval_data = await self.db.fetch_task_eval(self.run_id, task.id)
                if eval_data:
                    self.progress_cb(event="skip", task_id=task.id, benchmark=benchmark.name)
                    trace = AgentTrace(task_id=task.id, final_output=existing.get("final_output", ""))
                    result = EvalResult(
                        sample_id=task.id,
                        score=eval_data["score"],
                        grade=eval_data["grade"],
                    )
                    return trace, result, ""

            self.progress_cb(event="start", task_id=task.id, benchmark=benchmark.name)
            await self.db.save_task_start(self.run_id, task.id, benchmark.name)
            timeout = self.cfg.parallelism.timeout_per_task

            def _step_cb(step_type: str, content) -> None:
                self.progress_cb(
                    event="step",
                    task_id=task.id,
                    benchmark=benchmark.name,
                    step_type=step_type,
                    content=content,
                )

            harness = self._get_harness(benchmark)
            if task.sandbox is not None and not harness.supports_sandbox:
                raise ValueError(
                    f"Benchmark '{benchmark.name}' requires a sandbox-capable harness "
                    f"(supports_sandbox=True). Harness '{harness.type}' does not support it."
                )

            scorer = benchmark.make_scorer()

            base_ctx = ExecutionContext(
                timeout=timeout, step_cb=_step_cb, web_search=task.web_search,
                stream_idle_timeout=self.cfg.parallelism.stream_idle_timeout,
            )
            # Outer timeout covers sandbox start + run + eval + teardown
            outer_timeout = timeout + self.cfg.parallelism.eval_timeout + 120
            try:
                ctx = await asyncio.wait_for(
                    self._build_ctx(task, benchmark, base_ctx),
                    timeout=outer_timeout,
                )
            except asyncio.TimeoutError:
                logger.warning("Sandbox setup timed out for task %s after %ss", task.id, outer_timeout)
                result = EvalResult(sample_id=task.id, score=0.0, grade="TIMEOUT",
                                    explanation=f"Sandbox setup timeout after {outer_timeout}s")
                await self.db.save_eval_result(self.run_id, benchmark.name, result)
                self.progress_cb(event="done", task_id=task.id, benchmark=benchmark.name,
                                 grade=result.grade, score=result.score,
                                 ground_truth="", explanation=result.explanation or "",
                                 judge_model="", judge_output="", category="")
                return AgentTrace(task_id=task.id, final_output="", error=result.explanation), result, ""
            ctx.harness_type = harness.type

            try:
                try:
                    if harness.SUPPORTS_RUNNER_PROTOCOL:
                        runner = benchmark.make_runner(model_cfg=self.cfg.model)
                        trace = await asyncio.wait_for(
                            runner.run(task, harness.send_turn, ctx),
                            timeout=timeout,
                        )
                    else:
                        trace = await asyncio.wait_for(
                            harness.run_task(task, ctx),
                            timeout=timeout,
                        )
                except asyncio.TimeoutError:
                    trace = AgentTrace(
                        task_id=task.id, final_output="",
                        error=f"Timeout after {timeout}s",
                    )
                except Exception as exc:
                    logger.error("Runner failed for task %s: %s", task.id, exc, exc_info=True)
                    trace = AgentTrace(
                        task_id=task.id, final_output="",
                        error=str(exc) or repr(exc),
                    )

                # Score while sandbox is still alive (scorer decides if it needs it)
                if trace.error and trace.error.startswith("Timeout"):
                    result = EvalResult(sample_id=task.id, score=0.0, grade="TIMEOUT",
                                        explanation=trace.error)
                elif trace.error and trace.error.startswith("AgentDead"):
                    # Agent died (terminal signal or idle watchdog). Skip the scorer:
                    # an infra death is not a wrong answer. Covers both the propagated
                    # exception path (SingleTurnRunner) and the returned-in-trace path
                    # (BFCL runners set error=str(AgentDeadError), which keeps the prefix).
                    result = EvalResult(sample_id=task.id, score=0.0, grade="AGENT_DEAD",
                                        explanation=trace.error)
                else:
                    try:
                        result = await asyncio.wait_for(
                            scorer(task, trace, self.judge, sandbox=ctx.sandbox),
                            timeout=self.cfg.parallelism.eval_timeout,
                        )
                    except asyncio.TimeoutError:
                        logger.error("Scorer timed out for %s (eval_timeout=%ss)", task.id, self.cfg.parallelism.eval_timeout)
                        result = EvalResult(sample_id=task.id, score=0.0, grade="ERROR",
                                            explanation=f"Scorer timeout after {self.cfg.parallelism.eval_timeout}s")
                    except Exception as exc:
                        logger.error("Scorer failed for %s: %s", task.id, exc)
                        result = EvalResult(sample_id=task.id, score=0.0, grade="ERROR",
                                            explanation=str(exc) or repr(exc))

            finally:
                try:
                    await asyncio.wait_for(self._teardown_ctx(ctx), timeout=120)
                except (asyncio.TimeoutError, Exception):
                    logger.warning("Teardown timed out or failed for task %s, forcing cleanup", task.id)

            agent_patch = getattr(trace, "_agent_patch", None)
            eval_log = getattr(trace, "_eval_log", None)
            if agent_patch is not None or eval_log is not None:
                await self.db.save_task_output(self.run_id, benchmark.name, trace,
                                               agent_patch=agent_patch, eval_log=eval_log)
            else:
                await self.db.save_task_output(self.run_id, benchmark.name, trace)

            await self.db.save_eval_result(self.run_id, benchmark.name, result)

            gt = task.ground_truth
            if isinstance(gt, (list, dict)):
                gt_str = json.dumps(gt, ensure_ascii=False)
            else:
                gt_str = str(gt) if gt is not None else ""

            category = (
                task.metadata.get("category")
                or task.metadata.get("task_type")
                or task.metadata.get("topic")
                or task.metadata.get("repo", "")
                or ""
            )

            self.progress_cb(
                event="done",
                task_id=task.id,
                benchmark=benchmark.name,
                grade=result.grade,
                score=result.score,
                ground_truth=gt_str,
                explanation=result.explanation or "",
                judge_model=result.judge_model or "",
                judge_output=result.judge_output or "",
                category=category,
            )
            return trace, result, category

    async def run(self) -> dict:
        # Preflight: fail fast with a clear message instead of letting every task
        # die on a cryptic "docker pull ... failed". Raise (not emit+return): the
        # web layer's _do_run catches it → run_error; the CLI catches it too.
        if getattr(self.harness, "needs_docker", True):
            self.progress_cb(event="log", level="info", message="Checking Docker availability")
            ok, msg = await docker_status()
            if not ok:
                self.progress_cb(event="log", level="error", message=msg)
                raise DockerUnavailableError(msg)
            self.progress_cb(event="log", level="info", message=msg)

        config_yaml = yaml.dump(self.cfg.model_dump(), allow_unicode=True)

        await self.db.connect()
        # Surface DB state in the run log: a missing/unavailable DB means the run
        # is valid but won't be saved to history (warn, not error).
        if self.db.cfg is None:
            self.progress_cb(event="log", level="warn",
                             message="No database configured — run will not be saved to history")
        elif not self.db._enabled:
            self.progress_cb(event="log", level="warn",
                             message="Database unavailable — run will not be saved to history")
        else:
            c = self.db.cfg
            self.progress_cb(event="log", level="info",
                             message=f"Database connected: {c.user}@{c.host}:{c.port}/{c.name}")

        await self.db.save_run(
            self.run_id,
            self.cfg.model.model_name,
            self.cfg.model.base_url,
            self.cfg.harness.type,
            config_yaml,
        )

        semaphore = asyncio.Semaphore(self.cfg.parallelism.workers)
        all_summaries: dict[str, dict] = {}

        try:
            for bench_cfg in self.cfg.benchmarks:
                BenchmarkCls = _resolve_benchmark(bench_cfg.name)
                if bench_cfg.name == "pac1":
                    benchmark = BenchmarkCls(bench_cfg, harness_cfg=self.cfg.harness)
                else:
                    benchmark = BenchmarkCls(bench_cfg)
                tasks = benchmark.load_samples()

                self.progress_cb(event="benchmark_start", benchmark=bench_cfg.name, total=len(tasks))

                task_coros = [self._run_task(t, benchmark, semaphore) for t in tasks]
                running = [asyncio.create_task(c) for c in task_coros]
                self._running_tasks.extend(running)
                try:
                    results_pairs = await asyncio.gather(*running, return_exceptions=True)
                except asyncio.CancelledError:
                    for t in running:
                        t.cancel()
                    await asyncio.gather(*running, return_exceptions=True)
                    raise
                finally:
                    for t in running:
                        try:
                            self._running_tasks.remove(t)
                        except ValueError:
                            pass

                if self._stop_event.is_set():
                    break

                # Some harnesses (PAC1) grade per run, not per task — pick up
                # real scores now and rewrite the placeholder rows.
                await self._apply_run_grades(
                    harness=self._get_harness(benchmark),
                    benchmark_name=bench_cfg.name,
                    results_pairs=results_pairs,
                )

                scores = []
                grades: dict[str, int] = {}
                categories: dict[str, dict] = {}
                errors = 0
                total_input_tokens = 0
                total_output_tokens = 0
                for pair in results_pairs:
                    if isinstance(pair, Exception):
                        errors += 1
                        self.progress_cb(event="error", benchmark=bench_cfg.name, error=str(pair))
                        continue
                    trace, er, cat = pair
                    total_input_tokens += trace.input_tokens
                    total_output_tokens += trace.output_tokens
                    scores.append(er.score)
                    grades[er.grade] = grades.get(er.grade, 0) + 1
                    if cat:
                        if cat not in categories:
                            categories[cat] = {"scores": [], "grades": {}}
                        categories[cat]["scores"].append(er.score)
                        categories[cat]["grades"][er.grade] = categories[cat]["grades"].get(er.grade, 0) + 1

                n = len(scores)
                cat_summary = {}
                for cat, cdata in sorted(categories.items()):
                    cn = len(cdata["scores"])
                    cat_summary[cat] = {
                        "accuracy": round(sum(cdata["scores"]) / cn, 4) if cn else 0,
                        "n": cn,
                        "grades": cdata["grades"],
                    }
                summary = {
                    "total": len(tasks),
                    "completed": n,
                    "errors": errors,
                    "accuracy": round(sum(scores) / n, 4) if n else 0,
                    "grades": grades,
                    "categories": cat_summary,
                    "input_tokens": total_input_tokens,
                    "output_tokens": total_output_tokens,
                }
                all_summaries[bench_cfg.name] = summary
                await self.db.save_benchmark_summary(self.run_id, bench_cfg.name, summary)
                self.progress_cb(event="benchmark_done", benchmark=bench_cfg.name, summary=summary)

        finally:
            seen: set[int] = set()
            for h in [self.harness, *self._benchmark_harnesses.values()]:
                if id(h) in seen:
                    continue
                seen.add(id(h))
                try:
                    await asyncio.to_thread(h.finalize)
                except Exception as e:
                    logger.warning("harness.finalize() failed: %s", e)

            await self.db.close()

        return {
            "run_id": self.run_id,
            "model": self.cfg.model.model_name,
            "harness": self.cfg.harness.type,
            "benchmarks": all_summaries,
        }
