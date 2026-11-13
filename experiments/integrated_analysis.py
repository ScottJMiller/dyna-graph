"""Validate a completed integrated bundle and summarize its measured outcomes.

Validation replays the saved memory changes and checks them against SQLite.
Analysis never initializes evidence databases or makes inference requests.
"""

import argparse
from collections import Counter
import csv
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import statistics
import sys

from experiments.common import linear_context
from experiments.contracts import load_json, sha256, write_json
from experiments.integrated_contracts import (
    CONDITIONS, GENERATION_PROTOCOL, REPLY_ERROR_POLICY, INFERENCE_ERROR_POLICY,
    PRIMARY_OUTCOMES, HYPOTHESES, FactState, IntegratedPlan, IntegratedRow, project,
)
from experiments.integrated_scenario import evaluate, records, role_at, workload
from src.agents.base_agent import REJECTED_REPLY_CONTENT
from src.retrieval.context_assembler import quote_label


def require(value, message):
    """Reject inconsistent evidence with a specific validation message."""
    if not value:
        raise ValueError(message)


def _target(catalog, event):
    """Select all historical facts for the question's subject and predicate."""
    return tuple(
        fact for fact in catalog.values()
        if fact.subject.casefold() == event.subject.casefold()
        and fact.predicate == event.predicate
    )


def _apply(catalog, changes, stage):
    """Replay one stage's changes, checking each recorded prior status."""
    for change in changes:
        if change.stage != stage:
            continue
        previous = catalog.get(change.fact.id)
        previous_status = None if previous is None else previous.status
        require(change.before == previous_status, 'state transition has wrong prior status')
        require(previous != change.fact, 'redundant state transition')
        catalog[change.fact.id] = change.fact


def _checkpoint_rows(path, condition, turns):
    """Read durable turns from an existing SQLite file without migrating it."""
    with sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True) as connection:
        require(connection.execute('PRAGMA integrity_check').fetchone()[0] == 'ok',
                'SQLite integrity failure')
        require(not connection.execute('PRAGMA foreign_key_check').fetchall(),
                'SQLite foreign key failure')
        if condition == 'A':
            require(connection.execute('SELECT count(*) FROM triples').fetchone()[0] == 0,
                    'baseline database has graph facts')
            require(connection.execute('SELECT count(*) FROM workflow_turns').fetchone()[0] == 0,
                    'baseline has graph checkpoints')
            return []
        runs = connection.execute('SELECT status,next_step FROM workflow_runs').fetchall()
        require(runs == [('COMPLETED', turns)], 'graph run is not durably complete')
        rows = connection.execute(
            'SELECT run_id,step,governance_turn,agent_id,node,message,result '
            'FROM workflow_turns ORDER BY step'
        ).fetchall()
        require(len(rows) == turns, 'missing graph checkpoints')
        return rows


def _validate_manifest(path):
    """Check protocol declarations and return the manifest and validated plan."""
    require((path / 'manifest.json').stat().st_size <= 2_000_000, 'oversized manifest')
    manifest = load_json(path / 'manifest.json')
    require(
        type(manifest.get('version')) is int and manifest['version'] == 4
        and manifest.get('protocol') == 'integrated-memory-v3'
        and manifest.get('status') == 'COMPLETED',
        'integrated analysis requires a completed protocol-4 bundle',
    )
    require(manifest.get('source') in ('measured', 'synthetic'), 'invalid evidence source')
    plan = IntegratedPlan.model_validate(manifest['plan'])
    require(manifest['conditions'] == json.loads(json.dumps(CONDITIONS)), 'condition policies changed')
    require(manifest['generation_protocol'] == GENERATION_PROTOCOL, 'generation protocol differs')
    require(manifest['reply_error_policy'] == REPLY_ERROR_POLICY, 'reply error policy differs')
    require(manifest['inference_error_policy'] == INFERENCE_ERROR_POLICY, 'inference error policy differs')
    require(manifest['primary_outcomes'] == PRIMARY_OUTCOMES and manifest['hypotheses'] == HYPOTHESES,
            'declared outcomes differ')
    require(manifest['fully_counterbalanced'] == (plan.variants % 4 == 0),
            'balance declaration differs from plan')
    require(manifest['scenario_hashes'] == plan.scenario_hashes(), 'scenario fingerprint differs')
    expected_scenarios = {
        str(variant): records(plan.turns, variant)
        for variant in range(1, plan.variants + 1)
    }
    require(manifest['scenarios'] == json.loads(json.dumps(expected_scenarios)),
            'rendered inputs or independent oracle differ')
    _validate_policies(manifest, plan)
    return manifest, plan


