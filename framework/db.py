"""PostgreSQL storage layer using asyncpg."""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import asdict
from typing import TYPE_CHECKING

try:
    import asyncpg
    _HAS_ASYNCPG = True
except ImportError:
    _HAS_ASYNCPG = False

if TYPE_CHECKING:
    from framework.config import DatabaseConfig
    from framework.models import AgentTrace, EvalResult

log = logging.getLogger(__name__)

_SECRET_KEYS = {"api_key", "hermes_api_key", "tavily_api_key", "password"}


def _redact_config(yaml_str: str) -> str:
    """Replace secret values with *** in YAML string."""
    def _replace(m: re.Match) -> str:
        key = m.group(1)
        if any(s in key.lower() for s in _SECRET_KEYS):
            return f"{key}: ***"
        return m.group(0)
    return re.sub(r"(\w+):\s+\S+", _replace, yaml_str)


def _is_retryable_conn_error(e: Exception) -> bool:
    """True for a transient connection drop (worth retrying), not a data/SQL error."""
    if isinstance(e, (ConnectionError, OSError)):   # incl. ConnectionAbortedError (WinError 1236)
        return True
    msg = str(e).lower()
    return any(s in msg for s in (
        "connection was closed", "connection is closed",
        "connection does not exist", "the connection is closed",
        "cannot perform operation",
    ))


