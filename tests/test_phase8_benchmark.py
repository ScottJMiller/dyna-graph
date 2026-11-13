"""Synthetic-only pipeline verification; no physical-node experiment runs here."""

import asyncio
import csv
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys

import httpx
from pydantic import ValidationError
import pytest

from experiments import analyze_results, benchmark_harness, generate_plots
from experiments.benchmark_harness import run_experiment
from experiments.common import linear_context
from experiments.contracts import BenchmarkPlan, BenchmarkRow, load_json, sha256, write_json
from experiments.scenario import ScenarioTurn, scenario, scenario_hash, scenario_records, score
from src.config import Settings
from src.network.ollama_client import OllamaClient
from src.orchestration import StateMachine
from src.storage.database import Database
from src.storage.graph_store import GraphStore


@pytest.fixture(scope="module", autouse=True)
def no_real_network():
    patch = pytest.MonkeyPatch()
    async def blocked(*args, **kwargs):
        raise AssertionError("Phase 8 verification must never use real network requests")
    patch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", blocked)
    yield
    patch.undo()


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    names = {name.upper() for name in Settings.model_fields}
    for field in Settings.model_fields.values():
        if field.validation_alias is not None:
            names.update(alias.upper() for alias in field.validation_alias.choices)
    for name in list(os.environ):
        if name.upper() in names:
            monkeypatch.delenv(name)


def configured(path):
    return Settings(_env_file=None, node2_ollama_url="http://192.168.1.20:11434", database_path=path / "application.db")


