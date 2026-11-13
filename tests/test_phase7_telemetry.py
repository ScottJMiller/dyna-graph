"""Measured usage, durable snapshots, live display, and CSV recovery integrity."""

import asyncio
import csv
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace

import httpx
from pydantic import ValidationError
import pytest
from rich.console import Console

from src.config import Settings
from src.network.ollama_client import OllamaClient, OllamaError
from src.orchestration import StateMachine
from src.storage.database import Database
from src.storage.graph_store import GraphStore
from src.telemetry import CSVLogger, TelemetryProfiler, TurnMetrics
from src.telemetry.dashboard import Dashboard, _Keyboard, demo_metrics, main, observe
from src.telemetry.profiler import process_rss


@pytest.fixture
def settings():
    """Disable between-turn pruning so ordinary telemetry assertions isolate accounting."""
    return Settings(_env_file=None, node2_ollama_url="http://192.168.1.20:11434",
                    orchestration_prune_between_turns=False)


class Responses:
    """Simulate repairs, missing counters, fallback and blocked inference for telemetry."""

    def __init__(self, *, missing=False, repair=False, fallback=False):
        """Select fault options and start with an empty request log."""
        self.missing, self.repair, self.fallback = missing, repair, fallback
        self.calls = []
        self.gate = None

    async def __call__(self, request):
        """Return request-specific counters and faults through MockTransport."""
        body = json.loads(request.content)
        self.calls.append((request, body))
        if self.gate is not None:
            await self.gate.wait()
        if self.fallback and len(self.calls) == 1:
            return httpx.Response(503)
        extraction = body["system"].startswith("Extract explicitly")
        value = {"triples": [{"subject": "Project Cedar", "predicate": "uses", "object": "SQLite"}]} if extraction else {
            "content": "Project Cedar uses SQLite.", "next_subgoal": "Project Cedar", "complete": False}
        if self.repair and len(self.calls) == 1:
            value = {"wrong_schema": True}
        counters = {} if self.missing else {"prompt_eval_count": 180 if extraction else 120, "eval_count": 20}
        return httpx.Response(200, json={"model": body["model"], "response": json.dumps(value),
            "done": True, "done_reason": "stop", "total_duration": 10_000_000, **counters})


async def run_fixture(tmp_path, settings, callback, *, responses=None, profiler=None):
    """Run a telemetry callback within the lifetime of its mock client and storage."""
    responses = responses or Responses()
    async with OllamaClient(settings, transport=httpx.MockTransport(responses)) as client:
        async with Database(tmp_path / "telemetry.db") as database:
            graph = GraphStore(database)
            machine = StateMachine(client, graph, profiler=profiler, clock=lambda: 2_000_000_000.0)
            return await callback(machine, graph, responses)


# Measured counters and telemetry contracts

@pytest.mark.parametrize("value", [0, 1, 128, None])
def test_client_preserves_measured_prompt_count(settings, value):
    async def scenario():
        def response(request):
            return httpx.Response(200, json={"model": "local", "response": "OK", "done": True,
                **({} if value is None else {"prompt_eval_count": value})})
        async with OllamaClient(settings, transport=httpx.MockTransport(response)) as client:
            assert (await client.generate("hello")).prompt_tokens == value
    asyncio.run(scenario())


@pytest.mark.parametrize("value", [-1, True, "4", 1.5])
def test_client_rejects_invalid_prompt_counter(settings, value):
    async def scenario():
        def response(request):
            return httpx.Response(200, json={"model": "local", "response": "OK", "done": True, "prompt_eval_count": value})
        async with OllamaClient(settings, transport=httpx.MockTransport(response)) as client:
            with pytest.raises(OllamaError, match="protocol"):
                await client.generate("hello")
    asyncio.run(scenario())


