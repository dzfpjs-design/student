"""Current-utterance control classification; sources and execution stay local."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from contextlib import nullcontext
from datetime import datetime, timezone

from .cloud_privacy import sanitize_cloud_text, sensitive_categories
from .model_gateway import ModelRequest
from .repository import utc_now
from .pending_evaluation_control import QUERY_CONTROLS
from .controlled_voice import CONTROLS as EXTERNAL_CONTROLS
from .learning_input_voice import CONTROLS as INPUT_CONTROLS


VERSION = 'content_free_voice_control_v1'
# Values are static capabilities, never request-supplied business identifiers.
CONTROLS = {
    **EXTERNAL_CONTROLS,
    **INPUT_CONTROLS,
    'voice.configure_wake_word': ('configure_wake_word', {}),
    'oral.retry': ('retry_oral_answer', {}),
    'oral.independent_check': ('check_oral_independently', {}),
    'learning.help_current': ('ask_tutor', {}),
    'learning.independent_check': ('start_diagnostic', {}),
    'learning.retry_check_coaching': ('retry_tutor_check_coaching', {}),
    'session.request_end': ('request_end', {}),
    **QUERY_CONTROLS,
    'companion.start_activity': ('start_companion_activity', {}),
    'companion.chat': ('none', {}),
    'learning_inputs.resume_draft': ('select_learning_input_target', {}),
    'companion.activity_feedback': ('record_companion_activity_feedback', {}),
    'writing.read_original': ('read_writing_original', {}),
    'native_writing.submission_status': ('check_native_writing_submission', {}),
    'native_writing.submit_retained': ('submit_retained_native_writing', {}),
    'writing.confirm_original': ('confirm_writing_original', {}),
    'writing.revise_original': ('revise_writing_original', {}),
    'writing.independent_check': ('check_writing_independently', {}),
    'exam.pause': ('pause_exam_simulation', {}),
    'exam.resume': ('resume_exam_simulation', {}),
    'exam.cancel': ('cancel_exam_simulation', {}),
    'task.pause': ('pause_active_task', {}),
    'task.complete': ('complete_active_task', {}),
    'task.resume': ('resume_paused_task', {}),
    'task.reschedule': ('reschedule_current_task', {}),
    'task.start': ('start_next_task', {}),
    'break.begin': ('begin_break', {}),
    'break.resume': ('resume_study', {}),
    'speech.stop': ('stop_speech', {}),
    'speech.repeat': ('repeat_response', {}),
    'observation.pause': ('pause_observation', {}),
    'observation.resume': ('resume_observation', {}),
    'device.connect': ('connect_devices', {}),
    'diagnostic.replay': ('replay_diagnostic', {}),
    'diagnostic.cancel': ('cancel_diagnostic', {}),
    'pet_vocabulary.cancel': ('cancel_pet_vocabulary_review', {}),
    **{f'document.{command}': ('control_document_monitor', {'command': command})
       for command in ('pause', 'resume', 'inspect', 'review')},
    **{f'tutor.{command}': ('control_tutor_playback', {'command': command})
       for command in ('pause', 'resume', 'previous', 'restart', 'cancel')},
    **{f'pet_audio.{command}': ('control_pet_listening', {'command': command})
       for command in ('pause', 'resume', 'restart', 'cancel')},
}
STATE_FIELDS = frozenset({'has_pending_question', 'has_active_exam', 'has_paused_exam',
                          'has_active_task', 'has_tutor_playback', 'has_pet_listening',
                          'is_study_phase', 'is_break_phase'})
TASK_SUBJECTS = {'math': '数学', 'english': '英语', 'chinese': '语文', 'physics': '物理',
                'chemistry': '化学', 'biology': '生物', 'history': '历史',
                'geography': '地理', 'politics': '道德与法治'}


class VoiceControlIntent:
    @staticmethod
    def local_task_navigation(transcript):
        """A bounded offline grammar, not a substitute for semantic planning."""
        text = transcript.strip().rstrip('。！!').strip()
        prefix = r'(?:(?:那|好)[，,]?\s*)?(?:(?:我们|咱们|我)\s*)?(?:请|先)?'
        suffix = r'(?:吧|好吗)?'
        if re.fullmatch(prefix + r'(?:暂停(?:当前|这项)?任务|暂停这项|任务先停一下)' + suffix, text):
            return 'pause'
        if re.fullmatch(prefix + r'继续当前任务' + suffix, text):
            return 'current_continue'
        if re.fullmatch(prefix + r'(?:再读一遍(?:这道题|当前题目)?|重读(?:这道题|当前题目|题目)|重播题目|再播放一遍)' + suffix, text):
            return 'replay_current'
        if re.fullmatch(prefix + r'(?:接下来|下一项)(?:要|该)?(?:做什么|是什么|怎么安排)[？?]?', text):
            return 'describe'
        if re.fullmatch(prefix + r'(?:开始|做|进行)(?:下一个(?:任务)?|下一项(?:任务)?)' + suffix, text):
            return 'next'
        if re.fullmatch(prefix + r'(?:(?:开始|进入)(?:今天的)?复盘|回顾今天)' + suffix, text):
            # Session wrap-up is not a named study task. Let the session-end
            # classifier bind it to session.request_end and the reflection
            # transition instead of returning a no-task clarification.
            return None
        if re.fullmatch(prefix + r'(?:开始(?:这项|任务|学习)?|继续(?:原任务|任务|这项|学习)?|接着(?:做|学))' + suffix, text):
            return 'continue'
        # Ordered multi-target speech needs semantic scope resolution. The
        # local named-task shortcut must not merge "do A, then do B" into one
        # title or treat the second, future action as current authorization.
        if re.search(r'[，,；;]\s*(?:然后|之后|再)(?:我|我们)?(?:再)?(?:做|学|练|处理|开始)', text):
            return None
        # Completion reports are outcomes for the active task, not requests to
        # start a task whose title happens to follow the leading verb "做".
        # Leave them to the completion classifier and its evidence checks.
        if re.search(r'(?:做|写|检查|处理)?完(?:了|成)?|已完成|完成了|这项完成', text):
            return None
        if (re.fullmatch(prefix + r'(?:开始|做|进行)(?!任务(?:吧|好吗)?$).{2,80}' + suffix, text)
                and not re.search(
                    r'不是|不要|不想|先别|别做|除了|不做|如果|假如|要是|'
                    r'他说|她说|老师说|是什么意思|什么意思|为什么|会怎样|会怎么样|'
                    r'是否|能不能|可不可以|还是|或者|和结束',
                    text,
                )):
            return 'named'
        if re.fullmatch(r'(?:先)?(?:别|不要|暂不)(?:开始|继续)(?:任务|学习|下一项)?(?:了)?', text):
            return 'hold'
        if re.fullmatch(r'(?:上一项|这项|刚才的任务)(?:还)?(?:没做好|没做完|没弄懂)', text):
            return 'unfinished'
        if re.fullmatch(r'(?:他|她|老师|妈妈|爸爸)(?:刚才)?说[：:]?.*(?:开始|继续).*', text):
            return 'reported'
        return None

    def accept_local_task_navigation(self, session_id, child_id, action_id, transcript, binding):
        navigation = self.local_task_navigation(transcript)
        from .task_start_choices import choice_index
        selection = (binding.get('task_start') or {}).get('selection_offer')
        if selection and choice_index(transcript) == selection.get('index'):
            navigation = 'selected'
        # Subject/source hints may narrow the private schedule snapshot, but
        # they are never authorization to execute a named task.  Only an exact,
        # unique local title match is deterministic enough to work offline;
        # broader natural requests still require semantic admission so a
        # failed model call cannot fall back to a locally matched queue item.
        if navigation == 'named':
            target = binding.get('task_start') or {}
            requested = target.get('requested_filter') or {}
            item = target.get('item') or {}
            title_query = re.sub(r'[\s，。！!?？、:：]', '', requested.get('title_query') or '')
            item_title = re.sub(r'[\s，。！!?？、:：]', '', item.get('title') or '')
            if (target.get('status') != 'ready' or not title_query
                    or title_query != item_title):
                return None
        if navigation is None:
            return None
        control = ('planning.describe' if navigation == 'describe' else
                   'task.pause' if navigation == 'pause' else
                   'diagnostic.replay' if navigation == 'replay_current' else 'task.start')
        resume = binding.get('task_resume') or {}
        from .task_start_binding import TaskStartBinding
        if navigation == 'current_continue':
            if (binding.get('task') or binding.get('companion_paused_tasks')
                    or not TaskStartBinding.resume_focus_allowed(binding)):
                return None
            control = 'task.resume'
            selector = {'subject': None, 'task_kind': None, 'relative_order': None}
            binding = {**binding, 'task_resume': TaskStartBinding.select_resume_target(resume, selector)}
        elif (navigation == 'continue' and not binding.get('task') and resume.get('items')
                and TaskStartBinding.resume_focus_allowed(binding)
                and not binding.get('companion_paused_tasks')):
            control = 'task.resume'
            from .task_start_binding import TaskStartBinding
            selector = {'subject': None, 'task_kind': None, 'relative_order': None}
            binding = {**binding, 'task_resume': TaskStartBinding.select_resume_target(resume, selector)}
        decision = {'status': 'control', 'control': control, 'origin': 'explicit_local_task_navigation', 'navigation':navigation,
                    **({'selector': selector} if control == 'task.resume' else {})}
        fingerprint = hashlib.sha256(json.dumps([child_id, session_id, transcript], ensure_ascii=False).encode()).hexdigest()
        key = hashlib.sha256(('voice-control:' + action_id).encode()).hexdigest()
        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            if not self.scope_valid(c, session_id, child_id, action_id, require_cloud=False):
                return None
            if navigation in {'pause', 'current_continue', 'replay_current'} and (
                    binding.get('exam_run_id')
                    or (binding.get('companion_activity') or {}).get('status') in {'ready', 'ambiguous'}):
                return None
            if navigation in {'pause', 'replay_current'} and (
                    not binding.get('task' if navigation == 'pause' else 'diagnostic')
                    or (binding.get('task_start') or {}).get('interaction_focus')):
                return None
            old = c.execute('SELECT * FROM voice_control_intent_request WHERE request_key=?', (key,)).fetchone()
            if old:
                if (old['fingerprint'] != fingerprint or old['status'] != 'completed'
                        or json.loads(old['decision_json'] or '{}') != decision or json.loads(old['binding_json']) != binding):
                    return None
            else:
                c.execute('''INSERT INTO voice_control_intent_request
                    (request_key,child_id,session_id,fingerprint,binding_json,status,decision_json,created_at,completed_at)
                    VALUES (?,?,?,?,?,'completed',?,?,?)''',
                    (key,child_id,session_id,fingerprint,json.dumps(binding),json.dumps(decision),utc_now(),utc_now()))
            c.commit()
        return {**decision, 'binding': binding, 'request_key': key}

    @staticmethod
    def activity_focus(c, session_id, child_id):
        """Offer an exact current focus; callers must freeze it when recording starts."""
        from datetime import datetime, timezone
        from .companion_activity_outcome import CompanionActivityOutcomeService as Outcomes
        row = c.execute('''SELECT a.* FROM companion_activity a JOIN daily_session s USING(session_id)
            WHERE a.session_id=? AND a.child_id=? AND s.child_id=a.child_id
            AND s.status='active' AND a.status='active' ''', (session_id, child_id)).fetchone()
        if not row or datetime.fromisoformat(row['ends_at']) <= datetime.now(timezone.utc):
            return None
        outcome = c.execute("SELECT * FROM companion_activity_outcome WHERE source_kind='companion' AND source_id=? AND child_id=?",
                            (row['activity_id'], child_id)).fetchone()
        if not outcome:
            return None
        source, _, digest = Outcomes._source(c, child_id, 'companion', row['activity_id'])
        snapshot = json.loads(outcome['source_snapshot_json'])
        if Outcomes._request_provenance(c, snapshot, source, digest == outcome['source_fingerprint'])['status'] != 'verified':
            return None
        origin = c.execute('''SELECT t.action_json,p.context_manifest_json FROM agent_turn t
            JOIN agent_plan p USING(agent_turn_id) JOIN agent_step s USING(plan_id)
            WHERE s.idempotency_key=? AND t.child_id=? AND t.session_id=?''',
            (row['client_action_id'], child_id, session_id)).fetchone()
        if not origin:
            return None
        action = json.loads(origin['action_json'])
        question = (action.get('voice_input_binding') or {}).get('question_reference') or {}
        pending = json.loads(origin['context_manifest_json']).get('pending_evaluation')
        if pending:
            from .pending_evaluation_control import revalidate
            try:
                revalidate(c,pending,question_reference=question or None)
            except ValueError:
                return None
        elif question.get('kind') == 'diagnostic':
            from .diagnostic_prompt import DiagnosticPromptService, _version
            reference = question['binding']
            prompt = c.execute('''SELECT * FROM diagnostic_prompt WHERE prompt_id=? AND child_id=?
                AND origin_session_id=? AND status='waiting_answer' ''',
                (reference['prompt_id'], child_id, session_id)).fetchone()
            item = DiagnosticPromptService._item(c, child_id, prompt['diagnostic_item_id']) if prompt else None
            if (not item or prompt['item_version'] != reference['item_version'] or _version(item) != prompt['item_version']
                    or DiagnosticPromptService._scheduled_source_reason(c, prompt)):
                return None
        if row['turn_count'] == 0:
            previous = c.execute('''SELECT t.agent_turn_id,t.response_text,s.result_json FROM agent_turn t
                JOIN agent_plan p USING(agent_turn_id) JOIN agent_step s USING(plan_id)
                WHERE s.idempotency_key=? AND s.capability_name='companion.start_activity'
                AND s.status='completed' AND t.child_id=? AND t.session_id=?''',
                (row['client_action_id'], child_id, session_id)).fetchall()
            if len(previous) != 1:
                return None
            prior = previous[0]
            spoken = json.loads(prior['result_json'])['response_text']
        else:
            previous = c.execute('''SELECT t.agent_turn_id,t.response_text,t.transcript,t.action_json,p.context_manifest_json FROM agent_turn t
                JOIN agent_plan p USING(agent_turn_id)
                WHERE t.child_id=? AND t.session_id=?
                AND json_extract(p.context_manifest_json,'$.companion_activity_reply.activity_id')=?
                AND json_extract(p.context_manifest_json,'$.companion_activity_reply.turn_count')=?''',
                (child_id, session_id, row['activity_id'], row['turn_count'])).fetchall()
            if len(previous) != 1:
                return None
            prior, spoken = previous[0], previous[0]['response_text']
            from .agent_runtime import AgentRuntimeStore, InvalidAgentPlanError
            previous_action = json.loads(prior['action_json'])
            try:
                capture = AgentRuntimeStore.resolve_captured_voice(c, session_id=session_id, child_id=child_id,
                    voice_input_id=previous_action.get('voice_input_id'), transcript=prior['transcript'])
            except (InvalidAgentPlanError, ValueError):
                return None
            if AgentRuntimeStore.voice_capture_snapshot(capture) != previous_action.get('voice_input_binding'):
                return None
            receipt = json.loads(prior['context_manifest_json'])['companion_activity_reply']
            journal = c.execute('SELECT * FROM voice_control_intent_request WHERE request_key=?', (receipt['request_key'],)).fetchone()
            if (not journal or journal['status'] != 'completed'
                    or json.loads(journal['decision_json'] or '{}') != {'status': 'non_control', 'role': 'activity_reply'}
                    or json.loads(journal['binding_json']).get('activity_text_consent') != list(VoiceControlIntent.text_consent_version(c) or ())):
                return None
        basis = [dict(row), prior['agent_turn_id'], spoken, outcome['source_fingerprint']]
        version = hashlib.sha256(json.dumps(basis, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        return {'activity_id': row['activity_id'], 'version': version, 'last_turn_id': prior['agent_turn_id']}

    @staticmethod
    def validate_activity_focus(c, session_id, child_id, binding):
        if (not isinstance(binding, dict) or set(binding) != {'activity_id', 'version', 'last_turn_id'}
                or VoiceControlIntent.activity_focus(c, session_id, child_id) != binding):
            raise ValueError('CONTROL_ACTIVITY_FOCUS_CHANGED')
        return dict(c.execute('SELECT * FROM companion_activity WHERE activity_id=?', (binding['activity_id'],)).fetchone())

    @staticmethod
    def activity_arguments(activity_type):
        # These are invitations/instructions, not a claim of model-generated content.
        choices = {
            'interactive_story': ('共同创作', '我们一起编一个新故事。你想让谁当主角？'),
            'word_chain': ('词语接龙', '我们玩一小轮词语接龙。你想从哪个词开始？'),
            'riddle': ('一起猜谜', '我们来玩一小轮猜谜。你想猜生活用品还是自然事物？'),
            'movement': ('伸展', '先离开屏幕，站稳后轻轻伸展胳膊。不舒服就停下，准备好了告诉我。'),
            'tidy_challenge': ('整理桌面', '我们整理一下桌面。先选一小块地方，你想先收哪一块？'),
        }
        if not isinstance(activity_type, str) or activity_type not in choices:
            raise ValueError('CONTROL_INTENT_INVALID')
        topic, opening = choices[activity_type]
        return {'activity_type': activity_type, 'topic': topic, 'opening': opening}

    def __init__(self, database, repository, gateway, settings):
        self.database, self.repository = database, repository
        self.gateway, self.settings = gateway, settings

    def initialize(self):
        with self.database.connect() as c:
            c.execute('''CREATE TABLE IF NOT EXISTS voice_control_intent_request (
                request_key TEXT PRIMARY KEY,
                child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
                session_id TEXT NOT NULL REFERENCES daily_session(session_id) ON DELETE CASCADE,
                fingerprint TEXT NOT NULL, binding_json TEXT NOT NULL, status TEXT NOT NULL,
                decision_json TEXT, created_at TEXT NOT NULL, completed_at TEXT,
                execution_receipt_json TEXT)''')
            columns = {row['name'] for row in c.execute('PRAGMA table_info(voice_control_intent_request)')}
            if 'execution_receipt_json' not in columns:
                c.execute('ALTER TABLE voice_control_intent_request ADD COLUMN execution_receipt_json TEXT')
            c.commit()
        from .break_control import BreakControl
        BreakControl(self.database).initialize()
        # Regular semantic requests snapshot both catalogs even outside app startup.
        # Prepare their schemas here, never inside a borrowed request transaction.
        from .controlled_content import ControlledContentService
        from .controlled_game import ControlledGameService
        ControlledContentService(self.database)
        ControlledGameService(self.database)

    @staticmethod
    def direct_document_stop(transcript):
        from .voice_controls import is_direct_voice_control
        # Only a direct first clause is authority. Quoted/conditional commands
        # and a later retraction cannot be rescued by a substring match.
        if re.search(r'[“”"「」『』]|(?:如果|假如|要是|这句话|只是题目|我在读题|他说|她说|老师说|你说过|不对|说错|先别|别停|不要停|不用停|撤回|取消刚才)', transcript):
            return False
        first = re.split(r'[，。！,!.；;\n]', transcript, maxsplit=1)[0]
        return is_direct_voice_control(first, ('暂停高拍仪', '停止书写监控', '高拍仪先停一下', '关闭高拍仪', '停止高拍仪'))

    @staticmethod
    def document_stop_question(transcript):
        if not VoiceControlIntent.direct_document_stop(transcript):
            return None
        separator = re.search(r'[，。！,!.；;\n]', transcript)
        if not separator:
            return None
        start = separator.end()
        while start < len(transcript) and transcript[start].isspace():
            start += 1
        end = len(transcript.rstrip())
        return {'start': start, 'end': end, 'request_quote': transcript[start:end]} if start < end else None

    def accept_document_stop(self, session_id, child_id, action_id, transcript, binding, *, connection=None):
        from .agent_runtime import InvalidAgentPlanError
        if not self.direct_document_stop(transcript):
            raise InvalidAgentPlanError('ANSWER_CONTROL_SCOPE_CHANGED')
        key = hashlib.sha256(('voice-control:' + action_id).encode()).hexdigest()
        fingerprint = hashlib.sha256(json.dumps([child_id, session_id, transcript], ensure_ascii=False).encode()).hexdigest()
        decision = {'status': 'control', 'control': 'document.pause', 'origin': 'explicit_local_document_stop'}
        with (nullcontext(connection) if connection is not None else self.database.connect()) as c:
            if connection is None:
                c.execute('BEGIN IMMEDIATE')
            elif not c.in_transaction:
                raise InvalidAgentPlanError('ANSWER_CONTROL_TRANSACTION_REQUIRED')
            if not self.scope_valid(c, session_id, child_id, action_id, require_cloud=False):
                raise InvalidAgentPlanError('ANSWER_CONTROL_SCOPE_CHANGED')
            old = c.execute('SELECT * FROM voice_control_intent_request WHERE request_key=?', (key,)).fetchone()
            if old:
                if old['fingerprint'] != fingerprint or old['status'] != 'completed' or json.loads(old['decision_json']) != decision:
                    raise InvalidAgentPlanError('ANSWER_CONTROL_SCOPE_CHANGED')
                if json.loads(old['binding_json']).get('pending_evaluation') != binding.get('pending_evaluation'):
                    raise InvalidAgentPlanError('PENDING_EVALUATION_CONTROL_REQUEST_CONFLICT')
                binding = json.loads(old['binding_json'])
            else:
                question = None if binding.get('pending_evaluation') else self.document_stop_question(transcript)
                if question:
                    monitor = c.execute('SELECT monitor_id,generation,status FROM document_monitor_session WHERE child_id=? AND session_id=?',
                                        (child_id, session_id)).fetchone()
                    revision = c.execute('''SELECT revision_id,artifact_id,content_sha256,created_at FROM document_page_revision
                        WHERE monitor_id=? AND child_id=? AND session_id=? ORDER BY revision_index DESC LIMIT 1''',
                        (monitor['monitor_id'], child_id, session_id)).fetchone() if monitor else None
                    binding = {**binding, 'document_stop_followup': {**question, 'version': 1,
                        'monitor_id': monitor['monitor_id'] if monitor else None,
                        'paused_generation': monitor['generation'] + (monitor['status'] != 'paused') if monitor else None,
                        'revision': dict(revision) if revision else None}}
                c.execute('''INSERT INTO voice_control_intent_request
                    (request_key,child_id,session_id,fingerprint,binding_json,status,decision_json,created_at,completed_at)
                    VALUES(?,?,?,?,?,'completed',?,?,?)''',
                    (key,child_id,session_id,fingerprint,json.dumps(binding),json.dumps(decision),utc_now(),utc_now()))
            if connection is None:
                c.commit()
        return {**decision, 'binding': binding, 'request_key': key}

    def accept_verified_break_plan(self, *, session_id, child_id, action_id, transcript, control, binding, call_id):
        from .agent_runtime import InvalidAgentPlanError
        fingerprint = hashlib.sha256(json.dumps([child_id, session_id, transcript], ensure_ascii=False).encode()).hexdigest()
        key = hashlib.sha256(('voice-control:' + action_id).encode()).hexdigest()
        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            call = c.execute('''SELECT 1 FROM model_call WHERE call_id=? AND child_id=? AND session_id=?
                AND purpose='agent_plan' AND status='completed' AND verification_status='verified' AND safety_status='passed' ''',
                (call_id, child_id, session_id)).fetchone()
            if (control not in {'break.begin', 'break.resume'} or not call
                    or not self.scope_valid(c, session_id, child_id, action_id)):
                raise InvalidAgentPlanError('ANSWER_CONTROL_SCOPE_CHANGED')
            decision = {'status': 'control', 'control': control, 'origin': 'verified_agent_plan'}
            old = c.execute('SELECT * FROM voice_control_intent_request WHERE request_key=?', (key,)).fetchone()
            if old:
                if old['fingerprint'] != fingerprint or old['status'] != 'completed' or json.loads(old['decision_json']) != decision:
                    raise InvalidAgentPlanError('ANSWER_CONTROL_SCOPE_CHANGED')
                binding = json.loads(old['binding_json'])
            else:
                c.execute('''INSERT INTO voice_control_intent_request
                    (request_key,child_id,session_id,fingerprint,binding_json,status,decision_json,created_at,completed_at)
                    VALUES (?,?,?,?,?,'completed',?,?,?)''',
                    (key, child_id, session_id, fingerprint, json.dumps(binding), json.dumps(decision), utc_now(), utc_now()))
            c.commit()
        return {**decision, 'request_key': key, 'binding': binding}

    def accept_planned_task_start(self, *, session_id, child_id, action_id, transcript, binding, call_id, selector):
        from .task_start_binding import TaskStartBinding, TaskStartBindingError
        from .task_start_semantics import verify_plan_selection
        key = hashlib.sha256(('voice-control:' + action_id).encode()).hexdigest()
        fingerprint = hashlib.sha256(json.dumps([child_id, session_id, transcript], ensure_ascii=False).encode()).hexdigest()
        decision = {'status': 'control', 'control': 'task.start', 'origin': 'verified_agent_plan',
                    'start_selector': selector, 'model_call_id': call_id}
        resolved = {**binding, 'task_start': TaskStartBinding.semantic_start_target(binding['task_start'], selector)}
        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            if not self.scope_valid(c, session_id, child_id, action_id):
                raise TaskStartBindingError('TASK_START_PLAN_SCOPE_CHANGED')
            old = c.execute('SELECT * FROM voice_control_intent_request WHERE request_key=?', (key,)).fetchone()
            if old:
                try:
                    prior_decision = json.loads(old['decision_json'])
                    prior_binding = json.loads(old['binding_json'])
                    prior_selector = prior_decision['start_selector']
                    prior_call_id = prior_decision['model_call_id']
                except (ValueError, TypeError, KeyError):
                    raise TaskStartBindingError('TASK_START_PLAN_RECEIPT_CHANGED') from None
                if (old['fingerprint'] != fingerprint or old['status'] != 'completed'
                        or prior_decision.get('status') != 'control'
                        or prior_decision.get('control') != 'task.start'
                        or prior_decision.get('origin') != 'verified_agent_plan'):
                    raise TaskStartBindingError('TASK_START_PLAN_RECEIPT_CHANGED')
                # An exact action replay keeps the first verified selection and
                # frozen task source. A later model call cannot rewrite it after
                # a downstream admission failure or process restart.
                verify_plan_selection(c, session_id, child_id, action_id,
                    prior_call_id, transcript, prior_selector)
                c.commit()
                return {**prior_decision, 'binding': prior_binding, 'request_key': key}
            else:
                verify_plan_selection(c, session_id, child_id, action_id, call_id, transcript, selector)
                c.execute('''INSERT INTO voice_control_intent_request
                    (request_key,child_id,session_id,fingerprint,binding_json,status,decision_json,created_at,completed_at)
                    VALUES (?,?,?,?,?,'completed',?,?,?)''',
                    (key, child_id, session_id, fingerprint, json.dumps(resolved), json.dumps(decision), utc_now(), utc_now()))
            c.commit()
        return {**decision, 'binding': resolved, 'request_key': key}

    @staticmethod
    def recorded_execution(c, turn_id, control, run_id):
        from .agent_runtime import InvalidAgentPlanError
        row = c.execute('''SELECT t.child_id,t.session_id,t.transcript,p.context_manifest_json
            FROM agent_turn t JOIN agent_plan p ON p.agent_turn_id=t.agent_turn_id
            WHERE t.agent_turn_id=? AND p.child_id=t.child_id AND p.session_id=t.session_id''', (turn_id,)).fetchone()
        if not row:
            return None
        binding = json.loads(row['context_manifest_json']).get('voice_control_binding')
        if not binding:
            return None
        request = c.execute('SELECT * FROM voice_control_intent_request WHERE request_key=?', (binding['request_key'],)).fetchone()
        if not request or not request['execution_receipt_json']:
            return None
        receipt = json.loads(request['execution_receipt_json'])
        fingerprint = hashlib.sha256(json.dumps([row['child_id'], row['session_id'], row['transcript']], ensure_ascii=False).encode()).hexdigest()
        if (receipt.get('agent_turn_id') != turn_id or receipt.get('control') != control
                or receipt.get('run_id') != run_id or request['fingerprint'] != fingerprint
                or request['child_id'] != row['child_id'] or request['session_id'] != row['session_id']
                or binding['control'] != control or binding['targets'].get('exam_run_id') != run_id):
            raise InvalidAgentPlanError('ANSWER_CONTROL_SCOPE_CHANGED')
        return receipt

    @staticmethod
    def inspect_execution(c, turn_id, *, allow_planned=False, _completing_help=False):
        from .realtime_native_sources import inspect_control
        native=inspect_control(c,turn_id,allow_planned=allow_planned)
        if native is not None:return native
        from .agent_runtime import InvalidAgentPlanError, AgentRuntimeStore
        row = c.execute('''SELECT t.*,p.context_manifest_json,p.status AS plan_status,
            s.status AS step_status,s.capability_name,s.arguments_json,s.idempotency_key
            FROM agent_turn t JOIN agent_plan p ON p.agent_turn_id=t.agent_turn_id
            JOIN agent_step s ON s.plan_id=p.plan_id AND s.step_index=0
            WHERE t.agent_turn_id=? AND p.child_id=t.child_id AND p.session_id=t.session_id''', (turn_id,)).fetchone()
        if not row:
            return None
        if json.loads(row['action_json']).get('local_recovery_control'):
            from .local_voice_recovery import LocalVoiceRecovery
            return LocalVoiceRecovery.inspect_execution(c,turn_id,allow_planned=allow_planned)
        manifest = json.loads(row['context_manifest_json'])
        if manifest.get('local_controlled_launch_cancel'):
            from .controlled_local_cancel import inspect_execution
            return inspect_execution(c, row, manifest, allow_planned=allow_planned)
        if manifest.get('pending_evaluation'):
            from .pending_evaluation_control import inspect_bound_execution
            local_control = inspect_bound_execution(c, row, manifest,allow_planned=allow_planned)
            if local_control is not None:
                return local_control
        frozen = manifest.get('voice_control_binding')
        if not frozen:
            return None

        def require(valid):
            if not valid:
                raise InvalidAgentPlanError('ANSWER_CONTROL_SCOPE_CHANGED')

        receipt = c.execute('SELECT * FROM voice_control_intent_request WHERE request_key=?', (frozen['request_key'],)).fetchone()
        require(receipt and receipt['child_id'] == row['child_id']
                and receipt['session_id'] == row['session_id'])
        fingerprint = hashlib.sha256(json.dumps([row['child_id'], row['session_id'], row['transcript']], ensure_ascii=False).encode()).hexdigest()
        require(receipt['fingerprint'] == fingerprint)
        result, binding = json.loads(receipt['decision_json']), json.loads(receipt['binding_json'])
        control = result.get('control')
        require(result['status'] == 'control' and control in CONTROLS
                and control == frozen['control'] and binding == frozen['targets'])
        action = json.loads(row['action_json'])
        require(action.get('type') == CONTROLS[control][0])
        if control.startswith('native_writing.'):
            from .native_writing_control import validate
            require(action.get('native_writing_target')==binding.get('native_writing'))
            validate(c,row['session_id'],row['child_id'],binding.get('native_writing'))
        from .agent_runtime import CapabilityRegistry
        phase = (binding.get('break') or {}).get('state', 'study')
        require(CapabilityRegistry().owner_for_action_type(action['type'], phase, arguments=action).name == row['capability_name'])
        arguments = json.loads(row['arguments_json'])
        require(all(arguments.get(k) == v for k, v in CONTROLS[control][1].items()))
        if control == 'device.connect':
            require(result.get('device') in {'camera', 'document_camera', 'all'}
                    and action.get('device') == result['device']
                    and arguments.get('device') == result['device'])
        if control=='learning.help_current':
            from .learning_question_help import validate
            target=validate(c,row['session_id'],row['child_id'],binding.get('learning_help'))
            expected=dict(question=target['question'],subject=target['subject'],hint_level=1,
                diagnostic_prompt_id=target['prompt_id'],diagnostic_item_version=target['item_version'],
                tutor_request_action_id='learning-help:'+frozen['request_key'])
            require(all(action.get(k)==v and arguments.get(k)==v for k,v in expected.items()))
        if control=='learning.retry_check_coaching':
            from .check_coaching_retry_control import target, explicit
            require(explicit(row['transcript']))
            expected = target(c, row['session_id'], row['child_id'], binding.get('check_coaching_retry'))
            require(all(action.get(k)==v and arguments.get(k)==v for k,v in expected.items()))
        if control=='learning.independent_check':
            from .independent_question_control import target
            try:
                expected=target(c,row['session_id'],row['child_id'],binding.get('learning_help'),turn_id=turn_id)
            except (ValueError,KeyError,TypeError):
                require(False)
            require(all(action.get(k)==v and arguments.get(k)==v for k,v in expected.items()))
            require(VoiceControlIntent.parse(json.dumps(dict(role='control',control=control,
                quote=row['transcript'],confidence=1)),row['transcript']).get('status')=='control')
        if control in {'companion.chat', 'learning_inputs.resume_draft'}:
            require(VoiceControlIntent.parse(json.dumps({'role': 'control', 'control': control,
                'quote': row['transcript'], 'confidence': 1}), row['transcript']).get('status') == 'control')
            if control == 'learning_inputs.resume_draft':
                require(arguments == {'selection_quote': row['transcript']})
                saved = c.execute('SELECT result_json FROM learning_input_command WHERE turn_id=? AND child_id=?',
                                  (turn_id, row['child_id'])).fetchone()
                if saved:
                    return {'control': control, 'targets': binding, 'request_key': frozen['request_key'],
                            'recorded_receipt': json.loads(saved['result_json'])}
            from .learning_input_return import validate
            validate(c, row['child_id'], row['session_id'], binding.get('input_return'), turn_id=turn_id)
            require(binding.get('input_return_consent') == list(VoiceControlIntent.text_consent_version(c) or ()))
        if control == 'task.reschedule':
            require(arguments == {'reason': row['transcript']} and action.get('reason') == row['transcript'])
        if control in INPUT_CONTROLS:
            from .learning_input_voice import arguments as revision_arguments
            expected = revision_arguments(result.get('revision'), row['transcript'])
            require(arguments == expected and all(action.get(k) == v for k, v in expected.items()))
        if control == 'voice.configure_wake_word':
            try:
                expected = VoiceControlIntent.wake_word_arguments(result.get('wake_word'), row['transcript'])
            except ValueError:
                require(False)
            require(arguments == expected and action.get('candidate') == expected.get('candidate'))
        if control == 'companion.start_activity':
            expected = VoiceControlIntent.activity_arguments(result.get('activity_type'))
            require(arguments == expected and all(action.get(k) == v for k, v in expected.items()))
            require(VoiceControlIntent.parse(json.dumps({
                'role': 'control', 'control': control, 'quote': row['transcript'],
                'confidence': 1, 'activity_type': result.get('activity_type')}), row['transcript']).get('status') == 'control')
        recorded = None
        if control.startswith('exam.'):
            recorded = VoiceControlIntent.recorded_execution(c, turn_id, control, binding.get('exam_run_id'))
        elif control in {'task.pause', 'task.resume', 'task.complete', 'task.start', 'task.reschedule'}:
            target = binding.get('task_deferral') or {} if control == 'task.reschedule' else ((binding.get('task_start') or {}).get('item') or {}) if control == 'task.start' else (
                ((binding.get('task_resume') or {}).get('item') or {}) if control == 'task.resume' else binding.get('task') or {})
            if control == 'task.resume':
                from .legacy_listening_review import mapped_target
                target = mapped_target(c, turn_id, target)
            event_type = (('started' if target.get('operation') in {'start','supply'} else 'resumed')
                          if control == 'task.resume' else 'rescheduled' if control == 'task.reschedule' else 'started' if control == 'task.start' else 'completed' if control == 'task.complete' else 'paused')
            event = c.execute('''SELECT e.event_id,e.occurred_at FROM plan_item_event e
                JOIN plan_item pi USING(plan_item_id) JOIN daily_plan dp USING(plan_id)
                WHERE e.client_action_id=? AND e.plan_item_id=? AND e.event_type=?
                AND pi.plan_id=? AND dp.child_id=? AND dp.session_id=?''',
                (row['idempotency_key'], target.get('plan_item_id'), event_type, target.get('plan_id'),
                 row['child_id'], row['session_id'])).fetchone()
            if event:
                recorded = {'control': control, 'agent_turn_id': turn_id, 'plan_item_id': target['plan_item_id'],
                            'status': event_type, 'event_id': event['event_id'], 'recorded_at': event['occurred_at']}
        elif control.startswith('break.'):
            from .break_control import BreakControl
            recorded = BreakControl.recorded(c, row['session_id'], control.split('.')[1], row['idempotency_key'])
        if recorded:
            # This is a historical receipt, never permission to repeat the
            # control after cancellation, a new utterance or privacy revocation.
            return {'control': control, 'targets': binding, 'request_key': frozen['request_key'],
                    'recorded_receipt': recorded}
        states = {'planned', 'running', 'failed'} if allow_planned else {'running'}
        if allow_planned and control == 'companion.chat':
            states.add('waiting_child')
        tool_states = {'completed'} if _completing_help and control=='learning.help_current' else {'pending','failed'}
        require(receipt['status'] == 'completed' and row['plan_status'] in states
                and row['step_status'] == row['plan_status'] and row['tool_status'] in tool_states
                and row['intent'] != 'runtime_continuation')
        local_resume = result.get('origin') == 'explicit_local_break_resume'
        local_choice = result.get('origin') == 'explicit_local_task_resume_selection'
        local_stop = result.get('origin') == 'explicit_local_document_stop'
        local_navigation = result.get('origin') == 'explicit_local_task_navigation'
        if local_navigation:
            from .task_start_binding import TaskStartBinding
            nav = VoiceControlIntent.local_task_navigation(row['transcript'])
            from .task_start_choices import choice_index
            selection = (binding.get('task_start') or {}).get('selection_offer')
            if selection and choice_index(row['transcript']) == selection.get('index'):
                nav = 'selected'
            require((control == 'planning.describe' and nav == 'describe')
                    or (control == 'diagnostic.replay' and nav == 'replay_current'
                        and binding.get('diagnostic') and not binding.get('exam_run_id')
                        and not (binding.get('task_start') or {}).get('interaction_focus')
                        and (binding.get('companion_activity') or {}).get('status') not in {'ready','ambiguous'})
                    or (control == 'task.pause' and nav == 'pause' and binding.get('task')
                        and not binding.get('exam_run_id')
                        and not (binding.get('task_start') or {}).get('interaction_focus')
                        and (binding.get('companion_activity') or {}).get('status') not in {'ready','ambiguous'})
                    or (control == 'task.start' and nav in {'next', 'continue', 'named', 'selected'})
                    or (control == 'task.resume' and nav in {'continue', 'current_continue'}
                        and not binding.get('task') and not binding.get('companion_paused_tasks')
                        and TaskStartBinding.resume_focus_allowed(binding)))
        if local_stop:
            require(control == 'document.pause' and VoiceControlIntent.direct_document_stop(row['transcript']))
            monitor = c.execute('SELECT monitor_id FROM document_monitor_session WHERE session_id=? AND child_id=?',
                (row['session_id'], row['child_id'])).fetchone()
            require(binding.get('document_stop') == {'monitor_id': monitor[0] if monitor else None})
        denied_task, _ = VoiceControlIntent.task_negation_scope(row['transcript'], control, result.get('selector'))
        require(not denied_task)
        if control == 'task.resume' and not local_choice:
            selection = result.get('selector')
            require(isinstance(selection, dict) and set(selection) == {'subject', 'task_kind', 'relative_order'}
                    and (binding.get('task_resume') or {}).get('selector') == selection)
        if local_resume:
            require(control == 'break.resume' and VoiceControlIntent.explicit_break_resume(row['transcript']))
        if local_choice:
            require(control == 'task.resume' and VoiceControlIntent.resume_choice_valid(
                c, row['session_id'], row['child_id'], row['transcript'], result, binding))
        if local_navigation and control=='task.resume':
            from .task_start_binding import TaskStartBinding
            current_focus = TaskStartBinding.interaction_focus(c,row['session_id'],row['child_id'])
            own = ((binding.get('task_resume') or {}).get('foundation_remediation_focus')
                   or (binding.get('task_resume') or {}).get('mistake_review_focus'))
            require(not current_focus or bool(own and own['interaction_focus'] == current_focus))
        if local_navigation and nav in {'pause','current_continue','replay_current'}:
            from .task_start_binding import TaskStartBinding
            if control != 'task.resume':
                require(not TaskStartBinding.interaction_focus(c, row['session_id'], row['child_id']))
            require(not c.execute("SELECT 1 FROM specialized_exam_run WHERE session_id=? AND child_id=? AND status IN ('active','paused')",
                (row['session_id'], row['child_id'])).fetchone())
            require(not c.execute("SELECT 1 FROM pet_vocabulary_prompt_session WHERE session_id=? AND child_id=? AND status='active'",
                (row['session_id'], row['child_id'])).fetchone())
            from .companion_activity_outcome import SOURCES
            require(not any(c.execute(f"SELECT 1 FROM {table} WHERE session_id=? AND child_id=? AND status='active'",
                (row['session_id'], row['child_id'])).fetchone() for table, _ in SOURCES.values()))
            if control == 'task.pause':
                target = binding['task']
                expected = next((item for item in (binding.get('task_start') or {}).get('items', [])
                                 if item['plan_item_id'] == target['plan_item_id']), None)
                current = next((item for item in TaskStartBinding.start_snapshot(
                    c, row['session_id'], row['child_id'])['items']
                    if item['plan_item_id'] == target['plan_item_id']), None)
                identity = ('plan_item_id','plan_id','plan_version','source_type','source_ref',
                            'subject','title','task_type','position','original_source_version')
                require(expected and current and current['status'] == 'active'
                        and all(current.get(key) == expected.get(key) for key in identity))
            if binding.get('diagnostic'):
                from .diagnostic_prompt import DiagnosticPromptService, _version
                target = binding['diagnostic']
                prompt = c.execute("SELECT * FROM diagnostic_prompt WHERE prompt_id=? AND child_id=? AND origin_session_id=? AND status='waiting_answer'",
                    (target['prompt_id'], row['child_id'], row['session_id'])).fetchone()
                item = DiagnosticPromptService._item(c, row['child_id'], prompt['diagnostic_item_id']) if prompt else None
                require(prompt and item and prompt['item_version'] == target['item_version'] == _version(item)
                        and DiagnosticPromptService._scheduled_source_reason(c, prompt) is None)
        require(VoiceControlIntent.scope_valid(c, row['session_id'], row['child_id'], row['client_action_id'],
                                              require_cloud=not (local_resume or local_choice or local_stop or local_navigation)))
        if control == 'task.reschedule':
            from .task_deferral_voice import target_current
            require(target_current(c, row['child_id'], row['session_id'], binding.get('task_deferral'), row['transcript'], before_turn=turn_id,
                capture_reference=(action.get('voice_input_binding') or {}).get('task_choice_reference')))
        if control == 'companion.activity_feedback':
            expected = {**result['activity_feedback'], 'response_quote': row['transcript']}
            require(arguments == expected and action.get('operation') == expected['operation'])
            require(json.loads(row['context_manifest_json']).get('companion_activity_outcome_binding') ==
                    (binding.get('companion_activity') or {}).get('binding'))
        if action.get('voice_input_id'):
            capture = AgentRuntimeStore.resolve_captured_voice(c, session_id=row['session_id'], child_id=row['child_id'],
                voice_input_id=action['voice_input_id'], transcript=row['transcript'])
            require(AgentRuntimeStore.voice_capture_snapshot(capture) == action.get('voice_input_binding'))
        if control in EXTERNAL_CONTROLS:
            from .controlled_voice import action_fields, validate
            packet = binding.get('controlled')
            try:
                validate(c, row['child_id'], row['session_id'], control, packet, turn_id=turn_id)
            except ValueError:
                require(False)
            expected = action_fields(control, packet)
            require(all(action.get(k) == v and arguments.get(k) == v for k, v in expected.items()))
            parsed = {'role': 'control', 'control': control, 'quote': row['transcript'], 'confidence': 1}
            if control.endswith('.play'):
                parsed['query'] = packet['query']
            require(VoiceControlIntent.parse(json.dumps(parsed), row['transcript']).get('status') == 'control')
        if control == 'companion.start_activity' and binding.get('diagnostic'):
            from .diagnostic_prompt import DiagnosticPromptService, _version
            target = binding['diagnostic']
            prompt = c.execute('''SELECT * FROM diagnostic_prompt WHERE prompt_id=?
                AND origin_session_id=? AND child_id=? AND status='waiting_answer' ''',
                (target.get('prompt_id'), row['session_id'], row['child_id'])).fetchone()
            require(prompt and prompt['item_version'] == target.get('item_version'))
            item = DiagnosticPromptService._item(c, row['child_id'], prompt['diagnostic_item_id'])
            require(item and _version(item) == prompt['item_version']
                    and DiagnosticPromptService._scheduled_source_reason(c, prompt) is None)
        if control == 'task.resume':
            from .task_start_binding import TaskStartBinding
            require(not VoiceControlIntent.resume_deferred(row['transcript'], result.get('selector')))
            require(not VoiceControlIntent.activity_resume_conflict(binding, binding.get('task_resume') or {}, result.get('selector')))
            TaskStartBinding.validate_resume_target(c, row['session_id'], row['child_id'], binding.get('task_resume'))
            if (binding.get('task_resume') or {}).get('daily_supply_offer'):
                from .oral_daily_reminder import validate
                validate(c, row['child_id'], row['session_id'], binding['task_resume'], datetime.now(timezone.utc), turn_id)
        elif control == 'task.start':
            from .task_start_binding import TaskStartBinding
            semantic = (binding.get('task_start') or {}).get('semantic_start')
            if semantic or result.get('start_selector'):
                from .task_start_semantics import validate
                require(bool(semantic) and semantic['selector'] == result.get('start_selector'))
                validate(semantic['selector'], row['transcript'])
                if result.get('origin') == 'verified_agent_plan' or result.get('model_call_id'):
                    from .task_start_semantics import verify_plan_selection
                    verify_plan_selection(c, row['session_id'], row['child_id'], row['client_action_id'],
                        result.get('model_call_id'), row['transcript'], semantic['selector'])
            target = TaskStartBinding.validate_start_snapshot(c, row['session_id'], row['child_id'], binding.get('task_start'), turn_id=turn_id)
            require(manifest.get('task_start_arrival') == binding['task_start']
                    and manifest.get('task_start_binding') == {**{k: target[k] for k in ('plan_item_id','source_type','source_ref')}, 'operation':'start'})
        elif control.startswith('task.') and control != 'task.reschedule':
            target = binding.get('task') or {}
            current = c.execute('''SELECT pi.plan_item_id,pi.plan_id,dp.version,pi.status FROM plan_item pi JOIN daily_plan dp USING(plan_id)
                WHERE pi.plan_item_id=? AND dp.status='active' AND dp.child_id=? AND dp.session_id=?''',
                (target.get('plan_item_id'), row['child_id'], row['session_id'])).fetchone()
            completed_after_shutdown=False
            if control=='task.complete' and current and current['status']=='paused':
                from .writing_daily_followup import completion_shutdown_receipt
                completed_after_shutdown=completion_shutdown_receipt(c,turn_id)
            require(current and current['plan_id'] == target.get('plan_id')
                    and (current['status'] == 'active' or completed_after_shutdown) and current['version'] == target.get('version'))
        if control.startswith('break.'):
            from .break_control import BreakControl
            if control == 'break.resume' and binding.get('input_return'):
                from .learning_input_return import validate
                validate(c, row['child_id'], row['session_id'], binding['input_return'], turn_id=turn_id)
            current = BreakControl.snapshot(c, row['session_id'], row['child_id'])
            require(current == binding.get('break') and current['status'] == 'ready'
                    and current['state'] == ('study' if control == 'break.begin' else 'break'))
        if control.startswith('exam.'):
            run = c.execute('SELECT status FROM specialized_exam_run WHERE run_id=? AND session_id=? AND child_id=?',
                (binding.get('exam_run_id'), row['session_id'], row['child_id'])).fetchone()
            require(run and run['status'] in ({'paused'} if control == 'exam.resume' else {'active', 'paused'}))
            require(action.get('exam_run_id') == binding['exam_run_id'])
        if control.startswith('diagnostic.'):
            target = binding.get('diagnostic') or {}
            current = c.execute('''SELECT item_version FROM diagnostic_prompt WHERE prompt_id=? AND origin_session_id=?
                AND child_id=? AND status='waiting_answer' ''', (target.get('prompt_id'), row['session_id'], row['child_id'])).fetchone()
            require(current and current[0] == target.get('item_version'))
            require(action.get('diagnostic_prompt_id') == target.get('prompt_id')
                    and action.get('diagnostic_item_version') == target.get('item_version'))
        if control.startswith('pet_vocabulary.'):
            target = binding.get('vocabulary') or {}
            current = c.execute('''SELECT card_version FROM pet_vocabulary_prompt_session WHERE prompt_session_id=?
                AND session_id=? AND child_id=? AND status='active' AND invalidated_reason IS NULL''',
                (target.get('prompt_session_id'), row['session_id'], row['child_id'])).fetchone()
            require(current and current[0] == target.get('card_version'))
            require(action.get('vocabulary_prompt_id') == target.get('prompt_session_id')
                    and action.get('vocabulary_card_version') == target.get('card_version'))
        if control.startswith('tutor.'):
            target = binding.get('tutor') or {}
            current = c.execute('SELECT source_version FROM tutor_playback_checkpoint WHERE playback_id=? AND child_id=? AND session_id=?',
                (target.get('playback_id'), row['child_id'], row['session_id'])).fetchone()
            require(current and current[0] == target.get('source_version'))
            require(action.get('tutor_playback_id') == target.get('playback_id')
                    and action.get('tutor_source_version') == target.get('source_version'))
            if allow_planned:
                require(VoiceControlIntent.tutor_target_current(c, row['session_id'], row['child_id'], target))
        return {'control': control, 'targets': binding, 'request_key': frozen['request_key']}

    @staticmethod
    def text_eligible(text, names, local_sources, *, revision_command=False):
        if (len(text) > 2000 or sensitive_categories(text)
                or sanitize_cloud_text(text, names).text != text):
            return False
        # Do not turn a locally restricted quotation into a classifier input.
        compact = re.sub(r'\s+', '', text)
        quote_text = text
        # A bounded name literal may reach semantic classification; this
        # alone never authorizes renaming or bypasses source matching below.
        literal = re.search(r'(?:唤醒词|唤醒名字|唤醒名).{0,8}?[“「『"]([^“”「」『』"]{2,12})[”」』"]', text)
        if literal:
            try:
                VoiceControlIntent.wake_word_arguments({'candidate': literal[1]}, text)
                quote_text = VoiceControlIntent.wake_word_request_text(text, literal[1])
            except ValueError:
                pass
        from .controlled_voice import media_literal_request_text
        from .learning_input_intake import title_literal_text
        if (re.search(r'[“”「」『』"]', quote_text) and media_literal_request_text(text) is None and title_literal_text(text) is None
                or re.search(r'题(?:目|干)(?:是|写着|说)', text)):
            return False
        prefix=r'^(?:第[一二两三四五六七八九十百零\d]+段(?:回答)?|上一段(?:回答)?)(?:我)?改(?:成|为)[：:]'
        compare=re.sub(prefix,'',compact) if revision_command else compact
        for value in local_sources:
            if not value:
                continue
            source=re.sub(r'\s+','',str(value))
            core=re.sub(prefix,'',source) if revision_command else source
            # Public command syntax is not an old answer. Short answer cores
            # remain protected even after removing that syntax for comparison.
            short_core=core.rstrip('。.!！？?；;')
            if core!=source and short_core and len(short_core)<8 and short_core in compare:
                return False
            if len(core)>=8 and any(core[i:i+8] in compare for i in range(len(core)-7)):
                return False
        return True

    @staticmethod
    def scope_valid(c, session_id, child_id, action_id, *, require_cloud=True):
        from .voice_scope import scope_valid

        return scope_valid(c, session_id, child_id, action_id, require_cloud=require_cloud)

    @staticmethod
    def text_consent_version(c):
        from .voice_scope import text_consent_version

        return text_consent_version(c)

    @staticmethod
    def assert_current_input(c, session_id, child_id, action_id, transcript, voice_capture=None):
        from .agent_runtime import AgentRuntimeStore
        if not VoiceControlIntent.scope_valid(c, session_id, child_id, action_id):
            raise ValueError('CONTROL_SCOPE_CHANGED')
        arrival = c.execute('SELECT * FROM learning_input_arrival WHERE client_action_id=?', (action_id,)).fetchone()
        if (not arrival or (arrival['session_id'], arrival['child_id'], arrival['utterance']) !=
                (session_id, child_id, transcript)):
            raise ValueError('CONTROL_ARRIVAL_CHANGED')
        if c.execute("SELECT 1 FROM sqlite_master WHERE name='deletion_job'").fetchone() and c.execute(
                """SELECT 1 FROM deletion_job WHERE child_id=? AND target_type='child_scope'
                AND scope IN ('learning','knowledge','media','tutor','all_history') AND status!='completed'""", (child_id,)).fetchone():
            raise ValueError('CONTROL_DELETION_PENDING')
        if voice_capture:
            current = AgentRuntimeStore.resolve_captured_voice(c, session_id=session_id, child_id=child_id,
                voice_input_id=voice_capture['voice_input_id'], transcript=transcript)
            if AgentRuntimeStore.voice_capture_snapshot(current) != AgentRuntimeStore.voice_capture_snapshot(voice_capture):
                raise ValueError('CONTROL_VOICE_CHANGED')

    @staticmethod
    def spoken_answer_target(c, session_id, child_id, action_id, transcript, request_key):
        from .local_supply import validate_item
        from .open_response_profile import profile_for_item
        if request_key != hashlib.sha256(('voice-control:' + action_id).encode()).hexdigest():
            return None
        row = c.execute('SELECT * FROM voice_control_intent_request WHERE request_key=?', (request_key,)).fetchone()
        fingerprint = hashlib.sha256(json.dumps([child_id,session_id,transcript],ensure_ascii=False).encode()).hexdigest()
        if (not row or row['status'] != 'completed' or row['fingerprint'] != fingerprint
                or (row['session_id'],row['child_id']) != (session_id,child_id)
                or json.loads(row['decision_json'] or '{}') != {
                    'status':'non_control','role':'answer','role_version':'spoken_answer_role_v1'}):
            return None
        binding = json.loads(row['binding_json']).get('diagnostic') or {}
        prompt = c.execute("""SELECT * FROM diagnostic_prompt WHERE prompt_id=? AND item_version=?
            AND child_id=? AND origin_session_id=? AND status='waiting_answer'""",
            (binding.get('prompt_id'),binding.get('item_version'),child_id,session_id)).fetchone()
        if not prompt:
            return None
        try:
            frozen = json.loads(prompt['item_json'])
            validate_item(c,child_id,frozen['diagnostic_item_id'])
            item = c.execute('SELECT * FROM diagnostic_item WHERE diagnostic_item_id=?',(frozen['diagnostic_item_id'],)).fetchone()
            node = c.execute('SELECT * FROM knowledge_node WHERE knowledge_node_id=?',(item['knowledge_node_id'],)).fetchone()
            profile = profile_for_item(dict(item),dict(node))
        except (ValueError, KeyError, TypeError):
            return None
        return binding if profile['response_mode'] == 'spoken_explanation' else None

    @staticmethod
    def tutor_target_current(c, session_id, child_id, target):
        from .tutor_playback import TutorPlaybackService, TutorPlaybackError
        row = c.execute('''SELECT * FROM tutor_playback_checkpoint WHERE playback_id=?
            AND session_id=? AND child_id=? AND status IN ('paused','playing','playback_finished')''',
            (target.get('playback_id'), session_id, child_id)).fetchone()
        if not row or row['source_version'] != target.get('source_version'):
            return False
        try:
            version, _ = TutorPlaybackService._source(c, {'session_id': session_id, 'child_id': child_id}, row['turn_id'])
        except TutorPlaybackError:
            return False
        return version == target['source_version']

    @staticmethod
    def tutor_question_current(c, session_id, child_id, action_id, transcript, result, target):
        key = hashlib.sha256(('voice-control:' + action_id).encode()).hexdigest()
        if result.get('request_key') != key or not VoiceControlIntent.scope_valid(c, session_id, child_id, action_id):
            return False
        row = c.execute('SELECT * FROM voice_control_intent_request WHERE request_key=?', (key,)).fetchone()
        fingerprint = hashlib.sha256(json.dumps([child_id, session_id, transcript], ensure_ascii=False).encode()).hexdigest()
        projected = bool((result.get('binding') or {}).get('tutor_question_projection'))
        from .tutor_question_projection import ROLE_VERSION
        decision = {'status': 'non_control', 'role': 'question',
                    'role_version': ROLE_VERSION if projected else 'tutor_question_role_v1'}
        if (not row or row['status'] != 'completed' or row['fingerprint'] != fingerprint
                or (row['session_id'], row['child_id']) != (session_id, child_id)
                or json.loads(row['decision_json'] or '{}') != decision):
            return False
        binding = json.loads(row['binding_json'])
        consent = VoiceControlIntent.text_consent_version(c)
        return (binding == result.get('binding') and binding.get('tutor') == target
                and bool(consent) and binding.get('tutor_question_text_consent') == list(consent)
                and (not projected or VoiceControlIntent.tutor_projection_current(c, session_id, child_id, transcript, binding))
                and VoiceControlIntent.tutor_target_current(c, session_id, child_id, target))

    @staticmethod
    def tutor_projection_current(c, session_id, child_id, transcript, binding):
        from .tutor_question_projection import project
        from .agent_runtime import AgentRuntimeStore, InvalidAgentPlanError
        projection = project(transcript)
        frozen = binding.get('tutor_question_projection')
        consent = VoiceControlIntent.text_consent_version(c)
        if (not projection or not isinstance(frozen, dict) or not consent
                or consent[1] != 1 or consent[3] != 'parent_ui' or not consent[4]):
            return False
        reference = frozen.get('question_reference')
        if frozen != {**projection[1], 'question_reference': reference}:
            return False
        try:
            AgentRuntimeStore.validate_captured_question(c, session_id, child_id, reference)
        except InvalidAgentPlanError:
            return False
        return VoiceControlIntent.tutor_target_current(c, session_id, child_id, binding.get('tutor') or {})

    @staticmethod
    def resume_choice_index(transcript):
        match = re.fullmatch(r'(?:选|继续|恢复)?第([一二三四五六七八九十]|[1-9][0-9]?)项[。！!\s]*', transcript)
        if not match:
            return None
        value = match[1]
        return (int(value) if value.isdigit() else '一二三四五六七八九十'.index(value) + 1) - 1

    @staticmethod
    def resume_choice_valid(c, session_id, child_id, transcript, result, binding):
        prior = c.execute('''SELECT t.action_json,t.tool_status,p.status AS plan_status,
            p.context_manifest_json,s.status AS step_status FROM agent_turn t
            JOIN agent_plan p ON p.agent_turn_id=t.agent_turn_id
            JOIN agent_step s ON s.plan_id=p.plan_id AND s.step_index=0
            WHERE t.agent_turn_id=? AND t.session_id=? AND t.child_id=?
            AND p.session_id=t.session_id AND p.child_id=t.child_id''',
            (result.get('selection_turn_id'), session_id, child_id)).fetchone()
        if (not prior or prior['tool_status'] != 'completed' or prior['step_status'] != 'completed'
                or prior['plan_status'] not in {'completed', 'waiting_child'}):
            return False
        offer = json.loads(prior['context_manifest_json']).get('task_resume_dialogue') or {}
        journal = c.execute('SELECT * FROM voice_control_intent_request WHERE request_key=?',
                            (offer.get('request_key'),)).fetchone()
        index = VoiceControlIntent.resume_choice_index(transcript)
        items = (offer.get('targets') or {}).get('task_resume', {}).get('items', [])
        from .task_start_binding import TaskStartBinding
        TaskStartBinding.validate_resume_offer(c, session_id, child_id,
            {'offer_turn_id': result.get('selection_turn_id'), 'items': items})
        return bool(journal and journal['status'] == 'completed'
            and journal['session_id'] == session_id and journal['child_id'] == child_id
            and json.loads(journal['decision_json']).get('control') == 'task.resume'
            and json.loads(journal['binding_json']) == offer.get('targets')
            and json.loads(prior['action_json']).get('task_resume_choices') == items
            and index is not None and index < len(items)
            and binding.get('task_resume') == {'status': 'ready', 'items': items, 'item': items[index]})

    def select_resume_choice(self, session_id, child_id, action_id, transcript):
        from .agent_runtime import InvalidAgentPlanError
        from .task_start_binding import TaskStartBinding
        if self.resume_choice_index(transcript) is None:
            return None
        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            prior = c.execute('''SELECT agent_turn_id,action_json FROM agent_turn
                WHERE session_id=? AND child_id=? ORDER BY rowid DESC LIMIT 1''', (session_id, child_id)).fetchone()
            if not prior or json.loads(prior['action_json']).get('source') != 'task_resume_clarification':
                return None
            action = json.loads(prior['action_json'])
            items, index = action['task_resume_choices'], self.resume_choice_index(transcript)
            if index >= len(items):
                raise InvalidAgentPlanError('ANSWER_CONTROL_SCOPE_CHANGED')
            binding = {'task_resume': {'status': 'ready', 'items': items, 'item': items[index]}}
            decision = {'status': 'control', 'control': 'task.resume',
                        'origin': 'explicit_local_task_resume_selection', 'selection_turn_id': prior['agent_turn_id']}
            if (not self.resume_choice_valid(c, session_id, child_id, transcript, decision, binding)
                    or not self.scope_valid(c, session_id, child_id, action_id, require_cloud=False)):
                raise InvalidAgentPlanError('ANSWER_CONTROL_SCOPE_CHANGED')
            TaskStartBinding.validate_resume_target(c, session_id, child_id, binding['task_resume'])
            fingerprint = hashlib.sha256(json.dumps([child_id, session_id, transcript], ensure_ascii=False).encode()).hexdigest()
            key = hashlib.sha256(('voice-control:' + action_id).encode()).hexdigest()
            old = c.execute('SELECT * FROM voice_control_intent_request WHERE request_key=?', (key,)).fetchone()
            if old:
                if (old['fingerprint'] != fingerprint or old['status'] != 'completed'
                        or json.loads(old['decision_json']) != decision or json.loads(old['binding_json']) != binding):
                    raise InvalidAgentPlanError('ANSWER_CONTROL_SCOPE_CHANGED')
            else:
                c.execute('''INSERT INTO voice_control_intent_request
                    (request_key,child_id,session_id,fingerprint,binding_json,status,decision_json,created_at,completed_at)
                    VALUES (?,?,?,?,?,'completed',?,?,?)''',
                    (key, child_id, session_id, fingerprint, json.dumps(binding), json.dumps(decision), utc_now(), utc_now()))
            c.commit()
            return {**decision, 'request_key': key, 'binding': binding}

    @staticmethod
    def explicit_break_resume(transcript):
        return bool(re.fullmatch(r'(?:继续学习|开始学习|休息好了(?:[，,]?(?:继续|开始)学习)?)[。！!\s]*', transcript))

    def accept_local_break_resume(self, session_id, child_id, action_id, transcript, binding):
        from .break_control import BreakControl
        fingerprint = hashlib.sha256(json.dumps([child_id, session_id, transcript], ensure_ascii=False).encode()).hexdigest()
        key = hashlib.sha256(('voice-control:' + action_id).encode()).hexdigest()
        decision = {'status': 'control', 'control': 'break.resume', 'origin': 'explicit_local_break_resume'}
        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            current = BreakControl.snapshot(c, session_id, child_id)
            if (not self.explicit_break_resume(transcript) or current != binding.get('break')
                    or current['state'] != 'break' or current['status'] != 'ready'
                    or not self.scope_valid(c, session_id, child_id, action_id, require_cloud=False)):
                return {'status': 'unavailable', 'reason': 'CONTROL_SCOPE_CHANGED'}
            old = c.execute('SELECT * FROM voice_control_intent_request WHERE request_key=?', (key,)).fetchone()
            if old:
                if (old['fingerprint'] != fingerprint or old['status'] != 'completed'
                        or json.loads(old['decision_json'] or '{}') != decision or json.loads(old['binding_json']) != binding):
                    return {'status': 'unavailable', 'reason': 'CONTROL_SCOPE_CHANGED'}
            else:
                c.execute('''INSERT INTO voice_control_intent_request
                    (request_key,child_id,session_id,fingerprint,binding_json,status,decision_json,created_at,completed_at)
                    VALUES (?,?,?,?,?,'completed',?,?,?)''',
                    (key, child_id, session_id, fingerprint, json.dumps(binding), json.dumps(decision), utc_now(), utc_now()))
            c.commit()
        return {**decision, 'request_key': key, 'binding': binding}

    @staticmethod
    def activity_resume_conflict(binding, target, selector):
        if (binding.get('companion_activity') or {}).get('status') not in {'ready', 'ambiguous'}:
            return False
        return (not selector or not any(selector.values())
                or (target.get('item') or {}).get('plan_item_id') in binding.get('companion_paused_tasks', []))

    @staticmethod
    def resume_deferred(transcript, selector):
        clauses = [part.strip() for part in re.split(r'[，,。；;！!]', transcript) if part.strip()]
        delayed = [part for part in clauses if re.search(
            r'(?:明天|后天|以后|改天|等会|稍后).{0,6}(?:再|继续|恢复|做)', part)]
        if not delayed:
            return False
        selected = TASK_SUBJECTS.get((selector or {}).get('subject'))
        # A named different subject/activity can be deferred without deferring
        # an explicit present request. Unscoped delay is still a revocation.
        affirmative = selected and any(selected in part and part not in delayed
            and re.search(r'(?:继续|恢复|接着)', part)
            and not re.search(r'(?:不|别|没|如果|假如|要是)', part) for part in clauses)
        other_scopes = [name for name in TASK_SUBJECTS.values() if name != selected] + ['活动']
        return not (affirmative and all(selected not in part and any(name in part for name in other_scopes)
                                        for part in delayed))

    @staticmethod
    def task_negation_scope(transcript, control, selector):
        """Check a model-selected public subject, never infer a task from words."""
        if control not in {'task.pause', 'task.resume'}:
            return False, False
        clauses = [part.strip() for part in re.split(r'[，,。；;！!]', transcript) if part.strip()]
        negative = re.compile(r'(?:先别|不要|不用|不想|不需要|暂不|别).{0,8}(?:继续|恢复|接着)')
        excluded = [part for part in clauses if negative.search(part)]
        if not excluded:
            return False, False
        if control == 'task.pause':
            return True, False
        subjects = TASK_SUBJECTS
        selected = subjects.get((selector or {}).get('subject'))
        if (not selected or any(selected in part or not any(name in part for name in subjects.values())
                                for part in excluded)):
            return True, False
        # A contrast is permitted only with an explicit, non-negated destination
        # in the original utterance. Its local candidates still must match uniquely.
        affirmative = any(selected in part and part not in excluded
                          and not re.search(r'(?:不|别|没|否|稍后|以后|明天|后天)', part) for part in clauses)
        return not affirmative, affirmative

    @staticmethod
    def wake_word_request_text(transcript, candidate):
        # The model's candidate cannot supply its own request authority by
        # consuming the whole quoted command. Keep ambiguous delimiters intact.
        if not candidate:
            return transcript
        literals = []
        for left, right in (('“', '”'), ('「', '」'), ('『', '』'), ('"', '"'), ("'", "'")):
            literals.extend(re.finditer(re.escape(left + candidate + right), transcript))
        if len(literals) != 1:
            return transcript
        match = literals[0]
        before, after = transcript[:match.start()], transcript[match.end():]
        name = r'(?:唤醒词|唤醒名(?:字)?|名字|称呼)'
        assigns_before = re.search(
            name + r'\s*(?:就)?(?:改(?:成|为)?|换(?:成|为)?|设(?:置)?(?:成|为)?|定为|取为|叫(?:作|做)?|用)\s*$'
            r'|(?:叫你|喊你|称呼你|称你为|你(?:就)?叫)\s*$', before)
        assigns_after = re.fullmatch(
            r'[，,\s]*(?:就)?(?:用|拿)(?:这个(?:名字)?|它)(?:作(?:为)?|当(?:作)?)'
            r'(?:你(?:的)?)?' + name + r'(?:吧|好吗)?[。！!？?\s]*', after)
        if not assigns_before and not assigns_after:
            return transcript
        return before + candidate + after

    @staticmethod
    def wake_word_arguments(value, transcript):
        if not isinstance(value, dict) or set(value) != {'candidate'}:
            raise ValueError('CONTROL_WAKE_WORD_INVALID')
        candidate = value['candidate']
        if candidate is not None and (not isinstance(candidate, str)
                or candidate != candidate.strip() or not 2 <= len(candidate) <= 12
                or candidate not in transcript or re.search(r'[“”「」『』"\'\n\r]', candidate)):
            raise ValueError('CONTROL_WAKE_WORD_INVALID')
        text = VoiceControlIntent.wake_word_request_text(transcript, candidate)
        if re.search(r'[“”「」『』"\']|(?:他说|她说|老师说|假如|假设|如果|什么意思|先别|不要|不用|不想|不需要|别改|没决定|取消|撤回|说错了)', text):
            raise ValueError('CONTROL_WAKE_WORD_NOT_REQUESTED')
        return {} if candidate is None else {'candidate': candidate}

    @staticmethod
    def parse(content, transcript, *, restricted=False, tutor_question=False, independent_dialogue=False, foreground_dialogue=False, pending_dialogue=False):
        if pending_dialogue and not restricted:
            raise ValueError('CONTROL_PENDING_SCOPE_REQUIRED')
        value = json.loads(content)
        roles = {'control', 'answer', 'activity_reply', 'companion_reply', 'question', 'acknowledgement', 'mixed', 'unknown'}
        if ((independent_dialogue or foreground_dialogue) and not restricted and not tutor_question) or pending_dialogue:
            roles.update({'greeting', 'companionship', 'topic_mention'})
        if (not isinstance(value, dict) or set(value) - {'selector', 'start_selector', 'activity_feedback', 'activity_type', 'wake_word', 'query', 'revision', 'device', 'question_scope'} != {'role', 'control', 'quote', 'confidence'}
                or value['role'] not in roles
                or type(value['confidence']) not in (float, int) or not 0 <= value['confidence'] <= 1
                or value['quote'] != transcript):
            raise ValueError('CONTROL_INTENT_INVALID')
        if 'question_scope' in value:
            if (not (foreground_dialogue or pending_dialogue) or (restricted and not pending_dialogue) or tutor_question or value['control'] is not None
                    or value['role'] not in {'question', 'greeting', 'companionship', 'topic_mention'}
                    or value['question_scope'] not in {'independent', 'current', 'unknown'}):
                raise ValueError('CONTROL_QUESTION_SCOPE_INVALID')
        if value['control'] in INPUT_CONTROLS or 'revision' in value:
            from .learning_input_voice import parse
            return parse(value, transcript, restricted)
        external = value['control'] in EXTERNAL_CONTROLS
        if external:
            if restricted:
                return {'status': 'clarify', 'reason': 'CONTROL_PENDING_CAPABILITY_FORBIDDEN'}
            if value['role'] != 'control':
                return {'status': 'clarify', 'reason': 'CONTROL_INTENT_CONFLICT'}
            from .controlled_voice import media_literal_request_text
            external_text = (media_literal_request_text(transcript, value.get('query'))
                             if value['control'].endswith('.play') else None) or transcript
            if re.search(r'[“”「」『』\"\']|(?:假如|假设|如果|他说|她说|老师说|什么意思|是否支持|能否支持|然后|同时|并且|而且)', external_text):
                return {'status': 'clarify', 'reason': 'CONTROL_INTENT_CONFLICT'}
            if value['control'].endswith('.play'):
                query = value.get('query')
                if not isinstance(query, str) or not query.strip() or len(query) > 200 or query not in transcript:
                    return {'status': 'clarify', 'reason': 'CONTROL_INTENT_CONFLICT'}
                if re.search(r'(?:别|不要|不用|不想|不需要|并非|不是要).{0,10}(?:看|玩|开|播|启动)', transcript):
                    return {'status': 'clarify', 'reason': 'CONTROL_INTENT_CONFLICT'}
            elif 'query' in value:
                raise ValueError('CONTROL_INTENT_INVALID')
        elif 'query' in value:
            raise ValueError('CONTROL_INTENT_INVALID')
        activity_type = value.get('activity_type')
        wake_word = value.get('wake_word')
        if value['role'] == 'control' and value['control'] in {'companion.chat', 'learning_inputs.resume_draft'}:
            from .child_safety import classify_child_safety
            from .learning_input_intake import estimate
            if (restricted or classify_child_safety(transcript)
                    or estimate(transcript) is not None
                    or re.fullmatch(r'[\s，,。！!]*(?:确认|是|是的|对|好|好的|嗯|yes|ok|okay)[\s，,。！!]*', transcript, re.I)
                    or re.search(r'[“”「」『』"\']|(?:他说|她说|老师说|假如|假设|如果|什么意思|是否支持|功能|先别|不要|不用|不想|不需要|暂不|不是要|取消|撤回|算了|还没决定)', transcript)):
                return {'status': 'clarify', 'reason': 'INPUT_DIALOGUE_NOT_REQUESTED'}
        if value['role'] == 'control' and value['control'] == 'voice.configure_wake_word':
            if restricted:
                raise ValueError('CONTROL_INTENT_INVALID')
            VoiceControlIntent.wake_word_arguments(wake_word, transcript)
        elif 'wake_word' in value:
            raise ValueError('CONTROL_INTENT_INVALID')
        if value['role'] == 'control' and value['control'] in {'device.status', 'parent.request'} and re.search(
                r'[“”「」『』"]|(?:他说|她说|老师说|假如|假设|如果|什么意思|先别|不要|不用|不想|不需要|取消|撤回)', transcript):
            return {'status': 'clarify', 'reason': 'CONTROL_INTENT_CONFLICT'}
        device = value.get('device')
        if value['role'] == 'control' and value['control'] == 'device.connect':
            if restricted or not isinstance(device, str) or device not in {'camera', 'document_camera', 'all'}:
                raise ValueError('CONTROL_DEVICE_TARGET_INVALID')
        elif 'device' in value:
            raise ValueError('CONTROL_INTENT_INVALID')
        if value['role'] == 'control' and value['control'] == 'task.start' and 'start_selector' not in value and (
                VoiceControlIntent.local_task_navigation(transcript) in {'describe','hold','unfinished','reported'} or re.search(
                r'(?:别|不要|不用|不想|暂不)(?:再|先|马上)?(?:开始|继续|做|进行)|(?:没做完|没做好|还没|他说|她说|老师说|如果|假如|什么意思)|[“”「」『』"]', transcript)):
            return {'status':'clarify','reason':'CONTROL_INTENT_CONFLICT'}
        if value['role'] == 'control' and value['control'] in {'task.start', 'task.resume'} and re.search(
                r'[“”「」『』"]|(?:假如|假设|如果|什么意思|有.{0,8}功能|是否支持|能否支持)', transcript):
            return {'status':'clarify','reason':'CONTROL_INTENT_CONFLICT'}
        if value['role']=='control' and value['control']=='task.complete' and re.search(
                r'(?:没(?:有)?(?:做|写)?完|还没|不要|先别|并非|不是|如果|假如|什么意思)|[“”「」『』"]', transcript):
            return {'status':'clarify','reason':'CONTROL_INTENT_CONFLICT'}
        if value['role'] == 'control' and value['control'] == 'companion.start_activity':
            VoiceControlIntent.activity_arguments(activity_type)
            if re.search(r'(?:不要|不用|不想|不需要|先别|并非|不是要|假如|假设|如果|什么意思|功能|能否支持)|[“”「」『』"]', transcript):
                return {'status': 'clarify', 'reason': 'CONTROL_INTENT_CONFLICT'}
        elif 'activity_type' in value:
            raise ValueError('CONTROL_INTENT_INVALID')
        feedback = value.get('activity_feedback')
        if value['control'] == 'companion.activity_feedback' and value['role'] == 'control':
            from .agent_runtime import _validate_activity_feedback_arguments
            if not isinstance(feedback, dict) or set(feedback) - {'operation', 'execution', 'experience'}:
                raise ValueError('CONTROL_INTENT_INVALID')
            _validate_activity_feedback_arguments({**feedback, 'response_quote': transcript}, transcript)
        elif 'activity_feedback' in value:
            raise ValueError('CONTROL_INTENT_INVALID')
        if value['role'] == 'control' and value['control'] == 'task.resume' and 'selector' not in value:
            return {'status': 'clarify', 'reason': 'CONTROL_RESUME_SELECTOR_REQUIRED'}
        start_selector = value.get('start_selector')
        if 'start_selector' in value:
            from .task_start_semantics import validate
            if value['role'] != 'control' or value['control'] != 'task.start':
                raise ValueError('CONTROL_START_SELECTOR_INVALID')
            validate(start_selector, transcript)
        selector = value.get('selector')
        if 'selector' in value:
            enums = {'subject': {'math', 'english', 'chinese', 'physics', 'chemistry', 'biology', 'history', 'geography', 'politics'},
                     'task_kind': {'review', 'written', 'oral', 'reading', 'listening'},
                     'relative_order': {'displayed_question'}}
            if (value['role'] != 'control' or value['control'] != 'task.resume'
                    or not isinstance(selector, dict) or set(selector) != set(enums)
                    or any(v is not None and (not isinstance(v, str) or v not in enums[k]) for k, v in selector.items())):
                raise ValueError('CONTROL_INTENT_INVALID')
        if value['role'] != 'control':
            if value['control'] is not None:
                raise ValueError('CONTROL_INTENT_INVALID')
            if value['role'] in {'answer', 'activity_reply', 'companion_reply'} and value['confidence'] < .82:
                return {'status':'clarify','reason':'CONTROL_ANSWER_ROLE_UNCERTAIN'}
            if value['role'] == 'answer':
                return {'status':'non_control','role':'answer','role_version':'spoken_answer_role_v1'}
            if value['role'] in {'question', 'greeting', 'companionship', 'topic_mention'} and value['confidence'] < .82:
                return {'status': 'clarify', 'reason': 'CONTROL_QUESTION_ROLE_UNCERTAIN'}
            if value['role'] == 'question' and tutor_question:
                if value['confidence'] < .82:
                    return {'status': 'clarify', 'reason': 'CONTROL_QUESTION_ROLE_UNCERTAIN'}
                return {'status': 'non_control', 'role': 'question', 'role_version': 'tutor_question_role_v1'}
            if (foreground_dialogue or pending_dialogue) and value['role'] in {'question', 'greeting', 'companionship', 'topic_mention'}:
                if value.get('question_scope') != 'independent':
                    return {'status': 'clarify', 'reason': 'CONTROL_QUESTION_SCOPE_UNCERTAIN'}
                if pending_dialogue:
                    from .pending_evaluation_control import INDEPENDENT_VERSION as VERSION
                else:
                    from .foreground_independent_dialogue import VERSION
                return {'status': 'non_control', 'role': value['role'], 'question_scope': 'independent', 'role_version': VERSION}
            return {'status': 'non_control', 'role': value['role']}
        if value['control'] not in CONTROLS:
            raise ValueError('CONTROL_INTENT_INVALID')
        if value['control'] == 'task.reschedule':
            from .task_deferral_voice import valid_words
            if restricted or not valid_words(transcript):
                return {'status': 'clarify', 'reason': 'CONTROL_DEFERRAL_NOT_REQUESTED'}
        if value['control']=='session.request_end':
            if restricted or re.search(r'(?:先别|不要|不用|不想|暂不|别).{0,12}(?:结束|收尾|复盘)|\b(?:do not|don.t|not now|never mind)\b',transcript,re.I):
                return {'status':'clarify','reason':'SESSION_END_NOT_REQUESTED'}
        if value['control'] == 'evaluation.status' and not restricted:
            raise ValueError('CONTROL_INTENT_INVALID')
        if value['confidence'] < .82:
            return {'status': 'clarify', 'reason': 'CONTROL_INTENT_UNCERTAIN'}
        if value['control']=='learning.help_current' and re.search(
                r'(?:先别|不要|不用|不想|不需要|别|暂不).{0,8}(?:讲|解释|提示|帮我|引导)'
                r'|\b(?:do not|don.t|not now|never mind)\b',transcript,re.I):
            return {'status':'clarify','reason':'LEARNING_HELP_NOT_REQUESTED'}
        if value['control']=='learning.retry_check_coaching':
            from .check_coaching_retry_control import explicit
            if restricted or not explicit(transcript):
                return {'status':'clarify','reason':'TUTOR_CHECK_RETRY_NOT_REQUESTED'}
        if value['control']=='learning.independent_check' and re.search(
                r'(?:先别|不要|不用|不想|不需要|暂不|别|不是要).{0,12}(?:换|新题|出题|给我|另一道|再来|再做)'
                r'|(?:晚点|稍后|等会|改天|明天).{0,8}(?:换|题|再做|再来)'
                r'|\b(?:do not|don.t|not now|never mind)\b',transcript,re.I):
            return {'status':'clarify','reason':'INDEPENDENT_QUESTION_NOT_REQUESTED'}
        if value['control'] == 'writing.confirm_original' and re.search(
                r'[?？]|(?:不是我|不是本人|不确定|不准确|不对|错了|识别.{0,8}(?<!没)有错|还.{0,5}要改|需要改|先别|不要|没写|他写|她写)'
                r'|\b(?:not mine|not sure|incorrect|someone else|needs? changes?)\b|^(?:嗯+|哦+|听到了|知道了)[。！!\s]*$', transcript, re.I):
            return {'status': 'clarify', 'reason': 'WRITING_CONFIRMATION_UNCERTAIN'}
        if value['control'] in {'writing.revise_original', 'writing.independent_check'} and re.search(
                r'(?:不改|先别|不要.{0,6}(?:改|重写)|不用.{0,6}(?:改|重写)|改天再|以后再|没准备)', transcript):
            return {'status': 'clarify', 'reason': 'WRITING_REVISION_NOT_REQUESTED'}
        if value['control'] in {'oral.retry','oral.independent_check'}:
            operation = (r'(?:再说|再读|重说|重读|重答|再答|补充|重来)' if value['control']=='oral.retry'
                         else r'(?:换|新题|另一道|再来一道)')
            if (re.search(r'[“”「」『』"]',transcript)
                    or re.fullmatch(r'(?:先不|不做|不练|晚点|稍后|等会|改天|明天再|不要|不用|不想)(?:了|吧|再说|再练|再做|开始)?[。！!\s]*',transcript)
                    or re.search(r'(?:先别|不要|不用|不想|不需要|别|暂不).{0,8}'+operation,transcript)
                    or re.search(r'\b(?:do not|don.t|not now|never mind)\b',transcript,re.I)):
                return {'status':'clarify','reason':'ORAL_FOLLOWUP_NOT_REQUESTED'}
        # The complete sentence, including a cancellation/negation, wins over
        # the model's label. This is deliberately not an affirmative keyword gate.
        denied_task, affirmative_contrast = VoiceControlIntent.task_negation_scope(transcript, value['control'], selector)
        quote_text = (VoiceControlIntent.wake_word_request_text(transcript, wake_word['candidate'])
                      if value['control'] == 'voice.configure_wake_word' else transcript)
        if external and value['control'].endswith('.play'):
            quote_text = external_text
        if (re.search(r'[“”「」『』"]', quote_text)
                or re.search(r'(?:假如|假设|如果|什么意思|有.{0,8}功能|是否支持|能否支持)', transcript)):
            return {'status': 'clarify', 'reason': 'CONTROL_INTENT_CONFLICT'}
        if denied_task or re.search(r'(?:先别执行|不要执行|取消刚才|不对[，,]|说错了)', transcript):
            return {'status': 'clarify', 'reason': 'CONTROL_INTENT_CONFLICT'}
        if re.search(r'(?:然后|同时|顺便|还要|再帮我).{0,8}(?:解答|回答|计算|写出答案)', transcript):
            return {'status': 'clarify', 'reason': 'CONTROL_INTENT_CONFLICT'}
        if not affirmative_contrast and re.search(
                r'(?:别|不要|不用|不想|不需要|并非|不是要).{0,8}(?:暂停|关闭|停止|取消|交卷|恢复|重播)', transcript):
            return {'status': 'clarify', 'reason': 'CONTROL_INTENT_CONFLICT'}
        if (value['control'] == 'task.resume' or (feedback or {}).get('operation') == 'request_resume') and (
                not affirmative_contrast and re.search(r'(?:先别|不要|不用|不想|不需要|暂不|不是|别再).{0,12}(?:继续|恢复|接着|做|数学|英语|语文|复习)', transcript)
                or VoiceControlIntent.resume_deferred(transcript, selector)
                or re.search(r'(?:算了|撤回|取消刚才|还没决定)', transcript)):
            return {'status': 'clarify', 'reason': 'CONTROL_INTENT_CONFLICT'}
        return {'status': 'control', 'control': value['control'], **({'selector': selector} if selector is not None else {}),
                **({'start_selector': start_selector} if start_selector is not None else {}),
                **({'query': value['query']} if 'query' in value else {}),
                **({'wake_word': wake_word} if wake_word is not None else {}),
                **({'device': device} if device is not None else {}),
                **({'activity_type': activity_type} if activity_type is not None else {}),
                **({'activity_feedback': feedback} if feedback is not None else {})}

    async def resolve(self, *, session_id, child_id, action_id, transcript, state, local_sources, names, allowed, binding, voice_capture=None, pre_dispatch=None, tutor_question_context=False):
        state = {**state, 'has_current_learning_question':bool(binding.get('learning_help')),
                 'has_retryable_check_coaching':bool(binding.get('check_coaching_retry')),
                 **({'has_frozen_document_page': True} if binding.get('frozen_document') else {})}
        daily_assent = bool(re.fullmatch(r'(?:好|好的|行|可以)[。！!\s]*', transcript))
        if daily_assent and binding.get('task_resume') and (voice_capture or {}).get('voice_input_id'):
            from .oral_daily_reminder import acknowledged, KEY
            with self.database.connect() as c:
                c.execute('BEGIN')
                offered = acknowledged(c, child_id, session_id, datetime.now(timezone.utc), voice_capture['voice_input_id'])
            if offered:
                binding = {**binding, 'task_resume': {**binding['task_resume'], KEY: offered}}
                state = {**state, 'has_acknowledged_daily_supply_offer': True}
        if binding.get('oral_followups'):
            from .oral_supply_obligation import acknowledged_offer
            with self.database.connect() as c:
                c.execute('BEGIN')
                offered = acknowledged_offer(c, child_id, session_id, datetime.now(timezone.utc))
            if offered:
                binding = {**binding, 'oral_supply_offer_id': offered}
                state = {**state, 'has_acknowledged_oral_supply_offer': True}
        restricted = bool(binding.get('pending_evaluation'))
        document_lane = bool(binding.get('document_review') or binding.get('document_revisit'))
        if binding.get('native_writing'):
            from .native_writing_control import source
            with self.database.connect() as c:
                native=source(c,session_id,child_id)
            if not native or native[0]!=binding['native_writing']:
                return {'status':'unavailable','reason':'CONTROL_SCOPE_CHANGED'}
            local_sources=[*local_sources,native[1]]
            state={**state,'has_native_writing_submission':True}
        if not restricted and state.get('is_break_phase') and self.explicit_break_resume(transcript):
            return self.accept_local_break_resume(session_id, child_id, action_id, transcript, binding)
        short_acknowledgement = bool(re.fullmatch(r'(?:嗯+|嗯哼|哦+|好|好的|行|可以|对)[。！!\s]*', transcript))
        acknowledged_offer = bool((state.get('has_acknowledged_oral_supply_offer')
            or state.get('has_acknowledged_daily_supply_offer')) and daily_assent)
        # A retained conversational focus needs semantics, not a context-free
        # acknowledgement shortcut. It grants neither control nor answer authority.
        contextual_short_reply = bool(short_acknowledgement and not acknowledged_offer and not restricted and (
            binding.get('input_companion') and state.get('has_companion_dialogue')
            or binding.get('activity_focus') and state.get('has_active_activity_focus')))
        if (not document_lane and not state.get('has_pending_writing_confirmation')
                and short_acknowledgement and not acknowledged_offer and not contextual_short_reply):
            return {'status': 'non_control', 'role': 'acknowledgement'}
        if not allowed:
            return {'status': 'unavailable', 'reason': 'CLOUD_TEXT_DISABLED'}
        projection = None
        feedback_projection = None
        revision_command=bool(not restricted and (binding.get('document_revisit') or {}).get('phase')=='check_feedback')
        if not self.text_eligible(transcript, names, local_sources,revision_command=revision_command):
            if binding.get('document_revisit') and not restricted:
                from .document_feedback_projection import project as feedback_project, KEY
                with self.database.connect() as c:
                    c.execute('BEGIN')
                    feedback_projection = feedback_project(c,child_id,session_id,transcript,binding)
                if feedback_projection and self.text_eligible(feedback_projection[0],names,local_sources):
                    binding = {**binding, KEY:feedback_projection[1]}
                else:
                    feedback_projection = None
            if not feedback_projection:
                from .tutor_question_projection import project
                projection = project(transcript) if tutor_question_context and not restricted and state.get('has_pending_question') and binding.get('tutor') else None
                if not projection or not self.text_eligible(projection[0], names, local_sources):
                    return {'status': 'unavailable', 'reason': 'CONTROL_TEXT_LOCAL_ONLY'}
                from .agent_runtime import AgentRuntimeStore
                reference = AgentRuntimeStore.captured_question(voice_capture or {})
                if not reference and binding.get('diagnostic'):
                    reference = {'kind': 'diagnostic', 'binding': binding['diagnostic']}
                if voice_capture and voice_capture.get('question_validation') != 'valid':
                    return {'status': 'unavailable', 'reason': 'CONTROL_SCOPE_CHANGED'}
                binding = {**binding, 'tutor_question_projection': {**projection[1], 'question_reference': reference}}
        if not getattr(self.gateway, 'configured', False):
            return {'status': 'unavailable', 'reason': 'MODEL_UNAVAILABLE'}
        if set(state) - {'has_retryable_check_coaching', 'has_foreground_independent_dialogue', 'has_frozen_document_page', 'has_unavailable_original_task', 'has_acknowledged_daily_supply_offer', 'has_acknowledged_oral_supply_offer', 'has_current_learning_question', 'has_companion_dialogue', 'has_pending_input_draft', 'has_native_writing_submission', 'has_next_task', 'has_oral_feedback', 'has_pending_evaluation', 'has_pending_writing_confirmation', 'has_writing_prompt', 'has_writing_reference', 'has_writing_feedback', 'has_paused_task', 'has_pending_task_start', 'has_ready_supply_obligation', 'has_companion_activity', 'has_active_activity_focus'} != STATE_FIELDS or any(type(v) is not bool for v in state.values()):
            raise ValueError('CONTROL_STATE_INVALID')
        fingerprint = hashlib.sha256(json.dumps([child_id, session_id, transcript], ensure_ascii=False).encode()).hexdigest()
        key = hashlib.sha256((('voice-document-revisit:' if binding.get('document_revisit') else 'voice-document-review:' if binding.get('document_review') else
            'voice-input-field:' if binding.get('input_slot') else 'voice-control:') + action_id).encode()).hexdigest()
        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            old = c.execute('SELECT * FROM voice_control_intent_request WHERE request_key=?', (key,)).fetchone()
            if not self.scope_valid(c, session_id, child_id, action_id):
                return {'status': 'unavailable', 'reason': 'CONTROL_SCOPE_CHANGED'}
            if projection and not self.tutor_projection_current(c, session_id, child_id, transcript, binding):
                return {'status': 'unavailable', 'reason': 'CONTROL_SCOPE_CHANGED'}
            if feedback_projection:
                from .document_feedback_projection import current as feedback_current
                if not feedback_current(c,child_id,session_id,transcript,binding):
                    return {'status':'unavailable','reason':'CONTROL_PROJECTION_CHANGED'}
            consent_version = self.text_consent_version(c)
            if state.get('has_foreground_independent_dialogue') and not restricted and not projection and not document_lane:
                binding = {**binding, 'foreground_text_consent': list(consent_version or ())}
            if binding.get('input_return'):
                binding = {**binding, 'input_return_consent': list(consent_version or ())}
            if binding.get('input_slot'):
                binding = {**binding, 'input_field_text_consent': list(consent_version) if consent_version else None}
            if not restricted and not projection and not document_lane:
                from .controlled_voice import snapshot
                binding = {**binding, 'controlled_sources': snapshot(c, child_id, session_id)}
                from .task_deferral_voice import snapshot as deferral_snapshot
                current_deferral = deferral_snapshot(c, child_id, session_id)
                if binding.get('task_deferral') and binding['task_deferral'] != current_deferral:
                    return {'status': 'unavailable', 'reason': 'CONTROL_SCOPE_CHANGED'}
                binding['task_deferral'] = current_deferral
            if binding.get('tutor') and state.get('has_pending_question'):
                binding = {**binding, 'tutor_question_text_consent': list(consent_version) if consent_version else None}
            if binding.get('activity_focus'):
                binding = {**binding, 'activity_text_consent': list(consent_version) if consent_version else None}
            if old:
                if old['fingerprint'] != fingerprint:
                    if restricted or json.loads(old['binding_json']).get('pending_evaluation'):
                        from .agent_runtime import InvalidAgentPlanError
                        raise InvalidAgentPlanError('PENDING_EVALUATION_CONTROL_REQUEST_CONFLICT')
                    raise ValueError('CONTROL_ACTION_CONFLICT')
                if json.loads(old['binding_json']).get('pending_evaluation') != binding.get('pending_evaluation'):
                    from .agent_runtime import InvalidAgentPlanError
                    raise InvalidAgentPlanError('PENDING_EVALUATION_CONTROL_REQUEST_CONFLICT')
                if json.loads(old['binding_json']).get('tutor_question_projection') != binding.get('tutor_question_projection'):
                    return {'status': 'unavailable', 'reason': 'CONTROL_PROJECTION_CHANGED'}
                if json.loads(old['binding_json']).get('document_feedback_projection') != binding.get('document_feedback_projection'):
                    return {'status':'unavailable','reason':'CONTROL_PROJECTION_CHANGED'}
                decision = (json.loads(old['decision_json']) if old['decision_json'] else
                            {'status': 'unavailable', 'reason': 'CONTROL_OUTCOME_UNKNOWN'})
                return {**decision, 'binding': json.loads(old['binding_json']), 'request_key': key}
            c.execute('''INSERT INTO voice_control_intent_request
                (request_key,child_id,session_id,fingerprint,binding_json,status,decision_json,created_at,completed_at)
                VALUES (?,?,?,?,?,?,?,?,?)''',
                      (key, child_id, session_id, fingerprint, json.dumps(binding), 'pending', None, utc_now(), None))
            c.commit()
        from .pending_evaluation_control import ALLOWED_CONTROLS
        if not restricted and not projection and not document_lane:
            sources = binding['controlled_sources']
            state = {**state,
                'has_pending_controlled_content_launch': len(sources['content'].get('failed_starts', [])) == 1,
                'has_pending_controlled_game_launch': len(sources['games'].get('failed_starts', [])) == 1,
                'has_active_controlled_content': any(row['status'] == 'active' for row in sources['content']['activities']),
                'has_active_controlled_game': any(row['status'] == 'active' for row in sources['games']['activities'])}
        catalog = sorted(ALLOWED_CONTROLS) if restricted else [key for key in CONTROLS if key != 'evaluation.status']
        foreground_dialogue = bool(not restricted and not projection and not document_lane
            and not binding.get('tutor') and state.get('has_foreground_independent_dialogue'))
        pending_dialogue = bool(restricted and state.get('has_foreground_independent_dialogue')
                                and not binding.get('activity_focus'))
        independent_dialogue = bool(not restricted and not projection and not document_lane
            and state.get('has_frozen_document_page') and not state.get('has_pending_question'))
        extra_roles = '|greeting|companionship|topic_mention' if independent_dialogue or foreground_dialogue or pending_dialogue else ''
        from .learning_input_voice import PROMPT as revision_prompt
        from .task_deferral_voice import PROMPT as deferral_prompt
        from .task_start_semantics import PROMPT as start_prompt
        messages = ({'role': 'system', 'content':
            '只分类当前原话，不执行、不评分、不回答题目。静态控制候选：' + ', '.join(catalog) +
            (revision_prompt if not restricted else '') +
            (deferral_prompt if not restricted else '') +
            ('。controlled_content.play和controlled_game.play用于明确请求播放许可视频或启动许可游戏，'
             '额外且仅必填query，必须是本轮quote中逐字存在的连续主题或名称片段，不得生成本地ID、路径或目录。'
             'controlled_content.stop和controlled_game.stop用于结束当前对应活动或取消对应未确认启动请求；'
             'has_pending_controlled_content_launch和has_pending_controlled_game_launch只表示有唯一原失败启动可核对，不表示已经打开或关闭；'
             '孩子请求停止原活动且只有一种明确未确认启动时可分类该类型stop，不能输出原ID；指代类型无法确定时用unknown，'
             'has_active_controlled_content和has_active_controlled_game只表示当前外部资源；两个同时存在且没明确对象则需澄清。'
             '停止分支禁止query。以上请求不表示学习完成或恢复原学习，不使用companion.start_activity代替。'
             if not restricted else '') +
            ('。当前是原评价等待期间的受限控制。evaluation.status只查询刚才那次评价；device.status仅查询当前设备状态；'
             'planning.describe只看计划；parent.request仅请求联系家长。礼貌请求问句可为control，但引用、否定、混合或真正答案不能执行。'
             '绝不授权确认原稿、重读诊断并恢复评分、提交答案或新评价；答案/混合/未知仍按真实role输出。' if restricted else '') +
            '。输出一个JSON对象。所有结果必填role(control|answer|activity_reply|companion_reply|question|acknowledgement|mixed|unknown' + extra_roles + ')、' +
            ('背景纸页只是来源候选，不代表正在等待作答。问独立问题用question，打招呼用greeting，'
             '表达感受或邀请普通陪聊用companionship，单说知识名词用topic_mention；都control=null。'
             '不能把操作、作答、原页求助或混合用途伪称聊天。' if independent_dialogue else '') +
            ((('原作答已提交并等待评价，但本轮可能是独立用途。' if pending_dialogue else
               '原题仍等待作答，但本轮可能是独立用途。') +
             'question/greeting/companionship/topic_mention且control=null时额外必填'
             'question_scope(independent|current|unknown)。只有不依赖原题、原答案或原页的独立问题/感受/招呼才为independent；'
             '这道题的提示、卡点、依据、读题为current，不猜原题；指代不明确为unknown。作答仍answer，混合作答和新问题为mixed。'
             '其它role/control禁止question_scope。不得通过independent释放原题作答权限。') if foreground_dialogue or pending_dialogue else '') +
            ('原作答正在评价，独立用途不查询原题内容或评分依据，不重新评价，也不发起另一待答。'
             '问原评价是否完成仍是evaluation.status控制，不是独立用途。' if pending_dialogue else '') +
            'control(候选或null)、quote(完整原话逐字复制)、confidence(0到1)。'
            'device.status只读取设备状态，不开启采集；parent.request只提示找家长，不代表联系成功或家长授权。'
            'system.inspect_learning仅用于孩子询问最近原话有没有收到、断线或换语音后处理到哪、是否完成。'
            '它只查本地请求回执，scope=requests和track=all由本地固定，不输出参数、题目、答案或原请求编号；'
            '不会重做原工作、恢复评分或计作当前题答案。要求再做一次、实际作答或同时查询并执行属于mixed，不把执行请求伪装成查询。'
            'device.connect用于孩子现在要求连接设备，额外且仅必填device(camera|document_camera|all)：'
            '摄像头或观察摄像头是camera，高拍仪是document_camera，同时提到两者或说两个都连接是all。'
            '它表示实际重新枚举并连接，不表示已经成功；询问当前状态仍用device.status。其它分支禁止device字段。'
            'session.request_end只用于孩子明确要求结束今天学习或进入复盘；仅请求核对未完成项后收尾，'
            '不表示题目答对、目标达成、跳过复盘或自动结束。未知、引用、否定不能执行。'
            'voice.configure_wake_word只进入本机命名确认，不直接保存。该分支额外必填wake_word对象，'
            '且仅包含candidate：从本轮原话逐字提取的2到12字名字，未明确名字填null。名称字面值可以有引号，'
            '引用他人整句命令、否定、假设、或同时作答属于mixed/unknown而非执行。'
            '其它role/control禁止wake_word。评价等待的受限目录不允许改名。'
            'companion.chat仅当has_pending_input_draft为真，孩子本轮明确请求普通陪伴聊天；不猜故事、谜语或运动。'
            'has_companion_dialogue为真表示有当前明确进入且未撤回的陪聊。孩子描述同伴或作业挫折、补充感受、否认上一句理解时，'
            '若明确是在回应陪聊用role=companion_reply/control=null，不要求再次说聊天口令。'
            '当前陪聊或活动中的短语气回应也按当前用途分类，不要求重复开始口令；'
            '仅嗯、好、对等短回应不是完成任务、提交答案或操作设备的授权。'
            '纠正误解中的否定不是取消聊天；明确停止、操作请求、返回草稿、题目答案、引用命令或混合用途不能用companion_reply。'
            '没有当前陪聊焦点时不得把unknown或答案当companion_reply。只判断本轮语义，禁止输出历史内容。'
            'learning_inputs.resume_draft仅当has_pending_input_draft为真且明确要回到尚未说完的作业草稿、继续补信息。'
            '它只重新读回缺项或待确认内容，不提交字段、不恢复做题计时、不确认草稿；仅时长、答案、泛化确认不能选它。'
            '聊天或返回草稿的否定、转述、假设、混合作答、询问功能都用mixed/unknown，禁止补充历史内容或ID。'
            'companion.start_activity仅用于孩子现在请求开始短活动，额外且仅必填activity_type：'
            'interactive_story|word_chain|riddle|movement|tidy_challenge。禁止topic/opening/ID等其它字段。'
            '礼貌请求式问句例如能带我活动一小会儿吗可为control/movement；询问功能、引用、假设、否定或混合请求不是执行。'
            '共同编故事是interactive_story，活动身体是movement，不猜不明确的活动类型。其它分支禁止activity_type。'
            'has_active_activity_focus为真表示这段录音属于正在进行的活动：对活动的回应（包括短数字游戏回答）用activity_reply/control=null，'
            '不把它当学习题答案，不评分。明确回原题使用已有恢复控制；否定继续或引用/混合/不明用途仍需澄清。'
            '当role=control且control=task.resume时，selector也是必填字段，不得省略，不得为null或空对象。'
            '此分支的完整结构为：'
            '{"role":"control","control":"task.resume","quote":"完整原话","confidence":0.99,'
            '"selector":{"subject":null,"task_kind":null,"relative_order":null}}。'
            'selector中subject、task_kind、relative_order三个键必须全部存在；原话没指定的维度才填null。'
            '其他role或control禁止selector。companion.activity_feedback额外必填activity_feedback对象，'
            '仅operation(feedback|decline|request_resume|defer_resume)，feedback才可附execution'
            '(completed|partial|not_executed|unknown|refused|stopped)和experience(comfortable|neutral|fatigued|uncomfortable|unknown)。'
            '只按孩子实际自报分类，不知道填unknown；疲劳反馈不等于要求恢复。'
            'has_companion_activity为真时，刚才活动的反馈、拒绝参与、稍后再学或明确回到活动前学习，选companion.activity_feedback。'
            '只有明确另选暂停任务才选task.resume；不能以task.resume绕过活动后恢复确认。'
            '其余分支禁止activity_feedback。所有分支均禁止结构以外的字段。'
            'subject仅math|english|chinese|physics|chemistry|biology|history|geography|politics或null；'
            'task_kind仅review|written|oral|reading|listening或null；relative_order仅displayed_question或null。'
            '学科枚举对应数学/英语/语文/物理/化学/生物/历史/地理/道德与法治；'
            '任务类型对应复习/笔头书写/口头作答/阅读/听力。'
            '只提取原话明确说的维度，不推测本地候选。即使学科或类型已在quote中出现，也必须在selector对应槽填出。'
            '刚才的复习或刚刚那道题用displayed_question，宿主仅用录音时精确显示的原题匹配，不能猜最近记录。'
            '没有学科或类型指代的维度用null，不输出名称、ID、候选、理由或答案。'
            '暂停保留进度；exam.cancel表示提前交卷，不是暂放。多目标、混合请求或目标不明确用mixed/unknown。'
            '被引用的命令、否定、假设或对命令的提问不是执行请求。答案只能role=answer/control=null。'
            '所有候选控制的礼貌请求都可用问句表达，问号本身不是否定；仅询问是否具备功能不是执行请求，混合或不明确仍澄清。'
            + start_prompt +
            '不能把查询下一步、上一项没做好或取消当作开始。'
            'planning.describe只读今天安排，不开始计时。has_active_task为真时不能开始另一项；'
            '已暂停或has_pending_task_start为真且孩子请求继续原任务，用task.resume及selector，不改题。'
            'task.complete只表示孩子明确要求结束当前任务；不是答案内容、否定、引用或已掌握的声明。task.pause指任务计时，task.resume只继续原任务，包括已暂停任务，或has_pending_task_start为真时已保留原题但尚未开始计时的原任务；has_ready_supply_obligation为真时也可响应孩子明确继续刚才缺题的原练习，服务端核对原义务后安排，不开始无关下一项；break.resume仅结束休息阶段。exam.pause指考试，observation指摄像头，document指高拍仪，'
            'tutor指讲解播放器，pet_audio指已有听力原音。词语不同但含义相同也应理解。'
            'writing.read_original是请核对已写原稿，不是让你代写。writing.confirm_original仅在has_pending_writing_confirmation为真，'
            '孩子明确确认刚读回的是自己本题作答且识别准确时使用；yes等简短明确肯定可承接这个确切询问。'
            '嗯或仅表示听到了、不确定、他人作品、尚需改字不能确认。它不提交英语答案、不授权上传。'
            'native_writing.submission_status用于询问刚才在本机输入的作文或原稿是否提交保存，只核对原回执，不重发。'
            'native_writing.submit_retained仅用于明确要求继续提交本机保留的原稿，不是查询状态、朗读纸稿或口述新的答案。'
            '两者必须has_native_writing_submission为真；不要求孩子说固定词句，不得把控制话当作答正文。'
            'writing.revise_original仅当has_writing_reference或has_writing_feedback为真且孩子明确请求自己修改原作答时使用。'
            'writing.independent_check仅当has_writing_feedback为真且孩子明确请求另一道写作题独立尝试时使用。'
            'oral.retry仅当has_oral_feedback为真且孩子明确想根据刚才的口语建议补充、重新说或重新读时使用；'
            'learning.help_current仅当has_current_learning_question为真，孩子在求助当前题目的思路、依据、卡点或解法时使用，'
            'learning.retry_check_coaching仅当has_retryable_check_coaching为真，孩子明确要求重试刚才失败或未确认的检查回答点评时使用；不重录原回答、不评分。'
            'learning.independent_check仅当has_current_learning_question为真，孩子明确要求另出一道未见过的题自己独立尝试时使用；'
            '再给我一道校内题、想自己试另一题均可表达该意图。它不是求助原题、原题答案、引用、否定或假设；'
            '不得把原题答案当新题请求；待答原题由宿主核对并保留，不提前取消。'
            'has_frozen_document_page为真表示本机在本轮开口时已保留原纸页引用；'
            '孩子求助这张纸页上的题目、想一起理清或读回时，用document.review，宿主只读取那张原页，不能猜题干或评分。'
            'document.inspect用于明确要求重新拍现在的页面；不要用它替代已有原页的求助。'
            '不要求固定说法。对当前题表示卡住、猜的或想弄懂也可以是求助，回答已提交仍可求助。'
            '不用于无关领域的不会、新话题、引用他人的求助、否定讲解或询问软件故障。'
            'oral.independent_check用于此时明确要求另一道同类口语题。不把新答案、引用、否定或闲聊当再试。'
            'has_acknowledged_oral_supply_offer为真表示刚播完新题已补齐、是否继续原练习的询问；'
            '孩子简短肯定如好、开始吧可承接这次询问，用oral.independent_check，不启动无关下一项。'
            '仅表示听到、拒绝、晚点或改变话题不能视为同意。没有该状态不能猜测简短肯定指新题。'
            'has_acknowledged_daily_supply_offer为真则是刚播完今天口述复查补题的询问；'
            '此时简短同意用task.resume，selector三个字段均为null，本机绑定刚询问的那项，不猜科目或下一项。'
            '两者都不是代写、回答内容、已完成声明或直接算通过。'},
            {'role': 'user', 'content': json.dumps({'transcript': transcript, 'state': state}, ensure_ascii=False)})
        request_version = VERSION
        if document_lane:
            if binding.get('document_revisit'):
                from .document_revisit import VERSION as request_version, PROMPT, ZONE
            else:
                from .document_review_schedule import VERSION as request_version, PROMPT, ZONE
            from zoneinfo import ZoneInfo
            messages = ({'role':'system', 'content':PROMPT}, {'role':'user', 'content':json.dumps({
                'transcript':transcript, 'phase':(binding.get('document_revisit') or binding['document_review'])['phase'],
                'current_time':datetime.fromisoformat(binding['review_clock']).astimezone(ZoneInfo(ZONE)).isoformat(),
                'timezone':ZONE}, ensure_ascii=False)})
        if binding.get('input_slot'):
            from .learning_input_field_semantics import VERSION as request_version, PROMPT
            messages = ({'role': 'system', 'content': PROMPT}, {'role': 'user', 'content': json.dumps({
                'transcript': transcript, 'pending_input_field': binding['input_slot']['field']}, ensure_ascii=False)})
        if projection:
            from .tutor_question_projection import VERSION as request_version
            messages = ({'role': 'system', 'content':
                '只判断去除本地引用后的这句话是否向当前讲解提出问题，不回答。'
                '[本地引用]是不提供的私有内容，不能猜测或复原。'
                '没有可执行控制或作答权限。输出且仅输出role(question|control|answer|mixed|unknown)、'
                'control(null)、quote(完整投影原话逐字复制)、confidence(0到1)。'
                '只明确提问才选question，命令、答案、混合、不确定不得当问题。'},
                {'role': 'user', 'content': json.dumps({'transcript': projection[0],
                    'state': {'has_pending_question': True, 'has_tutor_playback': True}}, ensure_ascii=False)})
        if feedback_projection:
            from .document_feedback_projection import VERSION as request_version, PROMPT as feedback_prompt
            messages = ({'role':'system','content':feedback_prompt},
                {'role':'user','content':json.dumps({'transcript':feedback_projection[0]},ensure_ascii=False)})
        manifest = {'lane': request_version, 'local_sources_included': False, 'history_included': False,
                    'state_fields': sorted(json.loads(messages[-1]['content']).get('state', {}))}
        call_id = self.repository.start_model_call(session_id, child_id, 'voice_interaction',
            self.settings.model_provider, self.settings.voice_intent_model_name or self.settings.model_name,
            sum(len(m['content']) for m in messages), request_version, manifest, 'low',
            self.settings.model_provider, request_version, None)
        def check_dispatch(c):
            if binding.get('document_revisit'):
                from .document_revisit import assert_focus
                assert_focus(c, session_id, child_id, action_id, binding['document_revisit'])
            if binding.get('document_review'):
                from .document_review_schedule import assert_focus
                assert_focus(c, session_id, child_id, action_id, binding['document_review'])
            if binding.get('input_companion'):
                from .input_companion_dialogue import validate
                validate(c, child_id, session_id, binding['input_companion'])
            if binding.get('input_return'):
                from .learning_input_return import validate
                validate(c, child_id, session_id, binding['input_return'])
            if binding.get('native_writing'):
                from .native_writing_control import validate
                validate(c,session_id,child_id,binding['native_writing'])
            if pre_dispatch:
                pre_dispatch(c)
            if self.text_consent_version(c) != consent_version:
                raise ValueError('CONTROL_CONSENT_CHANGED')
            self.assert_current_input(c, session_id, child_id, action_id, transcript, voice_capture)
            if revision_command and not feedback_projection and not projection and not self.text_eligible(
                    transcript,names,local_sources,revision_command=True):
                raise ValueError('CONTROL_SOURCE_CHANGED')
            if projection and (not self.text_eligible(projection[0], names, local_sources)
                    or not self.tutor_projection_current(c, session_id, child_id, transcript, binding)):
                raise ValueError('CONTROL_PROJECTION_CHANGED')
            if feedback_projection:
                from .document_feedback_projection import current as feedback_current
                if (not self.text_eligible(feedback_projection[0],names,local_sources)
                        or not feedback_current(c,child_id,session_id,transcript,binding)):
                    raise ValueError('CONTROL_PROJECTION_CHANGED')
            if binding.get('activity_focus'):
                self.validate_activity_focus(c, session_id, child_id, binding['activity_focus'])
            if binding.get('input_slot'):
                from .learning_input_field_semantics import current
                if not current(c, child_id, session_id, action_id, binding['input_slot']):
                    raise ValueError('INPUT_FIELD_READBACK_CHANGED')
            journal = c.execute('SELECT * FROM voice_control_intent_request WHERE request_key=?',(key,)).fetchone()
            if (not journal or journal['status'] != 'pending' or journal['fingerprint'] != fingerprint
                    or (journal['session_id'],journal['child_id']) != (session_id,child_id)
                    or json.loads(journal['binding_json']) != binding):
                raise ValueError('CONTROL_JOURNAL_CHANGED')

        def authorize_dispatch():
            with self.database.connect() as c:
                c.execute('BEGIN')
                check_dispatch(c)

        request = ModelRequest(request_id=call_id, child_id=child_id, session_id=session_id,
            request_type='voice_interaction', risk_level='low', context_manifest=manifest,
            provider_preference=self.settings.model_provider, policy_version=request_version, prompt_version=request_version,
            knowledge_version=None, output_type='dialogue', evidence_refs=(), messages=messages,
            pre_dispatch=authorize_dispatch)
        decision = {'status': 'unavailable', 'reason': 'CONTROL_MODEL_FAILED'}
        result = None
        try:
            authorize_dispatch()
            result = await asyncio.wait_for(self.gateway.complete(request), timeout=8)
            if feedback_projection:
                from .document_feedback_projection import parse
                decision = parse(result.content,feedback_projection[0],transcript,binding['review_clock'])
            elif binding.get('document_revisit'):
                from .document_revisit import parse
                decision = parse(result.content, transcript, binding['review_clock'])
            elif binding.get('document_review'):
                from .document_review_schedule import parse
                decision = parse(result.content, transcript, binding['review_clock'])
            elif binding.get('input_slot'):
                from .learning_input_field_semantics import parse
                decision = parse(result.content, transcript, binding['input_slot']['field'])
            elif projection:
                from .tutor_question_projection import parse
                decision = parse(result.content, projection[0])
            else:
                decision = self.parse(result.content, transcript, restricted=restricted,
                                      tutor_question=bool(not restricted and binding.get('tutor') and state.get('has_pending_question')),
                                      independent_dialogue=independent_dialogue, foreground_dialogue=foreground_dialogue,
                                      pending_dialogue=pending_dialogue)
            if contextual_short_reply and (decision.get('status') == 'control'
                    or decision.get('role') == 'answer'):
                decision = {'status': 'clarify', 'reason': 'SHORT_REPLY_AUTHORITY_REQUIRED'}
            activity_reply = (decision == {'status':'non_control','role':'activity_reply'}
                              and bool(binding.get('activity_focus')))
            if (decision.get('control') in {'companion.chat', 'learning_inputs.resume_draft'}
                    and (not binding.get('input_return') or not state.get('has_pending_input_draft'))):
                decision = {'status': 'clarify', 'reason': 'INPUT_RETURN_FOCUS_REQUIRED'}
            if (decision == {'status':'non_control','role':'companion_reply'}
                    and (not binding.get('input_companion') or not state.get('has_companion_dialogue'))):
                decision = {'status':'clarify','reason':'INPUT_COMPANION_FOCUS_REQUIRED'}
            independent_reply = (pending_dialogue and decision.get('status') == 'non_control'
                                 and decision.get('question_scope') == 'independent'
                                 and decision.get('role_version') == 'pending_evaluation_independent_v1')
            if restricted and decision.get('control') not in ALLOWED_CONTROLS and not activity_reply and not independent_reply:
                decision = {'status': 'clarify', 'reason': 'PENDING_EVALUATION_CONTROL_NON_CONTROL'}
            if decision.get('control') in {'task.pause','task.complete'} and (not state.get('has_active_task') or not binding.get('task')):
                decision = {'status': 'clarify', 'reason': 'CONTROL_PAUSE_TARGET_REQUIRED'}
            if decision.get('control') in {'oral.retry','oral.independent_check'}:
                kind = 'retry' if decision['control']=='oral.retry' else 'independent_check'
                if not state.get('has_oral_feedback') or not binding.get('oral_followups',{}).get(kind):
                    decision = {'status':'clarify','reason':'ORAL_FOLLOWUP_SCOPE_REQUIRED'}
            if decision.get('control')=='learning.help_current' and not binding.get('learning_help'):
                decision = {'status':'clarify','reason':'LEARNING_HELP_SCOPE_REQUIRED'}
            if decision.get('control')=='learning.retry_check_coaching' and not binding.get('check_coaching_retry'):
                return {'status':'unavailable','reason':'TUTOR_CHECK_RETRY_UNAVAILABLE'}
            if decision.get('control')=='learning.independent_check':
                from .independent_question_control import target
                try:
                    with self.database.connect() as c:
                        c.execute('BEGIN')
                        target(c,session_id,child_id,binding.get('learning_help'))
                except (ValueError,KeyError,TypeError):
                    decision={'status':'clarify','reason':'INDEPENDENT_QUESTION_SCOPE_REQUIRED'}
            if decision.get('control','').startswith('native_writing.') and not binding.get('native_writing'):
                decision={'status':'non_control','role':'unknown'}
            if decision.get('control') in {'writing.revise_original', 'writing.independent_check'}:
                kind = 'revision' if decision['control'] == 'writing.revise_original' else 'independent_check'
                reference = kind == 'revision' and state.get('has_writing_reference') and binding.get('writing_revision')
                formal = state.get('has_writing_feedback') and binding.get('writing_followups', {}).get(kind)
                if not reference and not formal:
                    decision = {'status': 'clarify', 'reason': 'WRITING_REVISION_SCOPE_REQUIRED'}
            elif decision.get('control', '').startswith('writing.') and (
                    not state.get('has_writing_prompt') or (decision['control'] == 'writing.confirm_original'
                    and (not state.get('has_pending_writing_confirmation') or not binding.get('writing_ocr')))):
                decision = {'status': 'clarify', 'reason': 'WRITING_CONFIRMATION_SCOPE_REQUIRED'}
        except Exception:
            pass
        resolved_binding = binding
        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            try:
                check_dispatch(c)
                if state.get('has_acknowledged_daily_supply_offer') and decision.get('control') != 'task.resume':
                    decision = {'status': 'clarify', 'reason': 'CONTROL_SCOPE_CHANGED'}
                if decision.get('control') == 'task.reschedule':
                    from .task_deferral_voice import target_current
                    if not target_current(c, child_id, session_id, binding.get('task_deferral'), transcript,
                            capture_reference=(voice_capture or {}).get('task_choice_reference')):
                        decision = {'status': 'clarify', 'reason': 'CONTROL_DEFERRAL_TARGET_REQUIRED'}
                if decision.get('control') in EXTERNAL_CONTROLS:
                    from .controlled_voice import resolve as resolve_external
                    resolved_binding = {**binding, 'controlled': resolve_external(c, child_id, session_id,
                        decision['control'], {**decision, 'quote': transcript}, binding.get('controlled_sources'), voice_capture, binding)}
                if decision.get('control') == 'task.start' and decision.get('start_selector') is not None:
                    from .task_start_binding import TaskStartBinding
                    resolved_binding = {**binding, 'task_start': TaskStartBinding.semantic_start_target(
                        binding.get('task_start') or {}, decision['start_selector'])}
                if decision.get('control') == 'task.resume' and decision.get('selector') is not None:
                    from .task_start_binding import TaskStartBinding
                    target = TaskStartBinding.select_resume_target(binding.get('task_resume') or {}, decision['selector'])
                    TaskStartBinding.validate_resume_offer(c, session_id, child_id, target)
                    if target.get('items') != TaskStartBinding.resume_candidates(c, session_id, child_id):
                        raise ValueError('CONTROL_SCOPE_CHANGED')
                    if target.get('daily_supply_offer'):
                        from .oral_daily_reminder import validate
                        validate(c, child_id, session_id, target, datetime.now(timezone.utc))
                    resolved_binding = {**binding, 'task_resume': target}
                    if self.activity_resume_conflict(binding, target, decision['selector']):
                        decision = {'status': 'clarify', 'reason': 'CONTROL_ACTIVITY_RESUME_SCOPE_REQUIRED'}
            except ValueError:
                decision = {'status': 'unavailable', 'reason': 'CONTROL_SCOPE_CHANGED'}
            c.execute("UPDATE voice_control_intent_request SET status=?,decision_json=?,binding_json=?,completed_at=? WHERE request_key=? AND status='pending' AND child_id=? AND session_id=? AND fingerprint=?",
                      ('completed', json.dumps(decision), json.dumps(resolved_binding), utc_now(), key, child_id, session_id, fingerprint))
            c.commit()
        self.repository.finish_model_call(call_id, 'completed' if decision['status'] != 'unavailable' else 'failed',
            result.latency_ms if result else None, len(result.content) if result else 0,
            error_code=decision.get('reason'), verification_status='verified' if decision['status'] in {'control', 'document_review', 'document_revisit'} else 'rejected',
            safety_status='passed' if decision['status'] != 'unavailable' else 'blocked')
        return {**decision, 'binding': resolved_binding, 'request_key': key}
