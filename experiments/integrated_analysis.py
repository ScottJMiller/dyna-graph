"""Validate protocol-4 evidence, including recorded rejected-reply outcomes."""

import argparse
from collections import Counter
import csv
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import statistics
import sys

from experiments.contracts import load_json, sha256, write_json
from experiments.integrated_contracts import CONDITIONS, GENERATION_PROTOCOL, REPLY_ERROR_POLICY, INFERENCE_ERROR_POLICY, PRIMARY_OUTCOMES, HYPOTHESES, FactState, IntegratedPlan, IntegratedRow, project
from experiments.integrated_scenario import evaluate, records, role_at, workload
from experiments.benchmark_harness import linear_context
from src.retrieval.context_assembler import _quoted
from src.agents.base_agent import REJECTED_REPLY_CONTENT


def require(value,message):
    if not value:raise ValueError(message)


def _target(catalog,event):
    return tuple(f for f in catalog.values() if f.subject.casefold()==event.subject.casefold() and f.predicate==event.predicate)


def _apply(catalog,changes,stage):
    for change in changes:
        if change.stage!=stage:continue
        old=catalog.get(change.fact.id)
        require(change.before==(None if old is None else old.status),'state transition has wrong prior status')
        require(old!=change.fact,'redundant state transition')
        catalog[change.fact.id]=change.fact


def _checkpoint_rows(path,condition,turns):
    with sqlite3.connect(path.as_uri()+'?mode=ro&immutable=1',uri=True) as connection:
        require(connection.execute('PRAGMA integrity_check').fetchone()[0]=='ok','SQLite integrity failure')
        require(not connection.execute('PRAGMA foreign_key_check').fetchall(),'SQLite foreign key failure')
        if condition=='A':
            require(connection.execute('SELECT count(*) FROM triples').fetchone()[0]==0,'baseline database has graph facts')
            require(connection.execute('SELECT count(*) FROM workflow_turns').fetchone()[0]==0,'baseline has graph checkpoints')
            return []
        runs=connection.execute('SELECT status,next_step FROM workflow_runs').fetchall()
        require(runs==[('COMPLETED',turns)],'graph run is not durably complete')
        rows=connection.execute('SELECT run_id,step,governance_turn,agent_id,node,message,result FROM workflow_turns ORDER BY step').fetchall()
        require(len(rows)==turns,'missing graph checkpoints')
        return rows