@pytest.mark.parametrize("field,value", [
    ("step", 0), ("step", 1001), ("governance_turn", 0), ("agent_calls", -1),
    ("active_nodes", True), ("active_edges", -1), ("prompt_tokens", 301),
    ("context_budget_used", 1001), ("context_budget_limit", 1001),
    ("turn_work_ms", -0.1), ("generation_latency_ms", 100.0),
    ("agent_latency_ms", float("nan")), ("observed_at", float("inf")),
    ("process_rss_bytes", None), ("rss_source", "unavailable"), ("node", "cloud"),
    ("schema_version", 2), ("run_id", "bad/run"), ("unexpected", 1),
])
def test_metric_schema_rejects_invalid_measurements(field, value):
    with pytest.raises(ValidationError):
        TurnMetrics.model_validate(demo_metrics(1).model_dump() | {field: value})


def test_missing_prompt_counts_are_unknown_not_context_bytes(tmp_path, settings):
    async def scenario(machine, graph, responses):
        run = await machine.start("Project Cedar uses SQLite.", max_turns=2)
        await machine.resume(run.id)
        history = await machine.history(run.id)
        metrics = TelemetryProfiler.from_checkpoint(history[1])
        assert metrics.prompt_tokens is None and metrics.generated_tokens is None
        assert metrics.agent_prompt_tokens is None and metrics.extraction_prompt_tokens is None
        assert metrics.context_budget_used > 0 and metrics.context_budget_method == "utf8_upper_bound"
        with CSVLogger(tmp_path / "unknown.csv") as logger:
            await logger.export_run(machine, run.id)
        rows = list(csv.DictReader((tmp_path / "unknown.csv").open()))
        assert rows[1]["prompt_tokens"] == "" and int(rows[1]["context_budget_used"]) > 0
    asyncio.run(run_fixture(tmp_path, settings, scenario, responses=Responses(missing=True)))


@pytest.mark.parametrize("repair,fallback,expected_prompts,expected_calls", [
    (False, False, 300, 1), (True, False, 420, 2), (False, True, 300, 1),
])
def test_committed_metrics_cover_repairs_fallback_and_actual_graph(tmp_path, settings, repair, fallback, expected_prompts, expected_calls):
    async def scenario(machine, graph, responses):
        run = await machine.start("Project Cedar uses SQLite.", max_turns=1)
        turn = await machine.step(run.id)
        metrics = TelemetryProfiler.from_checkpoint(turn.checkpoint)
        assert metrics.prompt_tokens == expected_prompts
        assert (metrics.agent_calls, metrics.extraction_calls) == (expected_calls, 1)
        assert metrics.generated_tokens == (expected_calls + 1) * 20
        assert metrics.fallback_attempts == int(fallback)
        assert metrics.active_nodes == 2 and metrics.active_edges == 1
        assert metrics.turn_work_ms >= metrics.generation_latency_ms >= 0
        assert metrics.node == ("node2" if fallback else "node1")
        summary = turn.checkpoint.result["agent_generations"][0]
        assert summary["prompt_tokens"] == 120 and summary["server_duration_ms"] == 10
        assert len(summary["failed_attempts"]) == int(fallback)
        with CSVLogger(tmp_path / "cycle.csv") as logger:
            assert await logger.export_run(machine, run.id) == (1, 0)
            assert await logger.export_run(machine, run.id) == (0, 0)
        print(f"TELEMETRY OK: prompt_tokens={metrics.prompt_tokens} active_nodes=2 active_edges=1")
    asyncio.run(run_fixture(tmp_path, settings, scenario, responses=Responses(repair=repair, fallback=fallback)))


def test_counts_exclude_inactive_endpoints_and_provenance(tmp_path, settings):
    async def scenario(machine, graph, responses):
        a, b, c = (graph.get_or_create_entity(n) for n in ("A", "B", "C"))
        graph.create_triple(a.id, "uses", b.id)
        graph.create_triple(b.id, "uses", c.id)
        with graph.database.transaction() as connection:
            connection.execute("UPDATE entities SET status='PRUNED' WHERE id=?", (c.id,))
            connection.execute("UPDATE triples SET status='SUPERSEDED' WHERE subject_id=?", (a.id,))
        run = await machine.start("Project Cedar uses SQLite.", max_turns=1)
        metrics = TelemetryProfiler.from_checkpoint((await machine.step(run.id)).checkpoint)
        assert metrics.active_nodes == 4 and metrics.active_edges == 1
    asyncio.run(run_fixture(tmp_path, settings, scenario))