def _validate_policies(manifest, plan):
    """Require the declared condition differences and fixed model/role settings."""
    require(set(manifest['policies']) == set(CONDITIONS), 'condition policies missing')
    base_policy = manifest['policies']['A']
    for condition, policy in manifest['policies'].items():
        expected = base_policy | dict(
            single_valued_predicates=list(CONDITIONS[condition]['single_valued_predicates']),
            orchestration_prune_between_turns=CONDITIONS[condition]['prune'],
            orchestration_allow_fallback=False,
            orchestration_max_turns=plan.turns,
            context_token_budget=plan.memory_budget,
            agent_max_tokens=plan.max_tokens,
            agent_max_tool_rounds=0,
            pruning_threshold=plan.pruning_threshold,
            pruning_batch_size=100,
            decay_recency_half_life_seconds=plan.recency_half_life_seconds,
            decay_turn_half_life=plan.turn_half_life,
        )
        require(policy == expected, 'condition policy differs from declared experiment')
        private_fields = {'database_path', 'node1_ollama_url', 'node2_ollama_url', 'coordinator_host'}
        require(not private_fields & set(policy), 'private configuration in manifest')
    require(set(manifest['inventory']) == {'node1', 'node2'}, 'node inventory missing')
    for node, model in manifest['inventory'].items():
        require(
            model['name'] == base_policy[f'{node}_ollama_model']
            and model['threads'] == base_policy[f'{node}_ollama_num_threads']
            and model['quantization_level'] == 'Q4_K_M'
            and isinstance(model['digest'], str) and len(model['digest']) == 64,
            'model inventory differs from policy',
        )
    require([(role['agent_id'], role['node']) for role in manifest['roles']]
            == [role_at(step) for step in (1, 2, 3)], 'role declarations differ')
    require(all(role['max_tokens'] == plan.max_tokens and role['tool_rounds'] == 0
                for role in manifest['roles']), 'role limits differ')


def _validate_artifacts(path, manifest, plan):
    """Verify the required artifact names and their original SHA-256 hashes."""
    expected_files = {'turns.jsonl', 'metrics.csv'}
    for variant, condition in plan.order():
        for suffix in ('db', 'csv'):
            expected_files.add(f'{condition}-{variant}.{suffix}')
    require(set(manifest['artifacts']) == expected_files, 'missing or unexpected experiment artifacts')
    for name, digest in manifest['artifacts'].items():
        target = (path / name).resolve()
        require(not Path(name).is_absolute() and target.is_relative_to(path), 'unsafe artifact path')
        require(target.is_file() and sha256(target) == digest, f'artifact checksum mismatch: {name}')


def _read_turns(path, plan):
    """Load bounded JSONL records and require the full planned cohort."""
    require((path / 'turns.jsonl').stat().st_size <= plan.row_count * 1_000_000,
            'turn evidence exceeds bound')
    rows = []
    with (path / 'turns.jsonl').open() as stream:
        for line in stream:
            require(len(line) <= 1_000_000 and len(rows) < plan.row_count,
                    'oversized or excess turn evidence')
            rows.append(IntegratedRow.model_validate_json(line))
    require(len(rows) == plan.row_count, 'incomplete scored turns')
    return rows


def _validate_turn_identity(row, event, manifest, plan, run):
    """Check a turn's frozen input, score, routing, retries and two clocks."""
    require(
        (row.source, row.bundle_id, row.condition, row.variant, row.position,
         row.step, row.scenario_sha256)
        == (manifest['source'], manifest['bundle_id'], run['condition'], run['variant'],
            run['position'], event.step, manifest['scenario_hashes'][str(run['variant'])]),
        'turn identity/order differs',
    )
    require((row.task, row.observation) == (event.task, event.observation),
            'input differs from frozen workload')
    expected_score = (False, False) if row.reply_failure else evaluate(row.answer, event)
    require((row.format_valid, row.correct) == expected_score, 'answer scoring differs')
    require((row.telemetry.agent_id, row.telemetry.node) == role_at(event.step), 'role/node route differs')
    require(row.telemetry.run_id == run['run_id'] and row.telemetry.step == row.step
            and row.telemetry.governance_turn == row.step, 'checkpoint identity differs')
    require(not row.telemetry.fallback_attempts, 'fallback is prohibited')
    for attempts, node in [
        (row.agent_attempts, row.telemetry.node),
        (row.observation_attempts, 'node2'),
        (row.response_attempts, 'node2'),
    ]:
        require(all(attempt.node == node and attempt.model == manifest['inventory'][node]['name']
                    for attempt in attempts), 'attempt route/model differs')
    for failure in (row.observation_failure, row.response_failure):
        if failure:
            require(len(failure.attempts) == GENERATION_PROTOCOL['extractor_max_retries'] + 1,
                    'extraction correction attempts are incomplete')
    if row.reply_failure:
        role = next(role for role in manifest['roles'] if role['agent_id'] == row.telemetry.agent_id)
        require(len(row.reply_failure.attempts) == role['max_retries'] + 1,
                'reply correction attempts are incomplete')
        require(all(attempt.node == row.telemetry.node
                    and attempt.model == manifest['inventory'][attempt.node]['name']
                    for attempt in row.reply_failure.attempts), 'rejected reply route differs')
    require(row.logical_time == event.step * plan.hours_per_turn * 3600, 'logical time differs')
    started = datetime.fromisoformat(manifest['started_at']).timestamp()
    finished = datetime.fromisoformat(manifest['finished_at']).timestamp()
    require(started <= row.telemetry.observed_at <= finished,
            'observation timestamp is not real run time')


