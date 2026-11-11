"""Offline pruning mechanism probe using declared simulated time and real SQLite."""

import argparse
from dataclasses import asdict
import csv
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from src.governance import DecayPolicy, GovernedWriter, Pruner, record_access, score_entity
from src.governance.decay import DEGREE_SQL
from src.retrieval.traversal import Traversal
from src.storage.database import Database
from src.storage.graph_store import GraphStore

VERSION = 'pruning-mechanism-v1'
REFERENCE = 'docs/results/study-20261111-002/manifest.json'
THRESHOLD = 0.15
POLICY = DecayPolicy(recency_half_life_seconds=86400.0, turn_half_life=10.0)
CHECKPOINTS = ((0, 1), (0.5, 20), (1, 40), (12, 60), (24, 80),
               (36, 100), (48, 120), (60, 140), (72, 160), (96, 200))
FACTS = (
    ('frequent', 'FrequentProject', 'uses', 'FrequentDatabase'),
    ('abandoned', 'TemporaryProject', 'uses', 'TemporaryDatabase'),
    ('pinned', 'CriticalProject', 'requires', 'CriticalConstraint'),
    ('delayed', 'DormantProject', 'uses', 'DormantDatabase'),
    ('cycle_ab', 'ArchiveAlpha', 'links', 'ArchiveBeta'),
    ('cycle_bc', 'ArchiveBeta', 'links', 'ArchiveGamma'),
    ('cycle_ca', 'ArchiveGamma', 'links', 'ArchiveAlpha'),
)


def require(value, message):
    if not value:
        raise ValueError(message)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + '\n')


def populate(graph):
    entities, facts = {}, {}
    writer = GovernedWriter(graph)
    for label, subject, predicate, obj in FACTS:
        for name in (subject, obj):
            if name not in entities:
                entities[name] = graph.create_entity(name, is_pinned=name == 'CriticalProject')
        facts[label] = writer.write_fact(entities[subject].id, predicate, entities[obj].id,
                                         turn=1, now=0.0).triple.id
    return entities, facts


def inspect_entities(graph, *, hour, turn):
    with graph.database.connection() as connection:
        rows = connection.execute(
            f"SELECT e.id,e.name,e.status,e.is_pinned,({DEGREE_SQL}) AS degree, "
            "m.last_access_at,m.last_access_turn,m.access_count FROM entities e "
            "JOIN entity_memory m ON m.entity_id=e.id ORDER BY e.id"
        ).fetchall()
        pinned = {r['id'] for r in rows if r['is_pinned'] and r['status'] == 'ACTIVE'}
        pairs = connection.execute(
            "SELECT subject_id,object_id FROM triples WHERE status='ACTIVE'"
        ).fetchall()
    neighbors_of_pins = {r['object_id'] for r in pairs if r['subject_id'] in pinned}
    neighbors_of_pins.update(r['subject_id'] for r in pairs if r['object_id'] in pinned)
    result = []
    for row in rows:
        item = dict(row)
        score = score_entity(graph, row['id'], current_turn=turn, now=hour * 3600,
                             policy=POLICY) if row['status'] == 'ACTIVE' else None
        item['score'] = None if score is None else asdict(score)
        if row['status'] != 'ACTIVE':
            reason = 'already_pruned'
        elif row['is_pinned']:
            reason = 'pinned'
        elif row['id'] in neighbors_of_pins:
            reason = 'pinned_neighbor'
        elif row['degree'] > 1:
            reason = 'non_leaf'
        elif score.retention >= THRESHOLD:
            reason = 'score_not_below_threshold'
        else:
            reason = 'eligible'
        item['eligibility'] = reason
        result.append(item)
    return result


