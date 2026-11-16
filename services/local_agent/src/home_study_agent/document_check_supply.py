"""Deliver one proven local question inside the original voice transaction."""
from datetime import datetime, timezone
import json
from uuid import uuid4

from .local_supply import LocalSupplyQueue, LocalSupplyError, build_candidate, validate_item, item_snapshot
from .specialized_supply import SpecializedDiagnosticSupplyService
from .learning_obligations import question_identity
from .mistake_book import MistakeBookService
from .diagnostic_history_identity import historical_identity, HistoryIdentityUnavailable


def deliver(c, child, seed_id, action_id):
    if not c.in_transaction:
        raise LocalSupplyError('SUPPLY_TRANSACTION_REQUIRED')
    seed=validate_item(c,child,seed_id)
    node=c.execute('SELECT * FROM knowledge_node WHERE knowledge_node_id=?',(seed['knowledge_node_id'],)).fetchone()
    _,seen=MistakeBookService._question_history(c,child)
    shown=c.execute('SELECT * FROM diagnostic_prompt WHERE child_id=?',(child,)).fetchall()
    original=question_identity(seed,connection=c)
    # Rejected/withdrawn candidates are not revived by an automatic retry.
    reserved={r[0] for r in c.execute('SELECT prompt FROM specialized_supply_candidate WHERE child_id=?',(child,))}
    for ordinal in range(36):
        try:
            candidate=build_candidate(seed,'parallel',ordinal)
        except LocalSupplyError:
            return None
        if candidate['validation'].get('auto_qualification_allowed') is False:
            return None
        identity=question_identity({'prompt':candidate['prompt']},connection=c)
        if candidate['prompt'] in reserved or identity==original or identity in seen:
            continue
        try:
            if any(historical_identity(c,child,previous,identity)==identity for previous in shown):
                continue
        except HistoryIdentityUnavailable:
            return None
        job_id,now=str(uuid4()),datetime.now(timezone.utc).isoformat()
        domain='document-check:'+node['knowledge_node_id']
        c.execute('''INSERT INTO specialized_supply_job(job_id,child_id,track,subject,domain_id,
            seed_item_id,stages_json,status,generation_mode,attempt_count,provider,model,
            created_at,updated_at,client_action_id,source_snapshot_json)
            VALUES(?,?,?,?,?,?,'["parallel"]','completed','local',1,'local','deterministic_v1',?,?,?,?)''',
            (job_id,child,node['track'],node['subject'],domain,seed_id,now,now,action_id,
             json.dumps(item_snapshot(seed),ensure_ascii=False)))
        service=SpecializedDiagnosticSupplyService(None)
        job=c.execute('SELECT * FROM specialized_supply_job WHERE job_id=?',(job_id,)).fetchone()
        service._store(c,job,None,candidate)
        row=c.execute('SELECT candidate_id FROM specialized_supply_candidate WHERE job_id=?',(job_id,)).fetchone()
        if row is None:
            raise LocalSupplyError('SUPPLY_CANDIDATE_CONFLICT')
        result=LocalSupplyQueue(service).qualify_local(child,row[0],action_id,connection=c)
        return result['diagnostic_item_id']
    return None
