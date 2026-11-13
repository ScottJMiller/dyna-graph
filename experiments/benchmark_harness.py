"""Plan by default; run a fresh local A/B bundle only with explicit execution."""

import argparse
import asyncio
import csv
import hashlib
import json
from pathlib import Path
import platform
import sys
from time import perf_counter
from uuid import uuid4

import httpx

from experiments.common import (
    RecordedExtractor, add_costs, make_agents, baseline_metrics, failure_record,
    model_inventory, package_versions, source_provenance, validate_routes,
    sum_attempt_field, linear_context,
)
from experiments.contracts import (
    BenchmarkPlan, BenchmarkRow, CSV_COLUMNS, flatten_row, sha256, utc_now, write_json,
)
from experiments.scenario import scenario, scenario_records, score
from src.config import Settings
from src.models import AgentMessage
from src.network.ollama_client import OllamaClient
from src.orchestration import StateMachine
from src.storage.database import Database
from src.storage.graph_store import GraphStore
from src.telemetry import CSVLogger, TelemetryProfiler


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
    validate_routes(extracted.attempts, machine.client, "node2")
    def write(graph):
        """Commit accepted observation facts with the next durable governance turn."""
        with graph.database.transaction() as connection:
            turn = connection.execute("SELECT max(COALESCE((SELECT max(last_access_turn) FROM entity_memory),0),"
                "COALESCE((SELECT max(governance_turn) FROM workflow_turns),0))+1").fetchone()[0]
            for fact in extracted.graph.triples:
                subject = graph.get_or_create_entity(fact.subject)
                obj = graph.get_or_create_entity(fact.object)
                machine.writer.write_fact(subject.id, fact.predicate, obj.id, turn=turn, confidence=fact.confidence)
    await machine.graph.write_async(write)
    return extracted.attempts


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
        "scenario": scenario_records(plan.turns), "code_sha256": source_provenance(),
        "python": platform.python_version(), "os": platform.system(), "architecture": platform.machine(),
        "packages": package_versions(), "policy": configured.model_dump(mode="json", exclude={
            "database_path", "node1_ollama_url", "node2_ollama_url", "coordinator_host"}),
        "protocol": {"model_context_tokens": 2048, "temperature": 0, "keep_alive": "5m",
            "extractor_max_tokens": 512, "extractor_max_retries": 1,
            "stop_on_verified": False, "memory_budget_method": "utf8_upper_bound",
            "scoring": "normalized_exact_sentence_v1"},
        "inventory": {}, "warmups": [], "runs": [], "artifacts": {}}
    write_json(output / "manifest.json", manifest)
    current_condition = current_repetition = current_step = current_task = None
    stage = "inventory"
    try:
        async with OllamaClient(configured, transport=transport) as client:
            manifest["roles"] = [{"agent_id": a.agent_id, "node": a.node,
                "system": a.system_prompt(), "max_tokens": a.max_tokens, "max_retries": a.max_retries}
                for a in make_agents(client)]
            manifest["inventory"] = await model_inventory(client, synthetic=synthetic)
            with (output / "turns.jsonl").open("w", encoding="utf-8") as raw:
                with (output / "metrics.csv").open("w", encoding="utf-8", newline="") as csv_file:
                    csv_writer = csv.DictWriter(csv_file, fieldnames=CSV_COLUMNS)
                    csv_writer.writeheader()
                    for position, (rep, condition) in enumerate(plan.order(), 1):
                        current_condition, current_repetition = condition, rep
                        current_step = current_task = None
                        stage = "inventory"
                        if await model_inventory(client, synthetic=synthetic) != manifest["inventory"]:
                            raise ValueError("model inventory changed during the paired experiment")
                        run_id = f"{bundle_id}-{condition}-{rep}"
                        agents = make_agents(client)
                        stage = "warmup"
                        for node in ("node1", "node2"):
                            for _ in range(plan.warmups_per_node):
                                g = await client.generate("Reply OK.", node=node, allow_fallback=False, max_tokens=8)
                                validate_routes((g,), client, node)
                                manifest["warmups"].append({"condition": condition, "repetition": rep,
                                    "node": g.node, "prompt_tokens": g.prompt_tokens, "generated_tokens": g.generated_tokens,
                                    "latency_ms": g.latency_ms})
                        run_meta = {"run_id": run_id, "condition": condition, "repetition": rep,
                            "position": position, "completed_turns": 0}
                        manifest["runs"].append(run_meta)
                        # Even A has an isolated empty database lifetime, so
                        # infrastructure setup is outside both turn timings.
                        stage = "setup"
                        async with Database(output / f"{condition}-{rep}.db",
                                pool_size=configured.sqlite_pool_size, busy_timeout_ms=configured.sqlite_busy_timeout_ms) as database:
                            extractor = RecordedExtractor(client)
                            machine = StateMachine(client, GraphStore(database), agents=agents, extractor=extractor)
                            items = scenario(plan.turns)
                            if condition == "B":
                                await machine.start(items[0].task, initial_subgoal=items[0].subject,
                                                    max_turns=plan.turns, run_id=run_id)
                            history = []
                            with CSVLogger(output / f"{condition}-{rep}.csv") as logger:
                                logger.bind_run(run_id)
                                for item in items:
                                    current_step, current_task = item.step, item.task
                                    stage = "role_turn"
                                    started = perf_counter()
                                    role = agents[(item.step - 1) % len(agents)]
                                    budget = min(plan.memory_budget, role.context_capacity(item.task, item.subject))
                                    input_generations = ()
                                    full_bytes = dropped = 0
                                    if condition == "A":
                                        context, full_bytes, dropped = linear_context(history, budget)
                                        result = await role.execute(item.task, item.subject, context,
                                            turn=item.step, allow_fallback=False)
                                        metrics = baseline_metrics(run_id, item, result, context, budget, started)
                                    else:
                                        stage = "observation_extraction"
                                        input_generations = await _observe_input(machine, item, extractor)
                                        stage = "role_turn"
                                        turn = await machine.step(run_id, task=item.task, subgoal=item.subject)
                                        result, context = turn.agent, turn.context.text
                                        metrics = TelemetryProfiler.from_checkpoint(turn.checkpoint)
                                        validate_routes(turn.extraction.attempts, client, "node2")
                                    validate_routes(result.generations, client, role.node)
                                    observation_prompt = sum_attempt_field(input_generations, "prompt_tokens")
                                    observation_generated = sum_attempt_field(input_generations, "generated_tokens")
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
                                        total_prompt_tokens=add_costs(metrics.prompt_tokens, observation_prompt),
                                        total_generated_tokens=add_costs(metrics.generated_tokens, observation_generated),
                                        total_inference_ms=metrics.generation_latency_ms + observation_ms,
                                        wall_ms=(perf_counter() - started) * 1000, telemetry=metrics)
                                    # Record successful turns only; any failure
                                    # marks the entire bundle incomplete below.
                                    logger.append(metrics)
                                    raw.write(row.model_dump_json() + "\n")
                                    csv_writer.writerow(flatten_row(row))
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
        write_json(output / "failure.json", failure_record(exc, manifest=manifest,
            condition=current_condition, repetition=current_repetition, step=current_step,
            stage=stage, task=current_task))
        raise
    finally:
        manifest["finished_at"] = utc_now()
        manifest["artifacts"] = {str(p.relative_to(output)): sha256(p) for p in sorted(output.rglob("*"))
            if p.is_file() and p.name not in ("manifest.json", "manifest.json.tmp")}
        write_json(output / "manifest.json", manifest)
    return manifest


def main(argv=None):
    """Print the original study plan unless execution is explicitly requested."""
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
            """Print the usage and latency of each successfully recorded turn."""
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