def test_metrics_and_graph_rollback_together(tmp_path, settings):
    async def scenario(machine, graph, responses):
        with graph.database.transaction() as connection:
            connection.execute(
                "CREATE TRIGGER fail_checkpoint BEFORE INSERT ON workflow_turns BEGIN SELECT RAISE(ABORT,'forced rollback'); END",
            )
        run = await machine.start("Project Cedar uses SQLite.", max_turns=1)
        with pytest.raises(Exception, match="forced rollback"):
            await machine.step(run.id)
        assert not graph.list_entities() and not await machine.history(run.id)
        with CSVLogger(tmp_path / "rollback.csv") as logger:
            assert await logger.export_run(machine, run.id) == (0, 0)
        assert not list(csv.DictReader((tmp_path / "rollback.csv").open()))
        print("ROLLBACK OK: no metric row or graph changes escaped the failed checkpoint")
    asyncio.run(run_fixture(tmp_path, settings, scenario))


def test_reopen_exports_saved_metrics_without_model_requests(tmp_path, settings):
    async def scenario(machine, graph, responses):
        run = await machine.start("Project Cedar uses SQLite.", max_turns=3, run_id="restart")
        await machine.resume(run.id)
        with CSVLogger(tmp_path / "restart.csv") as logger:
            logger.append(TelemetryProfiler.from_checkpoint((await machine.history(run.id))[0]))
    asyncio.run(run_fixture(tmp_path, settings, scenario))

    async def reopened(machine, graph, responses):
        with CSVLogger(tmp_path / "restart.csv") as logger:
            assert await logger.export_run(machine, "restart") == (2, 0)
            assert await logger.export_run(machine, "restart") == (0, 0)
        assert not responses.calls
        assert len(list(csv.DictReader((tmp_path / "restart.csv").open()))) == 3
        print("CSV RECOVERY OK: 3 unique turns; no repeated inference")
    asyncio.run(run_fixture(tmp_path, settings, reopened))


def test_old_checkpoints_are_skipped_without_inventing_metrics(tmp_path, settings):
    async def scenario(machine, graph, responses):
        run = await machine.start("Project Cedar uses SQLite.", max_turns=1)
        await machine.resume(run.id)
        with graph.database.transaction() as connection:
            connection.execute("UPDATE workflow_turns SET result=json_remove(result,'$.telemetry')")
        with CSVLogger(tmp_path / "old.csv") as logger:
            assert await logger.export_run(machine, run.id) == (0, 1)
    asyncio.run(run_fixture(tmp_path, settings, scenario))


@pytest.mark.parametrize(
    "field,value",
    [("run_id", "other"), ("step", 2), ("node", "node2"), ("agent_id", "other"), ("governance_turn", 2)],
)
def test_checkpoint_identity_must_match_metrics(field, value):
    row = demo_metrics(1)
    checkpoint = SimpleNamespace(run_id=row.run_id, step=row.step, governance_turn=row.governance_turn,
                                agent_id=row.agent_id, node=row.node, result={"telemetry": row.model_dump() | {field: value}})
    with pytest.raises(ValueError, match="identity"):
        TelemetryProfiler.from_checkpoint(checkpoint)


def test_csv_quotes_labels_and_flushes_before_close(tmp_path):
    path = tmp_path / "nested/metrics.csv"
    row = demo_metrics(1).model_copy(update={"agent_id": 'role, "review"\nline'})
    with CSVLogger(path) as logger:
        assert logger.append(row)
        raw = path.read_text()
        assert '"role, ""review""' in raw
        assert list(csv.DictReader(io.StringIO(raw)))[0]["agent_id"] == row.agent_id
        assert "content" not in CSVLogger.columns and "response" not in CSVLogger.columns
    with CSVLogger(path) as logger:
        assert not logger.append(row)


def test_csv_rejects_conflicting_replay_other_run_and_out_of_order(tmp_path):
    with CSVLogger(tmp_path / "metrics.csv") as logger:
        logger.append(demo_metrics(2))
        with pytest.raises(ValueError, match="increase"):
            logger.append(demo_metrics(1))
        with pytest.raises(ValueError, match="conflicting"):
            logger.append(demo_metrics(2).model_copy(update={"active_nodes": 99}))
        with pytest.raises(ValueError, match="one run"):
            logger.append(demo_metrics(3).model_copy(update={"run_id": "different"}))


