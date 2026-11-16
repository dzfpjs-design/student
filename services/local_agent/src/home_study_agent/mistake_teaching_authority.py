"""Local dialogue ownership for asynchronous teaching and its saved receipt."""
import json
from hashlib import sha256

from .diagnostic_prompt import _TransactionDatabase
from .mistake_dialogue import MistakeDialogueService
from .tutor_dispatch_authorization import TutorDispatchRevokedError


class MistakeTeachingRevokedError(TutorDispatchRevokedError):
    code = 'TUTOR_DIALOGUE_CHANGED'


def _hash(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def _state_hash(state):
    return _hash({key: value for key, value in state.items() if key != 'teaching_turn_id'})


def _content_hash(request):
    return _hash({key: value for key, value in request.items() if key != 'client_action_id'})


def _running_tasks(c, focus):
    return [tuple(row) for row in c.execute('''SELECT pi.plan_item_id,pi.active_segment_started_at,
        pi.active_seconds,(SELECT max(e.rowid) FROM plan_item_event e WHERE e.plan_item_id=pi.plan_item_id)
        FROM plan_item pi JOIN daily_plan dp USING(plan_id) WHERE dp.child_id=? AND dp.session_id=?
        AND dp.status='active' AND pi.status='active' ORDER BY pi.plan_item_id''',
        (focus['child_id'], focus['session_id']))]


def validate_saved_teaching(c, turn, envelope):
    binding = envelope.get('local_mistake_dialogue')
    if not binding:
        if (turn['client_action_id'] or '').startswith('mistake-teaching:'):
            raise MistakeTeachingRevokedError()
        return
    row = c.execute('SELECT * FROM mistake_dialogue WHERE session_id=? AND child_id=?',
        (turn['session_id'], turn['child_id'])).fetchone()
    active = c.execute("SELECT 1 FROM daily_session WHERE session_id=? AND child_id=? AND state='study' AND status='active'",
        (turn['session_id'], turn['child_id'])).fetchone()
    try:
        if (not active or not row or binding['version'] != 1 or row['phase'] in {'closed', 'paused'}
                or row['mistake_id'] != binding['mistake_id'] or row['phase'] != binding['phase']):
            raise MistakeTeachingRevokedError()
        state = json.loads(row['state_json'])
        if state.get('teaching_turn_id') != turn['turn_id'] or _state_hash(state) != binding['state_hash']:
            raise MistakeTeachingRevokedError()
        request = MistakeDialogueService(_TransactionDatabase(c)).teaching_request({**dict(row), 'state': state})
        if not request or _content_hash(request) != binding['content_hash']:
            raise MistakeTeachingRevokedError()
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        raise MistakeTeachingRevokedError() from error


class MistakeTeachingAuthority:
    def __init__(self, database, focus):
        self.database = database
        self.focus = json.loads(json.dumps({key: focus[key] for key in
            ('session_id', 'child_id', 'mistake_id', 'phase', 'state')}))
        self.request = dict(focus['teaching_request'])
        with database.connect() as c:
            self.task_stamp = _running_tasks(c, self.focus)

    def _check(self, c):
        expected = self.focus
        if _running_tasks(c, expected) != self.task_stamp:
            raise MistakeTeachingRevokedError()
        session = c.execute('''SELECT 1 FROM daily_session ds JOIN child_profile cp USING(child_id)
            WHERE ds.session_id=? AND ds.child_id=? AND ds.status='active' AND ds.state='study'
            AND cp.status='active' ''', (expected['session_id'], expected['child_id'])).fetchone()
        row = c.execute('SELECT * FROM mistake_dialogue WHERE session_id=?', (expected['session_id'],)).fetchone()
        if (not session or not row or row['phase'] in {'closed', 'paused'}
                or any(row[key] != expected[key] for key in ('child_id', 'mistake_id', 'phase'))):
            raise MistakeTeachingRevokedError()
        try:
            state = json.loads(row['state_json'])
            # Our own atomic attachment is the only allowed change on replay.
            attached = state.get('teaching_turn_id')
            if attached != expected['state'].get('teaching_turn_id'):
                own = c.execute('''SELECT 1 FROM tutor_turn WHERE turn_id=? AND client_action_id=?
                    AND child_id=? AND session_id=?''',
                    (attached, self.request['client_action_id'], expected['child_id'], expected['session_id'])).fetchone()
                if not own:
                    raise MistakeTeachingRevokedError()
                if 'teaching_turn_id' in expected['state']:
                    state['teaching_turn_id'] = expected['state']['teaching_turn_id']
                else:
                    state.pop('teaching_turn_id', None)
            if state != expected['state']:
                raise MistakeTeachingRevokedError()
            service = MistakeDialogueService(_TransactionDatabase(c))
            current = service.teaching_request({**expected, 'state': state})
            if current != self.request:
                raise MistakeTeachingRevokedError()
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            raise MistakeTeachingRevokedError() from error

    def assert_current(self):
        with self.database.connect() as c:
            c.execute('BEGIN')
            self._check(c)

    def attach(self, c, turn_id):
        """Runs in the same write transaction as the Tutor result and reservation."""
        self._check(c)
        own = c.execute('''SELECT 1 FROM tutor_turn WHERE turn_id=? AND client_action_id=?
            AND child_id=? AND session_id=?''', (turn_id, self.request['client_action_id'],
                self.focus['child_id'], self.focus['session_id'])).fetchone()
        if not own:
            raise MistakeTeachingRevokedError()
        c.execute("UPDATE mistake_dialogue SET state_json=json_set(state_json,'$.teaching_turn_id',?) WHERE session_id=? AND child_id=?",
            (turn_id, self.focus['session_id'], self.focus['child_id']))
        envelope = json.loads(c.execute('SELECT output_envelope_json FROM tutor_turn WHERE turn_id=?', (turn_id,)).fetchone()[0] or '{}')
        envelope['local_mistake_dialogue'] = {'version': 1, 'mistake_id': self.focus['mistake_id'],
            'phase': self.focus['phase'], 'state_hash': _state_hash(self.focus['state']),
            'content_hash': _content_hash(self.request)}
        c.execute('UPDATE tutor_turn SET output_envelope_json=? WHERE turn_id=?',
            (json.dumps(envelope, ensure_ascii=False, sort_keys=True), turn_id))

    def deliver(self, taught):
        # Also repairs a legacy saved turn without attachment, without regenerating.
        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            self.attach(c, taught['turn_id'])
            c.commit()
