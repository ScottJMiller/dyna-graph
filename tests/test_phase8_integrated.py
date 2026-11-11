"""Offline protocol-2 validation; synthetic HTTP is mandatory for these tests."""

import asyncio
from collections import Counter
import csv
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3

import httpx
from pydantic import ValidationError
import pytest

from experiments import integrated_analysis as analysis, integrated_harness as harness
from experiments.contracts import load_json,sha256,write_json
from experiments.integrated_contracts import CONDITIONS,IntegratedPlan,IntegratedRow,project
from experiments.integrated_scenario import Event,evaluate,fingerprint,records,role_at,workload
from src.config import Settings
from src.network.ollama_client import OllamaClient
from src.orchestration import StateMachine
from src.storage.database import Database
from src.storage.graph_store import GraphStore


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    async def forbidden(*args,**kwargs):raise AssertionError('integrated tests must not contact a real server')
    monkeypatch.setattr(httpx.AsyncHTTPTransport,'handle_async_request',forbidden)
    names={n.upper() for n in Settings.model_fields}
    for f in Settings.model_fields.values():
        if f.validation_alias:names.update(a.upper() for a in f.validation_alias.choices)
    for name in list(os.environ):
        if name.upper() in names:monkeypatch.delenv(name)


def settings(tmp_path):
    return Settings(_env_file=None,node2_ollama_url='http://192.168.1.20:11434',database_path=tmp_path/'application.db')