def run_condition(path, enabled):
    snapshots = []
    with Database(path) as database:
        graph = GraphStore(database)
        entities, facts = populate(graph)
        traversal = Traversal(graph)
        pruner = Pruner(graph, policy=POLICY, threshold=THRESHOLD, batch_size=100)
        for hour, turn in CHECKPOINTS:
            # Only this fact is actually consumed during maintenance checkpoints.
            # Reading scores/snapshots and the final diagnostic probes adds no use.
            if hour > 0:
                context = traversal.expand([entities['FrequentProject'].id])
                record_access(graph, [e.id for e in context.entities], turn=turn, now=hour * 3600)
            before = inspect_entities(graph, hour=hour, turn=turn)
            pruning = pruner.run_once(current_turn=turn, now=hour * 3600) if enabled else None
            require(pruning is None or pruning.next_after_id == 0, 'fixture must fit one full pruning batch')
            entity_states = {e.name: e.status for e in graph.list_entities(limit=100)}
            fact_states = {name: graph.get_triple(triple_id).status for name, triple_id in facts.items()}
            snapshots.append(dict(hour=hour, turn=turn, before=before,
                scanned=0 if pruning is None else pruning.scanned,
                newly_pruned_ids=[] if pruning is None else list(pruning.pruned_ids),
                entities=entity_states, facts=fact_states))
        probes = []
        for label, subject, predicate, obj in FACTS:
            context = traversal.expand([entities[subject].id])
            probes.append(dict(label=label, subject=subject, predicate=predicate, object=obj,
                expected_triple_id=facts[label], returned_triple_ids=[t.id for t in context.triples],
                available=facts[label] in {t.id for t in context.triples}))
        with database.connection() as connection:
            require(connection.execute('PRAGMA integrity_check').fetchone()[0] == 'ok', 'SQLite integrity failure')
            require(not connection.execute('PRAGMA foreign_key_check').fetchall(), 'foreign key failure')
    return dict(pruning_enabled=enabled, snapshots=snapshots, final_probes=probes)


def summarize(runs):
    result = {}
    for condition in ('off', 'on'):
        run = runs[f'{condition}-1']
        final = run['snapshots'][-1]
        probes = {p['label']: p['available'] for p in run['final_probes']}
        first = next((s['hour'] for s in run['snapshots'] if 'PRUNED' in s['facts'].values()), None)
        result[condition] = dict(active_entities=sum(v == 'ACTIVE' for v in final['entities'].values()),
            pruned_entities=sum(v == 'PRUNED' for v in final['entities'].values()),
            active_triples=sum(v == 'ACTIVE' for v in final['facts'].values()),
            pruned_triples=sum(v == 'PRUNED' for v in final['facts'].values()),
            first_observed_triple_pruning_hour=first, final_fact_availability=probes)
    off, on = runs['off-1'], runs['on-1']
    checks = dict(
        repeatability=runs['off-1'] == runs['off-2'] and runs['on-1'] == runs['on-2'],
        identical_initial_facts=off['snapshots'][0]['facts'] == on['snapshots'][0]['facts'],
        no_early_pruning=all('PRUNED' not in s['facts'].values() for s in on['snapshots'] if s['hour'] <= 36),
        observed_pruning_at_48_hours=result['on']['first_observed_triple_pruning_hour'] == 48,
        off_retains_every_fact=all(v == 'ACTIVE' for s in off['snapshots'] for v in s['facts'].values()),
        frequent_and_pinned_survive=all(s['facts'][k] == 'ACTIVE' for s in on['snapshots'] for k in ('frequent', 'pinned')),
        abandoned_and_delayed_pruned=all(on['snapshots'][-1]['facts'][k] == 'PRUNED' for k in ('abandoned', 'delayed')),
        unused_cycle_survives=all(s['facts'][k] == 'ACTIVE' for s in on['snapshots'] for k in ('cycle_ab', 'cycle_bc', 'cycle_ca')),
        later_needed_fact_missing=result['off']['final_fact_availability']['delayed'] and not result['on']['final_fact_availability']['delayed'],
        soft_deletion_preserves_rows=all(len(s['entities']) == 11 and len(s['facts']) == 7 for r in runs.values() for s in r['snapshots']),
        pruning_matches_eligibility=all(set(s['newly_pruned_ids']) == {e['id'] for e in s['before'] if e['eligibility'] == 'eligible'} for s in on['snapshots']),
        stale_cycle_below_threshold=all(e['score']['retention'] < THRESHOLD and e['eligibility'] == 'non_leaf'
            for e in on['snapshots'][-1]['before'] if e['name'].startswith('Archive')),
    )
    return dict(version=VERSION, source='deterministic_simulated_time', checks=checks, conditions=result,
        repeatability_note='Two executions per condition check reproducibility; they are not statistical repetitions.',
        interpretation='Mechanism and active-only fact availability only; no model accuracy, latency, token savings or disk savings measured.')