@pytest.mark.parametrize(
    "mutation",
    ["header", "partial", "duplicate", "unknown", "negative", "other_run", "reorder", "missing_newline", "extra"],
)
def test_csv_reopen_refuses_corruption_without_modifying_file(tmp_path, mutation):
    path = tmp_path / "metrics.csv"
    with CSVLogger(path) as logger:
        for step in (1, 2):
            logger.append(demo_metrics(step))
    lines = path.read_text().splitlines(keepends=True)
    if mutation == "header":
        lines[0] = "wrong,header\n"
    if mutation == "partial":
        lines[-1] = "demo,2\n"
    if mutation == "duplicate":
        lines.append(lines[-1])
    if mutation == "unknown":
        lines[-1] = lines[-1].replace("utf8_upper_bound", "unknown")
    if mutation == "negative":
        lines[-1] = lines[-1].replace(",300,", ",-300,")
    if mutation == "other_run":
        lines[-1] = lines[-1].replace("demo", "other")
    if mutation == "reorder":
        lines[1], lines[2] = lines[2], lines[1]
    if mutation == "missing_newline":
        lines[-1] = lines[-1].rstrip("\r\n")
    if mutation == "extra":
        lines[-1] = lines[-1].rstrip("\r\n") + ",extra\n"
    path.write_text("".join(lines))
    before = path.read_bytes()
    with pytest.raises((ValueError, csv.Error)):
        with CSVLogger(path):
            pass
    assert path.read_bytes() == before


def test_csv_requires_context_and_releases_file(tmp_path):
    logger = CSVLogger(tmp_path / "metrics.csv")
    with pytest.raises(RuntimeError):
        logger.append(demo_metrics(1))
    with logger:
        logger.append(demo_metrics(1))
        with pytest.raises(RuntimeError):
            logger.__enter__()
    assert logger._file is None
    with pytest.raises(RuntimeError):
        logger.append(demo_metrics(2))


def test_csv_binding_rejects_another_run_before_append(tmp_path):
    path = tmp_path / "existing.csv"
    with CSVLogger(path) as logger:
        logger.append(demo_metrics(1))
    before = path.read_bytes()
    with CSVLogger(path) as logger:
        logger.bind_run("demo")
        with pytest.raises(ValueError, match="belongs to run demo"):
            logger.bind_run("new-run")
    assert path.read_bytes() == before


def test_observer_rejects_reused_csv_before_inference(tmp_path, settings):
    path = tmp_path / "existing.csv"
    with CSVLogger(path) as logger:
        logger.append(demo_metrics(1))
    before = path.read_bytes()

    async def scenario(machine, graph, responses):
        run = await machine.start("Project Cedar uses SQLite.", max_turns=1)
        with CSVLogger(path) as logger:
            with pytest.raises(ValueError, match="belongs to run demo"):
                await observe(machine, run.id, logger, plain=True, console=Console(file=io.StringIO()))
        assert not responses.calls
        assert (await machine.get_checkpoint(run.id)).status == "READY"
        assert not await machine.history(run.id)
        assert path.read_bytes() == before
        print("CSV PREFLIGHT OK: reused CSV rejected before inference; existing data preserved")
    asyncio.run(run_fixture(tmp_path, settings, scenario))


def test_export_rejects_other_run_even_without_turns(tmp_path, settings):
    path = tmp_path / "existing.csv"
    with CSVLogger(path) as logger:
        logger.append(demo_metrics(1))

    async def scenario(machine, graph, responses):
        run = await machine.start("Project Cedar uses SQLite.", max_turns=1)
        with CSVLogger(path) as logger:
            with pytest.raises(ValueError, match="belongs to run demo"):
                await logger.export_run(machine, run.id)
        assert not responses.calls
    asyncio.run(run_fixture(tmp_path, settings, scenario))