def load_bundle(path):
    path=Path(path).expanduser().resolve()
    require((path/'manifest.json').stat().st_size<=2_000_000,'oversized manifest')
    m=load_json(path/'manifest.json')
    require(type(m.get('version')) is int and m['version']==4 and m.get('protocol')=='integrated-memory-v3' and m.get('status')=='COMPLETED',
        'integrated analysis requires a completed protocol-4 bundle')
    require(m.get('source') in ('measured','synthetic'),'invalid evidence source')
    plan=IntegratedPlan.model_validate(m['plan'])
    require(m['conditions']==json.loads(json.dumps(CONDITIONS)),'condition policies changed')
    require(m['generation_protocol']==GENERATION_PROTOCOL,'generation protocol differs')
    require(m['reply_error_policy']==REPLY_ERROR_POLICY,'reply error policy differs')
    require(m['inference_error_policy']==INFERENCE_ERROR_POLICY,'inference error policy differs')
    require(m['primary_outcomes']==PRIMARY_OUTCOMES and m['hypotheses']==HYPOTHESES,'declared outcomes differ')
    require(m['fully_counterbalanced']==(plan.variants%4==0),'balance declaration differs from plan')
    require(m['scenario_hashes']==plan.scenario_hashes(),'scenario fingerprint differs')
    require(m['scenarios']==json.loads(json.dumps({str(v):records(plan.turns,v) for v in range(1,plan.variants+1)})),
        'rendered inputs or independent oracle differ')
    require(set(m['policies'])==set(CONDITIONS),'condition policies missing')
    base=m['policies']['A']
    for condition,policy in m['policies'].items():
        expected=base|dict(single_valued_predicates=list(CONDITIONS[condition]['single_valued_predicates']),
            orchestration_prune_between_turns=CONDITIONS[condition]['prune'],orchestration_allow_fallback=False,
            orchestration_max_turns=plan.turns,context_token_budget=plan.memory_budget,agent_max_tokens=plan.max_tokens,
            agent_max_tool_rounds=0,pruning_threshold=plan.pruning_threshold,pruning_batch_size=100,
            decay_recency_half_life_seconds=plan.recency_half_life_seconds,decay_turn_half_life=plan.turn_half_life)
        require(policy==expected,'condition policy differs from declared experiment')
        require(not {'database_path','node1_ollama_url','node2_ollama_url','coordinator_host'} & set(policy),'private configuration in manifest')
    require(set(m['inventory'])=={'node1','node2'},'node inventory missing')
    for node,item in m['inventory'].items():
        require(item['name']==base[f'{node}_ollama_model'] and item['threads']==base[f'{node}_ollama_num_threads'] and
            item['quantization_level']=='Q4_K_M' and isinstance(item['digest'],str) and len(item['digest'])==64,'model inventory differs from policy')
    require([(r['agent_id'],r['node']) for r in m['roles']]==[role_at(s) for s in (1,2,3)],'role declarations differ')
    require(all(r['max_tokens']==plan.max_tokens and r['tool_rounds']==0 for r in m['roles']),'role limits differ')
    expected_files={'turns.jsonl','metrics.csv',*(f'{c}-{v}.{suffix}' for v,c in plan.order() for suffix in ('db','csv'))}
    require(set(m['artifacts'])==expected_files,'missing or unexpected experiment artifacts')
    for name,digest in m['artifacts'].items():
        target=(path/name).resolve()
        require(not Path(name).is_absolute() and target.is_relative_to(path),'unsafe artifact path')
        require(target.is_file() and sha256(target)==digest,f'artifact checksum mismatch: {name}')
    require((path/'turns.jsonl').stat().st_size<=plan.row_count*1_000_000,'turn evidence exceeds bound')
    rows=[]
    with (path/'turns.jsonl').open() as stream:
        for line in stream:
            require(len(line)<=1_000_000 and len(rows)<plan.row_count,'oversized or excess turn evidence')
            rows.append(IntegratedRow.model_validate_json(line))
    require(len(rows)==plan.row_count,'incomplete scored turns')
    expected_runs=[];flat=[];budgets={}
    start=datetime.fromisoformat(m['started_at']).timestamp();end=datetime.fromisoformat(m['finished_at']).timestamp()
    for position,(variant,condition) in enumerate(plan.order(),1):
        run_id=f'{m["bundle_id"]}-{condition}-{variant}'
        expected_runs.append(dict(run_id=run_id,condition=condition,variant=variant,position=position,completed_turns=plan.turns))
        checkpoints=_checkpoint_rows(path/f'{condition}-{variant}.db',condition,plan.turns)
        history=[];catalog={}
        selected=rows[(position-1)*plan.turns:position*plan.turns]
        with (path/f'{condition}-{variant}.csv').open(newline='') as stream:
            telemetry_rows=list(csv.DictReader(stream))
        require(len(telemetry_rows)==plan.turns,'telemetry CSV completeness differs')
        for event,row in zip(workload(plan.turns,variant),selected):
            require((row.source,row.bundle_id,row.condition,row.variant,row.position,row.step,row.scenario_sha256)==
                (m['source'],m['bundle_id'],condition,variant,position,event.step,m['scenario_hashes'][str(variant)]),'turn identity/order differs')
            require((row.task,row.observation)==(event.task,event.observation),'input differs from frozen workload')
            require((row.format_valid,row.correct)==((False,False) if row.reply_failure else evaluate(row.answer,event)),'answer scoring differs')
            require((row.telemetry.agent_id,row.telemetry.node)==role_at(event.step),'role/node route differs')
            require(row.telemetry.run_id==run_id and row.telemetry.step==row.step and row.telemetry.governance_turn==row.step,'checkpoint identity differs')
            require(not row.telemetry.fallback_attempts,'fallback is prohibited')
            for attempts,node in [(row.agent_attempts,row.telemetry.node),(row.observation_attempts,'node2'),(row.response_attempts,'node2')]:
                require(all(a.node==node and a.model==m['inventory'][node]['name'] for a in attempts),'attempt route/model differs')
            for failure in (row.observation_failure,row.response_failure):
                if failure:require(len(failure.attempts)==GENERATION_PROTOCOL['extractor_max_retries']+1,'extraction correction attempts are incomplete')
            if row.reply_failure:
                role=next(r for r in m['roles'] if r['agent_id']==row.telemetry.agent_id)
                require(len(row.reply_failure.attempts)==role['max_retries']+1,'reply correction attempts are incomplete')
                require(all(a.node==row.telemetry.node and a.model==m['inventory'][a.node]['name']
                    for a in row.reply_failure.attempts),'rejected reply route differs')
            require(row.logical_time==event.step*plan.hours_per_turn*3600,'logical time differs')
            require(start<=row.telemetry.observed_at<=end,'observation timestamp is not real run time')
            require(row.telemetry.context_budget_limit<=plan.memory_budget,'memory allowance exceeded')
            key=(variant,event.step);budgets.setdefault(key,row.telemetry.context_budget_limit)
            require(budgets[key]==row.telemetry.context_budget_limit,'memory allowances differ across conditions')
            for state in (row.memory_before,row.memory_after):
                require(all(f.subject.casefold()==event.subject.casefold() and f.predicate==event.predicate for f in state.target_facts),'snapshot includes wrong target facts')
                require(state.last_access_at is None or state.last_access_at<=row.logical_time,'mixed governance clocks')
                require(state.last_access_turn is None or state.last_access_turn<=row.step,'mixed governance turns')
            require((row.memory_after.active_nodes,row.memory_after.active_edges)==(row.telemetry.active_nodes,row.telemetry.active_edges),'graph counts differ from checkpoint')
            require([f.id for f in row.retrieved_facts]==list(row.context_triple_ids) and len(set(row.context_triple_ids))==len(row.context_triple_ids),'retrieved IDs differ')
            _apply(catalog,row.changes,'observation')
            require(row.memory_before.target_facts==_target(catalog,event),'pre-recall facts differ from event history')
            require(row.memory_before.active_edges==sum(f.live for f in catalog.values()),'pre-recall edge count differs')
            groups=Counter(f.subject for f in catalog.values() if f.live and f.predicate=='uses')
            require(row.memory_before.single_value_conflicts==sum(n>1 for n in groups.values()),'pre-recall conflicts differ')
            for fact in row.retrieved_facts:
                require(fact.live and catalog.get(fact.id)==fact,'retrieval contains inactive or unrecorded fact')
                line=f'- [t{fact.id}] {_quoted(fact.subject)} -- {_quoted(fact.predicate)} --> {_quoted(fact.object)}'
                require(line in row.context,'retrieved fact missing from supplied memory')
            _apply(catalog,row.changes,'response')
            require(row.memory_after.target_facts==_target(catalog,event),'post-write facts differ from event history')
            require(row.memory_after.active_edges==sum(f.live for f in catalog.values()),'post-write edge count differs')
            groups=Counter(f.subject for f in catalog.values() if f.live and f.predicate=='uses')
            require(row.memory_after.single_value_conflicts==sum(n>1 for n in groups.values()),'post-write conflicts differ')
            if condition=='A':
                context,full,dropped=linear_context(history,row.telemetry.context_budget_limit)
                require((row.context,row.history_full_bytes,row.history_dropped_turns)==(context,full,dropped),'baseline truncation differs')
            else:
                require(row.observation_calls>0 if event.observation else row.observation_calls==0,'input extraction usage differs')
                require(row.telemetry.extraction_calls==0 if row.reply_failure else row.telemetry.extraction_calls>0,'response extraction policy differs')
                cp=checkpoints[event.step-1];payload=json.loads(cp[6]);message=json.loads(cp[5])
                require(tuple(cp[:5])==(run_id,event.step,event.step,*role_at(event.step)),'SQLite checkpoint identity differs')
                require(message['content']==(REJECTED_REPLY_CONTENT if row.reply_failure else row.answer) and payload['task']==row.task,'SQLite answer/input differs')
                require(payload['reply_failure']==(None if row.reply_failure is None else row.reply_failure.model_dump(mode='json')),'SQLite reply failure differs')
                require(payload['extraction_failure']==(None if row.response_failure is None else row.response_failure.model_dump(mode='json')),'SQLite extraction failure differs')
                require(payload['agent_attempts']==[a.model_dump(mode='json') for a in row.agent_attempts] and
                    payload['response_attempts']==[a.model_dump(mode='json') for a in row.response_attempts],'SQLite inference attempts differ')
                if row.reply_failure:
                    require(not payload['fact_ids'] and not payload['extraction_attempts'] and not payload['tools'],
                        'rejected reply wrote response facts or ran extraction/tools')
                    require(len(payload['agent_generations'])==len(row.reply_failure.attempts),'SQLite failed generation count differs')
                    for generation,attempt in zip(payload['agent_generations'],row.reply_failure.attempts):
                        require(all(generation.get(k)==v for k,v in attempt.model_dump().items() if k!='response'),
                            'SQLite rejected generation metrics differ')
                require(payload['context']['text']==row.context and payload['context']['triple_ids']==list(row.context_triple_ids),'SQLite recalled context differs')
                require(payload['telemetry']==row.telemetry.model_dump() and payload['governance_time']==row.logical_time,'SQLite timing/telemetry differs')
                require(payload['pruned_ids']==list(row.pruned_entity_ids),'SQLite pruning event differs')
                require(payload['extracted_facts']==[f.model_dump() for f in row.response_facts],'SQLite extraction differs')
            if condition in ('A','B','C'):
                require(not row.pruned_entity_ids and not any(c.fact.status=='PRUNED' for c in row.changes),'disabled pruning changed state')
            if condition=='B':require(not any(c.fact.status=='SUPERSEDED' for c in row.changes),'disabled supersession changed state')
            if condition in ('C','D'):require(row.memory_before.single_value_conflicts==row.memory_after.single_value_conflicts==0,'single-valued invariant violated')
            require({k:str(v) if v is not None else '' for k,v in row.telemetry.model_dump().items()}==telemetry_rows[event.step-1],'telemetry CSV differs')
            flat.append(project(row,event));history.append(dict(task=event.task,answer=row.answer))
        if condition!='A':
            # Read existing SQLite directly; never initialize/migrate saved evidence.
            with sqlite3.connect((path/f'{condition}-{variant}.db').as_uri()+'?mode=ro&immutable=1',uri=True) as c:
                c.row_factory=sqlite3.Row
                raw=c.execute('''SELECT t.id,s.name AS subject,t.predicate,o.name AS object,t.status,
                    (s.status='ACTIVE') AS subject_active,(o.status='ACTIVE') AS object_active
                    FROM triples t JOIN entities s ON s.id=t.subject_id JOIN entities o ON o.id=t.object_id ORDER BY t.id''').fetchall()
            final={r['id']:FactState(**(dict(r)|{'subject_active':bool(r['subject_active']),'object_active':bool(r['object_active'])})) for r in raw}
            require(final==catalog,'final graph differs from event history')
    require(m['runs']==expected_runs,'manifest run completeness differs')
    with (path/'metrics.csv').open(newline='') as stream:
        reader=csv.DictReader(stream)
        require(tuple(reader.fieldnames or ())==tuple(flat[0]),'metric CSV schema differs')
        require(list(reader)==[{k:'' if v is None else str(v) for k,v in r.items()} for r in flat],'metric CSV differs from turn evidence')
    return m,plan,tuple(rows),tuple(flat)


