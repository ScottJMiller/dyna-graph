"""Plan by default; run a fresh local A/B bundle only with explicit execution."""

import argparse
import asyncio
from dataclasses import asdict
import csv
import hashlib
from importlib.metadata import PackageNotFoundError, version
import json
from pathlib import Path
import platform
import re
import sys
from time import perf_counter
from uuid import uuid4

import httpx

from experiments.contracts import BenchmarkPlan, BenchmarkRow, sha256, utc_now, write_json
from experiments.scenario import scenario, score
from src.agents import PlannerAgent, ResearchAgent, VerificationAgent
from src.agents.base_agent import encode
from src.config import Settings
from src.extraction import Extractor
from src.models import AgentMessage
from src.network.ollama_client import OllamaClient
from src.orchestration import StateMachine
from src.storage.database import Database
from src.storage.graph_store import GraphStore
from src.telemetry import CSVLogger, TelemetryProfiler, TurnMetrics
from src.telemetry.profiler import process_rss


class _ResearchRole(ResearchAgent):
    @property
    def role_prompt(self):
        # Identical condition-neutral framing for both history representations.
        return super().role_prompt.replace("graph memory", "memory")


def _agents(client):
    return (PlannerAgent(client), _ResearchRole(client), VerificationAgent(client))


def linear_context(history, budget):
    """Retain newest complete episodes; never truncate a message or JSON value."""
    full = encode(history) if history else ""
    kept, dropped = list(history), 0
    while kept and len(encode(kept).encode()) > budget:
        kept.pop(0)
        dropped += 1
    return (encode(kept) if kept else ""), len(full.encode()), dropped


def _sum(generations, field):
    values = [getattr(g, field) for g in generations]
    return None if any(v is None for v in values) else sum(values)


def _add(a, b):
    return None if a is None or b is None else a + b


def _routes(generations, client, node):
    if any(g.node != node or g.model != client.settings.node_model(node) or g.failed_attempts
           for g in generations):
        raise ValueError("experiment routing/model differs from the fixed condition plan")


def _baseline_metrics(run_id, item, result, context, budget, started):
    generations = result.generations
    prompt = _sum(generations, "prompt_tokens")
    latency = sum(g.latency_ms for g in generations)
    rss, source = process_rss()
    import time
    return TurnMetrics(run_id=run_id, step=item.step, governance_turn=item.step,
        agent_id=result.message.agent_id, node=result.generation.node, observed_at=time.time(),
        turn_work_ms=(perf_counter() - started) * 1000, agent_calls=len(generations), extraction_calls=0,
        fallback_attempts=sum(len(g.failed_attempts) for g in generations),
        agent_prompt_tokens=prompt, extraction_prompt_tokens=0, prompt_tokens=prompt,
        generated_tokens=_sum(generations, "generated_tokens"), agent_latency_ms=latency,
        extraction_latency_ms=0.0, generation_latency_ms=latency, context_budget_used=len(context.encode()),
        context_budget_limit=budget, context_budget_method="utf8_upper_bound", active_nodes=0,
        active_edges=0, process_rss_bytes=rss, rss_source=source)


async def _observe_input(machine, item, extractor):
    """Ingest only the newly supplied observation, never the expected answer.

    Incoming observations are a separate accepted input transaction before
    recall. Its model usage and wall time are included in the benchmark row.
    Agent-response extraction still runs through the ordinary state machine.
    """
    if not item.observation:
        return ()
    extracted = await extractor.extract(AgentMessage(agent_id="observation", content=item.observation,
        turn=item.step), node="node2", allow_fallback=False)
    _routes(extracted.attempts, machine.client, "node2")
    def write(graph):
        with graph.database.transaction() as connection:
            turn = connection.execute("SELECT max(COALESCE((SELECT max(last_access_turn) FROM entity_memory),0),"
                "COALESCE((SELECT max(governance_turn) FROM workflow_turns),0))+1").fetchone()[0]
            for fact in extracted.graph.triples:
                subject = graph.get_or_create_entity(fact.subject)
                obj = graph.get_or_create_entity(fact.object)
                machine.writer.write_fact(subject.id, fact.predicate, obj.id, turn=turn, confidence=fact.confidence)
    await machine.graph.write_async(write)
    return extracted.attempts


