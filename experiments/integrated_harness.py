"""Gated four-condition experiment with simulated aging and measured local models."""

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
from experiments.benchmark_harness import (_RecordedExtractor, _add, _agents, _baseline_metrics,
    _failure_record, _inventory, _packages, _provenance, _routes, _sum, linear_context)
from experiments.contracts import sha256, utc_now, write_json
from experiments.integrated_contracts import (CONDITIONS, GENERATION_PROTOCOL, REPLY_ERROR_POLICY, INFERENCE_ERROR_POLICY, PRIMARY_OUTCOMES, HYPOTHESES, Change, FactState, IntegratedPlan,
    IntegratedRow, MemoryState, project)
from experiments.integrated_scenario import evaluate, records, role_at, workload
from src.governance import record_access
from src.agents.base_agent import AgentError, rejected_reply, reply_failure_evidence
from src.models import AgentMessage
from src.network.ollama_client import OllamaClient, generation_evidence
from src.orchestration import StateMachine
from src.storage.database import Database
from src.storage.graph_store import GraphStore
from src.telemetry import CSVLogger, TelemetryProfiler


class ScenarioClock:
    def __init__(self, hours):
        self.hours, self.step = hours, 0

    def now(self):
        return float(self.step * self.hours * 3600)


def configure(settings, plan, condition):
    policy = CONDITIONS[condition]
    return settings.model_copy(update=dict(single_valued_predicates=policy['single_valued_predicates'],
        orchestration_prune_between_turns=policy['prune'], orchestration_allow_fallback=False,
        orchestration_max_turns=plan.turns,context_token_budget=plan.memory_budget,
        agent_max_tokens=plan.max_tokens,agent_max_tool_rounds=0,
        pruning_threshold=plan.pruning_threshold,pruning_batch_size=100,
        decay_recency_half_life_seconds=plan.recency_half_life_seconds,decay_turn_half_life=plan.turn_half_life))


def catalog(graph):
    with graph.database.connection() as connection:
        rows = connection.execute('''SELECT t.id,s.name AS subject,t.predicate,o.name AS object,t.status,
            (s.status='ACTIVE') AS subject_active,(o.status='ACTIVE') AS object_active
            FROM triples t JOIN entities s ON s.id=t.subject_id JOIN entities o ON o.id=t.object_id ORDER BY t.id''').fetchall()
    return {r['id']:FactState(**(dict(r)|{'subject_active':bool(r['subject_active']),'object_active':bool(r['object_active'])})) for r in rows}


def snapshot(graph, event, facts):
    with graph.database.connection() as connection:
        row = connection.execute('''SELECT e.is_pinned,m.last_access_at,m.last_access_turn FROM entities e
            LEFT JOIN entity_memory m ON m.entity_id=e.id WHERE e.canonical_name=?''',(event.subject.casefold(),)).fetchone()
        nodes = connection.execute("SELECT count(*) FROM entities WHERE status='ACTIVE'").fetchone()[0]
    groups = {}
    for f in facts.values():
        if f.live and f.predicate=='uses':
            groups[f.subject]=groups.get(f.subject,0)+1
    return MemoryState(target_facts=tuple(f for f in facts.values() if f.subject.casefold()==event.subject.casefold() and f.predicate==event.predicate),
        active_nodes=nodes,active_edges=sum(f.live for f in facts.values()),
        single_value_conflicts=sum(n>1 for n in groups.values()),subject_pinned=False if row is None else bool(row['is_pinned']),
        last_access_at=None if row is None else row['last_access_at'],last_access_turn=None if row is None else row['last_access_turn'])


def changes(before, after, stage):
    return tuple(Change(stage=stage,before=None if i not in before else before[i].status,fact=f)
        for i,f in after.items() if i not in before or before[i]!=f)


