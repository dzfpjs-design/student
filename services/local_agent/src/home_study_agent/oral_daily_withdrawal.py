"""Retire a withdrawn daily source without erasing work or its learning duty."""
import json
from datetime import datetime
from uuid import NAMESPACE_URL, uuid5

from .local_supply import digest
from .oral_supply_execution import require
from .repository import utc_now

SOURCE = 'oral_daily_check'
KEY = 'oral_daily_withdrawal_event_id'
VERSION = 'oral_daily_source_withdrawal_v1'


def record(c, child, candidate_id, curriculum_event_id):
    require(c.in_transaction)
    item = c.execute('''SELECT diagnostic_item_id FROM specialized_supply_candidate
        WHERE candidate_id=? AND child_id=?''', (candidate_id, child)).fetchone()
    if not item or not item[0]:
        return
    rows = c.execute('''SELECT pi.*,dp.child_id,dp.session_id,dp.version AS plan_version
        FROM plan_item pi JOIN daily_plan dp USING(plan_id) JOIN daily_session ds USING(session_id)
        JOIN agent_commitment ac ON ac.commitment_id=pi.source_ref AND ac.child_id=dp.child_id
        JOIN agent_objective ao ON ao.objective_id=ac.objective_id AND ao.child_id=ac.child_id
        WHERE dp.child_id=? AND ds.child_id=dp.child_id AND dp.status='active' AND ds.status='active' AND pi.source_type=?
          AND pi.status IN ('pending','active','paused') AND ac.status IN ('pending','due') AND ao.status IN ('active','paused')
          AND EXISTS (SELECT 1 FROM plan_constraint_decision d WHERE d.plan_id=dp.plan_id
            AND d.obligation_type=? AND d.source_ref=pi.source_ref AND d.decision='scheduled'
            AND json_extract(d.metadata_json,'$.plan_item_id')=pi.plan_item_id
            AND json_extract(d.metadata_json,'$.planning_evidence.oral_daily_binding.source.diagnostic_item_id')=?)''',
        (child, SOURCE, SOURCE, item[0])).fetchall()
    for row in rows:
        task = dict(c.execute('SELECT * FROM plan_item WHERE plan_item_id=?', (row['plan_item_id'],)).fetchone())
        now = utc_now()
        eid = str(uuid5(NAMESPACE_URL, VERSION+':'+curriculum_event_id+':'+row['plan_item_id']))
        gap_id = str(uuid5(NAMESPACE_URL, VERSION+':gap:'+eid))
        scheduled = c.execute('''SELECT * FROM plan_constraint_decision WHERE plan_id=? AND obligation_type=?
            AND source_ref=? AND decision='scheduled' AND json_extract(metadata_json,'$.plan_item_id')=?''',
            (row['plan_id'], SOURCE, row['source_ref'], row['plan_item_id'])).fetchall()
        require(len(scheduled) == 1)
        prior_gaps = [r[0] for r in c.execute('''SELECT decision_id FROM plan_constraint_decision
            WHERE plan_id=? AND obligation_type=? AND source_ref=? AND decision='deferred' ORDER BY rowid''',
            (row['plan_id'], SOURCE, row['source_ref']))]
        elapsed = int(row['active_seconds'] or 0)
        if row['active_segment_started_at']:
            elapsed += max(0, int((datetime.fromisoformat(now)-datetime.fromisoformat(row['active_segment_started_at'])).total_seconds()))
        c.execute('''UPDATE plan_item SET status='cancelled',active_seconds=?,active_segment_started_at=NULL,
            completion_note='题源已撤回，已做内容保留，原复查等待合格新题。' WHERE plan_item_id=?''', (elapsed, row['plan_item_id']))
        prompts = c.execute('''SELECT DISTINCT q.prompt_id,p.objective_id FROM agent_plan p JOIN diagnostic_prompt q
            ON q.prompt_id=json_extract(p.context_manifest_json,'$.goal_prompt_binding.prompt_id')
            AND q.child_id=p.child_id AND q.origin_session_id=p.session_id
            WHERE p.child_id=? AND p.session_id=?
            AND json_extract(p.context_manifest_json,'$.goal_prompt_binding.plan_item_id')=?''',
            (child, row['session_id'], row['plan_item_id'])).fetchall()
        for prompt in prompts:
            c.execute('''UPDATE diagnostic_prompt SET status='invalidated',reason='source_withdrawn',updated_at=?
                WHERE prompt_id=? AND child_id=?''', (now, prompt['prompt_id'], child))
            c.execute("UPDATE agent_objective SET status='paused',updated_at=? WHERE objective_id=? AND child_id=? AND status='active'",
                (now, prompt['objective_id'], child))
            pending = c.execute('''SELECT plan_id,agent_turn_id FROM agent_plan WHERE objective_id=? AND child_id=?
                AND session_id=? AND status IN ('planned','running','waiting_child') AND EXISTS (
                  SELECT 1 FROM agent_step s WHERE s.plan_id=agent_plan.plan_id
                  AND s.status IN ('planned','running','waiting_child','unknown'))''',
                (prompt['objective_id'], child, row['session_id'])).fetchall()
            for plan in pending:
                c.execute("UPDATE agent_step SET status='cancelled',error_code='SOURCE_WITHDRAWN',updated_at=? WHERE plan_id=? AND status IN ('planned','running','waiting_child','unknown')", (now, plan['plan_id']))
                c.execute("UPDATE agent_plan SET status='cancelled',updated_at=? WHERE plan_id=?", (now, plan['plan_id']))
                c.execute("UPDATE agent_turn SET tool_status='failed',tool_error_code='SOURCE_WITHDRAWN',completed_at=? WHERE agent_turn_id=? AND tool_status IN ('pending','running')", (now, plan['agent_turn_id']))
        after = dict(c.execute('SELECT * FROM plan_item WHERE plan_item_id=?', (row['plan_item_id'],)).fetchone())
        # An authorized start can still be queued before it has a prompt or a
        # dedicated learning objective; cancel that exact plan, not other work.
        for plan in c.execute('''SELECT plan_id,agent_turn_id FROM agent_plan WHERE child_id=? AND session_id=?
            AND status IN ('planned','running','waiting_child')
            AND json_extract(context_manifest_json,'$.task_start_binding.plan_item_id')=?
            AND EXISTS (SELECT 1 FROM agent_step s WHERE s.plan_id=agent_plan.plan_id
                AND s.status IN ('planned','running','waiting_child','unknown'))''',
            (child,row['session_id'],row['plan_item_id'])).fetchall():
            c.execute("UPDATE agent_step SET status='cancelled',error_code='SOURCE_WITHDRAWN',updated_at=? WHERE plan_id=? AND status IN ('planned','running','waiting_child','unknown')", (now,plan['plan_id']))
            c.execute("UPDATE agent_plan SET status='cancelled',updated_at=? WHERE plan_id=?", (now,plan['plan_id']))
            c.execute("UPDATE agent_turn SET tool_status='failed',tool_error_code='SOURCE_WITHDRAWN',completed_at=? WHERE agent_turn_id=? AND tool_status IN ('pending','running')", (now,plan['agent_turn_id']))
        proof = dict(version=VERSION,child_id=child,session_id=row['session_id'],candidate_id=candidate_id,
            curriculum_event_id=curriculum_event_id,diagnostic_item_id=item[0],task_before=task,task_after=after,
            scheduled_decision_id=scheduled[0]['decision_id'],scheduled_version=digest(dict(scheduled[0])),
            replacement_gap_id=gap_id,superseded_gap_ids=prior_gaps,prompt_ids=[p['prompt_id'] for p in prompts])
        c.execute('INSERT INTO plan_item_event VALUES(?,?,?,?,?,?)',
            (eid, row['plan_item_id'], 'source_withdrawn', now, json.dumps(proof,ensure_ascii=False), VERSION+':'+eid))
        c.execute("INSERT INTO plan_constraint_decision VALUES(?,?,?,?,'deferred',?,?,0,?,?)",
            (gap_id,row['plan_id'],SOURCE,row['source_ref'],'原题源已撤回，保留复查责任，等待合格新题并重新核验预算。',
             row['expected_minutes'],json.dumps({KEY:eid}),now))
        c.execute("UPDATE daily_plan SET total_minutes=MAX(0,total_minutes-?),version=version+1 WHERE plan_id=?",
            (row['expected_minutes'],row['plan_id']))