def _flatten(row):
    values = row.model_dump()
    telemetry = values.pop("telemetry")
    return values | {"m_" + key: value for key, value in telemetry.items()}


CSV_COLUMNS = tuple(k for k in BenchmarkRow.model_fields if k != "telemetry") + tuple(
    "m_" + k for k in TurnMetrics.model_fields)


def _packages():
    result = {}
    for name in ("pydantic", "pydantic-settings", "httpx", "rich", "matplotlib", "numpy"):
        try:
            result[name] = version(name)
        except PackageNotFoundError:
            result[name] = None
    return result


def _provenance():
    root = Path(__file__).resolve().parents[1]
    paths = [root / "requirements.txt", *(root / "src").rglob("*.py"),
             *(root / "src").rglob("*.sql"), *(root / "experiments").glob("*.py")]
    return {str(p.relative_to(root)): sha256(p) for p in sorted(paths)}


async def _inventory(client, *, synthetic):
    result = {}
    for node in ("node1", "node2"):
        health = await client.ping(node)
        installed = await client.list_models(node)
        matches = [m for m in installed.models if m.name == client.settings.node_model(node)]
        if len(matches) != 1 or matches[0].quantization_level != "Q4_K_M":
            raise ValueError(f"{node} must have its configured local Q4_K_M model installed")
        if not synthetic and not re.fullmatch(r"[a-fA-F0-9]{64}", matches[0].digest or ""):
            raise ValueError(f"{node} must report its installed model digest")
        result[node] = {"ollama_version": health.version, **asdict(matches[0]),
                        "threads": client.settings.node_threads(node)}
    return result