def test_cli_reused_csv_does_not_create_a_run_or_request_models(tmp_path, settings, monkeypatch, capsys):
    from src import config
    from src.network import ollama_client
    responses = Responses()
    path = tmp_path / "existing.csv"
    with CSVLogger(path) as logger:
        logger.append(demo_metrics(1))
    before = path.read_bytes()
    database = tmp_path / "cli.db"
    monkeypatch.setattr(config, "get_settings", lambda: settings.model_copy(update={"database_path": database}))
    monkeypatch.setattr(
        ollama_client,
        "OllamaClient",
        lambda s: OllamaClient(s, transport=httpx.MockTransport(responses)),
    )
    assert main(["--task", "Project Cedar uses SQLite.", "--csv", str(path), "--plain"]) == 1
    error = capsys.readouterr().err
    assert "belongs to run demo" in error and "--resume demo" in error
    assert not database.exists() and not responses.calls
    assert path.read_bytes() == before


def test_cli_repeated_tasks_use_separate_default_csv_files(tmp_path, settings, monkeypatch):
    from src import config
    from src.network import ollama_client
    responses = Responses()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        config,
        "get_settings",
        lambda: settings.model_copy(update={"database_path": tmp_path / "cli.db"}),
    )
    monkeypatch.setattr(
        ollama_client,
        "OllamaClient",
        lambda s: OllamaClient(s, transport=httpx.MockTransport(responses)),
    )
    arguments = ["--task", "Project Cedar uses SQLite.", "--turns", "1", "--plain"]
    assert main(arguments) == 0
    assert main(arguments) == 0
    files = list((tmp_path / "data/telemetry").glob("*.csv"))
    assert len(files) == 2
    run_ids = set()
    for path in files:
        with path.open(newline="") as stream:
            rows = list(csv.DictReader(stream))
        assert len(rows) == 1 and rows[0]["run_id"] == path.stem
        run_ids.add(rows[0]["run_id"])
    assert len(run_ids) == 2


# Interactive display and durable export

@pytest.mark.parametrize("resume_display", [False, True])
def test_commits_while_display_paused_keep_the_same_csv_run(tmp_path, settings, monkeypatch, resume_display):
    """Display pause must not change CSV ownership or stop committed-turn export."""
    recorded_while_paused = []
    original = Dashboard.record

    def record(self, metrics):
        recorded_while_paused.append(self.paused)
        original(self, metrics)
    monkeypatch.setattr(Dashboard, "record", record)

    async def scenario(machine, graph, responses):
        responses.gate = asyncio.Event()
        run = await machine.start("Project Cedar uses SQLite.", max_turns=3)
        calls = 0

        def keyboard():
            nonlocal calls
            calls += 1
            if calls == 2:
                return "p"
            if calls == 3:
                responses.gate.set()
            if calls == 4 and resume_display:
                return "p"
            return ""
        with CSVLogger(tmp_path / "paused.csv") as logger:
            done = await observe(machine, run.id, logger, plain=True, console=Console(file=io.StringIO()),
                                 keyboard=keyboard, interval=0.05)
            assert logger.run_id == run.id and len(logger._rows) == 3
        assert done.status == "COMPLETED" and any(recorded_while_paused)
        print("PAUSE OK: committed turns keep logging to the same run")
    asyncio.run(run_fixture(tmp_path, settings, scenario))


def test_process_rss_current_linux_or_unknown():
    value, source = process_rss()
    assert (value is None and source == "unavailable") or (value > 0 and source == "linux_proc_statm")


@pytest.mark.parametrize("exception", [OSError(), ValueError(), IndexError()])
def test_process_rss_unavailable_is_explicit(monkeypatch, exception):
    def fail(*args, **kwargs):
        raise exception
    monkeypatch.setattr(Path, "read_text", fail)
    assert process_rss() == (None, "unavailable")


def test_dashboard_history_is_bounded_and_markup_is_literal():
    dashboard = Dashboard(demo=True)
    for step in range(1, 30):
        dashboard.record(demo_metrics(step))
    assert len(dashboard.rows) == 12 and dashboard.rows[0].step == 18
    console = Console(file=io.StringIO(), width=140, color_system=None)
    console.print(dashboard.render(run_id="[red]literal[/red]", stage="GENERATE", status="RUNNING", elapsed=1))
    assert "[red]literal[/red]" in console.file.getvalue()
    assert "SIMULATED DEMO" in console.file.getvalue()
    with pytest.raises(ValueError):
        dashboard.record(demo_metrics(29))