def _validate_snapshots(row, event):
    """Check snapshot targets, governance timestamps and recorded retrieval IDs."""
    for state in (row.memory_before, row.memory_after):
        require(all(fact.subject.casefold() == event.subject.casefold()
                    and fact.predicate == event.predicate for fact in state.target_facts),
                'snapshot includes wrong target facts')
        require(state.last_access_at is None or state.last_access_at <= row.logical_time,
                'mixed governance clocks')
        require(state.last_access_turn is None or state.last_access_turn <= row.step,
                'mixed governance turns')
    require((row.memory_after.active_nodes, row.memory_after.active_edges)
            == (row.telemetry.active_nodes, row.telemetry.active_edges),
            'graph counts differ from checkpoint')
    require([fact.id for fact in row.retrieved_facts] == list(row.context_triple_ids)
            and len(set(row.context_triple_ids)) == len(row.context_triple_ids),
            'retrieved IDs differ')


def _validate_graph_snapshot(state, catalog, event, stage):
    """Compare one snapshot with the replayed facts, edge count and conflicts."""
    require(state.target_facts == _target(catalog, event), f'{stage} facts differ from event history')
    require(state.active_edges == sum(fact.live for fact in catalog.values()),
            f'{stage} edge count differs')
    groups = Counter(fact.subject for fact in catalog.values() if fact.live and fact.predicate == 'uses')
    require(state.single_value_conflicts == sum(count > 1 for count in groups.values()),
            f'{stage} conflicts differ')


def _validate_checkpoint(checkpoint, row, event, run_id):
    """Compare saved JSONL evidence with the corresponding durable SQL turn."""
    payload = json.loads(checkpoint[6])
    message = json.loads(checkpoint[5])
    require(tuple(checkpoint[:5]) == (run_id, event.step, event.step, *role_at(event.step)),
            'SQLite checkpoint identity differs')
    expected_content = REJECTED_REPLY_CONTENT if row.reply_failure else row.answer
    require(message['content'] == expected_content and payload['task'] == row.task,
            'SQLite answer/input differs')
    require(payload['reply_failure'] == (None if row.reply_failure is None
                                        else row.reply_failure.model_dump(mode='json')),
            'SQLite reply failure differs')
    require(payload['extraction_failure'] == (None if row.response_failure is None
                                             else row.response_failure.model_dump(mode='json')),
            'SQLite extraction failure differs')
    require(payload['agent_attempts'] == [attempt.model_dump(mode='json') for attempt in row.agent_attempts]
            and payload['response_attempts'] == [attempt.model_dump(mode='json') for attempt in row.response_attempts],
            'SQLite inference attempts differ')
    if row.reply_failure:
        require(not payload['fact_ids'] and not payload['extraction_attempts'] and not payload['tools'],
                'rejected reply wrote response facts or ran extraction/tools')
        require(len(payload['agent_generations']) == len(row.reply_failure.attempts),
                'SQLite failed generation count differs')
        for generation, attempt in zip(payload['agent_generations'], row.reply_failure.attempts):
            require(all(generation.get(key) == value for key, value in attempt.model_dump().items()
                        if key != 'response'), 'SQLite rejected generation metrics differ')
    require(payload['context']['text'] == row.context
            and payload['context']['triple_ids'] == list(row.context_triple_ids),
            'SQLite recalled context differs')
    require(payload['telemetry'] == row.telemetry.model_dump()
            and payload['governance_time'] == row.logical_time, 'SQLite timing/telemetry differs')
    require(payload['pruned_ids'] == list(row.pruned_entity_ids), 'SQLite pruning event differs')
    require(payload['extracted_facts'] == [fact.model_dump() for fact in row.response_facts],
            'SQLite extraction differs')