async def run_experiment(plan: BenchmarkPlan, settings: Settings, output: Path, *, execute=False,
                         transport: httpx.MockTransport | None = None, progress=None):
    """Execute only on explicit request; tests must supply a MockTransport.

    Fresh directory per bundle, independent graph/transcript per condition and
    repetition, no fallback, no early stop, and AB/BA alternating order. Failed
    bundles retain partial evidence but are rejected by analysis. No automatic
    resume, model pull, experiment analysis, or figure generation occurs here.
    """
    if execute is not True:
        raise PermissionError("execution requires execute=True (CLI: --execute)")
    plan = BenchmarkPlan.model_validate(plan)
    if transport is not None and not isinstance(transport, httpx.MockTransport):
        raise TypeError("test injection requires an HTTPX MockTransport")
    synthetic = transport is not None
    output = Path(output).expanduser().resolve()
    if settings.database_path.resolve().is_relative_to(output):
        raise ValueError("experiment directory must not contain the application database")
    # Never reuse a prior directory, including one from an interrupted run.
    output.mkdir(parents=True, exist_ok=False)
    configured = settings.model_copy(update={"single_valued_predicates": ("uses",),
        "context_token_budget": plan.memory_budget, "agent_max_tokens": plan.max_tokens,
        "orchestration_max_turns": plan.turns,
        "agent_max_tool_rounds": 0, "orchestration_allow_fallback": False,
        "orchestration_prune_between_turns": True})
    bundle_id = uuid4().hex
    manifest = {"version": 1, "bundle_id": bundle_id, "source": "synthetic" if synthetic else "measured",
        "status": "RUNNING", "started_at": utc_now(), "finished_at": None,
        "plan": plan.model_dump(), "scenario_sha256": plan.scenario_sha256,
        "scenario": [asdict(item) for item in scenario(plan.turns)], "code_sha256": _provenance(),
        "python": platform.python_version(), "os": platform.system(), "architecture": platform.machine(),
        "packages": _packages(), "policy": configured.model_dump(mode="json", exclude={
            "database_path", "node1_ollama_url", "node2_ollama_url", "coordinator_host"}),
        "protocol": {"model_context_tokens": 2048, "temperature": 0, "keep_alive": "5m",
            "extractor_max_tokens": 512, "extractor_max_retries": 1,
            "stop_on_verified": False, "memory_budget_method": "utf8_upper_bound",
            "scoring": "normalized_exact_sentence_v1"},
        "inventory": {}, "warmups": [], "runs": [], "artifacts": {}}
    write_json(output / "manifest.json", manifest)
    try:
        async with OllamaClient(configured, transport=transport) as client:
            manifest["roles"] = [{"agent_id": a.agent_id, "node": a.node,
                "system": a.system_prompt(), "max_tokens": a.max_tokens, "max_retries": a.max_retries}
                for a in _agents(client)]
            manifest["inventory"] = await _inventory(client, synthetic=synthetic)
            with (output / "turns.jsonl").open("w", encoding="utf-8") as raw:
                with (output / "metrics.csv").open("w", encoding="utf-8", newline="") as csv_file:
                    csv_writer = csv.DictWriter(csv_file, fieldnames=CSV_COLUMNS)
                    csv_writer.writeheader()
                    for position, (rep, condition) in enumerate(plan.order(), 1):
                        if await _inventory(client, synthetic=synthetic) != manifest["inventory"]:
                            raise ValueError("model inventory changed during the paired experiment")
                        run_id = f"{bundle_id}-{condition}-{rep}"
                        agents = _agents(client)
                        for node in ("node1", "node2"):
                            for _ in range(plan.warmups_per_node):
                                g = await client.generate("Reply OK.", node=node, allow_fallback=False, max_tokens=8)
                                _routes((g,), client, node)
                                manifest["warmups"].append({"condition": condition, "repetition": rep,
                                    "node": g.node, "prompt_tokens": g.prompt_tokens, "generated_tokens": g.generated_tokens,
                                    "latency_ms": g.latency_ms})
                        run_meta = {"run_id": run_id, "condition": condition, "repetition": rep,
                            "position": position, "completed_turns": 0}
                        manifest["runs"].append(run_meta)
                        # Even A has an isolated empty database lifetime, so
                        # infrastructure setup is outside both turn timings.
                        async with Database(output / f"{condition}-{rep}.db",
                                pool_size=configured.sqlite_pool_size, busy_timeout_ms=configured.sqlite_busy_timeout_ms) as database:
                            machine = StateMachine(client, GraphStore(database), agents=agents)
                            items = scenario(plan.turns)
                            if condition == "B":
                                await machine.start(items[0].task, initial_subgoal=items[0].subject,
                                                    max_turns=plan.turns, run_id=run_id)
                            history = []
                            with CSVLogger(output / f"{condition}-{rep}.csv") as logger:
                                logger.bind_run(run_id)
                                for item in items:
                                    started = perf_counter()
                                    role = agents[(item.step - 1) % len(agents)]
                                    budget = min(plan.memory_budget, role.context_capacity(item.task, item.subject))
                                    input_generations = ()
                                    full_bytes = dropped = 0
                                    if condition == "A":
                                        context, full_bytes, dropped = linear_context(history, budget)
                                        result = await role.execute(item.task, item.subject, context,
                                            turn=item.step, allow_fallback=False)
                                        metrics = _baseline_metrics(run_id, item, result, context, budget, started)
                                    else:
                                        input_generations = await _observe_input(machine, item, Extractor(client))
                                        turn = await machine.step(run_id, task=item.task, subgoal=item.subject)
                                        result, context = turn.agent, turn.context.text
                                        metrics = TelemetryProfiler.from_checkpoint(turn.checkpoint)
                                        _routes(turn.extraction.attempts, client, "node2")
                                    _routes(result.generations, client, role.node)
                                    observation_prompt = _sum(input_generations, "prompt_tokens")
                                    observation_generated = _sum(input_generations, "generated_tokens")
                                    observation_ms = sum(g.latency_ms for g in input_generations)
                                    row = BenchmarkRow(source=manifest["source"], bundle_id=bundle_id,
                                        condition=condition, repetition=rep, position=position,
                                        scenario_sha256=plan.scenario_sha256, observation=item.observation,
                                        subject=item.subject, expected=item.expected, answer=result.message.content,
                                        correct=score(result.message.content, item.expected), update_probe=item.update_probe,
                                        delayed_probe=item.delayed_probe, context_sha256=hashlib.sha256(context.encode()).hexdigest(),
                                        history_full_bytes=full_bytes, history_dropped_turns=dropped,
                                        observation_calls=len(input_generations), observation_prompt_tokens=observation_prompt,
                                        observation_generated_tokens=observation_generated, observation_latency_ms=observation_ms,
                                        total_prompt_tokens=_add(metrics.prompt_tokens, observation_prompt),
                                        total_generated_tokens=_add(metrics.generated_tokens, observation_generated),
                                        total_inference_ms=metrics.generation_latency_ms + observation_ms,
                                        wall_ms=(perf_counter() - started) * 1000, telemetry=metrics)
                                    # Record successful turns only; any failure
                                    # marks the entire bundle incomplete below.
                                    logger.append(metrics)
                                    raw.write(row.model_dump_json() + "\n")
                                    csv_writer.writerow(_flatten(row))
                                    raw.flush()
                                    csv_file.flush()
                                    history.append({"task": item.task, "answer": result.message.content})
                                    run_meta["completed_turns"] = item.step
                                    if progress is not None:
                                        progress(row)
                            write_json(output / "manifest.json", manifest)
        manifest["status"] = "COMPLETED"
    except BaseException as exc:
        manifest["status"] = "CANCELLED" if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt)) else "FAILED"
        manifest["error_type"] = type(exc).__name__
        raise
    finally:
        manifest["finished_at"] = utc_now()
        manifest["artifacts"] = {str(p.relative_to(output)): sha256(p) for p in sorted(output.rglob("*"))
            if p.is_file() and p.name not in ("manifest.json", "manifest.json.tmp")}
        write_json(output / "manifest.json", manifest)
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="explicitly run local model inference; default is plan only")
    parser.add_argument("--output", type=Path, help="new directory for one experiment bundle; must not exist")
    parser.add_argument("--turns", type=int, default=20)
    parser.add_argument("--repetitions", type=int, default=2)
    parser.add_argument("--memory-budget", type=int, default=1000)
    parser.add_argument("--max-tokens", type=int, default=128)
    parser.add_argument("--warmups-per-node", type=int, default=1)
    args = parser.parse_args(argv)
    try:
        plan = BenchmarkPlan(turns=args.turns, repetitions=args.repetitions,
            memory_budget=args.memory_budget, max_tokens=args.max_tokens, warmups_per_node=args.warmups_per_node)
        if not args.execute:
            print(f"PLAN ONLY: {len(plan.order())} condition runs; {2 * plan.repetitions * plan.turns} scored turns; no model requests or experiment files")
            print(json.dumps(plan.model_dump() | {"scenario_sha256": plan.scenario_sha256,
                "order": list(plan.order())}, indent=2))
            return 0
        if args.output is None:
            parser.error("--execute requires --output with a new directory")
        from src.config import get_settings
        def progress(row):
            print(f"TURN {row.condition} repetition={row.repetition} step={row.telemetry.step}/{plan.turns} "
                  f"role={row.telemetry.agent_id} node={row.telemetry.node} "
                  f"prompt_tokens={row.total_prompt_tokens} wall_ms={row.wall_ms:.1f}", flush=True)
        manifest = asyncio.run(run_experiment(plan, get_settings(), args.output, execute=True, progress=progress))
        print(f"EXPERIMENT COMPLETE: {manifest['bundle_id']} ({args.output})")
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("EXPERIMENT STOPPED: partial evidence retained; analysis requires a complete bundle", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