def test_observer_logs_while_execution_runs_and_then_catches_up(tmp_path, settings):
    async def scenario(machine, graph, responses):
        run = await machine.start("Project Cedar uses SQLite.", max_turns=3)
        exported = asyncio.Event()
        original = machine.agents[1].execute

        async def wait_for_first_export(*args, **kwargs):
            # Second role cannot finish until the first CSV row is visible.
            await asyncio.wait_for(exported.wait(), timeout=2)
            return await original(*args, **kwargs)
        machine.agents[1].execute = wait_for_first_export
        output = io.StringIO()
        class LiveLogger(CSVLogger):
            def append(self, metrics):
                changed = super().append(metrics)
                if metrics.step == 1:
                    assert machine.stage == "GENERATE"
                    assert len(list(csv.DictReader(self.path.open()))) == 1
                    exported.set()
                return changed
        with LiveLogger(tmp_path / "observe.csv") as logger:
            done = await observe(machine, run.id, logger, plain=True,
                console=Console(file=output, width=140, color_system=None), interval=0.05)
            assert len(logger._rows) == 3
        assert done.status == "COMPLETED" and "COMPLETED" in output.getvalue()
        assert "Python MiB" in output.getvalue()
        print("DASHBOARD OK: 3 committed rows exported; final status COMPLETED")
    asyncio.run(run_fixture(tmp_path, settings, scenario))


@pytest.mark.parametrize("missing_stage", ["agent", "extraction"])
def test_partial_missing_prompt_count_does_not_report_partial_sum(tmp_path, settings, missing_stage):
    responses = Responses()

    async def partial(request):
        response = await responses(request)
        data = response.json()
        extraction = json.loads(request.content)["system"].startswith("Extract explicitly")
        if extraction == (missing_stage == "extraction"):
            data.pop("prompt_eval_count")
        return httpx.Response(200, json=data)

    async def scenario(machine, graph, transport):
        run = await machine.start("Project Cedar uses SQLite.", max_turns=1)
        metrics = TelemetryProfiler.from_checkpoint((await machine.step(run.id)).checkpoint)
        assert metrics.prompt_tokens is None
        assert (metrics.agent_prompt_tokens is None) == (missing_stage == "agent")
        assert (metrics.extraction_prompt_tokens is None) == (missing_stage == "extraction")
    asyncio.run(run_fixture(tmp_path, settings, scenario, responses=partial))


def test_extraction_correction_is_included_in_turn_usage(tmp_path, settings):
    responses = Responses()

    async def correction(request):
        response = await responses(request)
        data = response.json()
        if len(responses.calls) == 2:
            data["response"] = '{"invalid":true}'
        return httpx.Response(200, json=data)

    async def scenario(machine, graph, transport):
        run = await machine.start("Project Cedar uses SQLite.", max_turns=1)
        metrics = TelemetryProfiler.from_checkpoint((await machine.step(run.id)).checkpoint)
        assert metrics.extraction_calls == 2 and metrics.extraction_prompt_tokens == 360
        assert metrics.prompt_tokens == 480 and metrics.generated_tokens == 60
    asyncio.run(run_fixture(tmp_path, settings, scenario, responses=correction))


def test_csv_revalidates_modified_metric_instances(tmp_path):
    with CSVLogger(tmp_path / "invalid.csv") as logger:
        with pytest.raises(ValidationError):
            logger.append(demo_metrics(1).model_copy(update={"prompt_tokens": -1}))


def test_profiler_reports_unknown_rss_and_uses_injected_timer(tmp_path, settings):
    ticks = iter((10.0, 11.5))
    profiler = TelemetryProfiler(timer=lambda: next(ticks), rss_reader=lambda: (None, "unavailable"))

    async def scenario(machine, graph, responses):
        run = await machine.start("Project Cedar uses SQLite.", max_turns=1)
        metrics = TelemetryProfiler.from_checkpoint((await machine.step(run.id)).checkpoint)
        assert metrics.turn_work_ms == 1500.0
        assert metrics.process_rss_bytes is None and metrics.rss_source == "unavailable"
    asyncio.run(run_fixture(tmp_path, settings, scenario, profiler=profiler))


