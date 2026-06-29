"""CLI entry point for the harness testing framework."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from framework.config import RunConfig
from framework.orchestrator import Orchestrator

console = Console()


GRADE_COLORS = {
    "CORRECT": "green", "PASS": "green",
    "INCORRECT": "red", "FAIL": "red",
    "PARTIAL": "yellow", "NOT_ATTEMPTED": "yellow",
}


def _silence_windows_proactor_noise() -> None:
    """Drop the benign Windows ProactorEventLoop self-pipe error (WinError 1236).

    On Windows the default ProactorEventLoop logs "Error on reading from the event
    loop self pipe" under heavy socket/subprocess load; the loop keeps running.
    We cannot switch to SelectorEventLoop — it has no subprocess support, which the
    harnesses need (docker run/exec). So we just filter out this one message.
    """
    if not sys.platform.startswith("win"):
        return
    import logging

    class _SelfPipeFilter(logging.Filter):
        def filter(self, record: "logging.LogRecord") -> bool:
            msg = record.getMessage()
            return not ("self pipe" in msg or "WinError 1236" in msg)

    logging.getLogger("asyncio").addFilter(_SelfPipeFilter())


_LOG_LEVEL_COLORS = {"info": "dim", "ok": "green", "warn": "yellow", "error": "red bold"}


def _progress(event: str, **kw):
    if event == "log":
        color = _LOG_LEVEL_COLORS.get(kw.get("level", "info"), "dim")
        console.print(f"  [{color}]{kw.get('message', '')}[/]")
    elif event == "benchmark_start":
        console.print(f"\n[bold cyan]► {kw['benchmark']}[/] — {kw['total']} tasks")
    elif event == "start":
        console.print(f"  [dim]→[/] {kw['task_id']}", end="\r")
    elif event == "skip":
        console.print(f"  [dim]skip[/] {kw['task_id']}", end="\r")
    elif event == "done":
        grade = kw["grade"]
        color = GRADE_COLORS.get(grade, "white")
        console.print(f"  [{color}]{grade:<15}[/] {kw['task_id']}")
    elif event == "error":
        console.print(f"  [red bold]ERROR[/] {kw.get('error', '')[:120]}")
    elif event == "benchmark_done":
        s = kw["summary"]
        console.print(
            f"[bold]  accuracy={s['accuracy']:.1%}  n={s['total']}  grades={s['grades']}[/]"
        )


@click.group()
def cli():
    """Harness Testing Framework."""


@cli.command()
@click.option("--config", "config_path", default=None, type=click.Path(exists=True),
              help="Preload a config preset (optional — configs can also be picked in the UI)")
@click.option("--open-browser", is_flag=True, default=False,
              help="Open a browser window (for local use; off by default for a service)")
def serve(config_path: str | None, open_browser: bool):
    """Start the Web UI as a long-running service.

    Pick, edit, save and run config presets in the browser. The database is a
    property of the deployment (env DB_HOST… or the --config file), not of the
    selected preset.
    """
    _silence_windows_proactor_noise()
    from framework.ui.web import start_web_ui
    cfg = RunConfig.from_yaml(config_path) if config_path else RunConfig()
    start_web_ui(cfg, config_path=config_path or "", open_browser=open_browser)


@cli.command()
@click.argument("config_path", type=click.Path(exists=True))
@click.option("--no-ui", is_flag=True, default=False, hidden=True,
              help="Deprecated: `run` is always headless; use `serve` for the Web UI.")
def run(config_path: str, no_ui: bool):
    """Run benchmarks from CONFIG_PATH (headless). For the Web UI use `serve`."""
    _silence_windows_proactor_noise()
    from framework.utils.docker import DockerUnavailableError

    if no_ui:
        console.print("[yellow]--no-ui is deprecated: `run` is already headless; "
                      "use `serve` for the Web UI.[/]")

    cfg = RunConfig.from_yaml(config_path)
    orch = Orchestrator(cfg, progress_cb=_progress)
    console.print(f"[bold]Run ID:[/] {orch.run_id}")
    console.print(f"[bold]Model:[/]  {cfg.model.model_name}  @ {cfg.model.base_url}")
    console.print(f"[bold]Harness:[/] {cfg.harness.type}")

    try:
        summary = asyncio.run(orch.run())
    except DockerUnavailableError as e:
        console.print(f"\n[red bold]Cannot start run:[/] {e}")
        sys.exit(1)

    console.print("\n[bold green]── SUMMARY ──[/]")
    table = Table("Benchmark", "Accuracy", "N", "Grades")
    for bname, s in summary["benchmarks"].items():
        table.add_row(bname, f"{s['accuracy']:.1%}", str(s["total"]), str(s["grades"]))
    console.print(table)
    console.print(f"\nResults saved to: [cyan]runs/{orch.run_id}/[/]")


def _load_run_summary(run_id: str, db=None) -> dict | None:
    """Load run summary from DB (preferred) or filesystem fallback."""
    if db and db._enabled:
        data = asyncio.run(db.fetch_run_summary(run_id))
        if data:
            return data
    path = Path("runs") / run_id / "summary.json"
    if path.exists():
        return json.loads(path.read_text())
    return None


@cli.command()
@click.argument("run_id_a")
@click.argument("run_id_b")
@click.option("--config", "config_path", default=None, type=click.Path(exists=True), help="Config with DB settings")
def compare(run_id_a: str, run_id_b: str, config_path: str | None):
    """Compare two runs side by side."""
    db = None
    if config_path:
        cfg = RunConfig.from_yaml(config_path)
        if cfg.database:
            from framework.db import Database
            db = Database(cfg.database)
            asyncio.run(db.connect())

    a = _load_run_summary(run_id_a, db)
    b = _load_run_summary(run_id_b, db)

    if not a:
        console.print(f"[red]Run not found: {run_id_a}[/]")
        sys.exit(1)
    if not b:
        console.print(f"[red]Run not found: {run_id_b}[/]")
        sys.exit(1)

    console.print(f"\n[bold]Compare:[/] {run_id_a} vs {run_id_b}")
    table = Table("Benchmark", f"{a['model']} ({run_id_a})", f"{b['model']} ({run_id_b})", "Δ")
    all_benchmarks = set(a["benchmarks"]) | set(b["benchmarks"])
    for bname in sorted(all_benchmarks):
        acc_a = a["benchmarks"].get(bname, {}).get("accuracy", None)
        acc_b = b["benchmarks"].get(bname, {}).get("accuracy", None)
        str_a = f"{acc_a:.1%}" if acc_a is not None else "—"
        str_b = f"{acc_b:.1%}" if acc_b is not None else "—"
        if acc_a is not None and acc_b is not None:
            delta = acc_b - acc_a
            color = "green" if delta > 0 else ("red" if delta < 0 else "dim")
            str_delta = f"[{color}]{delta:+.1%}[/]"
        else:
            str_delta = "—"
        table.add_row(bname, str_a, str_b, str_delta)
    console.print(table)


@cli.command()
@click.argument("run_id")
@click.option("--config", "config_path", default=None, type=click.Path(exists=True), help="Config with DB settings")
def results(run_id: str, config_path: str | None):
    """Show results for a run."""
    db = None
    if config_path:
        cfg = RunConfig.from_yaml(config_path)
        if cfg.database:
            from framework.db import Database
            db = Database(cfg.database)
            asyncio.run(db.connect())

    data = _load_run_summary(run_id, db)
    if not data:
        console.print(f"[red]Run not found: {run_id}[/]")
        sys.exit(1)
    console.print_json(json.dumps(data, indent=2))


@cli.command("runs")
@click.argument("config_path", type=click.Path(exists=True))
def list_runs(config_path: str):
    """List all runs from the database."""
    cfg = RunConfig.from_yaml(config_path)
    if not cfg.database:
        console.print("[red]No database section in config.[/]")
        sys.exit(1)
    from framework.db import Database
    db = Database(cfg.database)

    async def _list():
        await db.connect()
        if not db._enabled:
            console.print("[red]Database not available.[/]")
            return
        rows = await db.fetch_runs_list()
        table = Table("Run ID", "Model", "Harness", "Created", "Benchmarks")
        for r in rows:
            bench_str = "  ".join(
                f"{b}: {s['accuracy']:.0%}" for b, s in r["benchmarks"].items()
            )
            table.add_row(r["run_id"], r["model"], r["harness"], r["created_at"][:19], bench_str)
        console.print(table)
        await db.close()

    asyncio.run(_list())


@cli.command("db-init")
@click.argument("config_path", type=click.Path(exists=True))
def db_init(config_path: str):
    """Initialize PostgreSQL database (apply migrations)."""
    cfg = RunConfig.from_yaml(config_path)
    if not cfg.database:
        console.print("[red]No database section in config.[/]")
        sys.exit(1)

    from framework.db import Database
    db = Database(cfg.database)

    async def _init():
        await db.connect()
        if db._enabled:
            console.print(f"[green]✓ Database initialized:[/] {cfg.database.url}")
        else:
            console.print("[red]✗ Could not connect to database.[/]")
        await db.close()

    asyncio.run(_init())


@cli.command("db-runs")
@click.argument("config_path", type=click.Path(exists=True))
def db_runs(config_path: str):
    """List all runs stored in the database."""
    cfg = RunConfig.from_yaml(config_path)
    if not cfg.database:
        console.print("[red]No database section in config.[/]")
        sys.exit(1)

    from framework.db import Database
    db = Database(cfg.database)

    async def _list():
        await db.connect()
        if not db._enabled:
            console.print("[red]Database not available.[/]")
            return
        async with db._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, model_name, harness, created_at FROM runs ORDER BY created_at DESC LIMIT 20"
            )
        table = Table("Run ID", "Model", "Harness", "Created")
        for r in rows:
            table.add_row(r["id"], r["model_name"], r["harness"], str(r["created_at"])[:19])
        console.print(table)
        await db.close()

    asyncio.run(_list())