def proof_for_gap(c, gap):
    eid = json.loads(gap['metadata_json']).get(KEY)
    recorded = c.execute('''SELECT e.event_id FROM plan_item_event e JOIN plan_item pi USING(plan_item_id)
        WHERE pi.plan_id=? AND pi.source_type=? AND pi.source_ref=? AND e.event_type='source_withdrawn'
        AND json_extract(e.payload_json,'$.replacement_gap_id')=?''',
        (gap['plan_id'],SOURCE,gap['source_ref'],gap['decision_id'])).fetchall()
    if not eid:
        require(not recorded)
        return None
    require(len(recorded)==1 and recorded[0][0]==eid)
    event = c.execute("SELECT * FROM plan_item_event WHERE event_id=? AND event_type='source_withdrawn'", (eid,)).fetchone()
    require(event)
    proof = json.loads(event['payload_json'])
    require(proof['version']==VERSION and proof['replacement_gap_id']==gap['decision_id'])
    after = proof['task_after']
    task = c.execute('SELECT * FROM plan_item WHERE plan_item_id=?', (event['plan_item_id'],)).fetchone()
    owner = c.execute('SELECT child_id,session_id FROM daily_plan WHERE plan_id=?', (gap['plan_id'],)).fetchone()
    require(task and dict(task)==after and after['status']=='cancelled' and after['source_type']==SOURCE
        and after['plan_id']==gap['plan_id'] and after['source_ref']==gap['source_ref']
        and gap['obligation_type']==SOURCE and gap['decision']=='deferred'
        and gap['requested_minutes']==after['expected_minutes'] and gap['allocated_minutes']==0
        and owner and tuple(owner)==(proof['child_id'],proof['session_id']))
    source = c.execute('''SELECT e.*,s.diagnostic_item_id FROM curriculum_supply_event e
        JOIN specialized_supply_candidate s USING(candidate_id) JOIN diagnostic_item i USING(diagnostic_item_id)
        WHERE e.event_id=? AND e.child_id=? AND e.candidate_id=? AND i.status='retired' ''',
        (proof['curriculum_event_id'],proof['child_id'],proof['candidate_id'])).fetchone()
    require(source and source['kind']=='withdraw' and source['diagnostic_item_id']==proof['diagnostic_item_id']
        and json.loads(source['result_json'])['state']=='withdrawn')
    scheduled = c.execute('SELECT * FROM plan_constraint_decision WHERE decision_id=?', (proof['scheduled_decision_id'],)).fetchone()
    require(scheduled and digest(dict(scheduled))==proof['scheduled_version'])
    prior = [r[0] for r in c.execute('''SELECT decision_id FROM plan_constraint_decision WHERE plan_id=?
        AND obligation_type=? AND source_ref=? AND decision='deferred' AND rowid <
        (SELECT rowid FROM plan_constraint_decision WHERE decision_id=?) ORDER BY rowid''',
        (gap['plan_id'],SOURCE,gap['source_ref'],gap['decision_id']))]
    require(prior==proof['superseded_gap_ids'])
    return proof


def active_gaps(c, rows):
    superseded = set()
    for row in rows:
        try:
            proof = proof_for_gap(c, row)
            if proof:
                superseded.update(proof['superseded_gap_ids'])
        except (ValueError,KeyError,TypeError):
            pass  # Keep the invalid gap visible; it cannot authorize a candidate.
    return [r for r in rows if r['decision_id'] not in superseded]


def withdrawn_task(c, task):
    if task['status']!='cancelled':
        return False
    for gap in c.execute('''SELECT * FROM plan_constraint_decision WHERE plan_id=? AND obligation_type=?
        AND source_ref=? AND decision='deferred' ''', (task['plan_id'],SOURCE,task['source_ref'])):
        try:
            proof = proof_for_gap(c, gap)
            if proof and proof['task_after']['plan_item_id']==task['plan_item_id']:
                return True
        except (ValueError,KeyError,TypeError):
            continue
    return False