def execute(output):
    reference = ROOT / REFERENCE
    recorded = json.loads(reference.read_text())['policy']
    require((recorded['pruning_threshold'], recorded['decay_recency_half_life_seconds'], recorded['decay_turn_half_life']) ==
            (THRESHOLD, POLICY.recency_half_life_seconds, POLICY.turn_half_life), 'reference settings changed')
    plan = dict(version=VERSION, source='deterministic_simulated_time', threshold=THRESHOLD,
        decay_policy=asdict(POLICY), batch_size=100, logical_start_seconds=0,
        checkpoints=[dict(hour=h, turn=t) for h, t in CHECKPOINTS],
        facts=[dict(label=k, subject=s, predicate=p, object=o) for k, s, p, o in FACTS],
        conditions=['off', 'on'], executions_per_condition=2,
        refresh='Consume FrequentProject neighborhood before each checkpoint after initialization.',
        probes='At 96 simulated hours, inspect each subject through active-only Traversal; do not reinforce probes.',
        reference=REFERENCE, reference_sha256=digest(reference),
        limitations=['hand-authored isolated fixture', 'simulated governance age, not elapsed execution time',
                     'SQLite audit timestamps remain real', 'no model calls or factual-answer scoring',
                     'no performance or disk-reclamation measurement'])
    output = Path(output).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / 'plan.json', plan)  # Fixed before either condition executes.
    sources = [Path(__file__).resolve(), *sorted((ROOT / 'src').rglob('*.py')), *sorted((ROOT / 'src').rglob('*.sql'))]
    manifest = dict(version=VERSION, source='deterministic_simulated_time', status='RUNNING',
        plan_sha256=digest(output / 'plan.json'), source_sha256={str(p.relative_to(ROOT)): digest(p) for p in sources})
    write_json(output / 'manifest.json', manifest)
    try:
        runs = {f'{c}-{rep}': run_condition(output / f'{c}-{rep}.db', c == 'on')
                for rep in (1, 2) for c in ('off', 'on')}
        write_json(output / 'evidence.json', runs)
        summary = summarize(runs)
        write_json(output / 'summary.json', summary)
        columns = ('condition', 'execution', 'hour', 'turn', 'active_entities', 'pruned_entities', 'active_triples', 'pruned_triples', 'newly_pruned_entities')
        with (output / 'timeline.csv').open('w', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=columns); writer.writeheader()
            for key, run in runs.items():
                condition, execution = key.split('-')
                for s in run['snapshots']:
                    writer.writerow(dict(condition=condition, execution=int(execution), hour=s['hour'], turn=s['turn'],
                        active_entities=sum(v == 'ACTIVE' for v in s['entities'].values()),
                        pruned_entities=sum(v == 'PRUNED' for v in s['entities'].values()),
                        active_triples=sum(v == 'ACTIVE' for v in s['facts'].values()),
                        pruned_triples=sum(v == 'PRUNED' for v in s['facts'].values()), newly_pruned_entities=len(s['newly_pruned_ids'])))
        require(all(summary['checks'].values()), 'one or more mechanism checks failed; inspect summary.json')
        require(manifest['source_sha256'] == {str(p.relative_to(ROOT)): digest(p) for p in sources}, 'source changed during probe')
        manifest['status'] = 'COMPLETED'
    except BaseException as exc:
        manifest['status'] = 'FAILED'
        manifest['error_type'] = type(exc).__name__
        raise
    finally:
        manifest['artifacts'] = {p.name: digest(p) for p in sorted(output.iterdir()) if p.is_file() and p.name != 'manifest.json'}
        write_json(output / 'manifest.json', manifest)
    print('PRUNING PROBE OK: 12 checks passed; two identical logical executions per condition')
    print('FIRST OBSERVED PRUNING: 48 simulated hours; no triple pruning through 36 hours')
    print('FINAL ACTIVE TRIPLES: off=7 on=5; on has 2 PRUNED triples')
    print('DELAYED FACT AVAILABLE: off=yes on=no; pins, frequent fact and unused cycle survive')
    print('OUTPUT:', output)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='fresh directory for isolated databases and evidence')
    args = parser.parse_args()
    try:
        execute(args.output)
    except (OSError, ValueError) as exc:
        print(f'PRUNING PROBE ERROR: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