def _validate_final_graph(path, catalog):
    """Require the final read-only SQLite graph to match the replayed catalog."""
    with sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute('''SELECT t.id,s.name AS subject,t.predicate,o.name AS object,t.status,
            (s.status='ACTIVE') AS subject_active,(o.status='ACTIVE') AS object_active
            FROM triples t JOIN entities s ON s.id=t.subject_id JOIN entities o ON o.id=t.object_id ORDER BY t.id''').fetchall()
    final = {}
    for row in rows:
        fields = dict(row) | {
            'subject_active': bool(row['subject_active']),
            'object_active': bool(row['object_active']),
        }
        final[row['id']] = FactState(**fields)
    require(final == catalog, 'final graph differs from event history')


def _validate_run(path, manifest, plan, rows, run, budgets):
    """Replay one condition/variant in order and return its analysis projection."""
    condition = run['condition']
    variant = run['variant']
    checkpoints = _checkpoint_rows(path / f'{condition}-{variant}.db', condition, plan.turns)
    with (path / f'{condition}-{variant}.csv').open(newline='') as stream:
        telemetry_rows = list(csv.DictReader(stream))
    require(len(telemetry_rows) == plan.turns, 'telemetry CSV completeness differs')
    history = []
    catalog = {}
    flat = []
    for event, row in zip(workload(plan.turns, variant), rows):
        _validate_turn_identity(row, event, manifest, plan, run)
        require(row.telemetry.context_budget_limit <= plan.memory_budget, 'memory allowance exceeded')
        budget_key = (variant, event.step)
        budgets.setdefault(budget_key, row.telemetry.context_budget_limit)
        require(budgets[budget_key] == row.telemetry.context_budget_limit,
                'memory allowances differ across conditions')
        _validate_snapshots(row, event)

        _apply(catalog, row.changes, 'observation')
        _validate_graph_snapshot(row.memory_before, catalog, event, 'pre-recall')
        for fact in row.retrieved_facts:
            require(fact.live and catalog.get(fact.id) == fact,
                    'retrieval contains inactive or unrecorded fact')
            line = f'- [t{fact.id}] {quote_label(fact.subject)} -- {quote_label(fact.predicate)} --> {quote_label(fact.object)}'
            require(line in row.context, 'retrieved fact missing from supplied memory')
        _apply(catalog, row.changes, 'response')
        _validate_graph_snapshot(row.memory_after, catalog, event, 'post-write')

        if condition == 'A':
            context, full_bytes, dropped = linear_context(history, row.telemetry.context_budget_limit)
            require((row.context, row.history_full_bytes, row.history_dropped_turns)
                    == (context, full_bytes, dropped), 'baseline truncation differs')
        else:
            require(row.observation_calls > 0 if event.observation else row.observation_calls == 0,
                    'input extraction usage differs')
            require(row.telemetry.extraction_calls == 0 if row.reply_failure
                    else row.telemetry.extraction_calls > 0, 'response extraction policy differs')
            _validate_checkpoint(checkpoints[event.step - 1], row, event, run['run_id'])
        if condition in ('A', 'B', 'C'):
            require(not row.pruned_entity_ids and not any(change.fact.status == 'PRUNED' for change in row.changes),
                    'disabled pruning changed state')
        if condition == 'B':
            require(not any(change.fact.status == 'SUPERSEDED' for change in row.changes),
                    'disabled supersession changed state')
        if condition in ('C', 'D'):
            require(row.memory_before.single_value_conflicts == row.memory_after.single_value_conflicts == 0,
                    'single-valued invariant violated')
        expected_telemetry = {
            key: str(value) if value is not None else ''
            for key, value in row.telemetry.model_dump().items()
        }
        require(expected_telemetry == telemetry_rows[event.step - 1], 'telemetry CSV differs')
        flat.append(project(row, event))
        history.append(dict(task=event.task, answer=row.answer))
    if condition != 'A':
        _validate_final_graph(path / f'{condition}-{variant}.db', catalog)
    return flat


def _validate_metrics_csv(path, flat):
    """Check that the combined CSV is exactly the validated JSONL projection."""
    with (path / 'metrics.csv').open(newline='') as stream:
        reader = csv.DictReader(stream)
        require(tuple(reader.fieldnames or ()) == tuple(flat[0]), 'metric CSV schema differs')
        expected = [
            {key: '' if value is None else str(value) for key, value in row.items()}
            for row in flat
        ]
        require(list(reader) == expected, 'metric CSV differs from turn evidence')