def complete_total(values):
    return None if any(v is None for v in values) else sum(values)


def mean(values):
    return None if not values or any(v is None for v in values) else statistics.mean(values)


def accuracy(rows):
    return None if not rows else sum(r['correct'] for r in rows)/len(rows)


def describe(rows):
    by_category={c:dict(n=len(items),accuracy=accuracy(items)) for c in sorted({r['category'] for r in rows})
        if (items:=[r for r in rows if r['category']==c])}
    lag_groups={label:[r for r in rows if r['lag'] is not None and low<=r['lag']<=high]
        for label,low,high in [('0',0,0),('1-9',1,9),('10-29',10,29),('30+',30,1000)]}
    return dict(turns=len(rows),correct=sum(r['correct'] for r in rows),accuracy=accuracy(rows),
        format_valid=sum(r['format_valid'] for r in rows),invalid_reply_turns=sum(r['reply_failed'] for r in rows),
        invalid_reply_rate=sum(r['reply_failed'] for r in rows)/len(rows),
        observation_extraction_failures=sum(r['observation_failed'] for r in rows),
        response_extraction_failures=sum(r['response_extraction_failed'] for r in rows),
        prediction_aborts=sum(r['prediction_aborts'] for r in rows),
        role_prompt_tokens=complete_total([r['role_prompt_tokens'] for r in rows]),
        total_prompt_tokens=complete_total([r['total_prompt_tokens'] for r in rows]),
        total_generated_tokens=complete_total([r['total_generated_tokens'] for r in rows]),
        role_counter_coverage=sum(r['role_prompt_tokens'] is not None for r in rows),
        total_counter_coverage=sum(r['total_prompt_tokens'] is not None for r in rows),
        mean_wall_ms=mean([r['wall_ms'] for r in rows]),
        history_truncated_turns=sum(r['history_dropped_turns']>0 for r in rows),
        max_memory_bytes=max(r['memory_bytes'] for r in rows),
        max_unwindowed_history_bytes=max(r['history_full_bytes'] for r in rows),pruned_triple_events=sum(r['pruned_triples'] for r in rows),
        superseded_triple_events=sum(r['superseded_triples'] for r in rows),by_category=by_category,
        by_lag={k:dict(n=len(v),accuracy=accuracy(v)) for k,v in lag_groups.items()},
        cross_node=dict(n=len(cross:=[r for r in rows if r['cross_node'] and r['lag']>0]),accuracy=accuracy(cross)),
        delayed=dict(n=len(delayed:=[r for r in rows if r['lag'] is not None and r['lag']>=10]),accuracy=accuracy(delayed)),
        graph_support_missing=sum(r['support_available'] is False for r in rows),
        missing_support_states={state:sum(state in r['missing_support_states'].split('|') for r in rows) for state in ('absent','superseded','pruned')},
        turns_with_single_value_conflicts=sum(r['single_value_conflicts']>0 for r in rows),
        max_single_value_conflicts=max(r['single_value_conflicts'] for r in rows),
        retrieval_support_missing=sum(r['support_available'] is True and r['support_supplied'] is False for r in rows),
        answer_errors_with_support=sum(r['total_support_supplied'] is True and not r['correct'] for r in rows),
        final_active_edges_by_variant={str(v):[r for r in rows if r['variant']==v][-1]['active_edges'] for v in sorted({r['variant'] for r in rows})},
        mean_final_active_edges=mean([[r for r in rows if r['variant']==v][-1]['active_edges'] for v in sorted({r['variant'] for r in rows})]))


