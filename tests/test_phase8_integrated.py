"""Offline protocol-4 validation; synthetic HTTP is mandatory for these tests."""

import asyncio
from collections import Counter
import csv
import hashlib
import json
from pathlib import Path
import re
import shutil
import sqlite3

import httpx
from pydantic import ValidationError
import pytest

from experiments import integrated_analysis as analysis, integrated_harness as harness
from experiments.common import make_agents
from experiments.contracts import load_json, sha256, write_json
from experiments.integrated_contracts import CONDITIONS, IntegratedPlan, IntegratedRow, project
from experiments.integrated_scenario import Event, evaluate, fingerprint, records, role_at, workload
from src.config import Settings
from src.agents.base_agent import AgentError
from src.network.ollama_client import OllamaClient
from src.orchestration import StateMachine
from src.storage.database import Database
from src.storage.graph_store import GraphStore


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """Keep this protocol on MockTransport even when a real server is reachable."""

    async def forbidden(*args, **kwargs):
        """Fail immediately if an integrated test attempts real HTTP."""
        raise AssertionError('integrated tests must not contact a real server')
    monkeypatch.setattr(httpx.AsyncHTTPTransport, 'handle_async_request', forbidden)


def settings(tmp_path):
    """Create offline integrated-study settings with temporary application storage."""
    return Settings(
        _env_file=None,
        node2_ollama_url='http://192.168.1.20:11434',
        database_path=tmp_path / 'application.db',
    )


