"""Purpose-specific durable ownership and conservative budget accounting.

No model result or cost reservation is a learning assessment. A transmitted or
unknown request retains its full reserved ceiling; reads never trigger retries.
"""
import asyncio
import json
from datetime import timedelta
from uuid import uuid4

from .family_clock import FAMILY_ZONE
from .local_supply import digest
from .pet_audio_gateway import PetAudioProtocolError, decode_feedback
from .pet_audio_source import PetAudioAuthorityError, consent, now, require, source

_PROCESS_EPOCH = str(uuid4())
PURPOSE = 'pet_original_audio_formative_v1'
DELIVERY_BACKOFF_SECONDS = 5


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def ensure_schema(c):
    c.execute('''CREATE TABLE IF NOT EXISTS pet_audio_epoch(singleton INTEGER PRIMARY KEY CHECK(singleton=1),epoch TEXT NOT NULL)''')
    c.execute('INSERT OR IGNORE INTO pet_audio_epoch VALUES(1,?)', (str(uuid4()),))
    c.execute('CREATE TABLE IF NOT EXISTS pet_audio_config(singleton INTEGER PRIMARY KEY CHECK(singleton=1),endpoint_hash TEXT NOT NULL)')
    c.execute('''CREATE TABLE IF NOT EXISTS pet_audio_grant(
        grant_id TEXT PRIMARY KEY,child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
        revision INTEGER NOT NULL,allowed INTEGER NOT NULL,expires_at TEXT NOT NULL,day_limit_micros INTEGER NOT NULL,
        request_limit_micros INTEGER NOT NULL,request_json TEXT NOT NULL,endpoint_hash TEXT NOT NULL,
        epoch TEXT NOT NULL,consent_hash TEXT NOT NULL,created_at TEXT NOT NULL,action_id TEXT NOT NULL,
        UNIQUE(child_id,revision),UNIQUE(child_id,action_id))''')
    c.execute('''CREATE TABLE IF NOT EXISTS pet_audio_work(
        work_id TEXT PRIMARY KEY,child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
        session_id TEXT NOT NULL REFERENCES daily_session(session_id) ON DELETE CASCADE,
        assessment_id TEXT NOT NULL REFERENCES rubric_assessment(assessment_id) ON DELETE CASCADE,
        media_id TEXT NOT NULL REFERENCES pet_media_binding(media_id) ON DELETE CASCADE,
        source_hash TEXT NOT NULL,binding_json TEXT NOT NULL,original_key TEXT NOT NULL,live_hash TEXT,state TEXT NOT NULL,
        generation INTEGER NOT NULL,owner TEXT,runtime_epoch TEXT,lease_until TEXT,
        grant_id TEXT,endpoint_hash TEXT,epoch TEXT NOT NULL,result_json TEXT,error_code TEXT,
        created_at TEXT NOT NULL,updated_at TEXT NOT NULL,UNIQUE(child_id,assessment_id),UNIQUE(child_id,original_key))''')
    c.execute('''CREATE TABLE IF NOT EXISTS pet_audio_dispatch(
        dispatch_id TEXT PRIMARY KEY,work_id TEXT REFERENCES pet_audio_work(work_id) ON DELETE SET NULL,
        child_id TEXT REFERENCES child_profile(child_id) ON DELETE SET NULL,generation INTEGER NOT NULL,
        day TEXT NOT NULL,cost_micros INTEGER NOT NULL CHECK(cost_micros>0),state TEXT NOT NULL,
        created_at TEXT NOT NULL,UNIQUE(work_id,generation))''')
    c.execute('''CREATE TABLE IF NOT EXISTS pet_audio_retry(
        action_id TEXT NOT NULL,child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
        work_id TEXT NOT NULL REFERENCES pet_audio_work(work_id) ON DELETE CASCADE,
        request_json TEXT NOT NULL,PRIMARY KEY(child_id,action_id))''')
    c.execute('CREATE INDEX IF NOT EXISTS pet_audio_dispatch_day ON pet_audio_dispatch(day,child_id,state)')
    c.execute('''CREATE TRIGGER IF NOT EXISTS pet_audio_work_erase_budget_owner BEFORE DELETE ON pet_audio_work
        BEGIN UPDATE pet_audio_dispatch SET child_id=NULL,work_id=NULL,
            state=CASE state WHEN 'reserved' THEN 'released' WHEN 'sent' THEN 'unknown'
                            WHEN 'body_sent' THEN 'unknown' ELSE state END
            WHERE work_id=OLD.work_id; END''')
    for name in ('pet_audio_grant', 'pet_audio_retry'):
        c.execute(f'''CREATE TRIGGER IF NOT EXISTS {name}_immutable BEFORE UPDATE ON {name}
            BEGIN SELECT RAISE(ABORT,'PET audio audit is immutable'); END''')


