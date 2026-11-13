"""Plan or explicitly execute the four-condition local memory experiment.

Each condition/variant starts with fresh storage. Governance age is simulated;
inference latency and observation timestamps are measured from real clocks.
"""

import argparse
import asyncio
import csv
import json
from pathlib import Path
import platform
import sys
import time
from uuid import uuid4

import httpx

from experiments.common import (
    RecordedExtractor, add_costs, make_agents, baseline_metrics, failure_record,
    model_inventory, package_versions, source_provenance, validate_routes, sum_attempt_field, linear_context,
)
from experiments.contracts import sha256, utc_now, write_json
from experiments.integrated_contracts import (
    CONDITIONS, GENERATION_PROTOCOL, REPLY_ERROR_POLICY, INFERENCE_ERROR_POLICY,
    PRIMARY_OUTCOMES, HYPOTHESES, Change, FactState, IntegratedPlan, IntegratedRow,
    MemoryState, project,
)
from experiments.integrated_scenario import evaluate, records, role_at, workload
from src.agents.base_agent import AgentError, rejected_reply, reply_failure_evidence
from src.governance import record_access
from src.models import AgentMessage
from src.network.ollama_client import OllamaClient, generation_evidence
from src.orchestration import StateMachine
from src.storage.database import Database
from src.storage.graph_store import GraphStore
from src.telemetry import CSVLogger, TelemetryProfiler


class ScenarioClock:
    """Advance memory age by a fixed number of hours per scheduled turn."""

    def __init__(self, hours):
        """Start at turn zero, with the supplied hours-per-turn interval."""
        self.hours = hours
        self.step = 0

    def now(self):
        """Return simulated seconds; never use this value for wall latency."""
        return float(self.step * self.hours * 3600)


def configure(settings, plan, condition):
    """Copy settings with the fixed protocol and the selected memory policy."""
    policy = CONDITIONS[condition]
    return settings.model_copy(update=dict(
        single_valued_predicates=policy['single_valued_predicates'],
        orchestration_prune_between_turns=policy['prune'],
        orchestration_allow_fallback=False,
        orchestration_max_turns=plan.turns,
        context_token_budget=plan.memory_budget,
        agent_max_tokens=plan.max_tokens,
        agent_max_tool_rounds=0,
        pruning_threshold=plan.pruning_threshold,
        pruning_batch_size=100,
        decay_recency_half_life_seconds=plan.recency_half_life_seconds,
        decay_turn_half_life=plan.turn_half_life,
    ))


def catalog(graph):
    """Read all fact states, including superseded and pruned history, by ID."""
    with graph.database.connection() as connection:
        rows = connection.execute('''SELECT t.id,s.name AS subject,t.predicate,o.name AS object,t.status,
            (s.status='ACTIVE') AS subject_active,(o.status='ACTIVE') AS object_active
            FROM triples t JOIN entities s ON s.id=t.subject_id JOIN entities o ON o.id=t.object_id ORDER BY t.id''').fetchall()
    facts = {}
    for row in rows:
        fields = dict(row) | {
            'subject_active': bool(row['subject_active']),
            'object_active': bool(row['object_active']),
        }
        facts[row['id']] = FactState(**fields)
    return facts


def snapshot(graph, event, facts):
    """Record target history and graph counts for one question's memory audit."""
    with graph.database.connection() as connection:
        memory = connection.execute('''SELECT e.is_pinned,m.last_access_at,m.last_access_turn FROM entities e
            LEFT JOIN entity_memory m ON m.entity_id=e.id WHERE e.canonical_name=?''',
            (event.subject.casefold(),)).fetchone()
        active_nodes = connection.execute("SELECT count(*) FROM entities WHERE status='ACTIVE'").fetchone()[0]
    groups = {}
    for fact in facts.values():
        if fact.live and fact.predicate == 'uses':
            groups[fact.subject] = groups.get(fact.subject, 0) + 1
    return MemoryState(
        target_facts=tuple(
            fact for fact in facts.values()
            if fact.subject.casefold() == event.subject.casefold() and fact.predicate == event.predicate
        ),
        active_nodes=active_nodes,
        active_edges=sum(fact.live for fact in facts.values()),
        single_value_conflicts=sum(count > 1 for count in groups.values()),
        subject_pinned=False if memory is None else bool(memory['is_pinned']),
        last_access_at=None if memory is None else memory['last_access_at'],
        last_access_turn=None if memory is None else memory['last_access_turn'],
    )