class SyntheticOllama:
    """Read only the provided prompt/memory, never the scenario oracle."""

    def __init__(self, settings, *, missing=False, fail_at=None, wrong_model=False, inventory_change=False, invalid_first=False):
        self.settings, self.missing, self.fail_at = settings, missing, fail_at
        self.wrong_model, self.inventory_change = wrong_model, inventory_change
        self.invalid_first = invalid_first
        self.posts, self.requests, self.inventories = [], [], 0

    async def __call__(self, request):
        self.requests.append(request)
        node = "node1" if request.url.host == "localhost" else "node2"
        model = self.settings.node_model(node)
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "synthetic-only"})
        if request.url.path == "/api/tags":
            self.inventories += 1
            digest = "b" if self.inventory_change and self.inventories > 2 else "a"
            return httpx.Response(200, json={"models": [{"name": model, "size": 1000,
                "digest": digest * 64, "details": {"quantization_level": "Q4_K_M"}}]})
        body = json.loads(request.content)
        self.posts.append(body)
        if self.fail_at == len(self.posts):
            raise httpx.ReadTimeout("synthetic failure", request=request)
        if "system" not in body:
            output = "OK"
        elif body["system"].startswith("Extract explicitly"):
            message = json.JSONDecoder().raw_decode(body["prompt"].split("\n", 1)[1])[0]["message"]
            facts = re.findall(r"(Project [A-Za-z]+) uses ([A-Za-z0-9]+)\.", message)
            output = json.dumps({"triples": [{"subject": s, "predicate": "uses", "object": o} for s, o in facts]})
        else:
            task = json.JSONDecoder().raw_decode(body["prompt"].split("TASK\n", 1)[1])[0]
            memory = body["prompt"].split("\nMEMORY\n", 1)[1]
            known = {}
            if memory.startswith("["):
                for entry in json.loads(memory):
                    for text in (entry["task"], entry["answer"]):
                        known.update(re.findall(r"(Project [A-Za-z]+) uses ([A-Za-z0-9]+)\.", text))
            else:
                known.update(re.findall(r'"(Project [A-Za-z]+)" -- "uses" --> "([A-Za-z0-9]+)"', memory))
            observation = task.split("\n", 1)[0]
            known.update(re.findall(r"(Project [A-Za-z]+) uses ([A-Za-z0-9]+)\.", observation))
            subject = re.search(r"Which database does (Project [A-Za-z]+) use now", task)[1]
            answer = f"{subject} uses {known[subject]}." if subject in known else "I do not know."
            output = json.dumps({"content": answer, "next_subgoal": None, "complete": False})
        # Invented counters are confined to explicitly labeled synthetic tests.
        counters = {} if self.missing else {"prompt_eval_count": 100 + len(body["prompt"]) // 4, "eval_count": 20}
        if self.invalid_first and len(self.posts) == 1:
            output = '{"invalid":true}'
        return httpx.Response(200, json={"model": "wrong-model" if self.wrong_model else model,
            "response": output, "done": True, "done_reason": "stop", "total_duration": 1_000_000, **counters})


def make_bundle(path, *, turns=20, repetitions=1, **fake_options):
    settings = configured(path)
    fake = SyntheticOllama(settings, **fake_options)
    plan = BenchmarkPlan(turns=turns, repetitions=repetitions, warmups_per_node=0)
    output = path / "bundle"
    manifest = asyncio.run(run_experiment(plan, settings, output, execute=True, transport=httpx.MockTransport(fake)))
    return output, manifest, fake


@pytest.fixture(scope="module")
def evidence(tmp_path_factory):
    return make_bundle(tmp_path_factory.mktemp("synthetic-phase8"), repetitions=2)


def test_default_cli_only_plans_without_settings_models_or_output(tmp_path, monkeypatch, capsys):
    from src import config
    def forbidden(): raise AssertionError("plan-only must not load deployment configuration")
    monkeypatch.setattr(config, "get_settings", forbidden)
    output = tmp_path / "must-not-exist"
    assert benchmark_harness.main(["--output", str(output)]) == 0
    assert not output.exists()
    assert "PLAN ONLY: 4 condition runs; 80 scored turns; no model requests or experiment files" in capsys.readouterr().out


def test_api_requires_explicit_execution_before_any_side_effect(tmp_path):
    with pytest.raises(PermissionError, match="execute=True"):
        asyncio.run(run_experiment(BenchmarkPlan(), configured(tmp_path), tmp_path / "none"))
    assert not (tmp_path / "none").exists()


def test_cli_execution_requires_new_output_argument():
    with pytest.raises(SystemExit) as exc:
        benchmark_harness.main(["--execute"])
    assert exc.value.code == 2


@pytest.mark.parametrize("kwargs", [{"turns": 14}, {"turns": 21}, {"turns": True}, {"repetitions": 0},
    {"repetitions": 11}, {"memory_budget": 127}, {"memory_budget": 1001}, {"max_tokens": 63},
    {"max_tokens": 257}, {"warmups_per_node": -1}, {"warmups_per_node": 4}, {"version": 2}])
def test_invalid_plans_fail_before_execution(kwargs):
    with pytest.raises(ValidationError): BenchmarkPlan(**kwargs)


@pytest.mark.parametrize("turns", [15, 16, 17, 18, 19, 20])
def test_scenario_is_bounded_versioned_and_has_updates_and_reversions(turns):
    items = scenario(turns)
    assert len(items) == turns and len(scenario_hash(turns)) == 64
    assert [t.step for t in items] == list(range(1, turns + 1))
    assert items[4].expected == "Project Cedar uses PostgreSQL."
    assert items[10].expected == "Project Cedar uses SQLite."
    assert any(t.update_probe and t.delayed_probe for t in items)
    assert all(len(t.task) <= 500 and t.subject in t.task for t in items)


@pytest.mark.parametrize("value", [True, 0, 14, 21, "20"])
def test_scenario_rejects_invalid_lengths(value):
    with pytest.raises(ValueError): scenario(value)


def test_rendered_tasks_are_placeholder_free_and_part_of_fingerprint(monkeypatch):
    items = scenario(20)
    assert all("<current database>" not in t.task and "<" not in t.task and ">" not in t.task for t in items)
    assert [record["task"] for record in scenario_records(20)] == [t.task for t in items]
    original_hash = scenario_hash(20)
    original = ScenarioTurn.task.fget
    monkeypatch.setattr(ScenarioTurn, "task", property(lambda self: original(self) + " Revised framing."))
    assert scenario_hash(20) != original_hash


@pytest.mark.parametrize("answer,expected", [("Project Cedar uses SQLite.", True),
    ("  project cedar  uses sqlite ", True), ("It uses SQLite.", False),
    ("Project Cedar uses SQLite or PostgreSQL.", False),
    ("Project Cedar uses PostgreSQL. Project Cedar uses SQLite.", False),
    ("Project Cedar does not use SQLite.", False)])
def test_answer_contract_scoring_is_explicit_and_not_substring_matching(answer, expected):
    assert score(answer, "Project Cedar uses SQLite.") is expected


def test_history_window_preserves_whole_newest_records_and_reports_truncation():
    history = [{"observation": "old" * 80, "answer": "stale"}, {"observation": "最新", "answer": "new"}]
    text, full, dropped = linear_context(history, 90)
    assert text.isascii() and len(text.encode()) <= 90
    assert json.loads(text) == history[-1:] and dropped == 1 and full > 90
    assert linear_context(history, 1) == ("", full, 2)
    assert linear_context([], 1000) == ("", 0, 0)


def test_synthetic_end_to_end_uses_real_workflow_graph_and_analysis(evidence):
    output, manifest, fake = evidence
    validated, plan, rows = analyze_results.load_bundle(output)
    assert manifest["source"] == "synthetic" and manifest["status"] == "COMPLETED"
    assert [(r["repetition"], r["condition"]) for r in manifest["runs"]] == [(1, "A"), (1, "B"), (2, "B"), (2, "A")]
    assert len(rows) == 80 and all(r.telemetry.context_budget_used <= r.telemetry.context_budget_limit <= 1000 for r in rows)
    assert all(r.telemetry.active_edges == 0 and r.observation_calls == 0 for r in rows if r.condition == "A")
    assert any(r.telemetry.active_edges > 0 and r.observation_calls > 0 for r in rows if r.condition == "B")
    assert any(r.history_dropped_turns for r in rows if r.condition == "A")
    assert all(r.total_prompt_tokens >= r.telemetry.agent_prompt_tokens for r in rows)
    assert all(body["options"]["num_ctx"] == 2048 and body["options"]["temperature"] == 0 for body in fake.posts)
    assert all("graph memory" not in body.get("system", "").lower() for body in fake.posts)
    # Expected values occur only in the independent record/oracle, not as a
    # special prompt field or external answer lookup in the model fixture.
    assert all('"expected"' not in body["prompt"] for body in fake.posts)
    with sqlite3.connect(output / "B-1.db") as connection:
        assert connection.execute("SELECT count(*) FROM workflow_turns").fetchone()[0] == 20
        assert connection.execute("SELECT count(*) FROM supersession_edges").fetchone()[0] > 0
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        cedar = connection.execute("SELECT o.name FROM triples t JOIN entities s ON s.id=t.subject_id "
            "JOIN entities o ON o.id=t.object_id WHERE s.name='Project Cedar' AND t.predicate='uses' AND t.status='ACTIVE'").fetchall()
        assert cedar == [("DuckDB",)]
    report = analyze_results.analyze_bundle(output)
    assert report["source"] == "synthetic" and len(report["paired"]) == 2
    assert report["conditions"]["B"]["total_prompt_tokens"] > report["conditions"]["B"]["role_prompt_tokens"]
    print("SYNTHETIC PIPELINE OK: 80 scored turns; AB/BA order; isolated graphs; no real model requests")


def test_model_inventory_and_metadata_preserve_digests_without_private_addresses(evidence):
    output, manifest, _ = evidence
    assert all(m["digest"] == "a" * 64 for m in manifest["inventory"].values())
    text = (output / "manifest.json").read_text()
    assert "192.168.1.20" not in text and "localhost" not in text and "application.db" not in text
    assert "experiments/benchmark_harness.py" in manifest["code_sha256"]


@pytest.mark.parametrize("options", [{"fail_at": 1}, {"wrong_model": True}, {"inventory_change": True}])
def test_failed_or_changed_model_bundles_cannot_be_analyzed(tmp_path, options):
    with pytest.raises(Exception): make_bundle(tmp_path, **options)
    manifest = load_json(tmp_path / "bundle/manifest.json")
    assert manifest["status"] == "FAILED" and manifest["error_type"]
    with pytest.raises(ValueError, match="complete"):
        analyze_results.analyze_bundle(tmp_path / "bundle")


@pytest.mark.parametrize("failure_stage", ["observation_extraction", "role_turn"])
def test_extraction_failure_preserves_exact_source_and_raw_attempts(tmp_path, failure_stage):
    settings = configured(tmp_path)
    fake = SyntheticOllama(settings)
    role_calls = 0
    async def bad_extraction(request):
        nonlocal role_calls
        body = json.loads(request.content) if request.method == "POST" else {}
        extraction = body.get("system", "").startswith("Extract explicitly")
        if body.get("system") and not extraction:
            role_calls += 1
        response = await fake(request)
        if extraction and (failure_stage == "observation_extraction" or role_calls > 15):
            data = response.json()
            data["response"] = json.dumps({"triples": [{"subject": "Project Cedar", "predicate": "uses", "object": "NotInSource"}]})
            response = httpx.Response(200, json=data)
        return response
    output = tmp_path / "bundle"
    with pytest.raises(Exception, match="source|verbatim|grounding"):
        asyncio.run(run_experiment(BenchmarkPlan(turns=15, repetitions=1, warmups_per_node=0), settings,
            output, execute=True, transport=httpx.MockTransport(bad_extraction)))
    failure = load_json(output / "failure.json")
    assert failure["stage"] == failure_stage and failure["step"] == 1
    assert failure["condition"] == "B" and failure["repetition"] == 1
    assert failure["source_message"]["content"] == "Project Cedar uses SQLite."
    assert failure["source_message"]["agent_id"] == ("observation" if failure_stage == "observation_extraction" else "planner")
    assert len(failure["generation_attempts"]) == 2
    assert all("NotInSource" in g["response"] for g in failure["generation_attempts"])
    assert all(e["kind"] == "grounding" for e in failure["validation_errors"])
    manifest = load_json(output / "manifest.json")
    assert manifest["status"] == "FAILED" and "failure.json" in manifest["artifacts"]
    with pytest.raises(ValueError, match="complete"):
        analyze_results.analyze_bundle(output)


def test_fresh_output_and_application_database_are_protected(tmp_path):
    settings = configured(tmp_path)
    existing = tmp_path / "existing"
    existing.mkdir()
    sentinel = existing / "keep.txt"
    sentinel.write_text("preserve")
    with pytest.raises(FileExistsError):
        asyncio.run(run_experiment(BenchmarkPlan(), settings, existing, execute=True, transport=httpx.MockTransport(SyntheticOllama(settings))))
    assert sentinel.read_text() == "preserve"
    with pytest.raises(ValueError, match="application database"):
        asyncio.run(run_experiment(BenchmarkPlan(), settings, tmp_path, execute=True))
    assert not settings.database_path.exists()


def test_arbitrary_transport_cannot_be_mislabeled_as_synthetic(tmp_path):
    class UnknownTransport(httpx.AsyncBaseTransport): pass
    with pytest.raises(TypeError, match="MockTransport"):
        asyncio.run(run_experiment(BenchmarkPlan(), configured(tmp_path), tmp_path / "bundle", execute=True, transport=UnknownTransport()))
    assert not (tmp_path / "bundle").exists()


def test_missing_counters_remain_unknown_and_prevent_savings_claims(tmp_path):
    output, _, _ = make_bundle(tmp_path, turns=15, missing=True)
    report = analyze_results.analyze_bundle(output)
    assert all(c["total_prompt_tokens"] is None and c["role_prompt_tokens"] is None for c in report["conditions"].values())
    assert report["paired"][0]["total_prompt_savings_pct"] is None
    assert report["paired"][0]["role_prompt_savings_pct"] is None
    assert all(c["total_counter_coverage"] == 0 for c in report["conditions"].values())


def test_corrective_role_generation_is_included_in_comparison_costs(tmp_path):
    output, _, _ = make_bundle(tmp_path, turns=15, invalid_first=True)
    _, _, rows = analyze_results.load_bundle(output)
    assert rows[0].telemetry.agent_calls == 2 and rows[0].telemetry.generated_tokens == 40
    report = analyze_results.analyze_bundle(output)
    assert report["paired"][0]["total_prompt_savings_pct"] < 0
    assert report["conditions"]["B"]["total_prompt_tokens"] > report["conditions"]["A"]["total_prompt_tokens"]


def test_warmups_are_recorded_separately_from_scored_turns(tmp_path):
    settings = configured(tmp_path)
    fake = SyntheticOllama(settings)
    plan = BenchmarkPlan(turns=15, repetitions=1, warmups_per_node=1)
    output = tmp_path / "bundle"
    manifest = asyncio.run(run_experiment(plan, settings, output, execute=True, transport=httpx.MockTransport(fake)))
    _, _, rows = analyze_results.load_bundle(output)
    assert len(manifest["warmups"]) == 4 and len(rows) == 30
    assert len(fake.posts) == 4 + sum(r.telemetry.agent_calls + r.telemetry.extraction_calls + r.observation_calls for r in rows)


def test_cancelled_bundle_retains_partial_evidence_but_cannot_be_analyzed(tmp_path):
    settings = configured(tmp_path)
    async def case():
        entered = asyncio.Event()
        fake = SyntheticOllama(settings)
        async def blocked(request):
            if request.url.path == "/api/generate":
                entered.set()
                await asyncio.Event().wait()
            return await fake(request)
        task = asyncio.create_task(run_experiment(BenchmarkPlan(turns=15, repetitions=1, warmups_per_node=0),
            settings, tmp_path / "bundle", execute=True, transport=httpx.MockTransport(blocked)))
        await asyncio.wait_for(entered.wait(), timeout=3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
    asyncio.run(case())
    assert load_json(tmp_path / "bundle/manifest.json")["status"] == "CANCELLED"
    with pytest.raises(ValueError, match="complete"):
        analyze_results.analyze_bundle(tmp_path / "bundle")


@pytest.mark.parametrize("mutation", ["checksum", "incomplete", "missing", "duplicate", "wrong_score", "wrong_order",
    "oracle", "source", "scenario", "nonfinite", "csv", "traversal", "run_count"])
def test_analysis_refuses_damaged_incomplete_or_inconsistent_evidence(tmp_path, evidence, mutation):
    original, _, _ = evidence
    output = tmp_path / "bundle"
    shutil.copytree(original, output)
    manifest = load_json(output / "manifest.json")
    lines = (output / "turns.jsonl").read_text().splitlines()
    row = json.loads(lines[0])
    if mutation == "checksum":
        (output / "metrics.csv").write_text("damaged")
    elif mutation == "incomplete": manifest["status"] = "RUNNING"
    elif mutation == "missing": (output / "B-1.csv").unlink()
    elif mutation == "duplicate": lines.append(lines[0])
    elif mutation == "wrong_score": row["correct"] = not row["correct"]
    elif mutation == "wrong_order": row["condition"] = "B"
    elif mutation == "oracle": row["expected"] = "wrong"
    elif mutation == "source": row["source"] = "measured"
    elif mutation == "scenario": manifest["scenario_sha256"] = "0" * 64
    elif mutation == "nonfinite": row["wall_ms"] = float("nan")
    elif mutation == "csv":
        (output / "metrics.csv").write_text("bad,header\n")
        manifest["artifacts"]["metrics.csv"] = sha256(output / "metrics.csv")
    elif mutation == "traversal": manifest["artifacts"]["../outside"] = "0" * 64
    elif mutation == "run_count": manifest["runs"][0]["completed_turns"] = 19
    if mutation in ("wrong_score", "wrong_order", "oracle", "source", "nonfinite"):
        lines[0] = json.dumps(row)
    if mutation in ("duplicate", "wrong_score", "wrong_order", "oracle", "source", "nonfinite"):
        (output / "turns.jsonl").write_text("\n".join(lines) + "\n")
        manifest["artifacts"]["turns.jsonl"] = sha256(output / "turns.jsonl")
    write_json(output / "manifest.json", manifest)
    with pytest.raises((ValueError, OSError, KeyError)):
        analyze_results.analyze_bundle(output)


def test_analysis_csv_outputs_and_existing_directory_protection(tmp_path, evidence):
    bundle, _, _ = evidence
    report = analyze_results.write_analysis(bundle, tmp_path / "analysis")
    assert load_json(tmp_path / "analysis/summary.json")["bundle_id"] == report["bundle_id"]
    with (tmp_path / "analysis/paired.csv").open() as stream:
        assert len(list(csv.DictReader(stream))) == 2
    assert len(report["curves"]) == 40
    with pytest.raises(FileExistsError): analyze_results.write_analysis(bundle, tmp_path / "analysis")


def test_figures_are_standalone_and_synthetic_source_is_visible(tmp_path, evidence, monkeypatch):
    from xml.etree import ElementTree
    monkeypatch.setenv("MPLCONFIGDIR", str(tmp_path / "matplotlib-cache"))
    output, _, _ = evidence
    paths = generate_plots.generate_plots(output, tmp_path / "figures")
    assert len(paths) == 12
    for path in paths:
        if path.suffix == ".svg":
            text = path.read_text()
            assert "SYNTHETIC TEST DATA" in text and "NOT EXPERIMENT RESULTS" in text
            for element in ElementTree.fromstring(text).iter():
                for key, value in element.attrib.items():
                    if key.endswith("href"):
                        assert value.startswith("#"), "figure must not fetch external resources"
        elif path.suffix == ".pdf": assert path.read_bytes().startswith(b"%PDF")
        elif path.suffix == ".png": assert path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    print("FIGURES SYNTHETIC OK: 12 standalone files; synthetic labels retained")


def test_analysis_and_plot_cli_report_invalid_bundle_without_creating_output(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert analyze_results.main(["--input", str(empty), "--output", str(tmp_path / "analysis")]) == 1
    assert generate_plots.main(["--input", str(empty), "--output", str(tmp_path / "figures")]) == 1
    assert not (tmp_path / "analysis").exists() and not (tmp_path / "figures").exists()


def test_per_turn_input_override_is_validated_and_saved_atomically(tmp_path):
    settings = configured(tmp_path)
    fake = SyntheticOllama(settings)
    async def case():
        async with OllamaClient(settings, transport=httpx.MockTransport(fake)) as client:
            async with Database(tmp_path / "inputs.db") as database:
                machine = StateMachine(client, GraphStore(database))
                first, second = scenario(15)[:2]
                run = await machine.start(first.task, initial_subgoal=first.subject, max_turns=2)
                with pytest.raises(ValueError): await machine.step(run.id, task="")
                saved = await machine.get_checkpoint(run.id)
                assert saved.status == "READY" and saved.task == first.task and not fake.posts
                turn = await machine.step(run.id, task=second.task, subgoal=second.subject)
                assert turn.checkpoint.result["task"] == second.task
                assert turn.checkpoint.result["subgoal"] == second.subject
                assert (await machine.get_checkpoint(run.id)).task == second.task
    asyncio.run(case())


def test_analysis_imports_do_not_load_runners_or_start_work():
    """Analysis imports must not require execution modules or start local services."""
    script = """
import importlib
import sqlite3
import sys
import httpx
from src import config
from src.storage.database import Database

def forbidden(*args, **kwargs):
    raise AssertionError('import started configuration, database or HTTP work')

config.get_settings = forbidden
config.Settings.__init__ = forbidden
Database.__init__ = forbidden
sqlite3.connect = forbidden
httpx.Client.send = forbidden
httpx.AsyncClient.send = forbidden
for name in ('experiments.common', 'experiments.analyze_results', 'experiments.integrated_analysis'):
    importlib.import_module(name)
assert 'experiments.benchmark_harness' not in sys.modules
assert 'experiments.integrated_harness' not in sys.modules
print('IMPORT BOUNDARIES OK')
"""
    result = subprocess.run(
        [sys.executable, '-B', '-c', script],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == 'IMPORT BOUNDARIES OK'