def quarantine_after_restore(c):
    if not c.execute("SELECT 1 FROM sqlite_master WHERE name='pet_audio_epoch'").fetchone():
        return
    c.execute('UPDATE pet_audio_epoch SET epoch=? WHERE singleton=1', (str(uuid4()),))
    c.execute("UPDATE pet_audio_work SET state='invalidated',owner=NULL,error_code='RESTORED_AUTHORITY_REVOKED'")
    c.execute("UPDATE pet_audio_dispatch SET state='unknown' WHERE state IN ('reserved','sent','body_sent')")


class PetAudioTeachingService:
    def __init__(self, database, *, media_root, gateway=None, data_encryption_key=None, runtime_epoch=None,
                 runtime_receipt_reader=None):
        self.database, self.media_root, self.gateway = database, media_root, gateway
        self.data_encryption_key = data_encryption_key
        self.runtime_epoch = runtime_epoch or _PROCESS_EPOCH
        self.runtime_receipt_reader = runtime_receipt_reader
        with database.connect() as c:
            ensure_schema(c)
            c.execute('INSERT INTO pet_audio_config VALUES(1,?) ON CONFLICT(singleton) DO UPDATE SET endpoint_hash=excluded.endpoint_hash WHERE endpoint_hash!=excluded.endpoint_hash',
                (self._endpoint() if gateway else '',))
            c.execute("UPDATE pet_audio_work SET state='unknown',owner=NULL,error_code='PROCESS_RESTARTED' WHERE state='running' AND runtime_epoch!=?", (self.runtime_epoch,))
            c.commit()

    def _source(self, c, child_id, assessment_id, *, live):
        return source(c, child_id, assessment_id, media_root=self.media_root,
            data_encryption_key=self.data_encryption_key, live=live)

    def _endpoint(self):
        require(self.gateway is not None, 'GATEWAY_NOT_CONFIGURED')
        return digest(self.gateway.endpoint.identity())

    def require_new_use(self):
        require(self.gateway is not None, 'GATEWAY_NOT_CONFIGURED')
        self.gateway.endpoint.require_current_control()

    def grant(self, child_id, *, allowed, expires_at, day_limit_micros, request_limit_micros,
              expected_revision, action_id):
        from datetime import datetime
        require(type(allowed) is bool and type(expected_revision) is int and expected_revision >= 0
            and isinstance(action_id, str) and 1 <= len(action_id) <= 200, 'GRANT_INVALID')
        end = datetime.fromisoformat(expires_at[:-1] + '+00:00' if expires_at.endswith('Z') else expires_at)
        require(end.tzinfo is not None and now() < end <= now() + timedelta(days=31), 'GRANT_EXPIRY')
        require(type(day_limit_micros) is int and type(request_limit_micros) is int
            and 0 < request_limit_micros <= day_limit_micros <= 100_000_000, 'GRANT_LIMIT')
        request = encoded(dict(allowed=allowed,expires_at=expires_at,day_limit_micros=day_limit_micros,
            request_limit_micros=request_limit_micros,expected_revision=expected_revision))
        if allowed:
            self.require_new_use()
        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            require(c.execute("SELECT 1 FROM child_profile WHERE child_id=? AND status='active'", (child_id,)).fetchone(), 'CHILD_UNAVAILABLE')
            prior = c.execute('SELECT * FROM pet_audio_grant WHERE child_id=? AND action_id=?', (child_id, action_id)).fetchone()
            if prior:
                require(prior['request_json'] == request, 'GRANT_KEY_CONFLICT')
                return dict(prior)
            latest = c.execute('SELECT revision FROM pet_audio_grant WHERE child_id=? ORDER BY revision DESC LIMIT 1', (child_id,)).fetchone()
            require((latest[0] if latest else 0) == expected_revision, 'GRANT_REVISION_CHANGED')
            identity = self._endpoint() if allowed else ''
            privacy = consent(c) if allowed else ''
            epoch = c.execute('SELECT epoch FROM pet_audio_epoch WHERE singleton=1').fetchone()[0]
            gid = str(uuid4())
            c.execute('INSERT INTO pet_audio_grant VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (gid, child_id, expected_revision+1, int(allowed), end.isoformat(), day_limit_micros,
                 request_limit_micros, request, identity, epoch, privacy, now().isoformat(), action_id))
            if not allowed:
                c.execute("UPDATE pet_audio_work SET state='invalidated',owner=NULL,error_code='PURPOSE_REVOKED' WHERE child_id=?", (child_id,))
            result = dict(c.execute('SELECT * FROM pet_audio_grant WHERE grant_id=?', (gid,)).fetchone())
            c.commit()
            return result

    def _grant(self, c, child_id, *, gid=None):
        from datetime import datetime
        g = c.execute('SELECT * FROM pet_audio_grant WHERE child_id=? ORDER BY revision DESC LIMIT 1', (child_id,)).fetchone()
        require(g and g['allowed'] == 1 and (gid is None or gid == g['grant_id'])
            and datetime.fromisoformat(g['expires_at']) > now(), 'PURPOSE_NOT_AUTHORIZED')
        require(g['epoch'] == c.execute('SELECT epoch FROM pet_audio_epoch WHERE singleton=1').fetchone()[0]
            and g['endpoint_hash'] == self._endpoint()
            and g['endpoint_hash'] == c.execute('SELECT endpoint_hash FROM pet_audio_config WHERE singleton=1').fetchone()[0]
            and g['consent_hash'] == consent(c), 'PURPOSE_CHANGED')
        return g

    def enqueue(self, child_id, assessment_id):
        self.require_new_use()
        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            existing = c.execute('SELECT * FROM pet_audio_work WHERE child_id=? AND assessment_id=?', (child_id, assessment_id)).fetchone()
            if existing:
                return self._public(c, existing, live=False)
            self._grant(c, child_id)
            original = self._source(c, child_id, assessment_id, live=True)
            original_key = digest([original['binding'][k] for k in ('child_id','attempt_id','media_id','audio_sha256')])
            require(not c.execute('SELECT 1 FROM pet_audio_work WHERE child_id=? AND original_key=?', (child_id,original_key)).fetchone(), 'ORIGINAL_ALREADY_QUEUED')
            stamp, wid = now().isoformat(), str(uuid4())
            epoch = c.execute('SELECT epoch FROM pet_audio_epoch WHERE singleton=1').fetchone()[0]
            c.execute('''INSERT INTO pet_audio_work(work_id,child_id,session_id,assessment_id,media_id,source_hash,binding_json,
                original_key,state,generation,epoch,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,'queued',0,?,?,?)''',
                (wid, child_id, original['binding']['session_id'], assessment_id, original['binding']['media_id'], original['source_hash'],
                 encoded(original['binding']), original_key, epoch, stamp, stamp))
            c.commit()
        return {'work_id': wid, 'state': 'queued', 'generation': 0, 'mastery_claim_allowed': False}

    def _owned(self, c, child_id, work_id):
        row = c.execute('SELECT * FROM pet_audio_work WHERE child_id=? AND work_id=?', (child_id, work_id)).fetchone()
        require(row is not None, 'WORK_NOT_OWNED')
        return row

    def _live(self, c, row, *, live):
        require(row['epoch'] == c.execute('SELECT epoch FROM pet_audio_epoch WHERE singleton=1').fetchone()[0], 'RESTORED_AUTHORITY_REVOKED')
        g = self._grant(c, row['child_id'], gid=row['grant_id'])
        original = self._source(c, row['child_id'], row['assessment_id'], live=live)
        require(original['source_hash'] == row['source_hash'], 'ORIGINAL_CHANGED')
        if live and row['live_hash']:
            require(original['live_hash'] == row['live_hash'], 'LIVE_GENERATION_CHANGED')
        return g, original

    def _public(self, c, row, *, live):
        from datetime import datetime
        state, result, question = row['state'], None, None
        verification, error = 'current', row['error_code']
        if state == 'running' and (row['runtime_epoch'] != self.runtime_epoch or datetime.fromisoformat(row['lease_until']) <= now()):
            state = 'unknown'
        try:
            _, original = self._live(c, row, live=live)
            question = original['question']
            if row['result_json'] and state in {'response_saved', 'delivered'}:
                result = json.loads(row['result_json'])
                decode_feedback(encoded(result['feedback']), duration_ms=json.loads(row['binding_json'])['duration_ms'])
        except OSError:
            state, result, verification, error = 'verification_pending', None, 'unavailable', 'SOURCE_READ_UNAVAILABLE'
        except (ValueError, TypeError, KeyError):
            state, result = 'invalidated', None
            verification = 'changed'
        delivery = ('awaiting_receipt' if row['state']=='response_saved' and row['error_code']=='SAVED_DELIVERY_UNCONFIRMED'
                    else 'retired' if row['error_code']=='SAVED_DELIVERY_RETIRED'
                    else 'runtime_delivered' if row['state']=='delivered' else 'pending')
        return {'work_id': row['work_id'], 'state': state, 'generation': row['generation'],
            'child_id': row['child_id'], 'question': question, 'created_at': row['created_at'],
            'result': result, 'error_code': error, 'source_verification': verification,
            'delivery_status': delivery, 'retry_after_seconds': DELIVERY_BACKOFF_SECONDS,
            'mastery_claim_allowed': False,
            'assessment_scope': 'uncalibrated_original_audio_practice', 'local_only': True,
            'cloud_transmission_allowed': False}

    def read(self, child_id, work_id, *, live=False):
        with self.database.connect() as c:
            c.execute('BEGIN')
            return self._public(c, self._owned(c, child_id, work_id), live=live)

    def _claim(self, child_id, work_id, *, expected_generation=None):
        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row = self._owned(c, child_id, work_id)
            if row['state'] != 'queued' or (expected_generation is not None and row['generation'] != expected_generation):
                return None
            g, original = self._live(c, row, live=True)
            cost = self.gateway.endpoint.reserve_cost(duration_ms=original['binding']['duration_ms'], question=original['question'])
            day = now().astimezone(FAMILY_ZONE).date().isoformat()
            spent = c.execute("SELECT coalesce(sum(cost_micros),0) FROM pet_audio_dispatch WHERE (child_id=? OR child_id IS NULL) AND day=? AND state!='released'", (child_id, day)).fetchone()[0]
            require(cost <= g['request_limit_micros'] and spent + cost <= g['day_limit_micros'], 'PURPOSE_BUDGET_EXHAUSTED')
            owner, dispatch = str(uuid4()), str(uuid4())
            lease = (now() + timedelta(seconds=90)).isoformat()
            c.execute("UPDATE pet_audio_work SET state='running',owner=?,runtime_epoch=?,lease_until=?,grant_id=?,endpoint_hash=?,live_hash=?,updated_at=? WHERE work_id=?",
                (owner, self.runtime_epoch, lease, g['grant_id'], self._endpoint(), original['live_hash'], now().isoformat(), work_id))
            c.execute("INSERT INTO pet_audio_dispatch VALUES(?,?,?,?,?,?,'reserved',?)",
                (dispatch, work_id, child_id, row['generation'], day, cost, now().isoformat()))
            c.commit()
            return {'owner': owner, 'dispatch_id': dispatch, 'generation': row['generation'], 'original': original}

    def _authorize(self, child_id, work_id, claim, phase):
        from datetime import datetime
        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row = self._owned(c, child_id, work_id)
            require(row['state'] == 'running' and row['owner'] == claim['owner'] and row['generation'] == claim['generation']
                and row['runtime_epoch'] == self.runtime_epoch and datetime.fromisoformat(row['lease_until']) > now(), 'OWNER_FENCED')
            g, original = self._live(c, row, live=True)
            require(original['audio'] == claim['original']['audio'], 'AUDIO_CHANGED')
            dispatch = c.execute('SELECT * FROM pet_audio_dispatch WHERE dispatch_id=? AND work_id=? AND generation=?',
                (claim['dispatch_id'], work_id, claim['generation'])).fetchone()
            day = now().astimezone(FAMILY_ZONE).date().isoformat()
            require(dispatch and dispatch['day'] == day and dispatch['state'] == ('reserved' if phase == 'headers' else 'sent'), 'DISPATCH_ALREADY_USED')
            spent = c.execute("SELECT coalesce(sum(cost_micros),0) FROM pet_audio_dispatch WHERE (child_id=? OR child_id IS NULL) AND day=? AND state!='released'", (child_id, day)).fetchone()[0]
            require(spent <= g['day_limit_micros'] and dispatch['cost_micros'] <= g['request_limit_micros'], 'PURPOSE_BUDGET_EXHAUSTED')
            if phase == 'headers':
                c.execute("UPDATE pet_audio_dispatch SET state='sent' WHERE dispatch_id=?", (claim['dispatch_id'],))
            else:
                c.execute("UPDATE pet_audio_dispatch SET state='body_sent' WHERE dispatch_id=?", (claim['dispatch_id'],))
            c.commit()

    def _save(self, child_id, work_id, claim, result):
        from datetime import datetime
        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row = self._owned(c, child_id, work_id)
            require(row['state'] == 'running' and row['owner'] == claim['owner'] and row['generation'] == claim['generation']
                and row['runtime_epoch'] == self.runtime_epoch and datetime.fromisoformat(row['lease_until']) > now(), 'OWNER_FENCED')
            require(c.execute("SELECT 1 FROM pet_audio_dispatch WHERE dispatch_id=? AND work_id=? AND generation=? AND state='body_sent'",
                (claim['dispatch_id'],work_id,claim['generation'])).fetchone(), 'DISPATCH_REQUIRED')
            _, original = self._live(c, row, live=True)
            decode_feedback(encoded(result['feedback']), duration_ms=original['binding']['duration_ms'])
            c.execute("UPDATE pet_audio_work SET state='response_saved',result_json=?,owner=NULL,updated_at=? WHERE work_id=?",
                (encoded(result), now().isoformat(), work_id))
            c.execute("UPDATE pet_audio_dispatch SET state='settled' WHERE dispatch_id=? AND state='body_sent'", (claim['dispatch_id'],))
            c.commit()

    def _failed(self, child_id, work_id, claim, error):
        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            dispatch = c.execute('SELECT state FROM pet_audio_dispatch WHERE dispatch_id=?', (claim['dispatch_id'],)).fetchone()
            sent = dispatch and dispatch[0] != 'reserved'
            code = str(error) if isinstance(error, (PetAudioAuthorityError, PetAudioProtocolError)) else type(error).__name__
            code = code[:100]
            c.execute("UPDATE pet_audio_work SET state=?,owner=NULL,error_code=?,updated_at=? WHERE child_id=? AND work_id=? AND owner=? AND generation=? AND state='running'",
                ('unknown' if sent else 'failed', code, now().isoformat(), child_id, work_id, claim['owner'], claim['generation']))
            c.execute("UPDATE pet_audio_dispatch SET state=? WHERE dispatch_id=? AND state IN ('reserved','sent','body_sent')",
                ('unknown' if sent else 'released', claim['dispatch_id']))
            c.commit()

    async def run(self, child_id, work_id, *, expected_generation=None):
        claim = self._claim(child_id, work_id, expected_generation=expected_generation)
        if claim:
            try:
                original = claim['original']
                result = await self.gateway.feedback(question=original['question'], audio=original['audio'],
                    duration_ms=original['binding']['duration_ms'], authorize=lambda phase: self._authorize(child_id, work_id, claim, phase))
                self._save(child_id, work_id, claim, result)
            except BaseException as error:
                self._failed(child_id, work_id, claim, error)
                if not isinstance(error, Exception):
                    raise
        try:
            return self.read(child_id, work_id, live=True)
        except PetAudioAuthorityError:
            if not claim:
                raise
            return {'work_id': work_id, 'state': 'invalidated', 'result': None, 'mastery_claim_allowed': False}

    def enqueue_for_feedback_turn(self, turn_id):
        if not self.gateway or not turn_id:
            return None
        with self.database.connect() as c:
            row = c.execute('''SELECT r.child_id,r.assessment_id FROM rubric_assessment r
                JOIN agent_step s ON s.step_id=r.step_id JOIN agent_plan p ON p.plan_id=s.plan_id
                JOIN learning_evaluation_execution e ON e.step_id=s.step_id
                JOIN diagnostic_item i ON i.diagnostic_item_id=json_extract(r.payload_json,'$.material.diagnostic_item_id')
                JOIN knowledge_node k ON k.knowledge_node_id=i.knowledge_node_id
                WHERE p.agent_turn_id=? AND p.child_id=r.child_id AND e.state='completed'
                AND k.track='pet' AND i.answer_type IN ('speaking','read_aloud')
                AND json_extract(e.record_json,'$.evaluation_origin')='local_diagnostic' ''', (turn_id,)).fetchone()
        if not row:
            return None
        try:
            return self.enqueue(row['child_id'], row['assessment_id'])
        except (ValueError, OSError) as error:
            code = str(error) if isinstance(error,(PetAudioAuthorityError,PetAudioProtocolError)) else 'PET_AUDIO_ORIGINAL_UNAVAILABLE'
            return {'state': 'blocked', 'error_code': code, 'mastery_claim_allowed': False}

    def work_for_feedback_turn(self, turn_id):
        with self.database.connect() as c:
            c.execute('BEGIN')
            row=c.execute('''SELECT w.* FROM pet_audio_work w JOIN rubric_assessment r ON r.assessment_id=w.assessment_id
                JOIN agent_step s ON s.step_id=r.step_id JOIN agent_plan p ON p.plan_id=s.plan_id
                WHERE p.agent_turn_id=? AND p.child_id=w.child_id AND r.child_id=w.child_id''',(turn_id,)).fetchone()
            return self._public(c,row,live=False) if row else None

    async def for_feedback_turn(self, turn_id):
        work=self.enqueue_for_feedback_turn(turn_id)
        if work and work.get('work_id'):
            current=self.work_for_feedback_turn(turn_id)
            if not current:
                return {'state':'invalidated','mastery_claim_allowed':False}
            return await self.run(current['child_id'],current['work_id'])
        return work

    @staticmethod
    def _defer_read(c, row):
        stamp = now()
        c.execute('''UPDATE pet_audio_work SET error_code=?,updated_at=?,lease_until=?
            WHERE child_id=? AND work_id=? AND generation=? AND state IN ('queued','response_saved')''',
            ('SAVED_DELIVERY_UNCONFIRMED' if row['error_code']=='SAVED_DELIVERY_UNCONFIRMED' else 'SOURCE_READ_RETRY',
             stamp.isoformat(), (stamp+timedelta(seconds=DELIVERY_BACKOFF_SECONDS)).isoformat(),
             row['child_id'],row['work_id'],row['generation']))

    def _retire_saved_delivery(self, child_id, work_id, generation):
        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute('''SELECT * FROM pet_audio_work WHERE child_id=? AND work_id=?
                AND generation=? AND state='response_saved' ''', (child_id, work_id, generation)).fetchone()
            if not row:
                return
            current = self._public(c, row, live=True)
            if current['state']=='verification_pending':
                self._defer_read(c,row)
                c.commit()
                return
            if current['state']!='invalidated':
                return
            # A stale live target retires delivery, not a valid historical result.
            prior = self._public(c, row, live=False)
            if prior['state']=='verification_pending':
                self._defer_read(c,row)
                c.commit()
                return
            historical = prior['state'] == 'response_saved'
            c.execute('''UPDATE pet_audio_work SET state=?,error_code=?,updated_at=?
                WHERE child_id=? AND work_id=? AND generation=? AND state='response_saved' ''',
                ('response_saved' if historical else 'invalidated',
                 'SAVED_DELIVERY_RETIRED' if historical else 'SAVED_SOURCE_CHANGED', now().isoformat(),
                 child_id, work_id, generation))
            c.commit()

    def _claim_saved_delivery(self, child_id, work_id, generation):
        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute('''SELECT * FROM pet_audio_work WHERE child_id=? AND work_id=?
                AND generation=? AND state='response_saved' ''',(child_id,work_id,generation)).fetchone()
            if not row or row['error_code']=='SAVED_DELIVERY_RETIRED':
                return None
            current=self._public(c,row,live=True)
            if current['state']=='verification_pending':
                self._defer_read(c,row)
                c.commit()
                return None
            if current['state']!='response_saved':
                return None
            check_only=row['error_code']=='SAVED_DELIVERY_UNCONFIRMED'
            stamp=now()
            # Commit before the callback: a crash can only lead to receipt reads,
            # never an automatic second execution of an uncertain learning turn.
            c.execute('''UPDATE pet_audio_work SET error_code='SAVED_DELIVERY_UNCONFIRMED',updated_at=?,lease_until=?
                WHERE child_id=? AND work_id=? AND generation=? AND state='response_saved' ''',
                (stamp.isoformat(),(stamp+timedelta(seconds=DELIVERY_BACKOFF_SECONDS)).isoformat(),child_id,work_id,generation))
            c.commit()
            return 'receipt' if check_only else 'deliver'

    def _recover_saved_delivery(self, child_id, work_id, generation, turn_id, allow_projected=False):
        with self.database.connect() as c:
            c.execute('BEGIN')
            row=c.execute('''SELECT s.result_json FROM pet_audio_work w
                JOIN rubric_assessment r ON r.assessment_id=w.assessment_id AND r.child_id=w.child_id
                JOIN agent_step s ON s.step_id=r.step_id
                JOIN agent_plan p ON p.plan_id=s.plan_id AND p.child_id=w.child_id AND p.session_id=w.session_id
                JOIN agent_turn t ON t.agent_turn_id=p.agent_turn_id AND t.child_id=w.child_id AND t.session_id=w.session_id
                WHERE w.child_id=? AND w.work_id=? AND w.generation=? AND w.state='response_saved'
                AND t.agent_turn_id=? AND t.tool_status='completed' AND t.tool_error_code IS NULL
                AND p.status='completed' AND s.status='completed' AND s.capability_name='learning.evaluate_response'
                AND s.step_index=0''',(child_id,work_id,generation,turn_id)).fetchone()
            if not row:
                return False
            try:
                result=json.loads(row['result_json'])
            except (ValueError,TypeError,KeyError):
                result=None
        def matches(value):
            try:
                ref=value['result']['oral_feedback']['audio_work_ref']
                return (value['capability']=='learning.evaluate_response'
                    and (ref['work_id'],ref['generation'])==(work_id,generation))
            except (TypeError,KeyError):
                return False
        if not matches(result):
            if not allow_projected or self.runtime_receipt_reader is None:
                return False
            receipt=self.runtime_receipt_reader(turn_id)
            if not isinstance(receipt,dict) or receipt.get('status')!='completed':
                return False
            try:
                completed=receipt['result']
                result={'capability':'learning.evaluate_response','response_text':completed['response_text'],
                        'result':completed['evaluation']}
            except (TypeError,KeyError):
                return False
            if not matches(result):
                return False
        # This verifies the current exact source/result and marks only delivery.
        # It does not run the evaluator, complete a turn, or enqueue continuation.
        self.record_runtime_delivery(turn_id,result)
        return True

    async def process_pending(self, deliver):
        if not self.gateway:
            return
        with self.database.connect() as c:
            rows=c.execute('''SELECT w.work_id,w.child_id,w.generation,p.agent_turn_id FROM pet_audio_work w
                JOIN rubric_assessment r ON r.assessment_id=w.assessment_id AND r.child_id=w.child_id
                JOIN agent_step s ON s.step_id=r.step_id JOIN agent_plan p ON p.plan_id=s.plan_id AND p.child_id=w.child_id
                JOIN daily_session d ON d.session_id=w.session_id AND d.child_id=w.child_id
                WHERE w.state IN ('queued','response_saved') AND d.status='active' AND d.observation_status!='paused'
                AND (w.state='queued' OR coalesce(w.error_code,'')!='SAVED_DELIVERY_RETIRED')
                AND (coalesce(w.error_code,'') NOT IN ('SAVED_DELIVERY_UNCONFIRMED','SOURCE_READ_RETRY')
                     OR w.lease_until IS NULL OR julianday(w.lease_until)<=julianday(?))
                ORDER BY CASE WHEN w.state='queued' THEN 0 ELSE 1 END,w.updated_at,w.created_at,w.work_id LIMIT 5''',
                (now().isoformat(),)).fetchall()
        for row in rows:
            try:
                current=await self.run(row['child_id'],row['work_id'],expected_generation=row['generation'])
                if current['state']=='response_saved':
                    mode=self._claim_saved_delivery(row['child_id'],row['work_id'],row['generation'])
                    recovered=(self._recover_saved_delivery(row['child_id'],row['work_id'],row['generation'],row['agent_turn_id'],mode=='receipt')
                               if mode else False)
                    if mode=='deliver' and not recovered:
                        try:
                            await deliver(row['agent_turn_id'])
                        except Exception as error:
                            import sqlite3
                            if isinstance(error,sqlite3.Error):
                                raise
                            # The committed uncertainty marker survives cancellation
                            # and ordinary callback failures; recovery is read-only.
                elif current['state']=='invalidated':
                    self._retire_saved_delivery(row['child_id'],row['work_id'],row['generation'])
                elif current['state']=='verification_pending':
                    with self.database.connect() as c:
                        c.execute('BEGIN IMMEDIATE')
                        current=c.execute('SELECT * FROM pet_audio_work WHERE child_id=? AND work_id=? AND generation=?',
                            (row['child_id'],row['work_id'],row['generation'])).fetchone()
                        if current:
                            self._defer_read(c,current)
                        c.commit()
            except OSError:
                with self.database.connect() as c:
                    c.execute('BEGIN IMMEDIATE')
                    current=c.execute('SELECT * FROM pet_audio_work WHERE child_id=? AND work_id=? AND generation=?',
                        (row['child_id'],row['work_id'],row['generation'])).fetchone()
                    if current:
                        self._defer_read(c,current)
                    c.commit()
            except ValueError:
                # No unknown/failed dispatch is requeued. A revoked queued source
                # cannot keep presenting an automatic first-send opportunity.
                with self.database.connect() as c:
                    c.execute("UPDATE pet_audio_work SET state='invalidated',owner=NULL,error_code='QUEUED_SOURCE_CHANGED' WHERE work_id=? AND generation=? AND state='queued'",
                        (row['work_id'],row['generation']))
                    c.commit()
                self._retire_saved_delivery(row['child_id'],row['work_id'],row['generation'])

    def record_runtime_delivery(self, turn_id, result):
        ref = ((result.get('result') or {}).get('oral_feedback') or {}).get('audio_work_ref')
        if not ref:
            return
        from .pet_audio_projection import apply_formative_feedback
        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute('''SELECT w.* FROM pet_audio_work w JOIN rubric_assessment r ON r.assessment_id=w.assessment_id
                JOIN agent_step s ON s.step_id=r.step_id JOIN agent_plan p ON p.plan_id=s.plan_id
                JOIN agent_turn t ON t.agent_turn_id=p.agent_turn_id AND t.child_id=w.child_id AND t.session_id=w.session_id
                WHERE w.work_id=? AND p.agent_turn_id=? AND t.tool_status='completed' ''', (ref['work_id'],turn_id)).fetchone()
            require(row and row['state'] in {'response_saved','delivered'} and row['generation'] == ref['generation'], 'DELIVERY_SCOPE_CHANGED')
            self._live(c,row,live=True)
            binding=json.loads(row['binding_json'])
            projected=apply_formative_feedback(c,dict(child_id=row['child_id'],session_id=row['session_id'],prompt_id=binding['prompt_id']))
            require(projected.get('audio_work_ref') == ref and projected.get('spoken_feedback') == result['response_text'], 'DELIVERY_RESULT_CHANGED')
            c.execute("UPDATE pet_audio_work SET state='delivered',error_code=NULL,updated_at=? WHERE work_id=?", (now().isoformat(),row['work_id']))
            c.commit()

    def retry(self, child_id, work_id, *, expected_generation, action_id, acknowledge_possible_charge):
        from datetime import datetime
        require(acknowledge_possible_charge is True and type(expected_generation) is int
            and isinstance(action_id, str) and 1 <= len(action_id) <= 200, 'EXPLICIT_RETRY_REQUIRED')
        request = encoded([work_id, expected_generation, acknowledge_possible_charge])
        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row = self._owned(c, child_id, work_id)
            if row['state'] == 'running' and (row['runtime_epoch'] != self.runtime_epoch or datetime.fromisoformat(row['lease_until']) <= now()):
                c.execute("UPDATE pet_audio_work SET state='unknown',owner=NULL,error_code='OWNER_EXPIRED' WHERE work_id=?", (work_id,))
                row = self._owned(c, child_id, work_id)
            old = c.execute('SELECT request_json FROM pet_audio_retry WHERE child_id=? AND action_id=?', (child_id, action_id)).fetchone()
            if old:
                require(old[0] == request, 'RETRY_KEY_CONFLICT')
                return self._public(c, row, live=True)
            require(row['state'] in {'unknown', 'failed'} and row['generation'] == expected_generation, 'RETRY_STATE_CHANGED')
            self._live(c, row, live=True)
            c.execute('INSERT INTO pet_audio_retry VALUES(?,?,?,?)', (action_id, child_id, work_id, request))
            c.execute("UPDATE pet_audio_work SET state='queued',generation=generation+1,owner=NULL,error_code=NULL,updated_at=? WHERE work_id=?",
                (now().isoformat(), work_id))
            c.commit()
        return self.read(child_id, work_id)
