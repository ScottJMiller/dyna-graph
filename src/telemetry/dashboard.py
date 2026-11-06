"""Run or resume a local workflow with a bounded Rich terminal dashboard.

    python3 -m src.telemetry.dashboard --demo --plain
    python3 -m src.telemetry.dashboard --task 'Confirm the supplied relationship.'
"""

import argparse
import asyncio
from collections import deque
from contextlib import AbstractContextManager
from pathlib import Path
import os
import select
import sys
from uuid import uuid4

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from src.telemetry.logger import CSVLogger
from src.telemetry.profiler import TelemetryProfiler, TurnMetrics


class Dashboard:
    """Display only the last 12 turns; complete history stays in SQLite/CSV."""

    def __init__(self, *, demo=False):
        self.rows = deque(maxlen=12)
        self.demo = demo
        self.paused = False

    def record(self, metrics: TurnMetrics):
        if self.rows and metrics.step <= self.rows[-1].step:
            raise ValueError("display turns must increase")
        self.rows.append(metrics)

    def render(self, *, run_id: str, stage: str, status: str, elapsed: float, active_role=""):
        title = "DynaGraph telemetry" + (" | SIMULATED DEMO" if self.demo else "")
        header = Text(f"Run: {run_id}\nStatus: {status} | Stage: {stage} | Elapsed: {elapsed:.1f}s")
        if active_role:
            header.append(f"\nCurrent role / preferred node: {active_role}")
        table = Table(expand=True)
        for label in ("Turn", "Agent / node", "Prompt tokens*", "Gen ms", "Graph N/E", "Python MiB"):
            table.add_column(label, justify="left" if label == "Agent / node" else "right")
        for row in self.rows:
            table.add_row(str(row.step), Text(f"{row.agent_id} / {row.node}"),
                "unknown" if row.prompt_tokens is None else str(row.prompt_tokens),
                f"{row.generation_latency_ms:.1f}", f"{row.active_nodes}/{row.active_edges}",
                "unknown" if row.process_rss_bytes is None else f"{row.process_rss_bytes / 1048576:.1f}")
        footer = Text("*Role + extraction counters from Ollama; unknown if any counter is absent.\n"
            "Graph: active semantic nodes/edges. RSS: coordinator Python only; excludes Ollama.\n"
            "p: pause/resume display (execution continues) | q / Ctrl+C: stop safely\n"
            + ("DISPLAY PAUSED" if self.paused else "Showing the last 12 committed turns."))
        return Panel(Group(header, table, footer), title=title, border_style="cyan")


class _Keyboard(AbstractContextManager):
    """Nonblocking POSIX TTY controls, restored even after cancellation/errors."""

    def __init__(self, enabled):
        self.enabled, self.saved = enabled, None

    def __enter__(self):
        if self.enabled:
            try:
                import termios
                import tty
                self.saved = termios.tcgetattr(sys.stdin.fileno())
                tty.setcbreak(sys.stdin.fileno())
            except (ImportError, OSError, ValueError):
                self.enabled = False
        return self

    def read(self):
        if self.enabled and select.select([sys.stdin], [], [], 0)[0]:
            return os.read(sys.stdin.fileno(), 1).decode("ascii", errors="ignore").lower()
        return ""

    def __exit__(self, *args):
        if self.saved is not None:
            import termios
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self.saved)


async def observe(machine, run_id, logger, *, console=None, plain=False,
                  recover_interrupted=False, keyboard=None, interval=0.5):
    """Execute and poll at 2 Hz; CSV failure stops further work, retains commits.

    Checkpoint telemetry is authoritative. Export catches up after a process
    restart without re-running completed inference. No output I/O runs inside
    the workflow transaction. Older turns are skipped, not reconstructed.
    """
    if not 0.05 <= interval <= 5:
        raise ValueError("refresh interval must be 0.05-5 seconds")
    logger.bind_run(run_id)
    console = console or Console()
    dashboard = Dashboard()
    started = asyncio.get_running_loop().time()
    after = 0
    interactive = console.is_terminal and not plain
    worker = asyncio.create_task(machine.resume(run_id, recover_interrupted=recover_interrupted))
    try:
        with _Keyboard(interactive and sys.stdin.isatty()) as terminal:
            with Live(console=console, auto_refresh=False, transient=False,
                      vertical_overflow="visible", screen=False) as live:
                while True:
                    # Repeated pages are bounded, including a resumed 1,000-turn run.
                    while True:
                        history = await machine.history(run_id, after_step=after, limit=100)
                        for checkpoint in history:
                            metrics = TelemetryProfiler.from_checkpoint(checkpoint)
                            if metrics is not None:
                                logger.append(metrics)
                                dashboard.record(metrics)
                            after = checkpoint.step
                        if len(history) < 100:
                            break
                    checkpoint = await machine.get_checkpoint(run_id)
                    key = terminal.read() if keyboard is None else keyboard()
                    if key == "p":
                        dashboard.paused = not dashboard.paused
                    if key == "q":
                        worker.cancel()
                    done = worker.done()
                    if not dashboard.paused or done or key == "p":
                        active = machine.agents[checkpoint.next_step % len(machine.agents)]
                        live.update(dashboard.render(run_id=run_id, stage=str(machine.stage),
                            status=checkpoint.status, elapsed=asyncio.get_running_loop().time() - started,
                            active_role="" if done else f"{active.agent_id} / {active.node}"), refresh=interactive)
                    if done:
                        # A turn can commit between the history read and status read.
                        # Catch up once more before final rendering/return.
                        if checkpoint.next_step > after:
                            continue
                        result = await worker
                        return result
                    await asyncio.sleep(interval)
    finally:
        if not worker.done():
            worker.cancel()
        # Drain accepted SQLite work; never leave inference or DB tasks behind.
        try:
            await worker
        except BaseException:
            pass