async def ingest(machine, event, extractor, clock):
    if not event.observation:
        return (), (), None
    source_role, _ = role_at(event.step)
    result = await extractor.extract(AgentMessage(agent_id=source_role,content=event.observation,turn=event.step),node='node2',allow_fallback=False)
    _routes(result.attempts,machine.client,'node2')
    def write(graph):
        if event.pin_subject:
            entity=graph.get_or_create_entity(event.pin_subject)
            graph.update_entity(entity.id,is_pinned=True)
            record_access(graph,[entity.id],turn=event.step,now=clock.now())
        for fact in result.graph.triples:
            subject=graph.get_or_create_entity(fact.subject);obj=graph.get_or_create_entity(fact.object)
            machine.writer.write_fact(subject.id,fact.predicate,obj.id,turn=event.step,now=clock.now(),confidence=fact.confidence)
    await machine.graph.write_async(write)
    return result.attempts, tuple(result.graph.triples), result.failure


async def run_integrated(plan, settings, output, *, execute=False, transport=None, progress=None):
    if execute is not True:
        raise PermissionError('execution requires execute=True')
    plan=IntegratedPlan.model_validate(plan)
    if transport is not None and not isinstance(transport,httpx.MockTransport):
        raise TypeError('offline injection requires HTTPX MockTransport')
    output=Path(output).expanduser().resolve()
    if settings.database_path.resolve().is_relative_to(output):
        raise ValueError('experiment directory must not contain application database')
    output.mkdir(parents=True,exist_ok=False)
    bundle_id=uuid4().hex;source='synthetic' if transport is not None else 'measured'
    manifest=dict(version=4,protocol='integrated-memory-v3',bundle_id=bundle_id,source=source,status='RUNNING',
        started_at=utc_now(),finished_at=None,plan=plan.model_dump(),conditions=CONDITIONS,
        scenario_hashes=plan.scenario_hashes(),scenarios={str(v):records(plan.turns,v) for v in range(1,plan.variants+1)},
        code_sha256=_provenance(),python=platform.python_version(),os=platform.system(),architecture=platform.machine(),
        packages=_packages(),inventory={},roles=[],warmups=[],runs=[],artifacts={},
        time_basis='Simulated governance age; real observation timestamps and perf_counter latency.',
        input_policy='Scripted observation is supplied to the routed role. Later cross-node probes use that role as source; no autonomous discovery is claimed.',
        fully_counterbalanced=plan.variants%4==0,
        primary_outcomes=PRIMARY_OUTCOMES,hypotheses=HYPOTHESES,generation_protocol=GENERATION_PROTOCOL,
        reply_error_policy=REPLY_ERROR_POLICY,
        inference_error_policy=INFERENCE_ERROR_POLICY,
        policies={c:configure(settings,plan,c).model_dump(mode='json',exclude={
            'database_path','node1_ollama_url','node2_ollama_url','coordinator_host'}) for c in CONDITIONS},
        measurement='Wall time includes diagnostic snapshots, observation extraction, role generation, response extraction and checkpoint work; excludes warmup/setup/exports.')
    write_json(output/'manifest.json',manifest)
    condition=variant=step=task=None;stage='setup'
    try:
        with (output/'turns.jsonl').open('x',encoding='utf-8') as raw, (output/'metrics.csv').open('x',encoding='utf-8',newline='') as csv_file:
            writer=None
            for position,(variant,condition) in enumerate(plan.order(),1):
                configured=configure(settings,plan,condition);clock=ScenarioClock(plan.hours_per_turn)
                stage='inventory';step=task=None
                async with OllamaClient(configured,transport=transport,record_prediction_aborts=True) as client:
                    inventory=await _inventory(client,synthetic=transport is not None)
                    if manifest['inventory'] and manifest['inventory']!=inventory:
                        raise ValueError('model inventory changed')
                    manifest['inventory']=inventory
                    agents=_agents(client)
                    roles=[dict(agent_id=a.agent_id,node=a.node,system=a.system_prompt(),
                        max_tokens=a.max_tokens,max_retries=a.max_retries,tool_rounds=a.max_tool_rounds) for a in agents]
                    if manifest['roles'] and manifest['roles']!=roles:
                        raise ValueError('role prompts or generation limits changed across conditions')
                    manifest['roles']=roles
                    items=workload(plan.turns,variant)
                    for event in items:
                        for actor in agents:actor.context_capacity(event.task,event.subject)
                    stage='warmup'
                    for node in ('node1','node2'):
                        for _ in range(plan.warmups_per_node):
                            g=await client.generate('Reply OK.',node=node,allow_fallback=False,max_tokens=8)
                            if g.error:raise AgentError('warmup','warmup prediction aborted',(g,))
                            _routes((g,),client,node)
                            manifest['warmups'].append(dict(condition=condition,variant=variant,node=node,
                                prompt_tokens=g.prompt_tokens,generated_tokens=g.generated_tokens,latency_ms=g.latency_ms))
                    run_id=f'{bundle_id}-{condition}-{variant}'
                    run=dict(run_id=run_id,condition=condition,variant=variant,position=position,completed_turns=0)
                    manifest['runs'].append(run)
                    async with Database(output/f'{condition}-{variant}.db',pool_size=configured.sqlite_pool_size,
                            busy_timeout_ms=configured.sqlite_busy_timeout_ms) as database:
                        graph=GraphStore(database);extractor=_RecordedExtractor(client,
                            max_tokens=GENERATION_PROTOCOL['extractor_max_tokens'],max_retries=GENERATION_PROTOCOL['extractor_max_retries'],record_errors=True)
                        machine=StateMachine(client,graph,agents=agents,extractor=extractor,
                            clock=clock.now,observation_clock=time.time,record_reply_errors=True)
                        if condition!='A':
                            await machine.start(items[0].task,initial_subgoal=items[0].subject,max_turns=plan.turns,run_id=run_id)
                        history=[]
                        with CSVLogger(output/f'{condition}-{variant}.csv') as logger:
                            logger.bind_run(run_id)
                            for event in items:
                                step,task=event.step,event.task;clock.step=step;stage='observation_extraction'
                                started=time.perf_counter();initial=catalog(graph)
                                incoming,observed,observation_failure=await ingest(machine,event,extractor,clock) if condition!='A' else ((),(),None)
                                pre=catalog(graph);before=snapshot(graph,event,pre)
                                stage='role_turn';role=agents[(step-1)%3]
                                budget=min(plan.memory_budget,role.context_capacity(task,event.subject))
                                full=dropped=0;pruned=();response_facts=();retrieved=();context_ids=();outgoing=();response_failure=None
                                if condition=='A':
                                    context,full,dropped=linear_context(history,budget)
                                    try:
                                        result=await role.execute(task,event.subject,context,turn=step,allow_fallback=False)
                                    except AgentError as exc:
                                        result=rejected_reply(exc,agent_id=role.agent_id,turn=step)
                                    metrics=_baseline_metrics(run_id,event,result,context,budget,started)
                                else:
                                    turn=await machine.step(run_id,task=task,subgoal=event.subject,governance_turn=step)
                                    result,context=turn.agent,turn.context.text
                                    metrics=TelemetryProfiler.from_checkpoint(turn.checkpoint)
                                    _routes(turn.extraction.attempts,client,'node2')
                                    context_ids=turn.context.triple_ids
                                    retrieved=tuple(pre[i] for i in context_ids)
                                    pruned=tuple(turn.checkpoint.result['pruned_ids'])
                                    response_facts=tuple(turn.extraction.graph.triples)
                                    outgoing=turn.extraction.attempts
                                    response_failure=getattr(turn.extraction,'failure',None)
                                _routes(result.generations,client,role.node)
                                post=catalog(graph);after=snapshot(graph,event,post)
                                answer=result.message.content if result.reply_error is None else None
                                valid,correct=evaluate(answer,event) if answer is not None else (False,False)
                                observation_ms=_sum(incoming,'latency_ms')
                                row=IntegratedRow(source=source,bundle_id=bundle_id,condition=condition,variant=variant,
                                    position=position,step=step,scenario_sha256=manifest['scenario_hashes'][str(variant)],
                                    task=task,observation=event.observation,answer=answer,reply_failure=reply_failure_evidence(result),
                                    observation_failure=observation_failure,response_failure=response_failure,
                                    observation_attempts=tuple(generation_evidence(g) for g in incoming),
                                    agent_attempts=tuple(generation_evidence(g) for g in result.generations),
                                    response_attempts=tuple(generation_evidence(g) for g in outgoing),
                                    format_valid=valid,correct=correct,
                                    context=context,context_triple_ids=context_ids,retrieved_facts=retrieved,logical_time=clock.now(),
                                    memory_before=before,memory_after=after,changes=changes(initial,pre,'observation')+changes(pre,post,'response'),
                                    pruned_entity_ids=pruned,observation_facts=observed,response_facts=response_facts,
                                    observation_calls=len(incoming),observation_prompt_tokens=_sum(incoming,'prompt_tokens'),
                                    observation_generated_tokens=_sum(incoming,'generated_tokens'),observation_latency_ms=observation_ms,
                                    total_prompt_tokens=_add(metrics.prompt_tokens,_sum(incoming,'prompt_tokens')),
                                    total_generated_tokens=_add(metrics.generated_tokens,_sum(incoming,'generated_tokens')),
                                    total_inference_ms=metrics.generation_latency_ms+observation_ms,wall_ms=(time.perf_counter()-started)*1000,
                                    history_full_bytes=full,history_dropped_turns=dropped,telemetry=metrics)
                                flat=project(row,event)
                                if writer is None:
                                    writer=csv.DictWriter(csv_file,fieldnames=tuple(flat));writer.writeheader()
                                raw.write(row.model_dump_json()+'\n');raw.flush()
                                writer.writerow(flat);csv_file.flush();logger.append(metrics)
                                history.append(dict(task=task,answer=answer));run['completed_turns']=step
                                if progress:progress(row)
                write_json(output/'manifest.json',manifest)
        if _provenance()!=manifest['code_sha256']:
            raise ValueError('source changed during experiment')
        manifest['status']='COMPLETED'
    except BaseException as exc:
        manifest['status']='CANCELLED' if isinstance(exc,(KeyboardInterrupt,asyncio.CancelledError)) else 'FAILED'
        manifest['error_type']=type(exc).__name__
        write_json(output/'failure.json',_failure_record(exc,manifest=manifest,condition=condition,
            repetition=variant,step=step,stage=stage,task=task))
        raise
    finally:
        manifest['finished_at']=utc_now()
        manifest['artifacts']={str(p.relative_to(output)):sha256(p) for p in sorted(output.rglob('*')) if p.is_file() and p.name!='manifest.json'}
        write_json(output/'manifest.json',manifest)
    return manifest


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute',action='store_true');parser.add_argument('--output',type=Path)
    parser.add_argument('--turns',type=int,default=60);parser.add_argument('--variants',type=int,default=4)
    args=parser.parse_args(argv)
    try:
        plan=IntegratedPlan(turns=args.turns,variants=args.variants)
        if not args.execute:
            print(f'PLAN ONLY: {len(plan.order())} condition runs; {plan.row_count} scored turns; no model requests or experiment files')
            print(json.dumps(plan.model_dump()|{'order':plan.order(),'scenario_hashes':plan.scenario_hashes(),
                'fully_counterbalanced':plan.variants%4==0,'reply_error_policy':REPLY_ERROR_POLICY,
                'inference_error_policy':INFERENCE_ERROR_POLICY},indent=2))
            return 0
        if args.output is None:parser.error('--execute requires --output with a new directory')
        from src.config import get_settings
        def progress(row):
            outcome='rejected' if row.reply_failure else 'valid'
            incoming='failed' if row.observation_failure else ('valid' if row.observation_calls else 'skipped')
            outgoing='failed' if row.response_failure else ('valid' if row.telemetry.extraction_calls else 'skipped')
            print(f'TURN {row.condition} variant={row.variant} step={row.step}/{plan.turns} reply_json={outcome} '
                f'observation_extraction={incoming} response_extraction={outgoing} wall_ms={row.wall_ms:.1f}',flush=True)
        manifest=asyncio.run(run_integrated(plan,get_settings(),args.output,execute=True,progress=progress))
        print('INTEGRATED EXPERIMENT COMPLETE:',manifest['bundle_id'])
    except (KeyboardInterrupt,asyncio.CancelledError):
        print('EXPERIMENT STOPPED: partial evidence retained',file=sys.stderr);return 130
    except Exception as exc:
        print(f'ERROR: {type(exc).__name__}: {exc}',file=sys.stderr);return 1
    return 0


if __name__=='__main__':raise SystemExit(main())