def analyze_bundle(path):
    manifest,plan,rows,flat=load_bundle(path)
    conditions={c:describe([r for r in flat if r['condition']==c]) for c in CONDITIONS}
    contrasts=[]
    for v in range(1,plan.variants+1):
        stats={c:describe([r for r in flat if r['condition']==c and r['variant']==v]) for c in CONDITIONS}
        for left,right,purpose in [('A','D','complete framework'),('B','C','supersession'),('C','D','pruning')]:
            a,b=stats[left],stats[right]
            contrasts.append(dict(variant=v,left=left,right=right,purpose=purpose,
                accuracy_delta=b['accuracy']-a['accuracy'],
                invalid_reply_rate_delta=b['invalid_reply_rate']-a['invalid_reply_rate'],
                delayed_accuracy_delta=None if a['delayed']['accuracy'] is None else b['delayed']['accuracy']-a['delayed']['accuracy'],
                role_prompt_delta=None if a['role_prompt_tokens'] is None or b['role_prompt_tokens'] is None else b['role_prompt_tokens']-a['role_prompt_tokens'],
                pipeline_prompt_delta=None if a['total_prompt_tokens'] is None or b['total_prompt_tokens'] is None else b['total_prompt_tokens']-a['total_prompt_tokens'],
                mean_wall_ms_delta=b['mean_wall_ms']-a['mean_wall_ms']))
    curves=[]
    for c in CONDITIONS:
        for step in range(1,plan.turns+1):
            items=[r for r in flat if r['condition']==c and r['step']==step]
            curves.append(dict(condition=c,step=step,n=len(items),accuracy=accuracy(items),
                **{k:mean([r[k] for r in items]) for k in ['role_prompt_tokens','total_prompt_tokens','wall_ms','active_edges']}))
    checkpoints=[]
    for n in (20,40,60,80):
        if n<=plan.turns:
            for c in CONDITIONS:checkpoints.append(dict(condition=c,through_step=n,**describe([r for r in flat if r['condition']==c and r['step']<=n])))
    contrast_summary={}
    for purpose in ('complete framework','supersession','pruning'):
        selected=[c for c in contrasts if c['purpose']==purpose]
        contrast_summary[purpose]={}
        for key in ('accuracy_delta','invalid_reply_rate_delta','delayed_accuracy_delta','role_prompt_delta','pipeline_prompt_delta','mean_wall_ms_delta'):
            values=[c[key] for c in selected];known=all(v is not None for v in values)
            contrast_summary[purpose][key]=dict(n_variants=len(values),known_variants=sum(v is not None for v in values),
                mean=mean(values),sd=statistics.stdev(values) if known and len(values)>1 else None)
    return dict(version=4,source=manifest['source'],bundle_id=manifest['bundle_id'],plan=plan.model_dump(),conditions=conditions,
        contrasts=contrasts,contrast_summary=contrast_summary,curves=curves,checkpoints=checkpoints,
        interpretation='Descriptive workload-variant comparisons; simulated age and measured inference. '
            'Bounded history and graph prompts are both capped. No asymptotic proof, biological validation, '
            'replicated-storage claim, or universal preservation guarantee. Missing counters remain unknown. '
            'Scripted inputs reach routed roles; cross-node scores measure later consumers, not autonomous discovery.')