def demo_metrics(step):
    """Explicit synthetic data for offline dashboard/CSV verification."""
    role = ("planner", "researcher", "verifier")[(step - 1) % 3]
    return TurnMetrics(run_id="demo", step=step, governance_turn=step, agent_id=role,
        node="node2" if role == "researcher" else "node1", observed_at=2_000_000_000.0 + step,
        turn_work_ms=1500.0, agent_calls=1, extraction_calls=1, fallback_attempts=0,
        agent_prompt_tokens=120, extraction_prompt_tokens=180, prompt_tokens=300,
        generated_tokens=40, agent_latency_ms=1000.0, extraction_latency_ms=400.0,
        generation_latency_ms=1400.0, context_budget_used=0 if step == 1 else 61,
        context_budget_limit=1000, context_budget_method="utf8_upper_bound",
        active_nodes=2, active_edges=1, process_rss_bytes=32 * 1048576,
        rss_source="linux_proc_statm")


async def _run(args, console):
    if args.demo:
        dashboard = Dashboard(demo=True)
        path = args.csv or Path("data/telemetry/demo.csv")
        with CSVLogger(path) as logger:
            with Live(console=console, auto_refresh=False) as live:
                for step in range(1, 4):
                    row = demo_metrics(step)
                    logger.append(row)
                    dashboard.record(row)
                    live.update(dashboard.render(run_id="demo", stage="IDLE", status="COMPLETED" if step == 3 else "RUNNING", elapsed=float(step)),
                                refresh=console.is_terminal and not args.plain)
                    if not args.plain:
                        await asyncio.sleep(0.5)
        console.print()
        console.print("SIMULATED DEMO OK: 3 turns; no model requests or database writes")
        console.print(Text(f"CSV: {path}"))
        return
    from src.config import get_settings
    from src.network.ollama_client import OllamaClient
    from src.orchestration import StateMachine
    from src.storage.database import Database
    from src.storage.graph_store import GraphStore

    settings = get_settings()
    run_id = args.resume or uuid4().hex
    path = args.csv or Path("data/telemetry") / f"{run_id}.csv"
    # Bind before creating a workflow or scheduling any model work. A repeated
    # --task is a new run and must not append to a previous run's CSV.
    with CSVLogger(path) as logger:
        logger.bind_run(run_id)
        async with OllamaClient(settings) as client:
            async with Database.from_settings(settings) as database:
                machine = StateMachine(client, GraphStore(database))
                run = await machine.get_checkpoint(run_id) if args.resume else await machine.start(
                    args.task, initial_subgoal=args.subgoal, max_turns=args.turns, run_id=run_id)
                console.print(Text(f"RUN_ID: {run.id}"))
                console.print(Text(f"CSV: {path}"))
                if args.export_only:
                    appended, skipped = await logger.export_run(machine, run.id)
                    console.print(f"EXPORT OK: appended={appended} older_without_telemetry={skipped}")
                else:
                    done = await observe(machine, run.id, logger, console=console, plain=args.plain,
                        recover_interrupted=args.recover_interrupted)
                    console.print()
                    console.print(f"WORKFLOW OK: {done.status} turns={done.next_step}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Local DynaGraph telemetry dashboard and CSV export")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--demo", action="store_true", help="synthetic offline preview; no models or database")
    action.add_argument("--task", help="start a real task using configured local models and graph")
    action.add_argument("--resume", metavar="RUN_ID", help="resume/inspect a checkpointed run")
    parser.add_argument("--subgoal", help="initial sub-goal for a new task")
    parser.add_argument("--turns", type=int, default=None, help="new task's turn budget (1-1000)")
    parser.add_argument("--csv", type=Path, help="one-run CSV path (default: data/telemetry/<run_id>.csv)")
    parser.add_argument("--plain", action="store_true", help="print final snapshot without live terminal refresh")
    parser.add_argument("--export-only", action="store_true", help="export --resume checkpoints without inference")
    parser.add_argument("--recover-interrupted", action="store_true", help="recover --resume RUNNING only after its former process stopped")
    args = parser.parse_args(argv)
    if (args.export_only or args.recover_interrupted) and not args.resume:
        parser.error("--export-only and --recover-interrupted require --resume")
    if args.export_only and args.recover_interrupted:
        parser.error("export does not recover or modify workflow state")
    if (args.subgoal is not None or args.turns is not None) and not args.task:
        parser.error("--subgoal and --turns require --task")
    if args.turns is not None and not 1 <= args.turns <= 1000:
        parser.error("--turns must be 1-1000")
    try:
        asyncio.run(_run(args, Console()))
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("STOPPED: committed turns are retained; resume with the printed RUN_ID.")
        return 130
    except Exception as exc:
        # Keep error text literal; task/role data must not become terminal markup.
        Console(stderr=True).print(Text(f"ERROR: {type(exc).__name__}: {exc}"))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