def changes(before, after, stage):
    """Record added or changed facts in their existing catalog iteration order."""
    return tuple(
        Change(stage=stage, before=None if fact_id not in before else before[fact_id].status, fact=fact)
        for fact_id, fact in after.items() if fact_id not in before or before[fact_id] != fact
    )


async def ingest(machine, event, extractor, clock):
    """Extract the scripted observation and commit accepted facts before recall.

    Return attempts, accepted triples and any recorded failure. Pinning follows
    the explicit scenario metadata; reference answers never enter this write.
    """
    if not event.observation:
        return (), (), None
    source_role, _ = role_at(event.step)
    result = await extractor.extract(
        AgentMessage(agent_id=source_role, content=event.observation, turn=event.step),
        node='node2', allow_fallback=False,
    )
    validate_routes(result.attempts, machine.client, 'node2')

    def write(graph):
        """Apply this observation inside the graph's queued write transaction."""
        if event.pin_subject:
            entity = graph.get_or_create_entity(event.pin_subject)
            graph.update_entity(entity.id, is_pinned=True)
            record_access(graph, [entity.id], turn=event.step, now=clock.now())
        for fact in result.graph.triples:
            subject = graph.get_or_create_entity(fact.subject)
            obj = graph.get_or_create_entity(fact.object)
            machine.writer.write_fact(
                subject.id, fact.predicate, obj.id, turn=event.step,
                now=clock.now(), confidence=fact.confidence,
            )

    await machine.graph.write_async(write)
    return result.attempts, tuple(result.graph.triples), result.failure


def _new_manifest(plan, settings, bundle_id, source):
    """Describe the plan and source before any model or condition run begins."""
    return dict(
        version=4, protocol='integrated-memory-v3', bundle_id=bundle_id, source=source,
        status='RUNNING', started_at=utc_now(), finished_at=None,
        plan=plan.model_dump(), conditions=CONDITIONS,
        scenario_hashes=plan.scenario_hashes(),
        scenarios={str(variant): records(plan.turns, variant) for variant in range(1, plan.variants + 1)},
        code_sha256=source_provenance(), python=platform.python_version(), os=platform.system(),
        architecture=platform.machine(), packages=package_versions(),
        inventory={}, roles=[], warmups=[], runs=[], artifacts={},
        time_basis='Simulated governance age; real observation timestamps and perf_counter latency.',
        input_policy=(
            'Scripted observation is supplied to the routed role. Later cross-node probes use that role as source; '
            'no autonomous discovery is claimed.'
        ),
        fully_counterbalanced=plan.variants % 4 == 0,
        primary_outcomes=PRIMARY_OUTCOMES, hypotheses=HYPOTHESES,
        generation_protocol=GENERATION_PROTOCOL, reply_error_policy=REPLY_ERROR_POLICY,
        inference_error_policy=INFERENCE_ERROR_POLICY,
        policies={
            condition: configure(settings, plan, condition).model_dump(mode='json', exclude={
                'database_path', 'node1_ollama_url', 'node2_ollama_url', 'coordinator_host',
            }) for condition in CONDITIONS
        },
        measurement=(
            'Wall time includes diagnostic snapshots, observation extraction, role generation, response extraction '
            'and checkpoint work; excludes warmup/setup/exports.'
        ),
    )