def test_observer_stage_and_pause_controls_during_blocked_inference(tmp_path, settings, monkeypatch):
    seen = []
    original = Dashboard.render

    def render(self, **kwargs):
        seen.append(kwargs["stage"])
        return original(self, **kwargs)
    monkeypatch.setattr(Dashboard, "render", render)

    async def scenario(machine, graph, responses):
        responses.gate = asyncio.Event()
        run = await machine.start("Project Cedar uses SQLite.", max_turns=1)
        calls = 0

        def keyboard():
            nonlocal calls
            calls += 1
            if calls == 3:
                return "p"
            if calls == 4:
                return "p"
            if calls >= 5:
                responses.gate.set()
            return ""
        with CSVLogger(tmp_path / "controls.csv") as logger:
            await observe(machine, run.id, logger, console=Console(file=io.StringIO()), plain=True,
                          keyboard=keyboard, interval=0.05)
        assert "GENERATE" in seen and calls >= 5
    asyncio.run(run_fixture(tmp_path, settings, scenario))


def test_observer_quit_cancels_inference_and_retains_run(tmp_path, settings):
    async def scenario(machine, graph, responses):
        responses.gate = asyncio.Event()
        run = await machine.start("Project Cedar uses SQLite.", max_turns=1)

        def keyboard():
            return "q" if responses.calls else ""
        with CSVLogger(tmp_path / "quit.csv") as logger:
            with pytest.raises(asyncio.CancelledError):
                await observe(machine, run.id, logger, console=Console(file=io.StringIO()), plain=True,
                              keyboard=keyboard, interval=0.05)
        saved = await machine.get_checkpoint(run.id)
        assert saved.status == "CANCELLED" and saved.next_step == 0
        assert not await machine.history(run.id)
    asyncio.run(run_fixture(tmp_path, settings, scenario))


def test_csv_failure_cannot_undo_committed_turn(tmp_path, settings):
    async def scenario(machine, graph, responses):
        run = await machine.start("Project Cedar uses SQLite.", max_turns=1)
        class BrokenLogger:
            def bind_run(self, run_id):
                pass

            def append(self, metrics):
                raise OSError("simulated full disk")
        with pytest.raises(OSError, match="full disk"):
            await observe(machine, run.id, BrokenLogger(), console=Console(file=io.StringIO()), plain=True, interval=0.05)
        assert (await machine.get_checkpoint(run.id)).status == "COMPLETED"
        assert TelemetryProfiler.from_checkpoint((await machine.history(run.id))[0]).prompt_tokens == 300
        with CSVLogger(tmp_path / "recovered.csv") as logger:
            assert await logger.export_run(machine, run.id) == (1, 0)
    asyncio.run(run_fixture(tmp_path, settings, scenario))


def test_keyboard_disabled_without_tty():
    with _Keyboard(False) as keyboard:
        assert keyboard.read() == ""


