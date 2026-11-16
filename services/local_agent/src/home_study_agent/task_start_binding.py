"""Persist one selected task before starting its timer or preparing its work."""
import json
import hashlib
import re
from contextlib import nullcontext

from .agent_continuation import ContinuationStore


class TaskStartBindingError(ValueError):
    pass


class TaskStartBinding:
    def __init__(self, database):
        self.database = database

    @staticmethod
    def interaction_focus(c, session_id, child_id):
        """An unfinished interaction is an ambiguity, not permission to switch."""
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        focus = []
        if 'tutor_playback_checkpoint' in tables:
            focus += [{'kind':'tutor', **dict(r)} for r in c.execute('''SELECT playback_id,source_version,generation,revision
                FROM tutor_playback_checkpoint WHERE child_id=? AND session_id=? AND status IN ('playing','paused')''',
                (child_id,session_id))]
        if 'mistake_dialogue' in tables:
            for row in c.execute('''SELECT phase,updated_at,state_json,mistake_id FROM mistake_dialogue
                    WHERE child_id=? AND session_id=? AND phase NOT IN ('closed','scheduled')''', (child_id,session_id)):
                try:
                    state = json.loads(row['state_json'])
                    # A saved standalone paper correction may be explicitly
                    # deferred without releasing an unfinished task or answer.
                    deferred = (isinstance(state, dict) and row['phase'] == 'paused'
                                and row['mistake_id'] and state.get('deferred_for_other_tasks') is True
                                and not state.get('pending_operation') and not state.get('foundation_origin'))
                except (ValueError, TypeError):
                    deferred = False
                if not deferred:
                    focus.append({'kind': 'mistake', 'phase': row['phase'], 'updated_at': row['updated_at']})
        if 'pet_media_binding' in tables and 'diagnostic_prompt' in tables:
            for row in c.execute('''SELECT m.media_id,m.state_json,m.source_version FROM pet_media_binding m
                JOIN diagnostic_prompt q USING(prompt_id) WHERE m.child_id=? AND m.session_id=? AND m.kind='listening'
                AND q.child_id=m.child_id AND q.origin_session_id=m.session_id AND q.status IN ('waiting_answer','answer_saved')''',
                (child_id,session_id)):
                try:
                    state=json.loads(row['state_json'])
                    if not isinstance(state,dict):
                        raise ValueError('invalid media state')
                except (ValueError,TypeError):
                    state={'status':'unknown'}
                if state.get('status') not in {'cancelled','invalidated','playback_finished'}:
                    focus.append({'kind':'listening','media_id':row['media_id'],'source_version':row['source_version'],
                                  'status':state.get('status'),'generation':state.get('generation'),'revision':state.get('revision')})
        return focus

    @classmethod
    def capture_start(cls,c,arrival_id,*,is_new,transcript='',voice_capture=None):
        row=c.execute('SELECT * FROM learning_input_arrival WHERE arrival_id=?',(arrival_id,)).fetchone()
        try:
            entries=json.loads(row['snapshot_json'])
            if not isinstance(entries,list):
                raise ValueError('invalid arrival')
            saved=[x['snapshot'] for x in entries if isinstance(x,dict) and x.get('kind')=='task_navigation_arrival']
            if len(saved)==1 and isinstance(saved[0],dict):
                return saved[0]
            if saved or not is_new:
                raise ValueError('old arrival has no task choice')
        except (ValueError,TypeError,KeyError):
            return {'status':'blocked','reason':'arrival_unavailable','item':None,'items':[],
                    'local_only':True,'cloud_transmission_allowed':False}
        snapshot=cls.start_snapshot(c,row['session_id'],row['child_id'])
        from .task_start_choices import capture_reply
        reply = capture_reply(c, row['session_id'], row['child_id'], snapshot, transcript, voice_capture)
        requested = cls.requested_start_filter(transcript)
        if reply:
            snapshot = reply
        elif requested:
            snapshot = cls.select_start_target(snapshot, requested)
        entries.append({'kind':'task_navigation_arrival','snapshot':snapshot,
                        'local_only':True,'cloud_transmission_allowed':False})
        c.execute('UPDATE learning_input_arrival SET snapshot_json=? WHERE arrival_id=?',
                  (json.dumps(entries,ensure_ascii=False),arrival_id))
        return snapshot

    @classmethod
    def start_snapshot(cls, c, session_id, child_id):
        """Freeze the schedule, not a choice made later when execution arrives."""
        rows = [dict(row) for row in c.execute('''SELECT pi.plan_item_id,pi.plan_id,dp.version AS plan_version,dp.plan_date,dp.total_minutes,
            pi.position,pi.status,pi.source_type,pi.source_ref,pi.subject,pi.title,pi.task_type,pi.started_at,
            pi.active_segment_started_at,pi.active_seconds,pi.expected_minutes
            FROM plan_item pi JOIN daily_plan dp USING(plan_id)
            WHERE dp.session_id=? AND dp.child_id=? AND dp.status='active'
              AND pi.status IN ('active','paused','pending')
            ORDER BY pi.position,pi.plan_item_id''', (session_id, child_id))]
        from .task_source_recovery import original_state
        from .task_start_choices import fingerprint
        for item in rows:
            if item['source_type'] == 'mistake_review':
                item['original_source_version'] = fingerprint(original_state(c, child_id, item))
        focus=cls.interaction_focus(c,session_id,child_id)
        reason = ('interaction_focus' if focus else 'active_task' if any(x['status'] == 'active' for x in rows) else
                  'paused_task' if any(x['status'] == 'paused' for x in rows) else
                  'no_task' if not rows else None)
        return {'version': 'task_start_arrival_v1', 'status': 'blocked' if reason else 'ready',
                'reason': reason, 'items': rows, 'item': None if reason else rows[0], 'interaction_focus':focus,
                'local_only': True, 'cloud_transmission_allowed': False}

    @staticmethod
    def requested_start_filter(transcript):
        """Constrain an authorized start with explicit local task names only.

        This never authorizes a command. The intent lane still decides whether
        the child requested an action; private schedule matching stays local.
        """
        subjects = [name for name in ('数学', '英语', '语文', '物理', '化学', '生物',
                    '历史', '地理', '道德与法治') if name in transcript]
        sources = [source for word, source in (('错题', 'mistake_review'), ('作业', 'school_homework'))
                   if word in transcript]
        kinds = [kind for word, kind in (('听力', 'listening'), ('阅读', 'reading'),
                 ('口语', 'oral'), ('写作', 'writing')) if word in transcript]
        if not kinds and not sources and re.search(r'复习|复查|复测', transcript):
            kinds = ['review']
        title_match = re.fullmatch(
            r'(?:(?:那|好)[，,]?\s*)?(?:(?:我们|咱们|我)\s*)?(?:请|先)?'
            r'(?:开始|做|进行)(?P<title>.{2,80}?)(?:吧|好吗)?[。！!\s]*',
            transcript.strip(),
        )
        title_query = title_match.group('title').strip() if title_match else None
        if title_query in {'任务', '学习', '这项', '下一项', '下一个任务'}:
            title_query = None
        # Natural modifiers such as "今天的错题复习" are not persisted task
        # titles.  Once the utterance already provides reliable local slots,
        # requiring an exact title match would reject the intended task.  Keep
        # title matching only as the fallback for otherwise unstructured names.
        if subjects or sources or kinds:
            title_query = None
        if not subjects and not sources and not kinds and not title_query:
            return None
        conflict = (len(subjects) > 1 or len(sources) > 1 or len(kinds) > 1
                    or bool(re.search(r'不是|不要|不想|先别|别做|除了|不做', transcript)))
        return {'subject': subjects[0] if len(subjects) == 1 else None,
                'source_type': sources[0] if len(sources) == 1 else None,
                'task_type': kinds[0] if len(kinds) == 1 else None,
                'title_query': title_query, 'conflict': conflict}

    @staticmethod
    def select_start_target(snapshot, requested):
        result = {**snapshot, 'requested_filter': requested}
        if snapshot.get('status') != 'ready':
            return result
        def matches_type(item):
            kinds = {item.get('task_type')}
            if item.get('task_type') == 'speaking':
                kinds.add('oral')
            if item.get('source_type') == 'oral_daily_check':
                kinds.update({'oral', 'review'})
            if item.get('source_type') in {'due_review', 'mistake_review'}:
                kinds.add('review')
            return not requested.get('task_type') or requested['task_type'] in kinds
        def normalized_title(value):
            return re.sub(r'[\s，。！!?？、:：]', '', value or '')
        from .task_start_semantics import SUBJECTS
        def matches_subject(item):
            expected = requested.get('subject')
            actual = item.get('subject')
            return not expected or SUBJECTS.get(actual, actual) == SUBJECTS.get(expected, expected)
        matches = [item for item in snapshot['items'] if matches_type(item) and matches_subject(item) and (
            not requested.get('source_type') or item.get('source_type') == requested['source_type']) and (
                not requested.get('title_query')
                or normalized_title(item.get('title')) == normalized_title(requested['title_query'])
            )]
        reason = ('requested_task_ambiguous' if requested.get('conflict') or len(matches) > 1 else
                  'requested_task_missing' if not matches else None)
        if (not reason and snapshot['items'][0]['source_type'] == 'planned_break'
                and matches[0]['plan_item_id'] != snapshot['items'][0]['plan_item_id']):
            reason = 'planned_break_first'
        return {**result, 'status': 'blocked' if reason else 'ready', 'reason': reason,
                'item': None if reason else matches[0],
                **({'matching_items': matches} if reason == 'requested_task_ambiguous' else {})}

    @classmethod
    def semantic_start_target(cls, origin, selector):
        from .task_start_semantics import SUBJECTS
        if origin.get('semantic_start') or origin.get('selection_offer'):
            raise TaskStartBindingError('TASK_START_SELECTOR_CONFLICT')
        base = {key: value for key, value in origin.items()
                if key not in {'requested_filter', 'matching_items'}}
        if base.get('reason') in {'requested_task_ambiguous', 'requested_task_missing'}:
            base.update(status='ready', reason=None, item=base['items'][0] if base['items'] else None)
        requested = {'subject': SUBJECTS.get(selector['subject']),
                     'source_type': selector['source_type'], 'task_type': selector['task_kind'], 'conflict': False}
        # A request for the next task retains queue order, including its break.
        result = cls.select_start_target(base, requested) if any(requested.values()) else base
        return {**result, 'semantic_start': {'origin': origin, 'selector': selector}}

    @classmethod
    def validate_start_snapshot(cls, c, session_id, child_id, snapshot, *, turn_id=None):
        current = cls.start_snapshot(c, session_id, child_id)
        if (snapshot and snapshot.get('status') == 'ready' and turn_id
                and current['items'] == snapshot.get('items')
                and current['reason'] == 'interaction_focus'):
            # Preparing this exact step's listening question creates a media
            # focus before its timer. It is not a different activity arriving.
            own_media = cls._prepared_start_media(c, session_id, child_id, turn_id, snapshot['item'])
            remaining = [f for f in current['interaction_focus']
                         if f.get('kind') != 'listening' or f.get('media_id') not in own_media]
            if not remaining:
                current = {**current, 'status': 'ready', 'reason': None,
                           'item': current['items'][0], 'interaction_focus': []}
        if snapshot and snapshot.get('semantic_start'):
            semantic = snapshot['semantic_start']
            origin = semantic['origin']
            projected = cls.select_start_target(current, origin['requested_filter']) if origin.get('requested_filter') else current
            if projected != origin:
                raise TaskStartBindingError('TASK_START_ARRIVAL_CHANGED')
            current = cls.semantic_start_target(origin, semantic['selector'])
        elif snapshot and snapshot.get('selection_offer'):
            from .task_start_choices import project
            current = project(c, session_id, child_id, current, snapshot['selection_offer'], turn_id=turn_id)
        elif snapshot and snapshot.get('requested_filter'):
            current = cls.select_start_target(current, snapshot['requested_filter'])
        if (not snapshot or snapshot.get('status') != 'ready'
                or snapshot != current):
            raise TaskStartBindingError('TASK_START_ARRIVAL_CHANGED')
        return snapshot['item']

    @staticmethod
    def _prepared_start_media(c, session_id, child_id, turn_id, target):
        row = c.execute('''SELECT p.context_manifest_json,s.idempotency_key FROM agent_plan p
            JOIN agent_step s USING(plan_id) JOIN agent_turn t ON t.agent_turn_id=p.agent_turn_id
            WHERE p.agent_turn_id=? AND p.session_id=? AND p.child_id=?
                AND t.session_id=p.session_id AND t.child_id=p.child_id AND t.tool_status='pending'
                AND p.status='running' AND s.status='running' AND s.step_index=0
                AND s.capability_name='task.start' ''', (turn_id, session_id, child_id)).fetchone()
        if not row:
            return set()
        try:
            binding = json.loads(row['context_manifest_json']).get('goal_prompt_binding')
            if not isinstance(binding, dict) or binding.get('plan_item_id') != target['plan_item_id']:
                return set()
            prompt = c.execute('''SELECT * FROM diagnostic_prompt WHERE prompt_id=? AND child_id=?
                AND origin_session_id=? AND item_version=? AND status='waiting_answer'
                AND prepare_action_id=?''', (binding.get('prompt_id'), child_id, session_id,
                binding.get('item_version'), 'goal-task:' + row['idempotency_key'])).fetchone()
            if not prompt:
                return set()
            from .diagnostic_prompt import DiagnosticPromptService, _version
            item = DiagnosticPromptService._item(c, child_id, prompt['diagnostic_item_id'])
            if not item or _version(item) != prompt['item_version']:
                return set()
            owned = set()
            for media in c.execute('''SELECT media_id,state_json FROM pet_media_binding
                    WHERE prompt_id=? AND child_id=? AND session_id=? AND item_version=?
                        AND diagnostic_item_id=? AND kind='listening' ''',
                    (prompt['prompt_id'], child_id, session_id, prompt['item_version'], prompt['diagnostic_item_id'])):
                state = json.loads(media['state_json'])
                if (state.get('status') == 'paused' and state.get('frame_position') == 0
                        and state.get('started') is None and state.get('completed_at') is None):
                    owned.add(media['media_id'])
            return owned
        except (ValueError, TypeError, KeyError, AttributeError):
            return set()

    @staticmethod
    def resume_candidates(c, session_id, child_id):
        rows = c.execute('''SELECT pi.*,dp.version AS plan_version FROM plan_item pi JOIN daily_plan dp USING(plan_id)
            WHERE dp.session_id=? AND dp.child_id=? AND dp.status='active' AND pi.status='paused'
            AND pi.source_type!='planned_break' ORDER BY pi.position,pi.plan_item_id''', (session_id, child_id)).fetchall()
        items = []
        for row in rows:
            pause = c.execute('''SELECT event_id,event_type FROM plan_item_event WHERE plan_item_id=?
                ORDER BY rowid DESC LIMIT 1''', (row['plan_item_id'],)).fetchone()
            source = c.execute('''SELECT p.agent_turn_id,p.context_manifest_json FROM agent_plan p
                WHERE p.child_id=? AND p.session_id=?
                AND EXISTS(SELECT 1 FROM agent_step s WHERE s.plan_id=p.plan_id AND
                    (s.capability_name='task.start' OR (s.capability_name='task.resume'
                     AND (json_type(p.context_manifest_json,'$.supply_resume_receipt')='object'
                        OR json_type(p.context_manifest_json,'$.legacy_listening_review_replacement')='object'))))
                AND json_extract(p.context_manifest_json,'$.goal_prompt_binding.plan_item_id')=?
                ORDER BY p.rowid DESC LIMIT 1''', (child_id, session_id, row['plan_item_id'])).fetchone()
            prompt = json.loads(source['context_manifest_json']).get('goal_prompt_binding') if source else None
            if not pause or pause['event_type'] != 'paused':
                continue
            consent = {}
            if source and json.loads(source['context_manifest_json']).get('legacy_listening_review_replacement'):
                from .supply_gap_resume import _consent_version
                consent['control_consent_version'] = _consent_version(c)
            items.append({**{key: row[key] for key in ('plan_item_id','plan_id','plan_version','title','source_type','subject',
                'source_ref','homework_id','task_type','expected_minutes','started_at','active_seconds')},
                **consent,
                'pause_event_id': pause['event_id'], 'goal_prompt_binding': prompt,
                'origin_turn_id': source['agent_turn_id'] if source else None})
        from .school_review_execution import pending_original_task_binding as pending_pet_task_binding
        try:
            pending = pending_pet_task_binding(c, child_id, session_id)
        except TaskStartBindingError:
            pending = None
        if pending:
            row = c.execute('''SELECT pi.*,dp.version AS plan_version FROM plan_item pi JOIN daily_plan dp USING(plan_id)
                WHERE pi.plan_item_id=? AND dp.child_id=? AND dp.session_id=? AND dp.status='active'
                AND pi.status='pending' AND pi.started_at IS NULL''',
                (pending['plan_item_id'], child_id, session_id)).fetchone()
            # The prepare action identifies its original owner, not the latest turn.
            sources = c.execute('''SELECT p.agent_turn_id,p.context_manifest_json FROM agent_plan p
                JOIN agent_step s USING(plan_id) JOIN diagnostic_prompt q
                    ON q.prepare_action_id='goal-task:'||s.idempotency_key
                WHERE p.child_id=? AND p.session_id=?
                    AND (s.capability_name='task.start' OR (s.capability_name='task.resume'
                        AND (json_type(p.context_manifest_json,'$.supply_resume_receipt')='object'
                            OR json_type(p.context_manifest_json,'$.legacy_listening_review_replacement')='object')))
                    AND p.status!='cancelled' AND s.status!='cancelled'
                    AND q.child_id=p.child_id AND q.origin_session_id=p.session_id
                    AND q.status='waiting_answer'
                    AND json_extract(p.context_manifest_json,'$.goal_prompt_binding.prompt_id')=q.prompt_id
                    AND json_extract(p.context_manifest_json,'$.goal_prompt_binding.plan_item_id')=?''',
                (child_id, session_id, pending['plan_item_id'])).fetchall()
            if row and len(sources) == 1:
                source = sources[0]
                manifest = json.loads(source['context_manifest_json'])
                consent = {}
                if manifest.get('supply_resume_receipt') or manifest.get('legacy_listening_review_replacement'):
                    from .supply_gap_resume import _consent_version
                    consent['control_consent_version'] = _consent_version(c)
                items.append({**{key: row[key] for key in ('plan_item_id','plan_id','plan_version','title','source_type','subject',
                    'source_ref','homework_id','task_type','expected_minutes','started_at','active_seconds')},
                    'operation': 'start', 'pause_event_id': None,
                    **consent, 'goal_prompt_binding': manifest['goal_prompt_binding'],
                    'origin_turn_id': source['agent_turn_id']})
        from .supply_gap_resume import candidates
        supply = candidates(c, session_id, child_id)
        supply_ids = {x['plan_item_id'] for x in supply}
        from .legacy_listening_review import candidates as legacy_candidates
        return [x for x in items if x['plan_item_id'] not in supply_ids] + supply + legacy_candidates(c, session_id, child_id)

    @classmethod
    def resume_snapshot(cls, c, session_id, child_id, captured_question=None):
        items = cls.resume_candidates(c, session_id, child_id)
        target = {'status': 'ready' if len(items) == 1 else 'ambiguous' if items else 'none',
                'items': items, 'item': items[0] if len(items) == 1 else None}
        prior = c.execute('''SELECT agent_turn_id,action_json FROM agent_turn WHERE session_id=?
            AND child_id=? ORDER BY rowid DESC LIMIT 1''', (session_id, child_id)).fetchone()
        if prior and json.loads(prior['action_json']).get('source') == 'task_resume_clarification':
            target.update(items=json.loads(prior['action_json'])['task_resume_choices'],
                          status='ambiguous', item=None, offer_turn_id=prior['agent_turn_id'])
        # Only a displayed question captured with this audio can identify "that
        # review just now". Database recency is not a user selection.
        if (captured_question or {}).get('kind') == 'diagnostic':
            ref = captured_question['binding']
            target['displayed_question'] = {k: ref[k] for k in ('prompt_id', 'item_version')}
        remediation = cls.foundation_resume_focus(c, session_id, child_id, target.get('item'))
        if remediation:
            target['foundation_remediation_focus'] = remediation
        review = cls.mistake_review_resume_focus(c, session_id, child_id, target.get('item'))
        if review:
            target['mistake_review_focus'] = review
        return target

    @classmethod
    def mistake_review_resume_focus(cls, c, session_id, child_id, item):
        if not item or item.get('source_type') != 'mistake_review':
            return None
        interactions = cls.interaction_focus(c, session_id, child_id)
        if len(interactions) != 1 or interactions[0]['kind'] != 'mistake':
            return None
        row = c.execute('''SELECT d.state_json,d.mistake_id FROM mistake_dialogue d
            JOIN mistake_case mc ON mc.mistake_id=d.mistake_id AND mc.child_id=d.child_id
            WHERE d.session_id=? AND d.child_id=? AND d.mistake_id=?
                AND mc.status NOT IN ('excluded','mastered')''', (session_id, child_id, item.get('source_ref'))).fetchone()
        if not row:
            return None
        try:
            state = json.loads(row['state_json'])
            if not isinstance(state, dict) or state.get('foundation_origin'):
                return None
        except (ValueError, TypeError):
            return None
        return {'plan_item_id': item['plan_item_id'], 'mistake_id': row['mistake_id'],
                'state_version': hashlib.sha256(row['state_json'].encode()).hexdigest(),
                'interaction_focus': interactions}

    @classmethod
    def foundation_resume_focus(cls, c, session_id, child_id, item, *, exclusive=True, _focus=None):
        """A saved correction may belong to this task, never to another task."""
        if not item or item.get('task_type') != 'goal_prerequisite':
            return None
        focus = cls.interaction_focus(c, session_id, child_id)
        if exclusive and (len(focus) != 1 or focus[0].get('kind') != 'mistake'):
            return None
        row = c.execute('SELECT * FROM mistake_dialogue WHERE child_id=? AND session_id=?',
                        (child_id, session_id)).fetchone()
        try:
            origin = (_focus['state'].get('foundation_origin') if _focus is not None else
                      json.loads(row['state_json']).get('foundation_origin') if row else None)
            mistake_id = _focus.get('mistake_id') if _focus is not None else row['mistake_id'] if row else None
            binding = item.get('goal_prompt_binding') or {}
            if (not isinstance(origin, dict) or origin.get('plan_item_id') != item['plan_item_id']
                    or any(not origin.get(k) or origin[k] != binding.get(k) for k in ('prompt_id', 'item_version'))):
                return None
            from .diagnostic_prompt import DiagnosticPromptService
            from .learning_state import LearningStateService
            from .goal_roadmap import goal_catalog_source_reason
            prompt = c.execute('''SELECT * FROM diagnostic_prompt WHERE prompt_id=? AND child_id=?
                AND origin_session_id=? AND item_version=? AND status='answered' ''',
                (origin['prompt_id'], child_id, session_id, origin['item_version'])).fetchone()
            receipt = DiagnosticPromptService._receipt(c, prompt) if prompt else None
            if (not receipt or receipt['attempt_id'] != origin.get('attempt_id')
                    or receipt['evidence_id'] != origin.get('evidence_id')
                    or receipt['correctness'] is None or receipt['correctness'] >= 1
                    or LearningStateService._attempt(c, receipt['attempt_id'])['score_status'] != 'verified_local'
                    or goal_catalog_source_reason(c, session_id, item['plan_item_id'])):
                return None
            owned = c.execute('''SELECT 1 FROM mistake_occurrence mo JOIN mistake_case mc USING(mistake_id)
                WHERE mo.mistake_id=? AND mo.evidence_id=? AND mo.child_id=? AND mc.child_id=mo.child_id
                AND mc.diagnostic_item_id=? AND mc.status NOT IN ('excluded','mastered')''',
                (mistake_id, origin['evidence_id'], child_id, prompt['diagnostic_item_id'])).fetchone()
            if not owned:
                return None
            return {'plan_item_id': item['plan_item_id'], 'mistake_id': mistake_id,
                    'origin': origin, 'interaction_focus': focus}
        except (ValueError, TypeError, KeyError, AttributeError):
            return None

    @staticmethod
    def resume_focus_allowed(binding):
        focus = (binding.get('task_start') or {}).get('interaction_focus')
        if not focus:
            return True
        target = binding.get('task_resume') or {}
        own = target.get('foundation_remediation_focus') or target.get('mistake_review_focus') or {}
        return bool(target.get('status') == 'ready' and own
                    and own.get('plan_item_id') == (target.get('item') or {}).get('plan_item_id')
                    and own.get('interaction_focus') == focus)

    @staticmethod
    def select_resume_target(target, selector):
        from .task_start_semantics import SUBJECTS as subjects
        def matches(item):
            if target.get('daily_supply_offer'):
                from .local_supply import digest
                if digest(item) != target['daily_supply_offer'].get('source_id'):
                    return False
            if selector.get('subject') and subjects.get(item.get('subject'), item.get('subject')) != subjects[selector['subject']]:
                return False
            kinds = {item.get('task_type')}
            if item.get('source_type') == 'oral_daily_check':
                # Scheduled checks use review; a same-day supplied replacement
                # uses oral. Both names address the same oral review workflow.
                kinds.update({'oral', 'review'})
            if item.get('source_type') == 'due_review':
                kinds.add('review')
                from .legacy_listening_review import VERSION
                if item.get('version') == VERSION:
                    kinds.add('listening')
            if item.get('task_type') == 'writing':
                kinds.add('written')
            question = (item.get('goal_prompt_binding') or {}).get('binding') or (
                (item.get('supply_item') or {}).get('planning_evidence') or {}).get('pet_question_binding') or {}
            if (question.get('version') == 'pet_question_binding_v1'
                    and question.get('diagnostic_item_id') == item.get('source_ref')):
                skill = question.get('skill')
                if skill in {'listening', 'reading', 'speaking'}:
                    kinds.add('oral' if skill == 'speaking' else skill)
            elif (question.get('version') == 'pet_listening_review_binding_v1'
                    and item.get('source_type') == 'due_review'
                    and question.get('review_task_id') == item.get('source_ref')
                    and question.get('skill') == 'listening'):
                kinds.add('listening')
            if selector.get('task_kind') and selector['task_kind'] not in kinds:
                return False
            if selector.get('relative_order') == 'displayed_question':
                shown, bound = target.get('displayed_question'), item.get('goal_prompt_binding') or {}
                if not shown or any(bound.get(k) != v for k, v in shown.items()):
                    return False
            return True
        items = [item for item in target.get('items', []) if matches(item)]
        return {**target, 'selector': selector, 'status': 'ready' if len(items) == 1 else 'ambiguous',
                'item': items[0] if len(items) == 1 else None}

    @staticmethod
    def validate_resume_offer(c, session_id, child_id, target):
        from .agent_runtime import InvalidAgentPlanError
        seen = set()
        while target.get('offer_turn_id'):
            if target['offer_turn_id'] in seen:
                raise InvalidAgentPlanError('ANSWER_CONTROL_SCOPE_CHANGED')
            seen.add(target['offer_turn_id'])
            prior = c.execute('''SELECT t.action_json,t.tool_status,p.context_manifest_json,p.status AS plan_status,
                s.status AS step_status FROM agent_turn t JOIN agent_plan p ON p.agent_turn_id=t.agent_turn_id
                JOIN agent_step s ON s.plan_id=p.plan_id AND s.step_index=0
                WHERE t.agent_turn_id=? AND t.session_id=? AND t.child_id=?
                AND p.child_id=t.child_id AND p.session_id=t.session_id''',
                (target['offer_turn_id'], session_id, child_id)).fetchone()
            offer = json.loads(prior['context_manifest_json']).get('task_resume_dialogue', {}) if prior else {}
            journal = c.execute('SELECT * FROM voice_control_intent_request WHERE request_key=?',
                                (offer.get('request_key'),)).fetchone()
            previous = offer.get('targets', {}).get('task_resume', {})
            if (not prior or prior['tool_status'] != 'completed' or prior['step_status'] != 'completed'
                    or prior['plan_status'] not in {'completed', 'waiting_child'} or not journal
                    or journal['status'] != 'completed' or journal['child_id'] != child_id or journal['session_id'] != session_id
                    or json.loads(journal['decision_json']).get('control') != 'task.resume'
                    or json.loads(journal['binding_json']) != offer.get('targets')
                    or previous.get('items') != target['items']
                    or json.loads(prior['action_json']).get('task_resume_choices') != target['items']):
                raise InvalidAgentPlanError('ANSWER_CONTROL_SCOPE_CHANGED')
            target = previous

    @classmethod
    def validate_resume_target(cls, c, session_id, child_id, target):
        from .agent_runtime import InvalidAgentPlanError
        item = (target or {}).get('item')
        review = (target or {}).get('mistake_review_focus')
        if review and review != cls.mistake_review_resume_focus(c, session_id, child_id, item):
            raise InvalidAgentPlanError('ANSWER_CONTROL_SCOPE_CHANGED')
        remediation = (target or {}).get('foundation_remediation_focus')
        if remediation and remediation != cls.foundation_resume_focus(c, session_id, child_id, item):
            raise InvalidAgentPlanError('ANSWER_CONTROL_SCOPE_CHANGED')
        if remediation:
            from .foundation_remediation import continuation_hold
            if continuation_hold(c, {'session_id': session_id, 'child_id': child_id,
                    'mistake_id': remediation['mistake_id'], 'state': {'foundation_origin': remediation['origin']}}, allow_paused=True):
                raise InvalidAgentPlanError('ANSWER_FOUNDATION_SCOPE_CHANGED')
        current = cls.resume_candidates(c, session_id, child_id)
        cls.validate_resume_offer(c, session_id, child_id, target or {})
        supply = bool(item and item.get('operation')=='supply')
        from .legacy_listening_review import VERSION, validate_target as validate_legacy, KEY
        legacy = bool(item and item.get('version') == VERSION)
        mapped = None
        if legacy:
            rows = c.execute('''SELECT context_manifest_json FROM agent_plan WHERE child_id=? AND session_id=?
                AND json_extract(context_manifest_json,?)=?''',
                (child_id, session_id, '$.' + KEY + '.origin_target.plan_item_id', item['plan_item_id'])).fetchall()
            matches = [json.loads(r[0])[KEY] for r in rows if json.loads(r[0])[KEY]['origin_target'] == item]
            if len(matches) > 1:
                raise InvalidAgentPlanError('ANSWER_CONTROL_SCOPE_CHANGED')
            mapped = matches[0] if matches else None
            try:
                validate_legacy(c, session_id, child_id, item, mapped=mapped)
            except ValueError as error:
                code = str(error) if str(error).startswith('TASK_START_LEGACY_REVIEW_') else 'TASK_START_LEGACY_REVIEW_CHANGED'
                raise InvalidAgentPlanError(code) from error
        if supply:
            from .supply_gap_resume import validate_target
            validate_target(c, session_id, child_id, item)
        if 'selector' in (target or {}):
            selected = cls.select_resume_target(target, target['selector'])
            if (not supply and not mapped and target.get('items') != current) or selected.get('item') != item or selected['status'] != 'ready':
                raise InvalidAgentPlanError('ANSWER_CONTROL_SCOPE_CHANGED')
        # validate_target checks current candidates before placement and the exact
        # original receipt/CAS afterwards; a placed supply is no longer offered.
        if (not item or target.get('status') != 'ready' or (not supply and not mapped and item not in current)
                or c.execute('''SELECT 1 FROM plan_item pi JOIN daily_plan dp USING(plan_id)
                    WHERE dp.session_id=? AND dp.child_id=? AND dp.status='active' AND pi.status='active' ''',
                    (session_id, child_id)).fetchone()):
            raise InvalidAgentPlanError('ANSWER_CONTROL_SCOPE_CHANGED')
        if item.get('goal_prompt_binding') and c.in_transaction:
            cls.resume_prompt(c, session_id, item)
        return item

    @staticmethod
    def resume_prompt(c, session_id, item):
        from .diagnostic_prompt import DiagnosticPromptService
        from .agent_runtime import InvalidAgentPlanError
        binding = item.get('goal_prompt_binding')
        if not binding:
            return None
        if not c.in_transaction:
            raise InvalidAgentPlanError('ANSWER_RESUME_TRANSACTION_REQUIRED')
        class TransactionPrompt(DiagnosticPromptService):
            def __init__(self):
                self.database = None

            def _transaction(self):
                return nullcontext(c)
        result = TransactionPrompt().read(session_id, prompt_id=binding['prompt_id'])
        prompt = result.get('prompt') or {}
        allowed = {'awaiting_task_start'} if item.get('operation') == 'start' else {'waiting_answer','answer_saved','answered'}
        if (result['status'] not in allowed
                or prompt.get('item_version') != binding.get('item_version')):
            raise InvalidAgentPlanError('ANSWER_TASK_RESUME_PROMPT_CHANGED')
        return {**result, 'local_only': True, 'cloud_transmission_allowed': False}

    @staticmethod
    def _turn(c, turn_id):
        row = c.execute("""SELECT p.plan_id,p.child_id,p.session_id,p.context_manifest_json,
            p.status AS plan_status,s.status AS step_status,t.tool_status,
            ds.status AS session_status,ds.state AS session_state
            FROM agent_plan p JOIN agent_step s USING(plan_id)
            JOIN agent_turn t ON t.agent_turn_id=p.agent_turn_id AND t.child_id=p.child_id AND t.session_id=p.session_id
            JOIN daily_session ds ON ds.session_id=p.session_id AND ds.child_id=p.child_id
            WHERE p.agent_turn_id=? AND s.step_index=0 AND s.capability_name='task.start'""", (turn_id,)).fetchone()
        if (not row or row['plan_status'] != 'running' or row['step_status'] != 'running'
                or row['tool_status'] != 'pending' or row['session_status'] != 'active'
                or row['session_state'] != 'study'
                or not ContinuationStore.execution_allowed(c, turn_id, running=True)):
            raise TaskStartBindingError('TASK_START_AUTHORIZATION_CHANGED')
        manifest = json.loads(row['context_manifest_json'])
        if manifest.get('native_source_targets_v1'):
            from .realtime_native_sources import inspect_control
            if inspect_control(c,turn_id) is None:raise TaskStartBindingError('TASK_START_AUTHORIZATION_CHANGED')
        if manifest.get('voice_control_binding'):
            # The preparation and timer commits are distinct. A newer utterance
            # between them must still revoke this original command's admission.
            from .voice_control_intent import VoiceControlIntent
            control = VoiceControlIntent.inspect_execution(c, turn_id)
            if not control or control['control'] != 'task.start':
                raise TaskStartBindingError('TASK_START_AUTHORIZATION_CHANGED')
        if manifest.get('task_start_arrival'):
            arrival=c.execute('''SELECT snapshot_json FROM learning_input_arrival WHERE arrival_id=?
                AND child_id=? AND session_id=? AND client_action_id=(SELECT client_action_id FROM agent_turn WHERE agent_turn_id=?)''',
                (manifest.get('learning_input_arrival_id'),row['child_id'],row['session_id'],turn_id)).fetchone()
            try:
                saved=[x['snapshot'] for x in json.loads(arrival[0]) if x.get('kind')=='task_navigation_arrival'] if arrival else []
            except (ValueError,TypeError,KeyError,AttributeError):
                saved=[]
            expected_arrival = manifest['task_start_arrival'].get('semantic_start', {}).get('origin', manifest['task_start_arrival'])
            if saved != [expected_arrival]:
                raise TaskStartBindingError('TASK_START_ARRIVAL_CHANGED')
            # An actual same-step receipt is replayable, never a new start.
            receipt = c.execute('''SELECT 1 FROM plan_item_event e JOIN agent_step s
                ON e.client_action_id=s.idempotency_key WHERE s.plan_id=? AND s.step_index=0
                AND e.event_type='started' AND e.plan_item_id=?''',
                (row['plan_id'], (manifest.get('task_start_binding') or {}).get('plan_item_id'))).fetchone()
            if not receipt:
                TaskStartBinding.validate_start_snapshot(c, row['session_id'], row['child_id'], manifest['task_start_arrival'], turn_id=turn_id)
        return row

    @staticmethod
    def _item(c, row, item_id):
        item = c.execute("""SELECT pi.* FROM plan_item pi JOIN daily_plan dp USING(plan_id)
            WHERE pi.plan_item_id=? AND dp.child_id=? AND dp.session_id=? AND dp.status='active'""",
            (item_id, row['child_id'], row['session_id'])).fetchone()
        if not item:
            raise TaskStartBindingError('TASK_START_ITEM_NOT_OWNED')
        return item

    def resolve(self, turn_id):
        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row = self._turn(c, turn_id)
            manifest = json.loads(row['context_manifest_json'])
            binding = manifest.get('task_start_binding')
            if binding is None:
                from .legacy_listening_review import start_arrival
                binding = start_arrival(c, turn_id)
                if binding is not None:
                    manifest['task_start_binding'] = binding
                    c.execute('UPDATE agent_plan SET context_manifest_json=? WHERE plan_id=?',
                              (json.dumps(manifest), row['plan_id']))
            if binding is None:
                from .school_review_execution import pending_original_task_binding as pending_pet_task_binding
                binding = pending_pet_task_binding(c, row['child_id'], row['session_id'])
                if binding is not None:
                    manifest['task_start_binding'] = binding
                    c.execute('UPDATE agent_plan SET context_manifest_json=? WHERE plan_id=?',
                              (json.dumps(manifest), row['plan_id']))
            if binding is None:
                item = c.execute("""SELECT pi.* FROM plan_item pi JOIN daily_plan dp USING(plan_id)
                    WHERE dp.child_id=? AND dp.session_id=? AND dp.status='active' AND pi.status IN ('paused','pending')
                    ORDER BY CASE pi.status WHEN 'paused' THEN 0 ELSE 1 END,pi.position,pi.plan_item_id LIMIT 1""",
                    (row['child_id'], row['session_id'])).fetchone()
                if not item:
                    return None
                binding = {'plan_item_id': item['plan_item_id'], 'source_type': item['source_type'],
                    'source_ref': item['source_ref'], 'operation': 'resume' if item['status'] == 'paused' else 'start'}
                manifest['task_start_binding'] = binding
                c.execute('UPDATE agent_plan SET context_manifest_json=? WHERE plan_id=?',
                    (json.dumps(manifest), row['plan_id']))
            item = self._item(c, row, binding['plan_item_id'])
            if (item['source_type'], item['source_ref']) != (binding['source_type'], binding['source_ref']):
                raise TaskStartBindingError('TASK_START_SOURCE_CHANGED')
            if item['source_type'] == 'due_review':
                from .legacy_listening_review import candidate, arrival, validate_target
                if candidate(c, row['session_id'], row['child_id'], item):
                    target = arrival(c, turn_id, item['plan_item_id'])
                    validate_target(c, row['session_id'], row['child_id'], target)
            c.commit()
            return {**dict(item), '_start_operation': binding['operation']}

    @staticmethod
    def recorded_start(c, turn_id):
        """Read the exact committed start after response loss, never restart it."""
        from .voice_control_intent import VoiceControlIntent
        row = c.execute('''SELECT p.context_manifest_json,p.status AS plan_status,
            p.child_id,p.session_id,s.status AS step_status,s.error_code,s.result_json,t.tool_error_code,
            ds.status AS session_status FROM agent_plan p JOIN agent_step s USING(plan_id)
            JOIN agent_turn t ON t.agent_turn_id=p.agent_turn_id AND t.child_id=p.child_id AND t.session_id=p.session_id
            JOIN daily_session ds ON ds.session_id=p.session_id AND ds.child_id=p.child_id
            WHERE p.agent_turn_id=? AND s.step_index=0 AND s.capability_name='task.start' ''', (turn_id,)).fetchone()
        if not row:
            return None
        manifest = json.loads(row['context_manifest_json'])
        if not manifest.get('task_start_arrival') or not manifest.get('voice_control_binding'):
            return None
        control = VoiceControlIntent.inspect_execution(c, turn_id, allow_planned=True)
        if not control or not control.get('recorded_receipt'):
            return None
        journal = c.execute('SELECT status FROM voice_control_intent_request WHERE request_key=?',
                            (control['request_key'],)).fetchone()
        # The task event is the durable side-effect receipt.  A constraint or
        # trigger can still reject the later agent-step receipt update, leaving
        # an IntegrityError even though this exact start was committed.  The
        # event, voice binding, owner and source checks below must all pass
        # before that failure is treated as replayable.
        failures = {'OSError', 'OperationalError', 'IntegrityError', 'APP_RESTARTED'}
        saved = json.loads(row['result_json']) if row['result_json'] else {}
        waiting = (row['plan_status'] == 'waiting_child' and row['step_status'] == 'completed'
                   and saved.get('requires_child_reply') is True
                   and isinstance(manifest.get('goal_prompt_binding'), dict)
                   and (saved.get('result', {}).get('diagnostic_prompt', {}).get('prompt') or {}).get('prompt_id')
                       == manifest['goal_prompt_binding'].get('prompt_id'))
        if (row['session_status'] != 'active' or control['control'] != 'task.start'
                or not journal or journal['status'] != 'completed'
                or (not waiting and (row['plan_status'] not in {'failed', 'completed'}
                                     or row['step_status'] != row['plan_status']))
                or (row['step_status'] == 'failed' and
                    (row['error_code'] not in failures or row['tool_error_code'] not in failures))):
            raise TaskStartBindingError('TASK_START_AUTHORIZATION_CHANGED')
        target = control['targets']['task_start']['item']
        item = TaskStartBinding._item(c, row, target['plan_item_id'])
        if any(item[k] != target[k] for k in ('plan_id', 'source_type', 'source_ref', 'task_type')):
            raise TaskStartBindingError('TASK_START_SOURCE_CHANGED')
        return {'capability': 'task.start', 'response_text': '原任务的启动记录已保存，这次没有再次启动计时。',
                'result': {**dict(item), 'replayed': True, 'recorded_receipt': control['recorded_receipt'],
                           'local_only': True, 'cloud_transmission_allowed': False},
                'replayed': True, 'recorded_receipt': True, 'replay_audio_allowed': False,
                'evidence': [], 'requires_child_reply': False}

    @classmethod
    def authorize(cls, c, turn_id, item_id, operation):
        voice = c.execute('''SELECT s.capability_name,p.context_manifest_json,p.child_id,p.session_id FROM agent_plan p
            JOIN agent_step s USING(plan_id) WHERE p.agent_turn_id=? AND s.step_index=0''', (turn_id,)).fetchone()
        if voice and voice['capability_name'] == 'task.resume':
            from .voice_control_intent import VoiceControlIntent
            control = VoiceControlIntent.inspect_execution(c, turn_id)
            target = (control or {}).get('targets', {}).get('task_resume', {}).get('item') or {}
            from .legacy_listening_review import mapped_target, validate_mapped_start
            original_target = target
            target = mapped_target(c, turn_id, target)
            binding = json.loads(voice['context_manifest_json']).get('task_start_binding')
            if (not control or control['control'] != 'task.resume' or control.get('recorded_receipt')
                    or operation != 'start' or target.get('operation') not in {'start','supply'}
                    or target.get('plan_item_id') != item_id
                    or binding != {'plan_item_id': item_id, 'source_type': target['source_type'],
                                   'source_ref': target['source_ref'], 'operation': 'start'}):
                raise TaskStartBindingError('TASK_START_AUTHORIZATION_CHANGED')
            if original_target != target:
                validate_mapped_start(c, turn_id, item_id)
            if (target.get('operation') == 'supply'
                    and json.loads(voice['context_manifest_json']).get('supply_resume_receipt') != target):
                raise TaskStartBindingError('TASK_START_AUTHORIZATION_CHANGED')
            owner = voice
        else:
            owner = cls._turn(c, turn_id)
            binding = json.loads(owner['context_manifest_json']).get('task_start_binding') or {}
            item = cls._item(c, owner, item_id)
            if (binding.get('plan_item_id') != item_id or binding.get('operation') != operation
                    or binding.get('source_type') != item['source_type'] or binding.get('source_ref') != item['source_ref']):
                raise TaskStartBindingError('TASK_START_BINDING_CHANGED')
            from .legacy_listening_review import validate_mapped_start
            validate_mapped_start(c, turn_id, item_id)
        if operation == 'start':
            from .supply_gap_resume import validate_placed_start_budget
            validate_placed_start_budget(c, owner['session_id'], owner['child_id'], item_id)
            from .legacy_listening_review import validate_current_budget
            validate_current_budget(c, owner['session_id'], owner['child_id'], item_id)
