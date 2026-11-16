"""Versioned confirmations over original opportunities, not a new fact store."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from uuid import uuid4

from .outcome_evidence import _digest, coverage_projection, parse_time, task_context_snapshot, qualified_observation


VERSION = 'goal_behavior_v1'
CRITERIA = {
    'blocked_candidate': ('marked_uncertain_step', 'stated_specific_blocker', 'requested_help_after_identification'),
    'distraction_candidate': ('single_task_segment_completed',),
    'frequent_switching': ('single_task_segment_completed', 'switched_after_segment'),
    'fatigue_candidate': ('child_initiated_short_break', 'returned_original_task'),
}
POLICY = dict(minimum_opportunities=5, minimum_confirmed=4, minimum_days=3, minimum_coverage=.75,
              minimum_observed_seconds=600, minimum_span_days=4, segment_seconds=300,
              improvement_margin=.2, minimum_success_lower_bound=.75, minimum_sensor_confidence=.6,
              confidence_kind='engineering_qualification_not_diagnostic_probability', timezone='Asia/Shanghai')
POLICY['opportunity_unit'] = 'child/session/task/context/local_day; unobserved starts retained'
POLICY['short_break_max_seconds'] = 300
AUTO_LIMITATIONS = {
    'blocked_candidate': ['no_qualified_mark_or_specific_blocker_proof', 'help_receipt_alone_does_not_prove_ordered_composite'],
    'distraction_candidate': ['writing_segment_is_not_attention_or_unassisted_work'],
    'frequent_switching': ['task_sequence_is_not_unassisted_work'],
    'fatigue_candidate': ['c05_return_is_not_child_initiation', 'no_reliable_visual_fatigue_denominator'],
}


class GoalBehaviorConflict(ValueError):
    pass


@dataclass(frozen=True)
class BehaviorActorProvenance:
    """Construct only after host authentication, never from a request body."""
    actor_type: str
    actor_id: str
    child_id: str
    authorization_ref: str


def _table(c, name):
    return bool(c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def _goal(c, child, eid):
    row = c.execute('SELECT * FROM habit_experiment WHERE experiment_id=? AND child_id=?', (eid, child)).fetchone()
    if not row:
        raise GoalBehaviorConflict('BEHAVIOR_GOAL_NOT_OWNED')
    return dict(row)


def goal_version(goal):
    return _digest([VERSION, CRITERIA.get(goal['target_state']), POLICY,
        {k:goal[k] for k in ('experiment_id','child_id','target_state','title','child_action',
        'success_signal','duration_days','proposed_at','started_at','ends_at')}])


def _task_source(c, child, sid, context):
    if not _table(c, 'plan_item'):
        return None, None
    task = c.execute('''SELECT pi.*,dp.version AS plan_version FROM plan_item pi JOIN daily_plan dp USING(plan_id)
        JOIN daily_session ds ON ds.session_id=dp.session_id AND ds.child_id=dp.child_id
        WHERE pi.plan_item_id=? AND dp.child_id=? AND dp.session_id=?''', (context['plan_item_id'],child,sid)).fetchone()
    if not task or task['status'] in {'cancelled','rescheduled','cancelled_deferred'}:
        return None, None
    try:
        if task_context_snapshot(dict(task)) != context:
            return None, None
    except (ValueError, KeyError):
        return None, None
    source = None
    if task['homework_id']:
        homework = c.execute('SELECT * FROM homework_item WHERE homework_id=? AND child_id=? AND session_id=?',
                             (task['homework_id'],child,sid)).fetchone()
        if not homework or homework['confirmation_status'] != 'child_confirmed' or homework['status'] in {'cancelled','deleted'}:
            return None, None
        source = {k:homework[k] for k in homework.keys() if k not in {'status'}}
        if homework['source_artifact_id']:
            artifact = c.execute('SELECT * FROM capture_artifact WHERE artifact_id=? AND child_id=? AND session_id=?',
                                (homework['source_artifact_id'],child,sid)).fetchone()
            if not artifact or artifact['status'] not in {'completed','quality_warning'}:
                return None, None
            source['artifact'] = dict(artifact)
    elif _table(c, 'diagnostic_prompt'):
        from ..companion_activity_outcome import CompanionActivityOutcomeService
        prompts = CompanionActivityOutcomeService._task_prompts(c, {'child_id':child,'session_id':sid}, task)
        if prompts:
            source = sorted((p['prompt_id'],p['item_version']) for p in prompts)
    if source is None:
        return None, None
    identity = {k:task[k] for k in ('plan_item_id','plan_id','source_type','source_ref','homework_id',
                                   'subject','title','task_type','cognitive_load','plan_version')}
    return dict(task), _digest([identity, source])


def _automatic(c, child, item, task, coverage_rows):
    result = dict(criteria={k:None for k in item['criteria']}, initiated_by='unknown', assistance_level='unknown',
                  evidence_refs=[], evidence_versions=[], independent_response=False, return_episodes=[], basis=[], confidence=None)
    if task is None:
        return result
    start, end = (parse_time(item[k]) for k in ('window_start','window_end'))
    sid, task_id = item['session_id'], task['plan_item_id']
    events = c.execute('''SELECT e.* FROM plan_item_event e JOIN plan_item pi USING(plan_item_id)
        JOIN daily_plan dp USING(plan_id) WHERE dp.child_id=? AND dp.session_id=?
        AND julianday(e.occurred_at)>=julianday(?) AND julianday(e.occurred_at)<=julianday(?) ORDER BY e.rowid''',
        (child,sid,(start-timedelta(seconds=5)).isoformat(),end.isoformat())).fetchall()
    result['evidence_versions'].append(_digest([dict(e) for e in events]))
    starts = [e for e in events if e['plan_item_id']==task_id and e['event_type'] in {'started','resumed'} and parse_time(e['occurred_at']) <= start]
    exact_segment = bool(starts) and not any(e['event_type'] in {'paused','completed','rescheduled','cancelled','started','resumed'}
        and e['event_id'] != starts[-1]['event_id'] and parse_time(e['occurred_at']) < end for e in events)
    valid_writing = 0.0
    rows = {r['event_id']:r for r in coverage_rows}
    writing_times = set()
    for ref in item['source_refs']:
        row = rows.get(ref)
        if not row or row['event_type'] != 'observation.sample':
            continue
        sample = json.loads(row['payload_json'])
        origin = c.execute('''SELECT oe.* FROM observable_event oe LEFT JOIN behavior_retraction br ON br.event_id=oe.event_id
            WHERE oe.event_id=? AND oe.child_id=? AND oe.session_id=? AND oe.source='local_vision'
            AND br.event_id IS NULL''', ('observation:'+str(sample.get('state_event_id')),child,sid)).fetchone()
        if (origin and qualified_observation(origin) and origin['event_type']=='work.writing' and json.loads(origin['payload_json']).get('shadow') is False
                and sample['state']=='writing_like' and sample['confidence']>=.6 and not row['behavior_source_invalid']):
            writing_times.add(sample['captured_at'])
            result['evidence_refs'].append(origin['event_id'])
            result['evidence_versions'].append(_digest(dict(origin)))
    for left,right in item['intervals']:
        if left in writing_times and right in writing_times:
            valid_writing += (parse_time(right)-parse_time(left)).total_seconds()
    segment = exact_segment and (end-start).total_seconds() >= POLICY['segment_seconds'] and valid_writing >= POLICY['segment_seconds']*.75
    if segment and 'single_task_segment_completed' in result['criteria']:
        result['criteria']['single_task_segment_completed'] = True
        result['basis'].append('original_timer_and_qualified_writing_segment_not_attention_diagnosis')
        result['evidence_refs'].extend(e['event_id'] for e in starts)
        if 'switched_after_segment' in result['criteria']:
            switch = c.execute('''SELECT e.* FROM plan_item_event e JOIN plan_item pi USING(plan_item_id)
                JOIN daily_plan dp USING(plan_id) WHERE dp.child_id=? AND dp.session_id=? AND e.plan_item_id!=?
                AND e.event_type IN ('started','resumed') AND julianday(e.occurred_at)>=julianday(?)
                AND julianday(e.occurred_at)<=julianday(?) ORDER BY e.rowid LIMIT 1''',
                (child,sid,task_id,end.isoformat(),(end+timedelta(minutes=5)).isoformat())).fetchone()
            if switch:
                result['criteria']['switched_after_segment'] = True
                result['evidence_refs'].append(switch['event_id'])
                result['evidence_versions'].append(_digest(dict(switch)))
    # Reuse the same exact task/prompt and effective-evidence qualification as C05.
    if _table(c, 'diagnostic_prompt'):
        from ..companion_activity_outcome import CompanionActivityOutcomeService
        from ..learning_obligations import project_child_mastery_evidence
        prompts = CompanionActivityOutcomeService._task_prompts(c, {'child_id':child,'session_id':sid},task)
        if prompts:
            effective = {e['evidence_id']:e for node in project_child_mastery_evidence(c,child).values() for e in node['effective_evidence']}
            for prompt in prompts:
                answer = c.execute('SELECT evidence_id FROM diagnostic_attempt WHERE attempt_id=? AND child_id=? AND session_id=?',
                                   (prompt['attempt_id'],child,sid)).fetchone()
                e = effective.get(answer[0]) if answer else None
                if e and start <= parse_time(e['occurred_at']) <= end and (e['hint_level']>0 or e['independent']==0):
                    result['assistance_level']='prompted'
                    result['evidence_refs'].append(e['evidence_id'])
                    result['evidence_versions'].append(_digest(e))
                if e and e['independent']==1 and e['hint_level']==0 and start <= parse_time(e['occurred_at']) <= end:
                    result['independent_response'] = True
                    result['basis'].append('bound_independent_response_not_unobserved_help_absence')
                    result['evidence_refs'].append(e['evidence_id'])
                    result['evidence_versions'].append(_digest(e))
    if 'returned_original_task' in result['criteria'] and _table(c,'companion_activity_outcome'):
        from ..companion_activity_outcome import project_companion_activity_evidence
        from zoneinfo import ZoneInfo
        local_start = start.astimezone(ZoneInfo(POLICY['timezone'])).replace(hour=0,minute=0,second=0,microsecond=0)
        for activity in project_companion_activity_evidence(c,child,(local_start-timedelta(days=1)).isoformat())['activities']:
            proof = activity['return_verification']
            episodes = [episode for episode in proof.get('episodes',[]) if
                local_start <= parse_time(episode['resumed_at']) <= end and any(
                    start <= parse_time(e['occurred_at']) <= end for e in episode.get('evidence',[]))]
            if activity['session_id']==sid and activity['source_valid'] and proof['plan_item_id']==task_id and proof['status']=='verified' and episodes:
                result['criteria']['returned_original_task'] = True
                result['evidence_refs'].extend(proof['evidence_refs'])
                result['basis'].append('current_c05_original_task_return')
                result['evidence_versions'].append(_digest(episodes))
                snapshot = json.loads(c.execute('SELECT source_snapshot_json FROM companion_activity_outcome WHERE outcome_id=? AND child_id=?',
                                                (activity['outcome_id'],child)).fetchone()[0])
                for episode in episodes:
                    duration=(parse_time(episode['resumed_at'])-parse_time(snapshot['started_at'])).total_seconds()
                    result['return_episodes'].append(dict(outcome_id=activity['outcome_id'],
                        resume_receipt_id=episode['resume_receipt_id'],resumed_at=episode['resumed_at'],
                        source_version=activity['current_source_fingerprint'],
                        short_interval=0<=duration<=POLICY['short_break_max_seconds']))
                if any(e.get('independent') and start <= parse_time(e['occurred_at']) <= end
                       for episode in episodes for e in episode.get('evidence',[])):
                    result['independent_response']=True
        # C05 intentionally has no qualified initial-child-trigger proof.
        if len(result['return_episodes'])==1 and not result['return_episodes'][0]['short_interval']:
            result['criteria']['child_initiated_short_break']=False
    result['evidence_refs'] = sorted(set(result['evidence_refs']))
    if result['basis']:
        result['confidence'] = 'qualified_direct_records_not_psychological_inference'
    return result


def opportunities(c, child, eid, now):
    return _opportunities(c, child, eid, now)


def _opportunities(c, child, eid, now, *, coverage_observer=None):
    from .service import BehaviorModelService
    goal = _goal(c,child,eid)
    definition = CRITERIA.get(goal['target_state'])
    result = dict(status='ready' if definition else 'unsupported', experiment_id=eid,child_id=child,
                  goal_version=goal_version(goal),criteria=list(definition or ()),opportunities=[],policy=POLICY,
                  automatic_limitations=AUTO_LIMITATIONS.get(goal['target_state'],['unsupported_goal']),
                  coverage={},local_only=True,cloud_transmission_allowed=False)
    if not definition or not goal['started_at']:
        return result
    started = parse_time(goal['started_at'])
    end = min(now,parse_time(goal['ends_at'])) if goal['ends_at'] else now
    if goal['status']=='stopped' and goal['closed_at']:
        end = min(end,parse_time(goal['closed_at']))
    records = BehaviorModelService._coverage_records(c,child)
    history = c.execute('''SELECT * FROM habit_experiment_event WHERE experiment_id=? AND child_id=?
        AND event_type IN ('goal_behavior_confirmed','goal_behavior_retracted') ORDER BY rowid''', (eid,child)).fetchall()
    latest = {}
    for row in history:
        payload = json.loads(row['payload_json'])
        latest[payload['opportunity_id']] = dict(row)
    for phase,left,right in (('baseline',started-timedelta(days=14),started),('current',started,end)):
        projection = coverage_projection(records,child,left,right,include_opportunities=True)
        if coverage_observer is not None:
            coverage_observer(projection)
        result['coverage'][phase] = projection['coverage_fraction']
        raw_opportunities = list(projection['opportunities'])
        # Keep failed/unobserved task starts in the denominator, not just successful camera intervals.
        if _table(c, 'plan_item_event'):
            from zoneinfo import ZoneInfo
            candidates = c.execute('''SELECT pi.*,dp.session_id,dp.version AS plan_version,e.occurred_at,e.event_id
                FROM plan_item_event e JOIN plan_item pi USING(plan_item_id) JOIN daily_plan dp USING(plan_id)
                WHERE dp.child_id=? AND e.event_type IN ('started','resumed')
                AND julianday(e.occurred_at)>=julianday(?) AND julianday(e.occurred_at)<julianday(?) ORDER BY e.rowid''',
                (child,left.isoformat(),right.isoformat())).fetchall()
            represented = {(r['session_id'],r['context']['plan_item_id'],r['day']) for r in raw_opportunities}
            for candidate in candidates:
                stamp = parse_time(candidate['occurred_at'])
                day = stamp.astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat()
                key = candidate['session_id'],candidate['plan_item_id'],day
                if key in represented:
                    continue
                context = task_context_snapshot(dict(candidate))
                compatible = {r['stratum_key'] for r in raw_opportunities if all(
                    r['context'][k]==context[k] for k in ('subject','task_type','cognitive_load','source_type'))}
                bucket = next(iter(compatible)) if len(compatible)==1 else _digest(['unobserved_context',context['stratum_key']])
                raw_opportunities.append(dict(source_key=_digest(key),session_id=key[0],day=day,context=context,
                    stratum_key=bucket,observed_seconds=0,behavior_seconds=0,intervals=[[stamp.isoformat(),stamp.isoformat()]],
                    source_refs=[candidate['event_id']],source_version=_digest(dict(candidate)),unobserved=True))
                represented.add(key)
        for raw in raw_opportunities:
            oid = _digest([eid,phase,raw['source_key']])
            task, source_version = _task_source(c,child,raw['session_id'],raw['context'])
            source = dict(raw,task_source_version=source_version,goal_version=result['goal_version'],phase=phase)
            item = {**raw,'opportunity_id':oid,'phase':phase,'criteria':list(definition),
                    'window_start':min(p[0] for p in raw['intervals']),'window_end':max(p[1] for p in raw['intervals']),
                    'source_current':source_version is not None,'task_source_version':source_version}
            automatic = _automatic(c,child,item,task,records)
            item['automatic'] = automatic
            source['automatic'] = automatic
            item['source_version'] = _digest(source)
            old = latest.get(oid)
            item['confirmation'] = None
            if old and old['event_type']=='goal_behavior_confirmed':
                payload = json.loads(old['payload_json'])
                if payload['source_version']==item['source_version'] and source_version is not None:
                    item['confirmation'] = dict(event_id=old['event_id'],occurred_at=old['occurred_at'],**payload)
            item['binding_version'] = _digest([item['source_version'],old['event_id'] if old else None])
            result['opportunities'].append(item)
    represented_ids = {item['opportunity_id'] for item in result['opportunities']}
    for oid,row in latest.items():
        if oid in represented_ids:
            continue
        payload = json.loads(row['payload_json'])
        frozen = payload.get('opportunity_snapshot')
        if not frozen:
            continue
        # A vanished source remains an unknown historical opportunity, never a positive witness.
        item = dict(frozen, opportunity_id=oid, source_current=False, confirmation=None,
                    observed_seconds=0,behavior_seconds=0,source_refs=[],task_source_version=None)
        item['source_version'] = _digest(['source_missing',payload['source_version'],result['goal_version']])
        item['binding_version'] = _digest([item['source_version'],row['event_id']])
        item['automatic'] = _automatic(c,child,item,None,[])
        result['opportunities'].append(item)
    return result


def confirm(c, child, eid, *, opportunity_id, expected_version, opportunity_present, criteria, initiated_by,
            assistance_level, actor_provenance, client_action_id, operation='confirm', reason='', at):
    if (not isinstance(actor_provenance,BehaviorActorProvenance) or actor_provenance.child_id!=child
            or actor_provenance.actor_type not in {'parent','child'} or not actor_provenance.actor_id or not actor_provenance.authorization_ref):
        raise GoalBehaviorConflict('BEHAVIOR_ACTOR_NOT_AUTHORIZED')
    if (operation not in {'confirm','retract'} or type(opportunity_present) not in {bool,type(None)}
            or not isinstance(criteria,dict) or any(type(v) not in {bool,type(None)} for v in criteria.values())
            or initiated_by not in {'child','parent','agent','unknown'} or assistance_level not in {'none','prompted','assisted','unknown'}
            or not isinstance(reason,str) or len(reason)>500 or not isinstance(client_action_id,str) or not client_action_id):
        raise GoalBehaviorConflict('BEHAVIOR_CONFIRMATION_INVALID')
    request = dict(opportunity_id=opportunity_id,expected_version=expected_version,opportunity_present=opportunity_present,
        criteria=criteria,initiated_by=initiated_by,assistance_level=assistance_level,operation=operation,reason=reason,
        actor_type=actor_provenance.actor_type,actor_id=actor_provenance.actor_id)
    old = c.execute('SELECT * FROM habit_experiment_event WHERE client_action_id=?',(client_action_id,)).fetchone()
    if old:
        payload = json.loads(old['payload_json'])
        if old['child_id']!=child or old['experiment_id']!=eid or payload.get('request')!=request:
            raise GoalBehaviorConflict('BEHAVIOR_ACTION_CONFLICT')
        return dict(status='recorded',event_id=old['event_id'],replayed=True,historical_receipt=True)
    view = opportunities(c,child,eid,parse_time(at))
    if view['status']=='unsupported':
        raise GoalBehaviorConflict('BEHAVIOR_GOAL_UNSUPPORTED')
    item = next((item for item in view['opportunities'] if item['opportunity_id']==opportunity_id),None)
    if not item or (operation=='confirm' and not item['source_current']) or item['binding_version']!=expected_version:
        raise GoalBehaviorConflict('BEHAVIOR_SOURCE_OR_VERSION_CHANGED')
    if set(criteria)!=set(item['criteria']) or parse_time(at)<parse_time(item['window_end']):
        raise GoalBehaviorConflict('BEHAVIOR_CRITERIA_OR_TIME_INVALID')
    event_id = str(uuid4())
    payload = dict(request=request,opportunity_id=opportunity_id,source_version=item['source_version'],goal_version=view['goal_version'],
        source_refs=item['source_refs'],task_source_version=item['task_source_version'],context=item['context'],
        opportunity_present=opportunity_present,criteria=criteria,initiated_by=initiated_by,assistance_level=assistance_level,
        actor_type=actor_provenance.actor_type,actor_id=actor_provenance.actor_id,authorization_ref=actor_provenance.authorization_ref)
    payload['opportunity_snapshot'] = {key:item[key] for key in (
        'phase','session_id','day','context','stratum_key','window_start','window_end','criteria','intervals')}
    c.execute('''INSERT INTO habit_experiment_event(event_id,experiment_id,child_id,event_type,payload_json,occurred_at,client_action_id)
        VALUES(?,?,?,?,?,?,?)''', (event_id,eid,child,'goal_behavior_confirmed' if operation=='confirm' else 'goal_behavior_retracted',
                                 json.dumps(payload,ensure_ascii=False,allow_nan=False),at,client_action_id))
    return dict(status='recorded',event_id=event_id,replayed=False,historical_receipt=False)


def assessment(c, child, eid, now):
    view = opportunities(c,child,eid,now)
    result = dict(version=VERSION,status='insufficient_evidence',direction='not_yet_clear',missing_evidence=[],
                  goal_version=view['goal_version'],habit_success=False,bounded_goal_success=False,policy=POLICY,baseline={},current={},
                  automatic_limitations=view['automatic_limitations'],
                  basis='bounded_goal_behavior_not_lifetime_habit',local_only=True,cloud_transmission_allowed=False)
    if view['status']=='unsupported':
        return {**result,'status':'unsupported','missing_evidence':['unsupported_goal']}
    grouped = {}
    for item in view['opportunities']:
        key = item['phase'],item['stratum_key']
        counts = grouped.setdefault(key,dict(opportunities=0,confirmed=0,successes=0,independent_successes=0,unknown=0,
                                             not_applicable=0,failures=0,independence_unknown=0,independent_responses=0,
                                             days=set(),success_days=set(),tasks=set(),seconds=0.0))
        counts['opportunities']+=1
        counts['days'].add(item['day'])
        counts['tasks'].add(item['context']['plan_item_id'])
        counts['seconds']+=item['observed_seconds']
        auto = item['automatic']
        witnessed = item['confirmation']
        eligible = item['source_current']
        values = dict(auto['criteria'])
        assisted, initiated = auto['assistance_level'],auto['initiated_by']
        counts['independent_responses'] += int(item['source_current'] and auto['independent_response'])
        if witnessed and witnessed['actor_type']=='parent':
            values = {k:(witnessed['criteria'][k] if witnessed['criteria'][k] is not None else v) for k,v in values.items()}
            assisted = (auto['assistance_level'] if auto['assistance_level'] in {'prompted','assisted'}
                        else witnessed['assistance_level'])
            initiated = witnessed['initiated_by']
            applicable = witnessed['opportunity_present']
            if 'returned_original_task' in values and values['returned_original_task'] is True:
                values['returned_original_task'] = auto['criteria']['returned_original_task']
        else:
            applicable = True if 'single_task_segment_completed' in values else None
        if 'child_initiated_short_break' in values and values['child_initiated_short_break'] is True:
            if len(auto['return_episodes'])!=1:
                values['child_initiated_short_break']=None
            elif not auto['return_episodes'][0]['short_interval']:
                values['child_initiated_short_break']=False
            else:
                values['child_initiated_short_break'] = True if initiated=='child' else None if initiated=='unknown' else False
        known = eligible and applicable is not None and (applicable is False or
            any(v is False for v in values.values()) or all(v is True for v in values.values()))
        if not known:
            counts['unknown']+=1
            continue
        counts['confirmed']+=1
        if applicable is False:
            counts['not_applicable']+=1
            continue
        success = all(v is True for v in values.values())
        counts['successes']+=int(success)
        counts['failures']+=int(not success)
        independent = success and assisted=='none'
        counts['independent_successes']+=int(independent)
        counts['independence_unknown']+=int(success and assisted=='unknown')
        if success:
            counts['success_days'].add(item['day'])
    for phase in ('baseline','current'):
        strata = {}
        for (p,key),counts in grouped.items():
            if p!=phase:
                continue
            n=counts['opportunities']-counts['not_applicable']
            strata[key]={**counts,'days':sorted(counts['days']),'success_days':sorted(counts['success_days']),'tasks':len(counts['tasks']),
                'eligible_denominator':n,
                'behavior_lower_bound':counts['successes']/n if n else None,
                'success_lower_bound':counts['independent_successes']/n if n else None,
                'success_upper_bound':(counts['independent_successes']+counts['unknown']+counts['independence_unknown'])/n if n else None,
                'confirmation_fraction':counts['confirmed']/counts['opportunities']}
        result[phase] = dict(opportunities=sum(s['opportunities'] for s in strata.values()),
            independent_successes=sum(s['independent_successes'] for s in strata.values()),unknown=sum(s['unknown'] for s in strata.values()),strata=strata)
    before,after = result['baseline']['strata'],result['current']['strata']
    if not before or before.keys()!=after.keys():
        result['missing_evidence'].append('comparable_task_coverage')
    if not grouped or any(s['tasks']<5 or len(s['days'])<3 or s['seconds']<600 for phase in ('baseline','current') for s in result[phase]['strata'].values()):
        result['missing_evidence'].append('representative_repeated_opportunities')
    if not grouped or any(s['confirmed']-s['not_applicable']<POLICY['minimum_confirmed'] or s['confirmation_fraction']<POLICY['minimum_coverage']
                          for phase in ('baseline','current') for s in result[phase]['strata'].values()):
        result['missing_evidence'].append('goal_behavior_confirmation')
    sustained = bool(after) and all(len(s['success_days'])>=3 and
        (datetime.fromisoformat(s['success_days'][-1])-datetime.fromisoformat(s['success_days'][0])).days>=4 for s in after.values())
    if not sustained:
        result['missing_evidence'].append('sustained_goal_behavior')
    if any((view['coverage'].get(phase) or 0)<POLICY['minimum_coverage'] for phase in ('baseline','current')):
        result['missing_evidence'].append('adequate_observation_coverage')
    if not after or any((s['success_lower_bound'] or 0)<POLICY['minimum_success_lower_bound'] for s in after.values()):
        result['missing_evidence'].append('independent_goal_behavior')
    # A representative automatically observed behavior is useful even when initiative/help are unknown.
    if sustained and (view['coverage'].get('current') or 0)>=POLICY['minimum_coverage'] and all(
        s['tasks']>=POLICY['minimum_opportunities'] and s['seconds']>=POLICY['minimum_observed_seconds']
        and s['confirmed']-s['not_applicable']>=POLICY['minimum_confirmed']
        and (s['behavior_lower_bound'] or 0)>=POLICY['minimum_success_lower_bound'] for s in after.values()):
        result['status']='repeated_observed_behavior'
        if all((s['success_lower_bound'] or 0)>=POLICY['minimum_success_lower_bound'] for s in after.values()):
            result['status']='bounded_confirmed_behavior'
            result['bounded_goal_success']=True
    if not result['missing_evidence']:
        if all(after[k]['success_lower_bound']>=before[k]['success_upper_bound']+POLICY['improvement_margin'] for k in before):
            result['direction']='improving'
    return result