@pytest.mark.skipif(os.name != "posix", reason="POSIX terminal controls")
def test_real_tty_pause_quit_and_terminal_mode_restoration(tmp_path):
    import pty
    import select
    import subprocess
    import sys
    import termios
    import time

    # Exercise actual cbreak input and Rich output, not only a mocked key source.
    script = tmp_path / "terminal_check.py"
    script.write_text('''import asyncio, sys
from rich.console import Console
from src.config import Settings
from src.network.ollama_client import OllamaClient
from src.orchestration import StateMachine
from src.storage.database import Database
from src.storage.graph_store import GraphStore
from src.telemetry import CSVLogger
from src.telemetry.dashboard import observe
import httpx
async def blocked(request):
    await asyncio.Event().wait()
async def main():
    settings = Settings(_env_file=None, node2_ollama_url="http://192.168.1.20:11434")
    async with OllamaClient(settings, transport=httpx.MockTransport(blocked)) as client:
        async with Database(sys.argv[1]) as database:
            machine = StateMachine(client, GraphStore(database))
            run = await machine.start("Project Cedar uses SQLite.", max_turns=1)
            with CSVLogger(sys.argv[2]) as logger:
                try:
                    await observe(machine, run.id, logger, console=Console(width=120), interval=0.05)
                except asyncio.CancelledError:
                    pass
            saved = await machine.get_checkpoint(run.id)
            print("TERMINAL CHECK:", saved.status, flush=True)
asyncio.run(main())
''')
    master, slave = pty.openpty()
    before = termios.tcgetattr(slave)
    environment = os.environ | {"PYTHONPATH": str(Path(__file__).resolve().parents[1]), "TERM": "xterm"}
    process = subprocess.Popen([sys.executable, "-B", str(script), str(tmp_path / "tty.db"), str(tmp_path / "tty.csv")],
        stdin=slave, stdout=slave, stderr=slave, env=environment)
    output = bytearray()

    def until(text):
        deadline = time.monotonic() + 8
        while text not in output and time.monotonic() < deadline:
            if select.select([master], [], [], 0.1)[0]:
                output.extend(os.read(master, 65536))
        assert text in output, output.decode(errors="replace")
    try:
        until(b"GENERATE")
        os.write(master, b"p")
        until(b"DISPLAY PAUSED")
        os.write(master, b"p")
        os.write(master, b"q")
        until(b"TERMINAL CHECK: CANCELLED")
        assert process.wait(timeout=3) == 0
        assert termios.tcgetattr(slave) == before
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        os.close(master)
        os.close(slave)


@pytest.mark.parametrize("arguments", [[], ["--demo", "--turns", "3"], ["--demo", "--export-only"],
    ["--task", "hello", "--turns", "0"], ["--task", "hello", "--resume", "test"],
    ["--resume", "test", "--export-only", "--recover-interrupted"]])
def test_cli_invalid_options_exit_without_models(arguments):
    with pytest.raises(SystemExit) as exc:
        main(arguments)
    assert exc.value.code == 2


def test_cli_offline_demo_generates_three_idempotent_rows(tmp_path, capsys):
    path = tmp_path / "demo.csv"
    arguments = ["--demo", "--plain", "--csv", str(path)]
    assert main(arguments) == 0
    assert main(arguments) == 0
    rows = list(csv.DictReader(path.open()))
    assert len(rows) == 3 and all(row["prompt_tokens"] == "300" for row in rows)
    assert "SIMULATED DEMO OK: 3 turns; no model requests or database writes" in capsys.readouterr().out


# Opt-in live LAN acceptance (skipped by default)

@pytest.mark.lan
@pytest.mark.both_nodes
def test_live_two_node_turn_exports_measured_usage(tmp_path):
    async def scenario():
        settings = Settings(orchestration_allow_fallback=False)
        async with OllamaClient(settings) as client:
            for node in ("node1", "node2"):
                inventory = await client.list_models(node)
                matching = [m for m in inventory.models if m.name == settings.node_model(node)]
                assert matching and matching[0].quantization_level == "Q4_K_M"
            async with Database(tmp_path / "live.db") as database:
                machine = StateMachine(client, GraphStore(database))
                run = await machine.start(
                    "Repeat this fact exactly: Project Cedar uses SQLite.",
                    initial_subgoal="Project Cedar",
                    max_turns=1,
                )
                turn = await machine.step(run.id)
                metrics = TelemetryProfiler.from_checkpoint(turn.checkpoint)
                assert metrics.prompt_tokens is not None and metrics.prompt_tokens > 0
                assert metrics.agent_prompt_tokens > 0 and metrics.extraction_prompt_tokens > 0
                assert metrics.generation_latency_ms > 0 and metrics.fallback_attempts == 0
                assert turn.agent.generation.node == "node1" and turn.extraction.generation.node == "node2"
                with CSVLogger(tmp_path / "live.csv") as logger:
                    assert await logger.export_run(machine, run.id) == (1, 0)
                with (tmp_path / "live.csv").open(newline="") as stream:
                    row = next(csv.DictReader(stream))
                assert int(row["prompt_tokens"]) == metrics.prompt_tokens
                print(f"LIVE TELEMETRY OK: role=node1 extraction=node2 prompt_tokens={metrics.prompt_tokens} "
                      f"generation_ms={metrics.generation_latency_ms:.1f} CSV_rows=1")
    asyncio.run(scenario())