async def run_integrated(plan, settings, output, *, execute=False, transport=None, progress=None):
    """Run a fresh A/B/C/D cohort only when execute=True is explicitly supplied.

    Tests may supply MockTransport instead of contacting Ollama. Every turn
    retains attempts and recorded failures; fatal errors preserve a partial
    bundle that analysis must reject. Return the final completed manifest.
    """
    if execute is not True:
        raise PermissionError('execution requires execute=True')
    plan = IntegratedPlan.model_validate(plan)
    if transport is not None and not isinstance(transport, httpx.MockTransport):
        raise TypeError('offline injection requires HTTPX MockTransport')
    output = Path(output).expanduser().resolve()
    if settings.database_path.resolve().is_relative_to(output):
        raise ValueError('experiment directory must not contain application database')
    output.mkdir(parents=True, exist_ok=False)
    bundle_id = uuid4().hex
    source = 'synthetic' if transport is not None else 'measured'
    manifest = _new_manifest(plan, settings, bundle_id, source)
    write_json(output / 'manifest.json', manifest)
    condition = variant = step = task = None
    stage = 'setup'
    try:
        with (output / 'turns.jsonl').open('x', encoding='utf-8') as raw, \
                (output / 'metrics.csv').open('x', encoding='utf-8', newline='') as csv_file:
            writer = None
            for position, (variant, condition) in enumerate(plan.order(), 1):
                configured = configure(settings, plan, condition)
                clock = ScenarioClock(plan.hours_per_turn)
                stage = 'inventory'
                step = task = None
                async with OllamaClient(configured, transport=transport, record_prediction_aborts=True) as client:
                    inventory = await model_inventory(client, synthetic=transport is not None)
                    if manifest['inventory'] and manifest['inventory'] != inventory:
                        raise ValueError('model inventory changed')
                    manifest['inventory'] = inventory
                    agents = make_agents(client)
                    roles = [dict(
                        agent_id=agent.agent_id, node=agent.node, system=agent.system_prompt(),
                        max_tokens=agent.max_tokens, max_retries=agent.max_retries, tool_rounds=agent.max_tool_rounds,
                    ) for agent in agents]
                    if manifest['roles'] and manifest['roles'] != roles:
                        raise ValueError('role prompts or generation limits changed across conditions')
                    manifest['roles'] = roles
                    events = workload(plan.turns, variant)
                    for event in events:
                        for agent in agents:
                            agent.context_capacity(event.task, event.subject)
                    stage = 'warmup'
                    for node in ('node1', 'node2'):
                        for _ in range(plan.warmups_per_node):
                            generation = await client.generate('Reply OK.', node=node, allow_fallback=False, max_tokens=8)
                            if generation.error:
                                raise AgentError('warmup', 'warmup prediction aborted', (generation,))
                            validate_routes((generation,), client, node)
                            manifest['warmups'].append(dict(
                                condition=condition, variant=variant, node=node,
                                prompt_tokens=generation.prompt_tokens,
                                generated_tokens=generation.generated_tokens, latency_ms=generation.latency_ms,
                            ))
                    run_id = f'{bundle_id}-{condition}-{variant}'
                    run = dict(run_id=run_id, condition=condition, variant=variant,
                               position=position, completed_turns=0)
                    manifest['runs'].append(run)
                    async with Database(
                        output / f'{condition}-{variant}.db', pool_size=configured.sqlite_pool_size,
                        busy_timeout_ms=configured.sqlite_busy_timeout_ms,
                    ) as database:
                        graph = GraphStore(database)
                        extractor = RecordedExtractor(
                            client, max_tokens=GENERATION_PROTOCOL['extractor_max_tokens'],
                            max_retries=GENERATION_PROTOCOL['extractor_max_retries'], record_errors=True,
                        )
                        machine = StateMachine(
                            client, graph, agents=agents, extractor=extractor, clock=clock.now,
                            observation_clock=time.time, record_reply_errors=True,
                        )
                        if condition != 'A':
                            await machine.start(events[0].task, initial_subgoal=events[0].subject,
                                                max_turns=plan.turns, run_id=run_id)
                        history = []
                        with CSVLogger(output / f'{condition}-{variant}.csv') as logger:
                            logger.bind_run(run_id)
                            for event in events:
                                step, task = event.step, event.task
                                clock.step = step
                                stage = 'observation_extraction'
                                started = time.perf_counter()
                                initial_facts = catalog(graph)
                                if condition != 'A':
                                    incoming, observed, observation_failure = await ingest(machine, event, extractor, clock)
                                else:
                                    incoming, observed, observation_failure = (), (), None
                                before_facts = catalog(graph)
                                memory_before = snapshot(graph, event, before_facts)
                                stage = 'role_turn'
                                role = agents[(step - 1) % 3]
                                budget = min(plan.memory_budget, role.context_capacity(task, event.subject))
                                full_bytes = dropped = 0
                                pruned = ()
                                response_facts = ()
                                retrieved = ()
                                context_ids = ()
                                outgoing = ()
                                response_failure = None
                                if condition == 'A':
                                    context, full_bytes, dropped = linear_context(history, budget)
                                    try:
                                        result = await role.execute(task, event.subject, context, turn=step, allow_fallback=False)
                                    except AgentError as exc:
                                        result = rejected_reply(exc, agent_id=role.agent_id, turn=step)
                                    metrics = baseline_metrics(run_id, event, result, context, budget, started)
                                else:
                                    turn = await machine.step(run_id, task=task, subgoal=event.subject, governance_turn=step)
                                    result, context = turn.agent, turn.context.text
                                    metrics = TelemetryProfiler.from_checkpoint(turn.checkpoint)
                                    validate_routes(turn.extraction.attempts, client, 'node2')
                                    context_ids = turn.context.triple_ids
                                    retrieved = tuple(before_facts[fact_id] for fact_id in context_ids)
                                    pruned = tuple(turn.checkpoint.result['pruned_ids'])
                                    response_facts = tuple(turn.extraction.graph.triples)
                                    outgoing = turn.extraction.attempts
                                    response_failure = getattr(turn.extraction, 'failure', None)
                                validate_routes(result.generations, client, role.node)
                                after_facts = catalog(graph)
                                memory_after = snapshot(graph, event, after_facts)
                                answer = result.message.content if result.reply_error is None else None
                                valid, correct = evaluate(answer, event) if answer is not None else (False, False)
                                observation_ms = sum_attempt_field(incoming, 'latency_ms')
                                row = IntegratedRow(
                                    source=source, bundle_id=bundle_id, condition=condition, variant=variant,
                                    position=position, step=step, scenario_sha256=manifest['scenario_hashes'][str(variant)],
                                    task=task, observation=event.observation, answer=answer,
                                    reply_failure=reply_failure_evidence(result),
                                    observation_failure=observation_failure, response_failure=response_failure,
                                    observation_attempts=tuple(generation_evidence(generation) for generation in incoming),
                                    agent_attempts=tuple(generation_evidence(generation) for generation in result.generations),
                                    response_attempts=tuple(generation_evidence(generation) for generation in outgoing),
                                    format_valid=valid, correct=correct, context=context,
                                    context_triple_ids=context_ids, retrieved_facts=retrieved, logical_time=clock.now(),
                                    memory_before=memory_before, memory_after=memory_after,
                                    changes=changes(initial_facts, before_facts, 'observation') + changes(before_facts, after_facts, 'response'),
                                    pruned_entity_ids=pruned, observation_facts=observed, response_facts=response_facts,
                                    observation_calls=len(incoming), observation_prompt_tokens=sum_attempt_field(incoming, 'prompt_tokens'),
                                    observation_generated_tokens=sum_attempt_field(incoming, 'generated_tokens'), observation_latency_ms=observation_ms,
                                    total_prompt_tokens=add_costs(metrics.prompt_tokens, sum_attempt_field(incoming, 'prompt_tokens')),
                                    total_generated_tokens=add_costs(metrics.generated_tokens, sum_attempt_field(incoming, 'generated_tokens')),
                                    total_inference_ms=metrics.generation_latency_ms + observation_ms,
                                    wall_ms=(time.perf_counter() - started) * 1000,
                                    history_full_bytes=full_bytes, history_dropped_turns=dropped, telemetry=metrics,
                                )
                                flat = project(row, event)
                                if writer is None:
                                    writer = csv.DictWriter(csv_file, fieldnames=tuple(flat))
                                    writer.writeheader()
                                raw.write(row.model_dump_json() + '\n')
                                raw.flush()
                                writer.writerow(flat)
                                csv_file.flush()
                                logger.append(metrics)
                                history.append(dict(task=task, answer=answer))
                                run['completed_turns'] = step
                                if progress:
                                    progress(row)
                write_json(output / 'manifest.json', manifest)
        if source_provenance() != manifest['code_sha256']:
            raise ValueError('source changed during experiment')
        manifest['status'] = 'COMPLETED'
    except BaseException as exc:
        manifest['status'] = 'CANCELLED' if isinstance(exc, (KeyboardInterrupt, asyncio.CancelledError)) else 'FAILED'
        manifest['error_type'] = type(exc).__name__
        write_json(output / 'failure.json', failure_record(
            exc, manifest=manifest, condition=condition, repetition=variant, step=step, stage=stage, task=task,
        ))
        raise
    finally:
        manifest['finished_at'] = utc_now()
        manifest['artifacts'] = {
            str(artifact.relative_to(output)): sha256(artifact)
            for artifact in sorted(output.rglob('*')) if artifact.is_file() and artifact.name != 'manifest.json'
        }
        write_json(output / 'manifest.json', manifest)
    return manifest