class SyntheticServer:
    """Answers only from the actual task/context. It has no scenario/oracle access."""
    def __init__(self,config,*,missing=False,fail_at=None,cancel=False,wrong_model=False,changed_inventory=False):
        self.config=config;self.missing=missing;self.fail_at=fail_at;self.cancel=cancel
        self.wrong_model=wrong_model;self.changed_inventory=changed_inventory
        self.posts=[];self.inventories=0

    @staticmethod
    def facts(text):
        return re.findall(r'\b([A-Za-z][A-Za-z0-9]+) (uses|supports|links) ([A-Za-z0-9]+)\.',text)

    async def __call__(self,request):
        node='node1' if request.url.host=='localhost' else 'node2'
        model=self.config.node_model(node)
        if request.url.path=='/api/version':return httpx.Response(200,json={'version':'synthetic-only'})
        if request.url.path=='/api/tags':
            self.inventories+=1;letter='b' if self.changed_inventory and self.inventories>2 else 'a'
            return httpx.Response(200,json={'models':[{'name':model,'size':1000,'digest':letter*64,'details':{'quantization_level':'Q4_K_M'}}]})
        body=json.loads(request.content);self.posts.append(body)
        if self.fail_at==len(self.posts):
            if self.cancel:raise asyncio.CancelledError()
            raise httpx.ReadTimeout('synthetic failure',request=request)
        if 'system' not in body:output='OK'
        elif body['system'].startswith('Extract explicitly'):
            text=json.JSONDecoder().raw_decode(body['prompt'].split('\n',1)[1])[0]['message']
            output=json.dumps({'triples':[dict(subject=s,predicate=p,object=o) for s,p,o in self.facts(text)]})
        else:
            task=json.JSONDecoder().raw_decode(body['prompt'].split('TASK\n',1)[1])[0]
            context=body['prompt'].split('\nMEMORY\n',1)[1]
            known={}
            def add(facts):
                for s,p,o in facts:
                    if p=='uses':known[(s,p)]={o}
                    else:known.setdefault((s,p),set()).add(o)
            if context.startswith('['):
                for entry in json.loads(context):add(self.facts(entry['task']));add(self.facts(entry['answer']))
            else:add(re.findall(r'"([A-Za-z0-9]+)" -- "(uses|supports|links)" --> "([A-Za-z0-9]+)"',context))
            add(self.facts(task.split('\n')[0]))
            predicate,subject=re.search(r'Question: current (uses|supports|links) values for ([A-Za-z0-9]+)\?',task).groups()
            answer=' '.join(f'{subject} {predicate} {v}.' for v in sorted(known.get((subject,predicate),()))) or 'Unknown.'
            output=json.dumps(dict(content=answer,next_subgoal=None,complete=False))
        counters={} if self.missing else dict(prompt_eval_count=100+len(body['prompt'])//4,eval_count=20)
        return httpx.Response(200,json=dict(model='wrong' if self.wrong_model else model,response=output,done=True,
            done_reason='stop',total_duration=1_000_000,**counters))


def make_bundle(path,*,variants=1,turns=60,budget=1000,**options):
    config=settings(path);fake=SyntheticServer(config,**options)
    plan=IntegratedPlan(variants=variants,turns=turns,memory_budget=budget,warmups_per_node=0)
    output=path/'bundle'
    manifest=asyncio.run(harness.run_integrated(plan,config,output,execute=True,transport=httpx.MockTransport(fake)))
    return output,manifest,fake


@pytest.fixture(scope='module')
def evidence(tmp_path_factory):
    return make_bundle(tmp_path_factory.mktemp('integrated'))


def test_default_only_plans_without_settings_or_files(tmp_path,monkeypatch,capsys):
    from src import config
    monkeypatch.setattr(config,'get_settings',lambda:pytest.fail('plan mode loaded private settings'))
    assert harness.main(['--output',str(tmp_path/'absent')])==0
    assert not (tmp_path/'absent').exists()
    assert '16 condition runs; 960 scored turns' in capsys.readouterr().out


def test_execution_gate_and_output_requirement(tmp_path):
    with pytest.raises(PermissionError):asyncio.run(harness.run_integrated(IntegratedPlan(),settings(tmp_path),tmp_path/'none'))
    assert not (tmp_path/'none').exists()
    with pytest.raises(SystemExit) as exc:harness.main(['--execute'])
    assert exc.value.code==2


@pytest.mark.parametrize('kwargs',[{'turns':59},{'turns':81},{'turns':True},{'variants':0},{'variants':9},
    {'memory_budget':127},{'memory_budget':1001},{'max_tokens':63},{'max_tokens':257},
    {'warmups_per_node':-1},{'hours_per_turn':1.0},{'pruning_threshold':.2},{'version':1}])
def test_plan_rejects_invalid_design(kwargs):
    with pytest.raises(ValidationError):IntegratedPlan(**kwargs)


def test_order_balances_positions_and_adjacent_predecessors():
    plan=IntegratedPlan();orders=[''.join(c for v,c in plan.order() if v==variant) for variant in range(1,5)]
    assert all(set(order)==set('ABCD') for order in orders)
    for position in range(4):assert {o[position] for o in orders}==set('ABCD')
    pairs=Counter(o[i:i+2] for o in orders for i in range(3))
    assert len(pairs)==12 and set(pairs.values())=={1}


@pytest.mark.parametrize('turns',[60,70,80])
def test_workload_has_required_cases_and_valid_prompt_capacity(turns,tmp_path):
    plan=IntegratedPlan(turns=turns)
    items=workload(turns,1)
    assert len(items)==turns and any(e.category=='delayed' and e.step-min(e.source_steps)>30 for e in items)
    assert {'update','reversion','pinned','multivalued','cycle','unknown'}<={e.category for e in items}
    assert len(items[5].expected)==2 and items[7].expected==items[0].expected
    assert items[1].pin_subject and all(e.pin_subject is None for e in items if e.step!=2)
    assert all(len(e.task)<=500 for e in items)
    client=OllamaClient(settings(tmp_path))
    for actor in harness._agents(client):
        assert all(actor.context_capacity(e.task,e.subject)>0 for e in items)
    assert records(turns,1)==records(turns,1)
    assert len({fingerprint(turns,v) for v in range(1,5)})==4


@pytest.mark.parametrize('turns,variant',[(20,1),(81,1),(60,0),(60,9),(True,1)])
def test_workload_invalid_arguments(turns,variant):
    with pytest.raises(ValueError):workload(turns,variant)


def test_fingerprint_includes_actual_prompt(monkeypatch):
    original=Event.task.fget;before=fingerprint()
    monkeypatch.setattr(Event,'task',property(lambda self:original(self)+' Changed framing.'))
    assert fingerprint()!=before


def test_scoring_sets_and_format_are_explicit():
    event=workload()[5]
    a,b=event.expected
    assert evaluate(f'{event.subject} supports {b}. {event.subject} supports {a}.',event)==(True,True)
    assert evaluate(f'{event.subject} supports {a}.',event)==(True,False)
    assert evaluate('Unknown.',event)==(True,False)
    assert evaluate('I think it supports both.',event)==(False,False)
    assert evaluate('Unknown.',next(e for e in workload() if not e.expected))==(True,True)


def test_four_policies_are_independent_of_private_settings(tmp_path):
    config=settings(tmp_path).model_copy(update={'single_valued_predicates':('supports',),'pruning_threshold':.9})
    for c in CONDITIONS:
        s=harness.configure(config,IntegratedPlan(),c)
        assert s.single_valued_predicates==(('uses',) if c in 'CD' else ())
        assert s.orchestration_prune_between_turns==(c=='D')
        assert s.pruning_threshold==.15 and s.decay_recency_half_life_seconds==86400
        assert not s.orchestration_allow_fallback


def test_complete_synthetic_bundle_validates_and_is_not_research_result(evidence):
    output,manifest,fake=evidence
    m,plan,rows,flat=analysis.load_bundle(output)
    assert m['source']=='synthetic' and len(rows)==240 and len(flat)==240
    assert [r['condition'] for r in m['runs']]==list('ABDC')
    assert len(m['artifacts'])==10 and not m['fully_counterbalanced']
    assert fake.posts
    for r in rows:
        assert r.logical_time==r.step*7200 and r.telemetry.observed_at>1e9
        assert r.telemetry.governance_turn==r.step
        assert r.memory_before.last_access_at is None or r.memory_before.last_access_at<=r.logical_time
        assert r.telemetry.context_budget_used<=r.telemetry.context_budget_limit<=1000
    assert not (output.parent/'application.db').exists()


def test_supersession_multivalue_pruning_and_delayed_loss_are_observable(evidence):
    _,_,rows,flat=analysis.load_bundle(evidence[0])
    assert any(r.memory_after.single_value_conflicts for r in rows if r.condition=='B')
    assert all(not r.memory_after.single_value_conflicts for r in rows if r.condition in 'CD')
    assert any(c.fact.status=='SUPERSEDED' for r in rows if r.condition=='C' for c in r.changes)
    assert any(r.pruned_entity_ids for r in rows if r.condition=='D')
    for c in 'BCD':
        r=next(r for r in rows if r.condition==c and r.step==6)
        assert len([f for f in r.memory_after.target_facts if f.live])==2
    delayed=next(r for r in flat if r['condition']=='D' and r['step']==40)
    assert delayed['support_available'] is False and not delayed['correct']
    pinned=next(r for r in flat if r['condition']=='D' and r['step']==50)
    assert pinned['support_available'] is True and pinned['correct']
    cycle=next(r for r in flat if r['condition']=='D' and r['step']==59)
    assert cycle['support_available'] is True
    assert any(r['cross_node'] and r['lag']>=10 for r in flat)


def test_analysis_and_plot_outputs_are_descriptive_and_synthetic(evidence,tmp_path):
    output=evidence[0];report=analysis.write_analysis(output,tmp_path/'analysis')
    assert set(report['conditions'])==set('ABCD') and len(report['contrasts'])==3
    assert {p['through_step'] for p in report['checkpoints']}=={20,40,60}
    assert {r['purpose'] for r in report['contrasts']}=={'complete framework','supersession','pruning'}
    paths=analysis.generate_plots(output,tmp_path/'figures')
    assert len(paths)==18 and {p.suffix for p in paths}=={'.svg','.pdf','.png'}
    for p in paths:
        if p.suffix=='.svg':assert 'SYNTHETIC TEST DATA' in p.read_text()
    with pytest.raises(FileExistsError):analysis.write_analysis(output,tmp_path/'analysis')
    with pytest.raises(FileExistsError):analysis.generate_plots(output,tmp_path/'figures')


def test_missing_token_counters_stay_unknown(tmp_path):
    output,_,_=make_bundle(tmp_path,missing=True)
    report=analysis.analyze_bundle(output)
    assert all(r['total_prompt_tokens'] is None and r['role_prompt_tokens'] is None for r in report['conditions'].values())
    assert all(c['pipeline_prompt_delta'] is None for c in report['contrasts'])


@pytest.mark.parametrize('options,status',[({'fail_at':1},'FAILED'),({'fail_at':1,'cancel':True},'CANCELLED'),
    ({'wrong_model':True},'FAILED'),({'changed_inventory':True},'FAILED')])
def test_failed_or_cancelled_cohorts_are_preserved_and_rejected(tmp_path,options,status):
    with pytest.raises(BaseException):make_bundle(tmp_path,**options)
    m=load_json(tmp_path/'bundle/manifest.json')
    assert m['status']==status and 'failure.json' in m['artifacts']
    with pytest.raises(ValueError,match='completed'):analysis.analyze_bundle(tmp_path/'bundle')


def rewrite_rows(path,change):
    rows=[json.loads(line) for line in (path/'turns.jsonl').read_text().splitlines()]
    change(rows)
    (path/'turns.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in rows))
    m=load_json(path/'manifest.json');m['artifacts']['turns.jsonl']=sha256(path/'turns.jsonl');write_json(path/'manifest.json',m)


@pytest.mark.parametrize('kind',['score','clock','route','context','transition','timestamp','budget','order','rows','checkpoint'])
def test_modified_evidence_is_rejected_even_with_updated_file_checksum(evidence,tmp_path,kind):
    output=tmp_path/'tampered';shutil.copytree(evidence[0],output)
    def change(rows):
        row=rows[60]
        if kind=='score':row['correct']=not row['correct']
        elif kind=='clock':row['logical_time']=10.0
        elif kind=='route':row['telemetry']['node']='node2'
        elif kind=='context':row['context']='invented'
        elif kind=='transition':row['changes'][0]['fact']['status']='PRUNED'
        elif kind=='timestamp':row['telemetry']['observed_at']=7200.0
        elif kind=='budget':row['telemetry']['context_budget_limit']=127
        elif kind=='order':rows[1],rows[2]=rows[2],rows[1]
        elif kind=='rows':rows.pop()
        elif kind=='checkpoint':row['answer']='Unknown.';row['correct']=False
    rewrite_rows(output,change)
    with pytest.raises((ValueError,ValidationError)):analysis.analyze_bundle(output)


def test_csv_corruption_checksum_and_path_escape_are_rejected(evidence,tmp_path):
    output=tmp_path/'tampered';shutil.copytree(evidence[0],output)
    (output/'metrics.csv').write_text('wrong\n')
    with pytest.raises(ValueError,match='checksum'):analysis.load_bundle(output)
    m=load_json(output/'manifest.json');m['artifacts']['metrics.csv']=sha256(output/'metrics.csv');write_json(output/'manifest.json',m)
    with pytest.raises(ValueError,match='CSV'):analysis.load_bundle(output)


def test_old_saved_case_study_still_analyzes():
    from experiments.analyze_results import analyze_bundle
    root=Path(__file__).resolve().parents[1]
    report=analyze_bundle(root/'docs/results/study-20261111-002')
    assert report['source']=='measured' and report['conditions']['A']['turns']==40


def test_source_and_private_paths_are_not_in_public_manifest(evidence):
    text=(evidence[0]/'manifest.json').read_text()
    assert 'application.db' not in text and '192.168.1.20' not in text and '/home/' not in text


def test_explicit_turns_and_separate_clocks_preserve_commit_semantics(tmp_path):
    async def run():
        config=settings(tmp_path);fake=SyntheticServer(config)
        async with OllamaClient(config,transport=httpx.MockTransport(fake)) as client:
            async with Database(tmp_path/'clocks.db') as database:
                graph=GraphStore(database)
                machine=StateMachine(client,graph,clock=lambda:7200.0,observation_clock=lambda:2e9)
                event=workload()[0];await machine.start(event.task,initial_subgoal=event.subject,max_turns=2,run_id='clock-test')
                await harness.ingest(machine,event,machine.extractor,type('Clock',(),{'now':lambda self:7200.0})())
                result=await machine.step('clock-test',governance_turn=1)
                assert result.checkpoint.governance_turn==1
                assert result.checkpoint.result['governance_time']==7200
                assert result.checkpoint.result['telemetry']['observed_at']==2e9
                with pytest.raises(ValueError,match='follow committed'):
                    await machine.step('clock-test',governance_turn=1)
                assert (await machine.get_checkpoint('clock-test')).next_step==1
    asyncio.run(run())


def test_full_counterbalanced_80_turn_design_executes_offline(tmp_path):
    output,manifest,_=make_bundle(tmp_path,turns=80,variants=4)
    report=analysis.analyze_bundle(output)
    assert manifest['fully_counterbalanced'] and len(manifest['runs'])==16
    assert all(c['turns']==320 for c in report['conditions'].values())
    assert len(report['contrasts'])==12 and len(report['curves'])==320
    assert {p['through_step'] for p in report['checkpoints']}=={20,40,60,80}
    assert report['contrast_summary']['pruning']['accuracy_delta']['n_variants']==4


def test_fresh_directory_and_application_data_guard(tmp_path):
    output=tmp_path/'existing';output.mkdir();(output/'keep').write_text('preserve')
    with pytest.raises(FileExistsError):
        asyncio.run(harness.run_integrated(IntegratedPlan(),settings(tmp_path),output,execute=True))
    assert (output/'keep').read_text()=='preserve'
    protected=tmp_path/'parent'
    with pytest.raises(ValueError,match='application database'):
        asyncio.run(harness.run_integrated(IntegratedPlan(),settings(tmp_path).model_copy(update={'database_path':protected/'application.db'}),protected,execute=True))
    assert not protected.exists()


def test_extraction_failure_records_exact_source(tmp_path):
    config=settings(tmp_path);fake=SyntheticServer(config)
    async def broken(request):
        response=await fake(request)
        body=json.loads(request.content) if request.method=='POST' else {}
        if body.get('system','').startswith('Extract explicitly'):
            data=response.json();data['response']=json.dumps({'triples':[dict(subject='NotInSource',predicate='uses',object='Invented')]})
            return httpx.Response(200,json=data)
        return response
    output=tmp_path/'failed'
    with pytest.raises(Exception,match='verbatim|source|grounding'):
        asyncio.run(harness.run_integrated(IntegratedPlan(variants=1,warmups_per_node=0),config,output,execute=True,transport=httpx.MockTransport(broken)))
    failure=load_json(output/'failure.json')
    assert failure['condition']=='B' and failure['step']==1 and failure['stage']=='observation_extraction'
    assert failure['source_message']['content']==workload()[0].observation
    assert len(failure['generation_attempts'])==2


def test_default_state_machine_still_uses_its_original_clock(tmp_path):
    async def run():
        config=settings(tmp_path);fake=SyntheticServer(config)
        async with OllamaClient(config,transport=httpx.MockTransport(fake)) as client:
            async with Database(tmp_path/'default-clock.db') as database:
                machine=StateMachine(client,GraphStore(database),clock=lambda:2e9)
                event=workload()[0]
                await machine.start(event.task,initial_subgoal=event.subject,max_turns=1,run_id='default-clock')
                result=await machine.step('default-clock')
                assert result.checkpoint.result['telemetry']['observed_at']==2e9
                assert result.checkpoint.governance_turn==1
                with pytest.raises(ValueError,match='observation_clock'):
                    StateMachine(client,machine.graph,observation_clock=4)
    asyncio.run(run())


def test_small_budget_preserves_validation_and_caps(tmp_path):
    output,_,_=make_bundle(tmp_path,budget=128)
    report=analysis.analyze_bundle(output)
    assert all(c['turns']==60 for c in report['conditions'].values())
    assert all(r.telemetry.context_budget_used<=128 for r in analysis.load_bundle(output)[2])


def test_diagnostics_distinguish_pruning_and_conflicting_active_values(evidence):
    report=analysis.analyze_bundle(evidence[0])
    assert report['conditions']['D']['missing_support_states']['pruned']>0
    assert report['conditions']['B']['turns_with_single_value_conflicts']>0
    assert report['conditions']['C']['turns_with_single_value_conflicts']==0


def test_snapshot_conflict_counts_cannot_be_relabelled(evidence,tmp_path):
    output=tmp_path/'tampered';shutil.copytree(evidence[0],output)
    def change(rows):
        row=next(r for r in rows if r['condition']=='B' and r['step']==7)
        row['memory_after']['single_value_conflicts']=0
    rewrite_rows(output,change)
    with pytest.raises(ValueError,match='conflicts'):analysis.load_bundle(output)


def test_omitted_retrieval_is_distinct_from_missing_graph_fact(evidence):
    rows=analysis.load_bundle(evidence[0])[2]
    original=next(r for r in rows if r.condition=='C' and r.step==6)
    omitted=original.model_copy(update={'context':'','context_triple_ids':(),'retrieved_facts':(),
        'telemetry':original.telemetry.model_copy(update={'context_budget_used':0})})
    row=project(omitted,workload()[5])
    assert row['support_available'] is True and row['support_supplied'] is False
    summary=analysis.describe([row])
    assert summary['graph_support_missing']==0 and summary['retrieval_support_missing']==1


def test_manifest_records_full_nonprivate_policies_and_rejects_policy_changes(evidence,tmp_path):
    m=evidence[1]
    assert set(m['policies'])==set('ABCD')
    assert m['generation_protocol']['extractor_max_retries']==1
    assert m['policies']['C']['single_valued_predicates']==['uses']
    output=tmp_path/'tampered';shutil.copytree(evidence[0],output)
    changed=load_json(output/'manifest.json');changed['policies']['C']['orchestration_prune_between_turns']=True
    write_json(output/'manifest.json',changed)
    with pytest.raises(ValueError,match='condition policy'):analysis.load_bundle(output)


def test_recall_gap_does_not_count_rehearsed_facts_as_long_delay():
    items=workload(80,1)
    assert items[39].recall_gap==37
    assert items[55].recall_gap==16
    assert items[27].recall_gap==26
    assert all(e.recall_gap<10 for e in items if e.category=='frequent')
    frequent=next(e for e in items if e.category=='frequent' and e.step>40)
    assert frequent.step-min(frequent.source_steps)>30 and frequent.recall_gap<4