class SyntheticServer:
    """Answers only from the actual task/context. It has no scenario/oracle access."""

    def __init__(self, config, *, missing=False, fail_at=None, cancel=False, wrong_model=False, changed_inventory=False):
        """Configure synthetic inference faults and initialize request/inventory counters."""
        self.config = config
        self.missing = missing
        self.fail_at = fail_at
        self.cancel = cancel
        self.wrong_model = wrong_model
        self.changed_inventory = changed_inventory
        self.posts = []
        self.inventories = 0

    @staticmethod
    def facts(text):
        """Read direct literal fact statements from supplied task or memory text."""
        return re.findall(r'\b([A-Za-z][A-Za-z0-9]+) (uses|supports|links) ([A-Za-z0-9]+)\.', text)

    async def __call__(self, request):
        """Simulate the installed inventory, extraction and role replies from request data."""
        node = 'node1' if request.url.host == 'localhost' else 'node2'
        model = self.config.node_model(node)
        if request.url.path == '/api/version':
            return httpx.Response(200, json={'version': 'synthetic-only'})
        if request.url.path == '/api/tags':
            self.inventories += 1
            letter = 'b' if self.changed_inventory and self.inventories > 2 else 'a'
            return httpx.Response(
                200,
                json={
                    'models': [{'name': model, 'size': 1000, 'digest': letter * 64, 'details': {'quantization_level': 'Q4_K_M'}}],
                },
            )
        body = json.loads(request.content)
        self.posts.append(body)
        if self.fail_at == len(self.posts):
            if self.cancel:
                raise asyncio.CancelledError()
            raise httpx.ReadTimeout('synthetic failure', request=request)
        if 'system' not in body:
            output = 'OK'
        elif body['system'].startswith('Extract explicitly'):
            text = json.JSONDecoder().raw_decode(body['prompt'].split('\n', 1)[1])[0]['message']
            output = json.dumps({'triples': [dict(subject=fact_subject, predicate=fact_predicate, object=fact_object) for fact_subject, fact_predicate, fact_object in self.facts(text)]})
        else:
            task = json.JSONDecoder().raw_decode(body['prompt'].split('TASK\n', 1)[1])[0]
            context = body['prompt'].split('\nMEMORY\n', 1)[1]
            known = {}

            def add(facts):
                """Apply the fake server's single-valued uses rule while preserving set-valued facts."""
                for fact_subject, fact_predicate, fact_object in facts:
                    if fact_predicate == 'uses':
                        known[(fact_subject, fact_predicate)] = {fact_object}
                    else:
                        known.setdefault((fact_subject, fact_predicate), set()).add(fact_object)
            if context.startswith('['):
                for entry in json.JSONDecoder().raw_decode(context)[0]:
                    add(self.facts(entry['task']))
                    add(self.facts(entry['answer'] or ''))
            else:
                add(re.findall(r'"([A-Za-z0-9]+)" -- "(uses|supports|links)" --> "([A-Za-z0-9]+)"', context))
            add(self.facts(task.split('\n')[0]))
            predicate, subject = re.search(r'Question: current (uses|supports|links) values for ([A-Za-z0-9]+)\?', task).groups()
            answer = ' '.join(f'{subject} {predicate} {value}.' for value in sorted(known.get((subject, predicate), ()))) or 'Unknown.'
            output = json.dumps(dict(content=answer, next_subgoal=None, complete=False))
        counters = {} if self.missing else dict(prompt_eval_count=100 + len(body['prompt']) // 4, eval_count=20)
        return httpx.Response(200, json=dict(model='wrong' if self.wrong_model else model, response=output, done=True,
            done_reason='stop', total_duration=1_000_000, **counters))


def make_bundle(path, *, variants=1, turns=60, budget=1000, **options):
    """Save a fresh synthetic cohort and return its path, manifest and fake server."""
    config = settings(path)
    fake = SyntheticServer(config, **options)
    plan = IntegratedPlan(variants=variants, turns=turns, memory_budget=budget, warmups_per_node=0)
    output = path / 'bundle'
    manifest = asyncio.run(harness.run_integrated(plan, config, output, execute=True, transport=httpx.MockTransport(fake)))
    return output, manifest, fake


@pytest.fixture(scope='module')
def evidence(tmp_path_factory):
    """Reuse one immutable synthetic cohort for read-only consistency checks."""
    return make_bundle(tmp_path_factory.mktemp('integrated'))


# Planning, execution gates and the frozen workload

def test_default_only_plans_without_settings_or_files(tmp_path, monkeypatch, capsys):
    from src import config
    monkeypatch.setattr(config, 'get_settings', lambda: pytest.fail('plan mode loaded private settings'))
    assert harness.main(['--output', str(tmp_path / 'absent')]) == 0
    assert not (tmp_path / 'absent').exists()
    assert '16 condition runs; 960 scored turns' in capsys.readouterr().out


def test_execution_gate_and_output_requirement(tmp_path):
    with pytest.raises(PermissionError):
        asyncio.run(harness.run_integrated(IntegratedPlan(), settings(tmp_path), tmp_path / 'none'))
    assert not (tmp_path / 'none').exists()
    with pytest.raises(SystemExit) as exc:
        harness.main(['--execute'])
    assert exc.value.code == 2


@pytest.mark.parametrize('kwargs', [{'turns': 59}, {'turns': 81}, {'turns': True}, {'variants': 0}, {'variants': 9},
    {'memory_budget': 127}, {'memory_budget': 1001}, {'max_tokens': 63}, {'max_tokens': 257},
    {'warmups_per_node': -1}, {'hours_per_turn': 1.0}, {'pruning_threshold': .2}, {'version': 1}])
def test_plan_rejects_invalid_design(kwargs):
    with pytest.raises(ValidationError):
        IntegratedPlan(**kwargs)


def test_order_balances_positions_and_adjacent_predecessors():
    plan = IntegratedPlan()
    orders = [''.join(c for v, c in plan.order() if v == variant) for variant in range(1, 5)]
    assert all(set(order) == set('ABCD') for order in orders)
    for position in range(4):
        assert {o[position] for o in orders} == set('ABCD')
    pairs = Counter(o[i:i + 2] for o in orders for i in range(3))
    assert len(pairs) == 12 and set(pairs.values()) == {1}


@pytest.mark.parametrize('turns', [60, 70, 80])
def test_workload_has_required_cases_and_valid_prompt_capacity(turns, tmp_path):
    plan = IntegratedPlan(turns=turns)
    items = workload(turns, 1)
    assert (
        len(items) == turns
        and any(e.category == 'delayed' and e.step - min(e.source_steps) > 30 for e in items)
    )
    assert {'update', 'reversion', 'pinned', 'multivalued', 'cycle', 'unknown'} <= {e.category for e in items}
    assert len(items[5].expected) == 2 and items[7].expected == items[0].expected
    assert items[1].pin_subject and all(e.pin_subject is None for e in items if e.step != 2)
    assert all(len(e.task) <= 500 for e in items)
    client = OllamaClient(settings(tmp_path))
    for actor in make_agents(client):
        assert all(actor.context_capacity(e.task, e.subject) > 0 for e in items)
    assert records(turns, 1) == records(turns, 1)
    assert len({fingerprint(turns, v) for v in range(1, 5)}) == 4


@pytest.mark.parametrize('turns,variant', [(20, 1), (81, 1), (60, 0), (60, 9), (True, 1)])
def test_workload_invalid_arguments(turns, variant):
    with pytest.raises(ValueError):
        workload(turns, variant)


def test_fingerprint_includes_actual_prompt(monkeypatch):
    original = Event.task.fget
    before = fingerprint()
    monkeypatch.setattr(Event, 'task', property(lambda self: original(self) + ' Changed framing.'))
    assert fingerprint() != before


@pytest.mark.parametrize('variant,semantic_hash', [
    (1, 'fae33b36cc9fa50cd4af024ae8a32fa2d17f742d39198e83d11c3ee42715604a'),
    (2, '1423aab125cccbf5e355f735a52cd65db683ce890eb0b86a9c900db67da61288'),
    (3, 'ee8ba1ab549e12dd3f06ce36e82835e3f1ed0947b8165a022f48ade1c190de6e'),
    (4, 'c666496b2e43856d2c9802fee3b20a3e65352296df78993e8b9af5c276d1cf40'),
])
def test_revised_answer_contract_preserves_observations_and_oracle(variant, semantic_hash):
    """Frozen v1 schedules exclude task wording; no observations/oracles change."""
    plan = IntegratedPlan()
    assert plan.scenario_version == 'integrated-memory-v2' and plan.max_tokens == 128
    entries = records(plan.turns, variant)
    semantic = [{k: v for k, v in entry.items() if k != 'task'} for entry in entries]
    assert (
        hashlib.sha256(json.dumps(semantic, sort_keys=True, separators=(',', ':')).encode()).hexdigest() == semantic_hash
    )
    for entry in entries:
        assert 'explicit direct facts' in entry['task']
        assert 'No repetitions, explanations, reverse or transitive inferences.' in entry['task']
        if entry['category'] == 'cycle':
            assert all(value not in entry['task'] for value in entry['expected'])


# Rejected replies and recorded truncation regressions

def make_reply_rejections(tmp_path, condition='C', fixture='integrated_truncated_cycle_v2.json', *, missing=False):
    """Inject recorded truncated replies without rerunning the original failed experiment."""
    attempts = load_json(Path(__file__).parent / 'fixtures' / fixture)
    config = settings(tmp_path)
    fake = SyntheticServer(config, missing=missing)
    injected = []
    inventory = 2 * ('ABDC'.index(condition) + 1)

    async def truncated(request):
        response = await fake(request)
        if request.method != 'POST':
            return response
        body = json.loads(request.content)
        if body.get('system', '').startswith('Extract explicitly') or not body.get('system'):
            return response
        task = json.JSONDecoder().raw_decode(body['prompt'].split('TASK\n', 1)[1])[0]
        if fake.inventories == inventory and task == workload()[58].task:
            attempt = attempts[len(injected)]
            injected.append(body)
            data = response.json()
            data.update(response=attempt['response'], done_reason=attempt['done_reason'])
            if not missing:
                data['eval_count'] = attempt['generated_tokens']
            return httpx.Response(200, json=data)
        return response
    output = tmp_path / 'rejected-cycle'
    manifest = asyncio.run(harness.run_integrated(IntegratedPlan(variants=1, warmups_per_node=0), config,
        output, execute=True, transport=httpx.MockTransport(truncated)))
    return output, manifest, injected, attempts


@pytest.mark.parametrize('condition', list('ABCD'))
@pytest.mark.parametrize('fixture', ['integrated_truncated_cycle.json', 'integrated_truncated_cycle_v2.json'])
def test_both_recorded_truncations_are_scored_and_checkpointed_in_every_condition(tmp_path, condition, fixture):
    """A bounded invalid reply remains an incorrect recorded turn in every condition."""
    output, manifest, injected, attempts = make_reply_rejections(tmp_path, condition, fixture)
    _, _, rows, _ = analysis.load_bundle(output)
    rejected = next(r for r in rows if r.condition == condition and r.step == 59)
    following = next(r for r in rows if r.condition == condition and r.step == 60)
    assert manifest['status'] == 'COMPLETED' and len(rows) == 240 and not (output / 'failure.json').exists()
    assert rejected.answer is None and not rejected.correct and not rejected.format_valid
    assert [a.response for a in rejected.reply_failure.attempts] == [a['response'] for a in attempts]
    assert (
        all(a.done_reason == 'length' and a.generated_tokens == 128 for a in rejected.reply_failure.attempts)
    )
    assert len(injected) == 2 and all(b['options']['num_predict'] == 128 for b in injected)
    assert rejected.telemetry.agent_calls == 2 and rejected.telemetry.generated_tokens == 256
    assert rejected.telemetry.extraction_calls == 0 and not rejected.response_facts
    assert (
        rejected.telemetry.agent_prompt_tokens == sum(a.prompt_tokens for a in rejected.reply_failure.attempts)
    )
    assert following.step == 60 and following.reply_failure is None
    assert following.logical_time - rejected.logical_time == 7200
    if condition == 'A':
        assert any(e['task'] == rejected.task and e['answer'] is None for e in json.loads(following.context))
    else:
        with sqlite3.connect((output / f'{condition}-1.db').as_uri() + '?mode=ro&immutable=1', uri=True) as c:
            assert c.execute('SELECT status,next_step FROM workflow_runs').fetchall() == [('COMPLETED', 60)]
            payload = json.loads(c.execute('SELECT result FROM workflow_turns WHERE step=59').fetchone()[0])
            assert payload['reply_failure'] == rejected.reply_failure.model_dump(mode='json')
            assert payload['extraction_attempts'] == payload['fact_ids'] == []
            assert len(payload['agent_generations']) == 2
    assert all(sha256(output / name) == digest for name, digest in manifest['artifacts'].items())
    report = analysis.analyze_bundle(output)
    assert report['conditions'][condition]['invalid_reply_turns'] == 1
    assert report['conditions'][condition]['invalid_reply_rate'] == 1 / 60
    assert report['conditions'][condition]['turns'] == 60


def test_rejected_reply_missing_counters_remain_unknown(tmp_path):
    output, _, _, _ = make_reply_rejections(tmp_path, missing=True)
    row = next(r for r in analysis.load_bundle(output)[2] if r.condition == 'C' and r.step == 59)
    assert (
        row.telemetry.agent_prompt_tokens is row.telemetry.generated_tokens is row.total_prompt_tokens is None
    )
    assert row.telemetry.extraction_prompt_tokens == 0


@pytest.mark.parametrize('kind', ['truncated', 'json', 'schema', 'duplicate', 'unknown_length'])
def test_every_invalid_role_reply_counts_against_accuracy_without_aborting(tmp_path, kind):
    config = settings(tmp_path)
    fake = SyntheticServer(config)
    outputs = {'truncated': '{"content":', 'json': 'not json', 'schema': '{"content":7}',
        'duplicate': '{"content":"Unknown.","content":"Unknown."}',
        'unknown_length': '{"content":"Unknown."}'}

    async def invalid(request):
        response = await fake(request)
        if request.method == 'POST' and json.loads(request.content).get('system', '').startswith('You are'):
            data = response.json()
            data['response'] = outputs[kind]
            if kind in ('truncated', 'unknown_length'):
                data.update(done_reason='length', eval_count=128)
            return httpx.Response(200, json=data)
        return response
    output = tmp_path / 'all-invalid'
    manifest = asyncio.run(harness.run_integrated(IntegratedPlan(variants=1, warmups_per_node=0), config,
        output, execute=True, transport=httpx.MockTransport(invalid)))
    _, _, rows, _ = analysis.load_bundle(output)
    assert manifest['status'] == 'COMPLETED' and len(rows) == 240
    assert all(r.answer is None and not r.correct and not r.format_valid and r.reply_failure for r in rows)
    assert all(
        (r.telemetry.agent_calls == 2 and r.telemetry.extraction_calls == 0 and not r.response_facts for r in rows),
    )
    report = analysis.analyze_bundle(output)
    assert all(
        (c['turns'] == c['invalid_reply_turns'] == 60 and c['accuracy'] == 0 for c in report['conditions'].values()),
    )


@pytest.mark.parametrize('kind', ['tool', 'prompt_budget', 'oversized'])
def test_non_reply_or_oversized_errors_are_never_converted_into_outcomes(kind):
    from src.agents.base_agent import rejected_reply
    from src.network.ollama_client import GenerationResult
    attempt = GenerationResult(node='node1', model='local', response='x' * (16385 if kind == 'oversized' else 1),
        latency_ms=1., node_latency_ms=1., server_duration_ms=None, generated_tokens=None, failed_attempts=())
    error = AgentError('reply' if kind == 'oversized' else kind, 'injected', (attempt,))
    with pytest.raises(AgentError) as caught:
        rejected_reply(error, agent_id='planner', turn=1)
    assert caught.value is error


@pytest.mark.parametrize('attack', ['score', 'answer', 'failure', 'cost', 'valid_attempt', 'sqlite'])
def test_rejected_reply_evidence_cannot_be_rewritten_into_success(tmp_path, attack):
    output, _, _, _ = make_reply_rejections(tmp_path)
    if attack == 'sqlite':
        db = output / 'C-1.db'
        with sqlite3.connect(db) as c:
            payload = json.loads(c.execute('SELECT result FROM workflow_turns WHERE step=59').fetchone()[0])
            payload['reply_failure'] = None
            c.execute('UPDATE workflow_turns SET result=? WHERE step=59', (json.dumps(payload),))
        c.close()
        m = load_json(output / 'manifest.json')
        m['artifacts'][db.name] = sha256(db)
        write_json(output / 'manifest.json', m)
    else:
        def change(rows):
            row = next(r for r in rows if r['condition'] == 'C' and r['step'] == 59)
            if attack == 'score':
                row['correct'] = row['format_valid'] = True
            elif attack == 'answer':
                row['answer'] = 'Unknown.'
            elif attack == 'failure':
                row['reply_failure'] = None
            elif attack == 'cost':
                row['reply_failure']['attempts'][0]['prompt_tokens'] += 1
            elif attack == 'valid_attempt':
                row['reply_failure']['attempts'][0].update(response='{"content":"Unknown."}', done_reason='stop')
        rewrite_rows(output, change)
    with pytest.raises(ValueError):
        analysis.analyze_bundle(output)


def test_ordinary_workflow_still_raises_on_invalid_agent_reply(tmp_path):
    config = settings(tmp_path)
    fake = SyntheticServer(config)

    async def truncated(request):
        response = await fake(request)
        if request.method == 'POST' and json.loads(request.content).get('system', '').startswith('You are'):
            data = response.json()
            data.update(response='{"content":', done_reason='length')
            return httpx.Response(200, json=data)
        return response

    async def run():
        async with OllamaClient(config, transport=httpx.MockTransport(truncated)) as client:
            async with Database(tmp_path / 'ordinary.db') as database:
                machine = StateMachine(client, GraphStore(database))
                cp = await machine.start(workload()[0].task, initial_subgoal=workload()[0].subject, max_turns=1)
                with pytest.raises(AgentError):
                    await machine.step(cp.id)
                assert (await machine.get_checkpoint(cp.id)).status == 'FAILED'
                assert not await machine.history(cp.id)
                with pytest.raises(ValueError, match='boolean'):
                    StateMachine(client, machine.graph, record_reply_errors=1)
                with pytest.raises(ValueError, match='tool-free'):
                    StateMachine(client, machine.graph, record_reply_errors=True)
    asyncio.run(run())


def test_reply_error_policy_is_part_of_workflow_compatibility(tmp_path):
    config = harness.configure(settings(tmp_path), IntegratedPlan(), 'C')
    fake = SyntheticServer(config)

    async def run():
        async with OllamaClient(config, transport=httpx.MockTransport(fake)) as client:
            async with Database(tmp_path / 'policy.db') as database:
                graph = GraphStore(database)
                agents = make_agents(client)
                normal = StateMachine(client, graph, agents=agents)
                recorded = StateMachine(client, graph, agents=agents, record_reply_errors=True)
                assert normal.config_hash != recorded.config_hash
                cp = await normal.start(workload()[0].task, initial_subgoal=workload()[0].subject, max_turns=1)
                with pytest.raises(Exception, match='configuration differs'):
                    await recorded.step(cp.id)
                assert not fake.posts
    asyncio.run(run())


# Scoring, memory policies and completed synthetic cohorts

def test_scoring_sets_and_format_are_explicit():
    event = workload()[5]
    a, b = event.expected
    assert evaluate(f'{event.subject} supports {b}. {event.subject} supports {a}.', event) == (True, True)
    assert evaluate(f'{event.subject} supports {a}.', event) == (True, False)
    assert evaluate('Unknown.', event) == (True, False)
    assert evaluate('I think it supports both.', event) == (False, False)
    assert evaluate('Unknown.', next(e for e in workload() if not e.expected)) == (True, True)


def test_four_policies_are_independent_of_private_settings(tmp_path):
    config = settings(tmp_path).model_copy(update={'single_valued_predicates': ('supports',), 'pruning_threshold': .9})
    for c in CONDITIONS:
        s = harness.configure(config, IntegratedPlan(), c)
        assert s.single_valued_predicates == (('uses',) if c in 'CD' else ())
        assert s.orchestration_prune_between_turns == (c == 'D')
        assert s.pruning_threshold == .15 and s.decay_recency_half_life_seconds == 86400
        assert not s.orchestration_allow_fallback


def test_complete_synthetic_bundle_validates_and_is_not_research_result(evidence):
    output, manifest, fake = evidence
    m, plan, rows, flat = analysis.load_bundle(output)
    assert m['source'] == 'synthetic' and len(rows) == 240 and len(flat) == 240
    assert [r['condition'] for r in m['runs']] == list('ABDC')
    assert len(m['artifacts']) == 10 and not m['fully_counterbalanced']
    assert fake.posts
    for r in rows:
        assert r.logical_time == r.step * 7200 and r.telemetry.observed_at > 1e9
        assert r.telemetry.governance_turn == r.step
        assert r.memory_before.last_access_at is None or r.memory_before.last_access_at <= r.logical_time
        assert r.telemetry.context_budget_used <= r.telemetry.context_budget_limit <= 1000
    assert not (output.parent / 'application.db').exists()


def test_supersession_multivalue_pruning_and_delayed_loss_are_observable(evidence):
    _, _, rows, flat = analysis.load_bundle(evidence[0])
    assert any(r.memory_after.single_value_conflicts for r in rows if r.condition == 'B')
    assert all(not r.memory_after.single_value_conflicts for r in rows if r.condition in 'CD')
    assert any(c.fact.status == 'SUPERSEDED' for r in rows if r.condition == 'C' for c in r.changes)
    assert any(r.pruned_entity_ids for r in rows if r.condition == 'D')
    for c in 'BCD':
        r = next(r for r in rows if r.condition == c and r.step == 6)
        assert len([f for f in r.memory_after.target_facts if f.live]) == 2
    delayed = next(r for r in flat if r['condition'] == 'D' and r['step'] == 40)
    assert delayed['support_available'] is False and not delayed['correct']
    pinned = next(r for r in flat if r['condition'] == 'D' and r['step'] == 50)
    assert pinned['support_available'] is True and pinned['correct']
    cycle = next(r for r in flat if r['condition'] == 'D' and r['step'] == 59)
    assert cycle['support_available'] is True
    assert any(r['cross_node'] and r['lag'] >= 10 for r in flat)


def test_analysis_and_plot_outputs_are_descriptive_and_synthetic(evidence, tmp_path):
    output = evidence[0]
    report = analysis.write_analysis(output, tmp_path / 'analysis')
    assert set(report['conditions']) == set('ABCD') and len(report['contrasts']) == 3
    assert {p['through_step'] for p in report['checkpoints']} == {20, 40, 60}
    assert {r['purpose'] for r in report['contrasts']} == {'complete framework', 'supersession', 'pruning'}
    paths = analysis.generate_plots(output, tmp_path / 'figures')
    assert len(paths) == 18 and {p.suffix for p in paths} == {'.svg', '.pdf', '.png'}
    for p in paths:
        if p.suffix == '.svg':
            assert 'SYNTHETIC TEST DATA' in p.read_text()
    with pytest.raises(FileExistsError):
        analysis.write_analysis(output, tmp_path / 'analysis')
    with pytest.raises(FileExistsError):
        analysis.generate_plots(output, tmp_path / 'figures')


def test_missing_token_counters_stay_unknown(tmp_path):
    output, _, _ = make_bundle(tmp_path, missing=True)
    report = analysis.analyze_bundle(output)
    assert all(
        (r['total_prompt_tokens'] is None and r['role_prompt_tokens'] is None for r in report['conditions'].values()),
    )
    assert all(c['pipeline_prompt_delta'] is None for c in report['contrasts'])


@pytest.mark.parametrize('options,status', [({'fail_at': 1}, 'FAILED'), ({'fail_at': 1, 'cancel': True}, 'CANCELLED'),
    ({'wrong_model': True}, 'FAILED'), ({'changed_inventory': True}, 'FAILED')])
def test_failed_or_cancelled_cohorts_are_preserved_and_rejected(tmp_path, options, status):
    with pytest.raises(BaseException):
        make_bundle(tmp_path, **options)
    m = load_json(tmp_path / 'bundle/manifest.json')
    assert m['status'] == status and 'failure.json' in m['artifacts']
    with pytest.raises(ValueError, match='completed'):
        analysis.analyze_bundle(tmp_path / 'bundle')


# Evidence tampering and historical compatibility

def rewrite_rows(path, change):
    """Alter a disposable bundle and its checksum to exercise semantic tampering checks."""
    rows = [json.loads(line) for line in (path / 'turns.jsonl').read_text().splitlines()]
    change(rows)
    (path / 'turns.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in rows))
    m = load_json(path / 'manifest.json')
    m['artifacts']['turns.jsonl'] = sha256(path / 'turns.jsonl')
    write_json(path / 'manifest.json', m)


@pytest.mark.parametrize(
    'kind',
    ['score', 'clock', 'route', 'context', 'transition', 'timestamp', 'budget', 'order', 'rows', 'checkpoint'],
)
def test_modified_evidence_is_rejected_even_with_updated_file_checksum(evidence, tmp_path, kind):
    output = tmp_path / 'tampered'
    shutil.copytree(evidence[0], output)

    def change(rows):
        row = rows[60]
        if kind == 'score':
            row['correct'] = not row['correct']
        elif kind == 'clock':
            row['logical_time'] = 10.0
        elif kind == 'route':
            row['telemetry']['node'] = 'node2'
        elif kind == 'context':
            row['context'] = 'invented'
        elif kind == 'transition':
            row['changes'][0]['fact']['status'] = 'PRUNED'
        elif kind == 'timestamp':
            row['telemetry']['observed_at'] = 7200.0
        elif kind == 'budget':
            row['telemetry']['context_budget_limit'] = 127
        elif kind == 'order':
            rows[1], rows[2] = rows[2], rows[1]
        elif kind == 'rows':
            rows.pop()
        elif kind == 'checkpoint':
            row['answer'] = 'Unknown.'
            row['correct'] = False
    rewrite_rows(output, change)
    with pytest.raises((ValueError, ValidationError)):
        analysis.analyze_bundle(output)


def test_csv_corruption_checksum_and_path_escape_are_rejected(evidence, tmp_path):
    output = tmp_path / 'tampered'
    shutil.copytree(evidence[0], output)
    (output / 'metrics.csv').write_text('wrong\n')
    with pytest.raises(ValueError, match='checksum'):
        analysis.load_bundle(output)
    m = load_json(output / 'manifest.json')
    m['artifacts']['metrics.csv'] = sha256(output / 'metrics.csv')
    write_json(output / 'manifest.json', m)
    with pytest.raises(ValueError, match='CSV'):
        analysis.load_bundle(output)


def test_old_saved_case_study_still_analyzes():
    from experiments.analyze_results import analyze_bundle
    root = Path(__file__).resolve().parents[1]
    report = analyze_bundle(root / 'docs/results/study-20261111-002')
    assert report['source'] == 'measured' and report['conditions']['A']['turns'] == 40


def test_source_and_private_paths_are_not_in_public_manifest(evidence):
    text = (evidence[0] / 'manifest.json').read_text()
    assert 'application.db' not in text and '192.168.1.20' not in text and '/home/' not in text


# Governance clocks, budgets and memory diagnostics

def test_explicit_turns_and_separate_clocks_preserve_commit_semantics(tmp_path):
    async def run():
        config = settings(tmp_path)
        fake = SyntheticServer(config)
        async with OllamaClient(config, transport=httpx.MockTransport(fake)) as client:
            async with Database(tmp_path / 'clocks.db') as database:
                graph = GraphStore(database)
                machine = StateMachine(client, graph, clock=lambda: 7200.0, observation_clock=lambda: 2e9)
                event = workload()[0]
                await machine.start(event.task, initial_subgoal=event.subject, max_turns=2, run_id='clock-test')
                await harness.ingest(machine, event, machine.extractor, type('Clock', (), {'now': lambda self: 7200.0})())
                result = await machine.step('clock-test', governance_turn=1)
                assert result.checkpoint.governance_turn == 1
                assert result.checkpoint.result['governance_time'] == 7200
                assert result.checkpoint.result['telemetry']['observed_at'] == 2e9
                with pytest.raises(ValueError, match='follow committed'):
                    await machine.step('clock-test', governance_turn=1)
                assert (await machine.get_checkpoint('clock-test')).next_step == 1
    asyncio.run(run())


def test_full_counterbalanced_80_turn_design_executes_offline(tmp_path):
    output, manifest, _ = make_bundle(tmp_path, turns=80, variants=4)
    report = analysis.analyze_bundle(output)
    assert manifest['fully_counterbalanced'] and len(manifest['runs']) == 16
    assert all(c['turns'] == 320 for c in report['conditions'].values())
    assert len(report['contrasts']) == 12 and len(report['curves']) == 320
    assert {p['through_step'] for p in report['checkpoints']} == {20, 40, 60, 80}
    assert report['contrast_summary']['pruning']['accuracy_delta']['n_variants'] == 4


def test_fresh_directory_and_application_data_guard(tmp_path):
    output = tmp_path / 'existing'
    output.mkdir()
    (output / 'keep').write_text('preserve')
    with pytest.raises(FileExistsError):
        asyncio.run(harness.run_integrated(IntegratedPlan(), settings(tmp_path), output, execute=True))
    assert (output / 'keep').read_text() == 'preserve'
    protected = tmp_path / 'parent'
    with pytest.raises(ValueError, match='application database'):
        asyncio.run(
            harness.run_integrated(
                IntegratedPlan(),
                settings(tmp_path).model_copy(update={'database_path': protected / 'application.db'}),
                protected,
                execute=True,
            ),
        )
    assert not protected.exists()


def test_extraction_failure_records_exact_source(tmp_path):
    config = settings(tmp_path)
    fake = SyntheticServer(config)

    async def broken(request):
        response = await fake(request)
        body = json.loads(request.content) if request.method == 'POST' else {}
        if body.get('system', '').startswith('Extract explicitly'):
            data = response.json()
            data['response'] = json.dumps({'triples': [dict(subject='NotInSource', predicate='uses', object='Invented')]})
            return httpx.Response(200, json=data)
        return response
    output = tmp_path / 'failed'
    manifest = asyncio.run(
        harness.run_integrated(
            IntegratedPlan(variants=1, warmups_per_node=0),
            config,
            output,
            execute=True,
            transport=httpx.MockTransport(broken),
        ),
    )
    rows = analysis.load_bundle(output)[2]
    assert manifest['status'] == 'COMPLETED' and len(rows) == 240
    failure = next(r for r in rows if r.condition == 'B').observation_failure
    assert failure.source_message.content == workload()[0].observation and len(failure.attempts) == 2
    assert all(not r.observation_facts and not r.response_facts for r in rows if r.condition != 'A')


def test_default_state_machine_still_uses_its_original_clock(tmp_path):
    async def run():
        config = settings(tmp_path)
        fake = SyntheticServer(config)
        async with OllamaClient(config, transport=httpx.MockTransport(fake)) as client:
            async with Database(tmp_path / 'default-clock.db') as database:
                machine = StateMachine(client, GraphStore(database), clock=lambda: 2e9)
                event = workload()[0]
                await machine.start(event.task, initial_subgoal=event.subject, max_turns=1, run_id='default-clock')
                result = await machine.step('default-clock')
                assert result.checkpoint.result['telemetry']['observed_at'] == 2e9
                assert result.checkpoint.governance_turn == 1
                with pytest.raises(ValueError, match='observation_clock'):
                    StateMachine(client, machine.graph, observation_clock=4)
    asyncio.run(run())


def test_small_budget_preserves_validation_and_caps(tmp_path):
    output, _, _ = make_bundle(tmp_path, budget=128)
    report = analysis.analyze_bundle(output)
    assert all(c['turns'] == 60 for c in report['conditions'].values())
    assert all(r.telemetry.context_budget_used <= 128 for r in analysis.load_bundle(output)[2])


def test_diagnostics_distinguish_pruning_and_conflicting_active_values(evidence):
    report = analysis.analyze_bundle(evidence[0])
    assert report['conditions']['D']['missing_support_states']['pruned'] > 0
    assert report['conditions']['B']['turns_with_single_value_conflicts'] > 0
    assert report['conditions']['C']['turns_with_single_value_conflicts'] == 0


def test_snapshot_conflict_counts_cannot_be_relabelled(evidence, tmp_path):
    output = tmp_path / 'tampered'
    shutil.copytree(evidence[0], output)

    def change(rows):
        row = next(r for r in rows if r['condition'] == 'B' and r['step'] == 7)
        row['memory_after']['single_value_conflicts'] = 0
    rewrite_rows(output, change)
    with pytest.raises(ValueError, match='conflicts'):
        analysis.load_bundle(output)


def test_omitted_retrieval_is_distinct_from_missing_graph_fact(evidence):
    rows = analysis.load_bundle(evidence[0])[2]
    original = next(r for r in rows if r.condition == 'C' and r.step == 6)
    omitted = original.model_copy(update={'context': '', 'context_triple_ids': (), 'retrieved_facts': (),
        'telemetry': original.telemetry.model_copy(update={'context_budget_used': 0})})
    row = project(omitted, workload()[5])
    assert row['support_available'] is True and row['support_supplied'] is False
    summary = analysis.describe([row])
    assert summary['graph_support_missing'] == 0 and summary['retrieval_support_missing'] == 1


def test_manifest_records_full_nonprivate_policies_and_rejects_policy_changes(evidence, tmp_path):
    m = evidence[1]
    assert set(m['policies']) == set('ABCD')
    assert m['generation_protocol']['extractor_max_retries'] == 1
    assert m['policies']['C']['single_valued_predicates'] == ['uses']
    output = tmp_path / 'tampered'
    shutil.copytree(evidence[0], output)
    changed = load_json(output / 'manifest.json')
    changed['policies']['C']['orchestration_prune_between_turns'] = True
    write_json(output / 'manifest.json', changed)
    with pytest.raises(ValueError, match='condition policy'):
        analysis.load_bundle(output)


def test_recall_gap_does_not_count_rehearsed_facts_as_long_delay():
    items = workload(80, 1)
    assert items[39].recall_gap == 37
    assert items[55].recall_gap == 16
    assert items[27].recall_gap == 26
    assert all(e.recall_gap < 10 for e in items if e.category == 'frequent')
    frequent = next(e for e in items if e.category == 'frequent' and e.step > 40)
    assert frequent.step - min(frequent.source_steps) > 30 and frequent.recall_gap < 4


# Known prediction aborts and extraction-failure accounting

def make_prediction_abort(tmp_path, stage='response', condition='B', recover=False):
    """Inject the exact known server abort at the selected inference stage."""
    from src.network.ollama_client import REPEAT_ABORT
    config = settings(tmp_path)
    fake = SyntheticServer(config)
    hits = []
    last_task = ''
    last_step = 0
    event = workload()[11]

    async def abort(request):
        nonlocal last_task, last_step
        response = await fake(request)
        if request.method != 'POST':
            return response
        body = json.loads(request.content)
        system = body.get('system', '')
        is_role = system.startswith('You are')
        if is_role:
            from src.agents.base_agent import CORRECTION
            if not body['prompt'].endswith(CORRECTION):
                last_step = last_step % 60 + 1
            last_task = json.JSONDecoder().raw_decode(body['prompt'].split('TASK\n', 1)[1])[0]
        eligible = False
        if stage == 'role':
            eligible = is_role and last_step == 12
        elif system.startswith('Extract explicitly'):
            message = json.JSONDecoder().raw_decode(body['prompt'].split('\n', 1)[1])[0]['message']
            if stage == 'observation':
                eligible = message == workload()[12].observation
            else:
                eligible = last_step == 12 and message != workload()[12].observation
        if fake.inventories == 2 * ('ABDC'.index(condition) + 1) and eligible and (not recover or not hits):
            hits.append(body)
            return httpx.Response(500, json={'error': REPEAT_ABORT})
        return response
    path = tmp_path / 'abort'
    manifest = asyncio.run(harness.run_integrated(IntegratedPlan(variants=1, warmups_per_node=0), config, path,
        execute=True, transport=httpx.MockTransport(abort)))
    return path, manifest, hits


@pytest.mark.parametrize(
    'stage,condition',
    [
        ('role', 'A'),
        ('role', 'B'),
        ('role', 'C'),
        ('role', 'D'),
        ('observation', 'C'),
        ('response', 'B'),
        ('response', 'C'),
    ],
)
@pytest.mark.parametrize('recover', [False, True])
def test_known_prediction_abort_is_retained_through_each_inference_stage(tmp_path, stage, condition, recover):
    path, manifest, hits = make_prediction_abort(tmp_path, stage, condition, recover)
    rows = analysis.load_bundle(path)[2]
    step = 13 if stage == 'observation' else 12
    row = next(r for r in rows if r.condition == condition and r.step == step)
    assert manifest['status'] == 'COMPLETED' and len(rows) == 240 and len(hits) == (1 if recover else 2)
    attempts = {'role': row.agent_attempts, 'observation': row.observation_attempts, 'response': row.response_attempts} [stage]
    assert len(attempts) == 2 and attempts[0].error == 'token_repeat_limit' and attempts[0].http_status == 500
    assert attempts[0].response is attempts[0].prompt_tokens is attempts[0].generated_tokens is None
    assert row.total_prompt_tokens is row.total_generated_tokens is None
    assert row.wall_ms >= sum(a.latency_ms for a in attempts)
    failure = {'role': row.reply_failure, 'observation': row.observation_failure, 'response': row.response_failure} [stage]
    assert (failure is None) == recover
    if stage == 'role' and not recover:
        assert row.answer is None and not row.correct
    if stage == 'observation' and not recover:
        assert not row.observation_facts
    if stage == 'response':
        assert row.answer is not None and row.reply_failure is None
        if not recover:
            assert row.response_facts == ()
            assert (row.format_valid, row.correct) == evaluate(row.answer, workload()[11])
            if condition == 'C':
                assert row.correct
    assert (
        next(r for r in rows if r.condition == condition and r.step == step + 1).logical_time == row.logical_time + 7200
    )
    report = analysis.analyze_bundle(path)
    assert report['conditions'][condition]['prediction_aborts'] == len(hits)
    assert report['conditions'][condition]['total_prompt_tokens'] is None


@pytest.mark.parametrize(
    'server_error',
    ['prediction aborted, token repeat limit reached', 'other server failure'],
)
@pytest.mark.parametrize('opt_in', [False, True])
def test_only_exact_repeat_abort_is_an_opt_in_generation_outcome(tmp_path, server_error, opt_in):
    from src.network.ollama_client import OllamaError, REPEAT_ABORT

    async def abort(request):
        return httpx.Response(500, json={'error': server_error})

    async def run():
        async with OllamaClient(settings(tmp_path), transport=httpx.MockTransport(abort), record_prediction_aborts=opt_in) as client:
            if opt_in and server_error == REPEAT_ABORT:
                result = await client.generate_json('message', system='system', allow_fallback=False)
                assert result.response is result.prompt_tokens is result.generated_tokens is None
                assert result.error == 'token_repeat_limit' and result.http_status == 500
            else:
                with pytest.raises(OllamaError):
                    await client.generate_json('message', system='system', allow_fallback=False)
    asyncio.run(run())


def test_warmup_repeat_abort_retains_failed_attempt_without_scoring_a_turn(tmp_path):
    from src.network.ollama_client import REPEAT_ABORT
    config = settings(tmp_path)
    fake = SyntheticServer(config)

    async def abort(request):
        if request.method == 'POST':
            return httpx.Response(500, json={'error': REPEAT_ABORT})
        return await fake(request)
    output = tmp_path / 'warmup-abort'
    with pytest.raises(AgentError, match='warmup'):
        asyncio.run(harness.run_integrated(IntegratedPlan(variants=1), config, output, execute=True,
            transport=httpx.MockTransport(abort)))
    failure = load_json(output / 'failure.json')
    manifest = load_json(output / 'manifest.json')
    assert manifest['status'] == 'FAILED' and failure['stage'] == 'warmup'
    assert failure['generation_attempts'][0]['error'] == 'token_repeat_limit'
    assert failure['generation_attempts'][0]['http_status'] == 500
    assert failure['generation_attempts'][0]['response'] is None
    assert (output / 'turns.jsonl').read_text() == ''


@pytest.mark.parametrize('attack', ['fake_zero', 'raw_text', 'source', 'failure', 'facts', 'sqlite'])
def test_prediction_abort_accounting_and_extraction_evidence_reject_tampering(tmp_path, attack):
    """Fabricated costs or accepted facts must fail even if an attacker refreshes hashes."""
    path, _, _ = make_prediction_abort(tmp_path)
    if attack == 'sqlite':
        with sqlite3.connect(path / 'B-1.db') as c:
            payload = json.loads(c.execute('SELECT result FROM workflow_turns WHERE step=12').fetchone()[0])
            payload['extraction_failure'] = None
            c.execute('UPDATE workflow_turns SET result=? WHERE step=12', (json.dumps(payload),))
        c.close()
        m = load_json(path / 'manifest.json')
        m['artifacts']['B-1.db'] = sha256(path / 'B-1.db')
        write_json(path / 'manifest.json', m)
    else:
        def change(rows):
            row = next(r for r in rows if r['condition'] == 'B' and r['step'] == 12)
            if attack == 'fake_zero':
                row['response_attempts'][0]['prompt_tokens'] = 0
            elif attack == 'raw_text':
                row['response_attempts'][0]['response'] = 'fabricated'
            elif attack == 'source':
                row['response_failure']['source_message']['content'] = 'Invented source.'
            elif attack == 'failure':
                row['response_failure'] = None
            elif attack == 'facts':
                row['response_facts'] = [dict(subject='X', predicate='uses', object='Y', confidence=1.)]
        rewrite_rows(path, change)
    with pytest.raises(ValueError):
        analysis.analyze_bundle(path)


@pytest.mark.parametrize('stage', ['role', 'observation', 'response'])
def test_unrelated_http_failure_still_aborts_and_records_available_source(tmp_path, stage):
    config = settings(tmp_path)
    fake = SyntheticServer(config)
    role_seen = False

    async def broken(request):
        nonlocal role_seen
        response = await fake(request)
        if request.method != 'POST':
            return response
        body = json.loads(request.content)
        is_role = body.get('system', '').startswith('You are')
        if is_role:
            role_seen = True
        eligible = (stage == 'role' and is_role) or (fake.inventories == 4 and
            body.get('system', '').startswith('Extract explicitly') and ((stage == 'response' and role_seen) or (stage == 'observation' and not role_seen)))
        if eligible:
            return httpx.Response(500, json={'error': 'unrelated server failure'})
        if fake.inventories == 2:
            role_seen = False
        return response
    # Use message content to distinguish B1's observation from its response.
    async def selected(request):
        nonlocal role_seen
        if request.url.path == '/api/tags' and request.url.host != 'localhost':
            role_seen = False
        return await broken(request)
    output = tmp_path / 'unrelated'
    with pytest.raises(Exception):
        asyncio.run(harness.run_integrated(IntegratedPlan(variants=1, warmups_per_node=0), config, output,
            execute=True, transport=httpx.MockTransport(selected)))
    manifest = load_json(output / 'manifest.json')
    failure = load_json(output / 'failure.json')
    assert manifest['status'] == 'FAILED' and failure['http_failure']['status_code'] == 500
    assert failure['http_failure']['server_error'] == 'unrelated server failure'
    if stage != 'role':
        assert failure['source_message'] is not None