def write_analysis(bundle,output):
    report=analyze_bundle(bundle);output=Path(output);output.mkdir(parents=True,exist_ok=False)
    write_json(output/'summary.json',report)
    for name,data in [('contrasts.csv',report['contrasts']),('curves.csv',report['curves'])]:
        with (output/name).open('w',newline='') as stream:
            writer=csv.DictWriter(stream,fieldnames=tuple(data[0]));writer.writeheader();writer.writerows(data)
    return report


def generate_plots(bundle,output):
    report=analyze_bundle(bundle)
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    from matplotlib import rc_context
    output=Path(output);output.mkdir(parents=True,exist_ok=False);paths=[]
    title='SYNTHETIC TEST DATA — NOT EXPERIMENT RESULTS' if report['source']=='synthetic' else 'Measured local inference with simulated memory aging'
    with rc_context({'font.size':10,'axes.spines.top':False,'axes.spines.right':False,'svg.fonttype':'none','pdf.fonttype':42}):
        for key,label in [('role_prompt_tokens','Role prompt tokens'),('total_prompt_tokens','Full-pipeline prompt tokens'),
                          ('wall_ms','Turn wall latency (ms)'),('active_edges','Active graph triples'),
                          ('accuracy','Answer-contract accuracy'),('delayed_recall','Answer accuracy by recall distance')]:
            fig=Figure(figsize=(8,4.8),layout='constrained');FigureCanvasAgg(fig);axes=fig.subplots()
            for c,color in zip(CONDITIONS,['#a35f22','#6e62a6','#087f8c','#167a48']):
                if key=='delayed_recall':
                    points=report['conditions'][c]['by_lag'];x=list(points);y=[points[k]['accuracy'] for k in x]
                else:
                    points=[r for r in report['curves'] if r['condition']==c];x=[r['step'] for r in points];y=[r[key] for r in points]
                axes.plot(x,[float('nan') if v is None else v for v in y],label=f'{c}: {CONDITIONS[c]["name"]}',color=color,marker='o',markersize=2)
            axes.set(xlabel='Recall distance (turns)' if key=='delayed_recall' else 'Scenario turn',ylabel=label)
            if key in ('accuracy','delayed_recall'):axes.set_ylim(-.05,1.05)
            axes.grid(axis='y',alpha=.2);axes.legend(loc='best',fontsize=7)
            fig.suptitle(title+f'\n{report["plan"]["variants"]} workload variants; descriptive means; missing counters are gaps'
                '\nRejected replies count as incorrect; failed attempts are included in costs.',fontsize=9)
            for suffix in ('svg','pdf','png'):
                path=output/f'{key}.{suffix}';fig.savefig(path,dpi=200);paths.append(path)
    return tuple(paths)


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--input',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True);p.add_argument('--figures',action='store_true')
    args=p.parse_args(argv)
    try:
        if args.figures:print(f'FIGURES OK: {len(generate_plots(args.input,args.output))} files (SVG, PDF, PNG)')
        else:
            report=write_analysis(args.input,args.output)
            print(f'INTEGRATED ANALYSIS OK: source={report["source"]} variants={report["plan"]["variants"]} contrasts={len(report["contrasts"])}')
    except (OSError,ValueError,KeyError,TypeError,csv.Error,ImportError) as exc:
        print(f'ERROR: {exc}',file=sys.stderr);return 1
    return 0


if __name__=='__main__':raise SystemExit(main())