def load_bundle(path):
    """Return validated manifest, plan, turn records and projected analysis rows.

    A partial, corrupted or inconsistent cohort raises ValueError. Existing
    database files are opened read-only, and no output files are created.
    """
    path = Path(path).expanduser().resolve()
    manifest, plan = _validate_manifest(path)
    _validate_artifacts(path, manifest, plan)
    rows = _read_turns(path, plan)
    expected_runs = []
    flat = []
    budgets = {}
    for position, (variant, condition) in enumerate(plan.order(), 1):
        run = dict(
            run_id=f'{manifest["bundle_id"]}-{condition}-{variant}',
            condition=condition, variant=variant, position=position, completed_turns=plan.turns,
        )
        expected_runs.append(run)
        selected = rows[(position - 1) * plan.turns:position * plan.turns]
        flat.extend(_validate_run(path, manifest, plan, selected, run, budgets))
    require(manifest['runs'] == expected_runs, 'manifest run completeness differs')
    _validate_metrics_csv(path, flat)
    return manifest, plan, tuple(rows), tuple(flat)


def complete_total(values):
    """Sum a complete series; one unavailable counter makes the total unknown."""
    return None if any(value is None for value in values) else sum(values)


def mean(values):
    """Return the mean only when the series is nonempty and fully observed."""
    return None if not values or any(value is None for value in values) else statistics.mean(values)


def accuracy(rows):
    """Count every scheduled answer, including rejected replies, in accuracy."""
    return None if not rows else sum(row['correct'] for row in rows) / len(rows)


def describe(rows):
    """Summarize one selected group of turns without dropping failed outcomes."""
    by_category = {}
    for category in sorted({row['category'] for row in rows}):
        items = [row for row in rows if row['category'] == category]
        if items:
            by_category[category] = dict(n=len(items), accuracy=accuracy(items))
    lag_groups = {}
    for label, low, high in [('0', 0, 0), ('1-9', 1, 9), ('10-29', 10, 29), ('30+', 30, 1000)]:
        lag_groups[label] = [
            row for row in rows if row['lag'] is not None and low <= row['lag'] <= high
        ]
    cross_node = [row for row in rows if row['cross_node'] and row['lag'] > 0]
    delayed = [row for row in rows if row['lag'] is not None and row['lag'] >= 10]
    final_edges = {}
    for variant in sorted({row['variant'] for row in rows}):
        variant_rows = [row for row in rows if row['variant'] == variant]
        final_edges[str(variant)] = variant_rows[-1]['active_edges']
    return dict(
        turns=len(rows),
        correct=sum(row['correct'] for row in rows),
        accuracy=accuracy(rows),
        format_valid=sum(row['format_valid'] for row in rows),
        invalid_reply_turns=sum(row['reply_failed'] for row in rows),
        invalid_reply_rate=sum(row['reply_failed'] for row in rows) / len(rows),
        observation_extraction_failures=sum(row['observation_failed'] for row in rows),
        response_extraction_failures=sum(row['response_extraction_failed'] for row in rows),
        prediction_aborts=sum(row['prediction_aborts'] for row in rows),
        role_prompt_tokens=complete_total([row['role_prompt_tokens'] for row in rows]),
        total_prompt_tokens=complete_total([row['total_prompt_tokens'] for row in rows]),
        total_generated_tokens=complete_total([row['total_generated_tokens'] for row in rows]),
        role_counter_coverage=sum(row['role_prompt_tokens'] is not None for row in rows),
        total_counter_coverage=sum(row['total_prompt_tokens'] is not None for row in rows),
        mean_wall_ms=mean([row['wall_ms'] for row in rows]),
        history_truncated_turns=sum(row['history_dropped_turns'] > 0 for row in rows),
        max_memory_bytes=max(row['memory_bytes'] for row in rows),
        max_unwindowed_history_bytes=max(row['history_full_bytes'] for row in rows),
        pruned_triple_events=sum(row['pruned_triples'] for row in rows),
        superseded_triple_events=sum(row['superseded_triples'] for row in rows),
        by_category=by_category,
        by_lag={label: dict(n=len(items), accuracy=accuracy(items)) for label, items in lag_groups.items()},
        cross_node=dict(n=len(cross_node), accuracy=accuracy(cross_node)),
        delayed=dict(n=len(delayed), accuracy=accuracy(delayed)),
        graph_support_missing=sum(row['support_available'] is False for row in rows),
        missing_support_states={
            state: sum(state in row['missing_support_states'].split('|') for row in rows)
            for state in ('absent', 'superseded', 'pruned')
        },
        turns_with_single_value_conflicts=sum(row['single_value_conflicts'] > 0 for row in rows),
        max_single_value_conflicts=max(row['single_value_conflicts'] for row in rows),
        retrieval_support_missing=sum(row['support_available'] is True and row['support_supplied'] is False for row in rows),
        answer_errors_with_support=sum(row['total_support_supplied'] is True and not row['correct'] for row in rows),
        final_active_edges_by_variant=final_edges,
        mean_final_active_edges=mean(list(final_edges.values())),
    )