def main(argv=None):
    """Print the plan by default; --execute and a new output path opt into inference."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--turns', type=int, default=60)
    parser.add_argument('--variants', type=int, default=4)
    args = parser.parse_args(argv)
    try:
        plan = IntegratedPlan(turns=args.turns, variants=args.variants)
        if not args.execute:
            print(f'PLAN ONLY: {len(plan.order())} condition runs; {plan.row_count} scored turns; no model requests or experiment files')
            print(json.dumps(plan.model_dump() | {
                'order': plan.order(), 'scenario_hashes': plan.scenario_hashes(),
                'fully_counterbalanced': plan.variants % 4 == 0,
                'reply_error_policy': REPLY_ERROR_POLICY, 'inference_error_policy': INFERENCE_ERROR_POLICY,
            }, indent=2))
            return 0
        if args.output is None:
            parser.error('--execute requires --output with a new directory')
        from src.config import get_settings

        def progress(row):
            """Print each recorded outcome without treating failures as accepted replies."""
            outcome = 'rejected' if row.reply_failure else 'valid'
            incoming = 'failed' if row.observation_failure else ('valid' if row.observation_calls else 'skipped')
            outgoing = 'failed' if row.response_failure else ('valid' if row.telemetry.extraction_calls else 'skipped')
            print(f'TURN {row.condition} variant={row.variant} step={row.step}/{plan.turns} reply_json={outcome} '
                  f'observation_extraction={incoming} response_extraction={outgoing} wall_ms={row.wall_ms:.1f}', flush=True)

        manifest = asyncio.run(run_integrated(plan, get_settings(), args.output, execute=True, progress=progress))
        print('INTEGRATED EXPERIMENT COMPLETE:', manifest['bundle_id'])
    except (KeyboardInterrupt, asyncio.CancelledError):
        print('EXPERIMENT STOPPED: partial evidence retained', file=sys.stderr)
        return 130
    except Exception as exc:
        print(f'ERROR: {type(exc).__name__}: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