class Database:
    def __init__(self, cfg: "DatabaseConfig | None") -> None:
        self.cfg = cfg
        self._pool = None
        self._enabled = cfg is not None and _HAS_ASYNCPG

    async def connect(self) -> None:
        if not self._enabled:
            return
        for attempt in range(3):
            try:
                self._pool = await asyncpg.create_pool(self.cfg.url, min_size=1, max_size=10)
                await self._apply_migrations()
                log.info("PostgreSQL connected: %s", self.cfg.url)
                return
            except Exception as e:
                if attempt == 2:
                    log.warning("PostgreSQL unavailable after 3 attempts (%s) — running without DB", e)
                    self._enabled = False
                    return
                await asyncio.sleep(2 ** attempt)

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()

    async def _apply_migrations(self) -> None:
        from pathlib import Path
        migrations_dir = Path(__file__).parent.parent / "db" / "migrations"
        if not migrations_dir.exists():
            return
        async with self._pool.acquire() as conn:
            # Advisory lock prevents parallel migration races
            await conn.execute("SELECT pg_advisory_lock(987654321)")
            try:
                # Ensure schema_migrations table exists (bootstrapping)
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS schema_migrations (
                        version    TEXT PRIMARY KEY,
                        applied_at TIMESTAMPTZ DEFAULT NOW()
                    )
                """)
                applied = {r["version"] for r in await conn.fetch("SELECT version FROM schema_migrations")}
                for sql_file in sorted(migrations_dir.glob("*.sql")):
                    version = sql_file.stem
                    if version not in applied:
                        await conn.execute(sql_file.read_text())
                        await conn.execute(
                            "INSERT INTO schema_migrations (version) VALUES ($1) ON CONFLICT DO NOTHING",
                            version,
                        )
                        log.info("Applied migration: %s", version)
            finally:
                await conn.execute("SELECT pg_advisory_unlock(987654321)")

    # ── Write ──────────────────────────────────────────────────────────────────

    async def _execute_write(self, label: str, sql: str, *params, attempts: int = 3) -> None:
        """One INSERT/UPDATE/DELETE with retry on a dropped connection.

        asyncpg discards a dead connection and hands out a live one on the next
        acquire(), so re-trying the operation usually succeeds. A transient drop
        must not lose the write nor disable the DB for the rest of the run, so
        `_enabled` is left untouched.
        """
        if not self._enabled or self._pool is None:
            return
        last_err = None
        for attempt in range(attempts):
            try:
                async with self._pool.acquire() as conn:
                    await conn.execute(sql, *params)
                return
            except Exception as e:
                last_err = e
                if attempt < attempts - 1 and _is_retryable_conn_error(e):
                    await asyncio.sleep(0.3 * (attempt + 1))
                    continue
                break
        log.warning("DB write failed (%s): %s", label, last_err)

    async def save_run(self, run_id: str, model_name: str, model_url: str, harness: str, config_yaml: str) -> None:
        await self._execute_write(
            f"save_run {run_id}",
            """
            INSERT INTO runs (id, model_name, model_url, harness, config_yaml)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (id) DO UPDATE SET
                model_name  = EXCLUDED.model_name,
                model_url   = EXCLUDED.model_url,
                harness     = EXCLUDED.harness,
                config_yaml = EXCLUDED.config_yaml,
                updated_at  = NOW()
            """,
            run_id, model_name, model_url, harness, _redact_config(config_yaml),
        )

    async def save_task_start(self, run_id: str, task_id: str, benchmark: str) -> None:
        """Mark task as running — called at the beginning of each task."""
        await self._execute_write(
            f"save_task_start {run_id}/{task_id}",
            """
            INSERT INTO task_outputs
              (run_id, benchmark, task_id, status, final_output, steps)
            VALUES ($1, $2, $3, 'running', '', '[]')
            ON CONFLICT (run_id, task_id) DO UPDATE SET
                status     = 'running',
                updated_at = NOW()
            """,
            run_id, benchmark, task_id,
        )

    async def save_task_output(
        self,
        run_id: str,
        benchmark: str,
        trace: "AgentTrace",
        agent_patch: str | None = None,
        eval_log: str | None = None,
    ) -> None:
        if not self._enabled:
            return
        d = asdict(trace)
        status = "error" if trace.error else "done"
        if trace.error and "Timeout" in trace.error:
            status = "timeout"
        await self._execute_write(
            f"save_task_output {run_id}/{trace.task_id}",
            """
            INSERT INTO task_outputs
              (run_id, benchmark, task_id, final_output, steps,
               input_tokens, output_tokens, duration_sec, error,
               status, agent_patch, eval_log)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
            ON CONFLICT (run_id, task_id) DO UPDATE SET
                final_output  = EXCLUDED.final_output,
                steps         = EXCLUDED.steps,
                input_tokens  = EXCLUDED.input_tokens,
                output_tokens = EXCLUDED.output_tokens,
                duration_sec  = EXCLUDED.duration_sec,
                error         = EXCLUDED.error,
                status        = EXCLUDED.status,
                agent_patch   = EXCLUDED.agent_patch,
                eval_log      = EXCLUDED.eval_log,
                updated_at    = NOW()
            """,
            run_id, benchmark, trace.task_id, trace.final_output,
            json.dumps(d["steps"], ensure_ascii=False),
            trace.input_tokens, trace.output_tokens, trace.duration_sec, trace.error,
            status, agent_patch, eval_log,
        )

    async def save_eval_result(self, run_id: str, benchmark: str, result: "EvalResult") -> None:
        if not self._enabled:
            return
        await self._execute_write(
            f"save_eval_result {run_id}/{result.task_id}",
            """
            INSERT INTO eval_results
              (run_id, benchmark, task_id, score, grade,
               explanation, judge_model, judge_input, judge_output, metadata)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
            ON CONFLICT (run_id, task_id) DO UPDATE SET
                score        = EXCLUDED.score,
                grade        = EXCLUDED.grade,
                explanation  = EXCLUDED.explanation,
                judge_model  = EXCLUDED.judge_model,
                judge_input  = EXCLUDED.judge_input,
                judge_output = EXCLUDED.judge_output,
                metadata     = EXCLUDED.metadata,
                updated_at   = NOW()
            """,
            run_id, benchmark, result.task_id, result.score, result.grade,
            result.explanation, result.judge_model, result.judge_input, result.judge_output,
            json.dumps(result.metadata, ensure_ascii=False),
        )

    async def save_benchmark_summary(self, run_id: str, benchmark: str, summary: dict) -> None:
        if not self._enabled:
            return
        await self._execute_write(
            f"save_benchmark_summary {run_id}/{benchmark}",
            """
            INSERT INTO benchmark_runs
              (run_id, benchmark, total_tasks, completed, accuracy, grades, categories)
            VALUES ($1,$2,$3,$4,$5,$6,$7)
            ON CONFLICT (run_id, benchmark) DO UPDATE SET
                total_tasks = EXCLUDED.total_tasks,
                completed   = EXCLUDED.completed,
                accuracy    = EXCLUDED.accuracy,
                grades      = EXCLUDED.grades,
                categories  = EXCLUDED.categories
            """,
            run_id, benchmark,
            summary.get("total"), summary.get("completed"),
            summary.get("accuracy"),
            json.dumps(summary.get("grades", {})),
            json.dumps(summary.get("categories", {})),
        )

    async def delete_run(self, run_id: str) -> None:
        await self._execute_write(
            f"delete run {run_id}",
            "DELETE FROM runs WHERE id = $1", run_id,
        )

    # ── Read ───────────────────────────────────────────────────────────────────

    async def fetch_runs_list(self) -> list[dict]:
        """Return all runs with per-benchmark accuracy. No steps."""
        if not self._enabled:
            return []
        try:
            async with self._pool.acquire() as conn:
                runs = await conn.fetch(
                    "SELECT id, model_name, harness, created_at FROM runs ORDER BY created_at DESC"
                )
                bench_rows = await conn.fetch(
                    "SELECT run_id, benchmark, accuracy, total_tasks, grades, categories FROM benchmark_runs"
                )
            bench_map: dict[str, dict] = {}
            for br in bench_rows:
                rid = br["run_id"]
                if rid not in bench_map:
                    bench_map[rid] = {}
                bench_map[rid][br["benchmark"]] = {
                    "accuracy": float(br["accuracy"] or 0),
                    "total": br["total_tasks"] or 0,
                    "grades": json.loads(br["grades"] or "{}"),
                    "categories": json.loads(br["categories"] or "{}"),
                }
            result = []
            for r in runs:
                result.append({
                    "run_id": r["id"],
                    "model": r["model_name"],
                    "harness": r["harness"],
                    "created_at": r["created_at"].isoformat() if r["created_at"] else "",
                    "benchmarks": bench_map.get(r["id"], {}),
                })
            return result
        except Exception as e:
            log.warning("DB read failed (fetch_runs_list): %s", e)
            return []

    async def fetch_run_summary(self, run_id: str) -> dict | None:
        """Return summary for one run. No steps."""
        if not self._enabled:
            return None
        try:
            async with self._pool.acquire() as conn:
                run = await conn.fetchrow(
                    "SELECT id, model_name, harness, created_at FROM runs WHERE id = $1", run_id
                )
                if not run:
                    return None
                bench_rows = await conn.fetch(
                    "SELECT benchmark, accuracy, total_tasks, grades, categories FROM benchmark_runs WHERE run_id = $1",
                    run_id,
                )
            benchmarks = {}
            for br in bench_rows:
                benchmarks[br["benchmark"]] = {
                    "accuracy": float(br["accuracy"] or 0),
                    "total": br["total_tasks"] or 0,
                    "grades": json.loads(br["grades"] or "{}"),
                    "categories": json.loads(br["categories"] or "{}"),
                }
            return {
                "run_id": run["id"],
                "model": run["model_name"],
                "harness": run["harness"],
                "created_at": run["created_at"].isoformat() if run["created_at"] else "",
                "benchmarks": benchmarks,
            }
        except Exception as e:
            log.warning("DB read failed (fetch_run_summary %s): %s", run_id, e)
            return None

    async def fetch_run_tasks(self, run_id: str) -> dict[str, list[dict]]:
        """Return tasks grouped by benchmark. Steps NOT included (use fetch_task_steps)."""
        if not self._enabled:
            return {}
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT t.benchmark, t.task_id, t.status, t.error,
                           t.input_tokens, t.output_tokens,
                           e.score, e.grade, e.explanation, e.judge_output
                    FROM task_outputs t
                    LEFT JOIN eval_results e USING (run_id, task_id)
                    WHERE t.run_id = $1
                    ORDER BY t.benchmark, t.task_id
                    """,
                    run_id,
                )
            result: dict[str, list] = {}
            for r in rows:
                bench = r["benchmark"]
                if bench not in result:
                    result[bench] = []
                result[bench].append({
                    "task_id": r["task_id"],
                    "status": r["status"],
                    "grade": r["grade"] or "",
                    "score": float(r["score"] or 0),
                    "explanation": r["explanation"] or "",
                    "judge_output": r["judge_output"] or "",
                    "error": r["error"] or "",
                })
            return result
        except Exception as e:
            log.warning("DB read failed (fetch_run_tasks %s): %s", run_id, e)
            return {}

    async def fetch_task_steps(self, run_id: str, task_id: str) -> list:
        """Return steps for one task (lazy load)."""
        if not self._enabled:
            return []
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT steps FROM task_outputs WHERE run_id = $1 AND task_id = $2",
                    run_id, task_id,
                )
            if not row or not row["steps"]:
                return []
            return json.loads(row["steps"])
        except Exception as e:
            log.warning("DB read failed (fetch_task_steps %s/%s): %s", run_id, task_id, e)
            return []

    async def fetch_task_patch(self, run_id: str, task_id: str) -> str:
        """Return git diff patch for a SWE-bench task."""
        if not self._enabled:
            return ""
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT agent_patch FROM task_outputs WHERE run_id = $1 AND task_id = $2",
                    run_id, task_id,
                )
            return (row["agent_patch"] or "") if row else ""
        except Exception as e:
            log.warning("DB read failed (fetch_task_patch %s/%s): %s", run_id, task_id, e)
            return ""

    async def fetch_task_eval_log(self, run_id: str, task_id: str) -> str:
        """Return eval.sh output for a SWE-bench task."""
        if not self._enabled:
            return ""
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT eval_log FROM task_outputs WHERE run_id = $1 AND task_id = $2",
                    run_id, task_id,
                )
            return (row["eval_log"] or "") if row else ""
        except Exception as e:
            log.warning("DB read failed (fetch_task_eval_log %s/%s): %s", run_id, task_id, e)
            return ""

    async def fetch_task_eval(self, run_id: str, task_id: str) -> dict | None:
        """Return eval result (score, grade) for one task."""
        if not self._enabled:
            return None
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT score, grade FROM eval_results WHERE run_id = $1 AND task_id = $2",
                    run_id, task_id,
                )
            if not row:
                return None
            return {"score": float(row["score"] or 0), "grade": row["grade"] or ""}
        except Exception as e:
            log.warning("DB read failed (fetch_task_eval %s/%s): %s", run_id, task_id, e)
            return None

    async def fetch_task_output(self, run_id: str, task_id: str) -> dict | None:
        """Return full task trace (final_output, steps, error, tokens) for one task."""
        if not self._enabled:
            return None
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT task_id, final_output, steps, input_tokens, output_tokens,
                           duration_sec, error, status
                    FROM task_outputs WHERE run_id = $1 AND task_id = $2
                    """,
                    run_id, task_id,
                )
            if not row:
                return None
            return {
                "task_id": row["task_id"],
                "final_output": row["final_output"] or "",
                "steps": json.loads(row["steps"]) if row["steps"] else [],
                "input_tokens": row["input_tokens"] or 0,
                "output_tokens": row["output_tokens"] or 0,
                "duration_sec": float(row["duration_sec"] or 0),
                "error": row["error"] or "",
                "status": row["status"] or "",
            }
        except Exception as e:
            log.warning("DB read failed (fetch_task_output %s/%s): %s", run_id, task_id, e)
            return None

    async def fetch_swe_f2p_p2p(self, run_id: str, bench: str) -> dict | None:
        """Return aggregated F2P/P2P stats for a SWE-bench run from explanation fields."""
        if not self._enabled:
            return None
        try:
            import re
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT explanation FROM eval_results WHERE run_id = $1 AND benchmark = $2",
                    run_id, bench,
                )
            f2p_pass = f2p_total = p2p_pass = p2p_total = 0
            for row in rows:
                expl = row["explanation"] or ""
                m_f2p = re.search(r"F2P:\s*(\d+)/(\d+)", expl)
                m_p2p = re.search(r"P2P:\s*(\d+)/(\d+)", expl)
                if m_f2p:
                    f2p_pass += int(m_f2p.group(1))
                    f2p_total += int(m_f2p.group(2))
                if m_p2p:
                    p2p_pass += int(m_p2p.group(1))
                    p2p_total += int(m_p2p.group(2))
            if f2p_total == 0 and p2p_total == 0:
                return None
            return {
                "f2p_pass": f2p_pass, "f2p_total": f2p_total,
                "p2p_pass": p2p_pass, "p2p_total": p2p_total,
            }
        except Exception as e:
            log.warning("DB read failed (fetch_swe_f2p_p2p %s/%s): %s", run_id, bench, e)
            return None