def _paired_contrasts(flat, plan):
    """Compare conditions within each workload variant, always right minus left."""
    contrasts = []
    for variant in range(1, plan.variants + 1):
        stats = {
            condition: describe([row for row in flat if row['condition'] == condition and row['variant'] == variant])
            for condition in CONDITIONS
        }
        for left, right, purpose in [
            ('A', 'D', 'complete framework'), ('B', 'C', 'supersession'), ('C', 'D', 'pruning'),
        ]:
            before, after = stats[left], stats[right]
            contrasts.append(dict(
                variant=variant, left=left, right=right, purpose=purpose,
                accuracy_delta=after['accuracy'] - before['accuracy'],
                invalid_reply_rate_delta=after['invalid_reply_rate'] - before['invalid_reply_rate'],
                delayed_accuracy_delta=None if before['delayed']['accuracy'] is None else after['delayed']['accuracy'] - before['delayed']['accuracy'],
                role_prompt_delta=(
                    None if before['role_prompt_tokens'] is None or after['role_prompt_tokens'] is None
                    else after['role_prompt_tokens'] - before['role_prompt_tokens']
                ),
                pipeline_prompt_delta=(
                    None if before['total_prompt_tokens'] is None or after['total_prompt_tokens'] is None
                    else after['total_prompt_tokens'] - before['total_prompt_tokens']
                ),
                mean_wall_ms_delta=after['mean_wall_ms'] - before['mean_wall_ms'],
            ))
    return contrasts


def _contrast_summary(contrasts):
    """Report descriptive variant means/SDs, keeping incomplete totals unknown."""
    summary = {}
    for purpose in ('complete framework', 'supersession', 'pruning'):
        selected = [contrast for contrast in contrasts if contrast['purpose'] == purpose]
        summary[purpose] = {}
        for key in (
            'accuracy_delta', 'invalid_reply_rate_delta', 'delayed_accuracy_delta',
            'role_prompt_delta', 'pipeline_prompt_delta', 'mean_wall_ms_delta',
        ):
            values = [contrast[key] for contrast in selected]
            complete = all(value is not None for value in values)
            summary[purpose][key] = dict(
                n_variants=len(values),
                known_variants=sum(value is not None for value in values),
                mean=mean(values),
                sd=statistics.stdev(values) if complete and len(values) > 1 else None,
            )
    return summary


def analyze_bundle(path):
    """Validate a complete bundle and return descriptive condition comparisons."""
    manifest, plan, rows, flat = load_bundle(path)
    conditions = {
        condition: describe([row for row in flat if row['condition'] == condition])
        for condition in CONDITIONS
    }
    contrasts = _paired_contrasts(flat, plan)
    curves = []
    for condition in CONDITIONS:
        for step in range(1, plan.turns + 1):
            items = [row for row in flat if row['condition'] == condition and row['step'] == step]
            curves.append(dict(
                condition=condition, step=step, n=len(items), accuracy=accuracy(items),
                **{key: mean([row[key] for row in items])
                   for key in ['role_prompt_tokens', 'total_prompt_tokens', 'wall_ms', 'active_edges']},
            ))
    checkpoints = []
    for through_step in (20, 40, 60, 80):
        if through_step <= plan.turns:
            for condition in CONDITIONS:
                selected = [row for row in flat if row['condition'] == condition and row['step'] <= through_step]
                checkpoints.append(dict(condition=condition, through_step=through_step, **describe(selected)))
    return dict(
        version=4, source=manifest['source'], bundle_id=manifest['bundle_id'], plan=plan.model_dump(),
        conditions=conditions, contrasts=contrasts, contrast_summary=_contrast_summary(contrasts),
        curves=curves, checkpoints=checkpoints,
        interpretation='Descriptive workload-variant comparisons; simulated age and measured inference. '
            'Bounded history and graph prompts are both capped. No asymptotic proof, biological validation, '
            'replicated-storage claim, or universal preservation guarantee. Missing counters remain unknown. '
            'Scripted inputs reach routed roles; cross-node scores measure later consumers, not autonomous discovery.',
    )


def write_analysis(bundle, output):
    """Validate first, then write JSON and CSV summaries to a fresh directory."""
    report = analyze_bundle(bundle)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / 'summary.json', report)
    for name, data in [('contrasts.csv', report['contrasts']), ('curves.csv', report['curves'])]:
        with (output / name).open('w', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=tuple(data[0]))
            writer.writeheader()
            writer.writerows(data)
    return report


def recall_gap_points(rows, condition):
    """Aggregate strict accuracy at every observed numeric gap; omit undefined gaps."""
    gaps = sorted({row['lag'] for row in rows
                   if row['condition'] == condition and row['lag'] is not None})
    points = []
    for gap in gaps:
        selected = [row for row in rows if row['condition'] == condition and row['lag'] == gap]
        points.append(dict(gap=gap, n=len(selected), correct=sum(row['correct'] for row in selected),
                           accuracy=accuracy(selected)))
    return points


def accuracy_bar_data(rows):
    """Keep pooled counts and paired variant scores for the two accuracy panels."""
    panels = {}
    for category in ('overall', 'delayed'):
        panels[category] = {}
        for condition in CONDITIONS:
            selected = [row for row in rows if row['condition'] == condition
                        and (category == 'overall' or row['category'] == 'delayed')]
            variants = {}
            for variant in sorted({row['variant'] for row in selected}):
                items = [row for row in selected if row['variant'] == variant]
                variants[str(variant)] = dict(n=len(items), accuracy=accuracy(items))
            panels[category][condition] = dict(
                n=len(selected), correct=sum(row['correct'] for row in selected),
                accuracy=accuracy(selected), variants=variants,
            )
    return panels


def generate_plots(bundle, output):
    """Export eight paper figure sets from validated evidence, without inference."""
    report = analyze_bundle(bundle)
    _, _, _, rows = load_bundle(bundle)
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    from matplotlib.ticker import PercentFormatter
    from experiments.plot_style import (
        COLORS, MARKERS, paper_style, plot_series, save_figure, write_figure_notes,
    )

    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    synthetic = report['source'] == 'synthetic'
    variants = report['plan']['variants']
    paths, captions, data = [], {}, {}
    titles = [
        ('role_prompt_tokens', 'Role Prompt Usage', 'Mean Role Prompt Tokens'),
        ('total_prompt_tokens', 'Full-Pipeline Prompt Usage', 'Mean Full-Pipeline Prompt Tokens'),
        ('wall_ms', 'Turn Latency', 'Mean Turn Latency (s)'),
        ('active_edges', 'Active Graph Size', 'Mean Active Triples'),
        ('accuracy', 'Strict Accuracy by Turn', 'Strict Accuracy'),
        ('delayed_recall', 'Accuracy by Recall Gap', 'Strict Accuracy'),
        ('delayed_recall_exact', 'Accuracy at Observed Recall Gaps', 'Strict Accuracy'),
    ]
    with paper_style():
        for key, title, ylabel in titles:
            figure = Figure(figsize=(7.16, 3.3), layout='constrained')
            FigureCanvasAgg(figure)
            axes = figure.subplots()
            data[key] = {}
            for condition in CONDITIONS:
                if key == 'active_edges' and condition == 'A':
                    continue
                if key == 'delayed_recall':
                    points = report['conditions'][condition]['by_lag']
                    x = list(points)
                    y = [points[label]['accuracy'] for label in x]
                    data[key][condition] = points
                elif key == 'delayed_recall_exact':
                    points = recall_gap_points(rows, condition)
                    x = [point['gap'] for point in points]
                    y = [point['accuracy'] for point in points]
                    data[key][condition] = points
                else:
                    points = [row for row in report['curves'] if row['condition'] == condition]
                    x = [row['step'] for row in points]
                    y = [None if row[key] is None else row[key] / (1000 if key == 'wall_ms' else 1)
                         for row in points]
                    data[key][condition] = [dict(turn=turn, value=value, n=point['n'])
                                            for turn, value, point in zip(x, y, points)]
                plot_series(axes, condition, x, y)
            axes.set(xlabel='Recall Gap (turns)' if key.startswith('delayed_recall') else 'Turn',
                     ylabel=ylabel, title=title)
            if key in ('accuracy', 'delayed_recall', 'delayed_recall_exact'):
                axes.yaxis.set_major_formatter(PercentFormatter(xmax=1))
                axes.set_ylim(-.025, 1.06)
            else:
                axes.set_ylim(bottom=0)
            axes.legend(loc='upper left', bbox_to_anchor=(1.01, 1), frameon=False)
            paths.extend(save_figure(figure, output, key, synthetic=synthetic))
            if key == 'delayed_recall':
                denominators = report['conditions']['A']['by_lag']
                counts = ', '.join(f'{label}: n={point["n"]}' for label, point in denominators.items())
                captions[key] = (
                    'Strict accuracy pooled within the declared recall-gap buckets. '
                    f'Scheduled counts per condition: {counts}. Points are categorical groups, '
                    'not a fitted forgetting curve. Thin straight lines guide the eye between observed groups; '
                    'they do not estimate accuracy between them. Undefined gaps (unknown-answer probes) are excluded. '
                    'Gap measures turns since scheduled observation/questioning, not original fact age.'
                )
            elif key == 'delayed_recall_exact':
                captions[key] = (
                    'Strict accuracy at every observed numeric recall gap, pooled across workload variants. '
                    'Points use their actual numeric x positions. Thin straight connecting lines guide the eye; '
                    'they do not estimate accuracy at unobserved gaps. No smoothing is applied. '
                    'Overlapping values share coordinates; marker shapes distinguish conditions. '
                    'Counts and successes at each gap are in plot_data.json. Undefined gaps are excluded. '
                    'Question composition differs between gaps; this does not isolate forgetting alone.'
                )
            else:
                captions[key] = f'Per-turn arithmetic means across {variants} workload variants. '
                if key == 'active_edges':
                    captions[key] += ('A has no graph and is omitted. Counts are active logical triples, '
                                      'not database-space savings; soft-pruned records remain stored.')
                elif key == 'wall_ms':
                    captions[key] += ('Recorded elapsed turn time converted from milliseconds to seconds. '
                                      'Includes the work and failed attempts recorded inside each turn; '
                                      'setup/warmups are outside these per-turn measurements.')
                elif key == 'accuracy':
                    captions[key] += ('Original strict answer-contract scoring, including rejected replies as incorrect. '
                                      'Turns are not independent experimental repetitions.')
                else:
                    captions[key] += ('Role costs include role correction attempts. Full-pipeline costs also '
                                      'include observation/response extraction and corrections. '
                                      'A missing counter in any contributing variant leaves a gap; '
                                      'there is no imputation or selective-subset mean. Failed-attempt costs '
                                      'are included when counters are available.')

        figure = Figure(figsize=(7.16, 3.5), layout='constrained')
        FigureCanvasAgg(figure)
        axes = figure.subplots(1, 2, sharey=True)
        panels = accuracy_bar_data(rows)
        data['overall_accuracy'] = panels
        for axis, category, title in zip(
            axes, ('overall', 'delayed'), ('Overall Strict Accuracy', 'Delayed Birch Probe Accuracy'),
        ):
            values = panels[category]
            for position, condition in enumerate(CONDITIONS):
                point = values[condition]
                fraction = point['accuracy']
                if fraction is None:
                    axis.text(position, .03, 'N/A', ha='center', fontsize=8)
                    continue
                axis.bar(position, fraction, width=.62, color=COLORS[condition], alpha=.8,
                         edgecolor='black', linewidth=.5, zorder=2)
                variant_scores = list(point['variants'].values())
                # Offset the paired variant points deterministically, even at 0% or 100%.
                offsets = [0] if len(variant_scores) == 1 else [
                    -.16 + .32 * index / (len(variant_scores) - 1)
                    for index in range(len(variant_scores))
                ]
                axis.scatter([position + offset for offset in offsets],
                             [item['accuracy'] for item in variant_scores],
                             marker=MARKERS[condition], s=12, facecolors='white',
                             edgecolors='black', linewidths=.6, zorder=3, clip_on=False)
                axis.text(position, max([fraction] + [item['accuracy'] for item in variant_scores]) + .035,
                          f'{fraction:.2%}\n{point["correct"]}/{point["n"]}',
                          ha='center', va='bottom', fontsize=7)
            axis.set(title=title, xlabel='Condition', xticks=range(4), xticklabels=list(CONDITIONS),
                     ylim=(0, 1.18))
            axis.set_yticks([0, .2, .4, .6, .8, 1])
            axis.yaxis.set_major_formatter(PercentFormatter(xmax=1))
            axis.grid(axis='x', visible=False, which='both')
            axis.set_axisbelow(True)
        axes[0].set_ylabel('Strict Accuracy')
        paths.extend(save_figure(figure, output, 'overall_accuracy', synthetic=synthetic))
        captions['overall_accuracy'] = (
            'Left: overall strict accuracy across all scheduled turns, with successes/denominators. '
            'Right: the dedicated delayed Birch probes for all four conditions; B is retained to show '
            'its successful recall alongside C. Open markers are individual workload-variant scores, '
            'in the same left-to-right variant order within each bar; they are not independent turn samples '
            'or confidence intervals. The Birch comparison is a specific scheduled probe result, not a '
            'universal retention guarantee. The C–D difference is the observed pruning-policy contrast, '
            'not an identical-graph intervention. A’s low overall strict score also reflects answer-format '
            'failures and cannot be attributed entirely to forgetting.'
        )
    write_figure_notes(output, report, captions,
                       {key: value['name'] for key, value in CONDITIONS.items()}, data, bundle)
    return tuple(paths)


def main(argv=None):
    """Analyze or plot an existing bundle; never launch a model-driven study."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--figures', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.figures:
            print(f'FIGURES OK: {len(generate_plots(args.input, args.output))} files (SVG, PDF, PNG)')
        else:
            report = write_analysis(args.input, args.output)
            print(f'INTEGRATED ANALYSIS OK: source={report["source"]} variants={report["plan"]["variants"]} contrasts={len(report["contrasts"])}')
    except (OSError, ValueError, KeyError, TypeError, csv.Error, ImportError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
