"""Current-session practice from a durable, individually confirmed old question."""
import hashlib
import json
import re
from uuid import uuid4

from . import document_goal_handoff as goal
from .document_review_schedule import ORIGIN, ZONE, parse as parse_time, time_label
from .repository import utc_now

VERSION = 'document_revisit_v13'
PROMPT = (
    '只理解当前复习对话的意图，不回答题目、不评分、不执行。输入只有当前原话、阶段、时间和时区。'
    '仅输出JSON对象，基础字段为meaning、quote、time、confidence；后述条件可额外输出query、revision、location或undo。quote逐字复制完整原话，confidence为0到1。'
    '只有describe_target或revise_target额外输出query字段：从当前原话提取的简短知识概念检索词，1至80字，不能猜测未说出的条件或答案。'
    'meaning=start|answer|help|retry_answer|independent_check|finish_check|propose|confirm|retry_confirm|retry_decline|cancel|status|other_request|unclear|select_1|select_2|select_3|resume_decline|describe_target|revise_target|locate_revision|undo_revision|review_explanation。'
    'check_feedback阶段已经保存刚做的新题结果。追问刚才新题用help；再要不同题用independent_check；查询已有结果用status。'
    '明确请求把自己前面几段、整套或完整解题思路合起来评价，meaning=review_explanation。'
    '例如“我讲完了，看看整体思路对不对”“前面几段合起来有什么遗漏”是review_explanation。'
    '只要求读回原话用status；只说讲完了但没请求评价用unclear；让助手完整讲题仍为help。'
    'check_feedback阶段明确请求把同一道题重新口述解释或按反馈重答，用retry_answer；仅提出请求，不把请求当作新答案。'
    '必须区分解释者：孩子自己要讲给助手听才是retry_answer；让助手再解释、讲解或提示某一步一律是help，不能替孩子开启重答。'
    '例如“让我再讲给你听”为retry_answer，“让我听你再解释”或“能再解释最后一步吗”为help。'
    '已经直接给出补充答案仍为answer，不是retry_answer；否定重答、引用或只说听懂了不是重答请求。'
    '优先看当前原话是否已经包含孩子自己的具体计算、条件、因果或解释：已有解题内容就用answer，即使开头说“不对”“我改一下”或“我应该”。'
    'retry_answer仅用于尚未实际作答的重讲请求；例如“不是先平均，我应该先求总数再除以人数”为answer，不要要求孩子重复已说出的内容。'
    'check_feedback阶段孩子明确给出替换自己先前回答的新说法，meaning仍为answer，并额外输出revision对象，恰好包含target、target_quote、replacement_quote。'
    '只有实际更正才输出revision；补充、求讲解、引用别人、假设修改、否定修改或仅要求重讲都不输出。'
    '询问一种假设改法是否正确是help，不是unclear或实际更正；例如“如果改成这样会不会对”应讨论这个问题，不直接采用假设内容。'
    'target_quote逐字复制原话中的定位短语；明确整段回答的序号时target为正整数，明确上一段或最后一段时为last；“那个”“那一步”“那句话”或无法唯一定位时target为null。'
    'replacement_quote逐字复制孩子实际说出的替换内容，不含修改请求的前缀，不补算答案、不扩写；只想改但没说新内容，不输出revision。'
    '例如“第二段回答我改成：先求总数，再平均分”输出target=2、target_quote=第二段回答、replacement_quote=先求总数，再平均分。'
    '例如“刚才那步不对，应该先算总数”输出target=null、target_quote=刚才那步、replacement_quote=先算总数；不能猜成最后一段。'
    'check_feedback阶段只指出要改的回答位置、没有给出新答案时，用locate_revision并额外输出location对象，恰好含target和target_quote，定位规则与revision相同。'
    '例如“第二段”“是我上一段回答”“改第一段就好”为locate_revision；只有本地存在唯一待定位更正时才会应用，不推测更正内容。'
    '单纯询问、否定修改、引用别人或让助手讲解某段不是locate_revision；“再解释第二段”为help，“不是第二段”没有明确新位置为unclear。'
    'check_feedback阶段明确撤回自己刚才最后一次更正、恢复该处原说法，用undo_revision，不附加revision或location。'
    '例如“刚才的更正先不要了”“撤回我最后一次修改，还是用原来的说法”为undo_revision；只是想问能不能撤回用help，否定撤回或引用他人不用undo_revision。'
    '否定撤回、明确保留更正，用unclear，不要执行撤回。undo_revision不是删除录音、撤销评分、结束学习或取消复习。'
    '明确撤回某段回答的更正时，meaning=undo_revision并必须附加undo对象，恰好含target和target_quote，定位规则与location相同。'
    '例如“把第二段的更正撤回”必须输出undo.target=2、undo.target_quote=第二段，不可省略目标或误当最后一次。'
    '若说那一步、那句话的更正，用undo.target=null及逐字定位短语，不猜成最后一次更正。明确第几次修改而非第几段回答用other_request，不能混淆两个序号。'
    '请求撤回昨天、别的题目或别人的更正，或同时要求撤回和结束今天学习，均用other_request，不操作当前回答。'
    'retry_confirm和retry_decline仅用于retry_proposal阶段的生成失败重试确认；check_feedback只说不想重答为unclear，不能取消回顾。'
    'target_choice阶段刚提出可能的学习目标。明确选第一/第二/第三项用select_1/select_2/select_3；同意唯一一项用start。'
    'target_choice说都不对、不选或先不检查，用resume_decline，不取消原题；不能从孩子的新问题推断目标。'
    'target_choice仅表示犹豫、还没想好或不知道选哪项，用unclear，保留候选；犹豫不等于拒绝。'
    'target_search_pending阶段上次查找题型未完成。明确要求再找、重新查找用independent_check；问原因或进度用status。'
    'target_search_pending说先别找、先回原题用resume_decline，明确继续原题用start；不会或求帮助用help。'
    '仅在target_search_pending或target_choice阶段，明确补充这道题要求、条件或学习目标用describe_target；'
    '明确纠正前面目标描述、说刚才描述错了用revise_target。两者不是作答、评分、选中目标或确认掌握。'
    '无法从当前原话识别概念用unclear，不输出query。新的一道题、设备操作、要求改写原题用other_request，不抢占。'
    '该阶段只是查找失败，不是孩子回答错误；不能从谢谢、好或沉默推断重新查找或作答。'
    '只有check_feedback阶段明确结束这次原题回顾用finish_check，不表示结束今天学习或掌握知识；听懂了、谢谢不是结束。'
    '要求助手代写或删除已保存原答用other_request；孩子给出自己的新说法按上述answer及revision规则处理，只形成派生版本不覆盖原音。要求结束今天学习同样用other_request。'
    'independent_check只表示明确要求换一道相同学习目标的新题考自己，不是继续回答原题、求提示或自称听懂。'
    '仅说听懂了、知道了、谢谢或表示理解，用unclear；不是help、answer或independent_check，不自动继续讲解或出题。'
    'memory_offer、memory_choice、memory_stale尚未选定原题，要求换题用other_request，不推断目标。'
    'memory_offer是刚回顾过旧学习记录，明确愿意继续原题用start；不要把新问题当作继续。'
    'memory_choice是刚读回候选题号，明确选第一/第二/第三题分别用select_1/select_2/select_3。'
    '只有明确说出的序号才可选择，不能推断题号；仅说继续用start，含糊指代用unclear。'
    'memory_offer或memory_choice阶段不愿继续用resume_decline，不是取消原来的复习计划。'
    'memory_stale表示刚才回顾的来源已失效，想继续原题仍用start，请求另一题、重新拍题或设备控制用other_request。'
    'memory_stale阶段不想继续用resume_decline；不能从失效记录推断答案或题号。'
    'offered阶段明确愿意现在复习才是start；没有说愿意不要自动开始。'
    'waiting_answer或feedback_ready阶段孩子在尝试作答才是answer；不会、请求提示或讲解为help；不把题目、指令或闲聊当作答。'
    '其它学习问题或设备控制用other_request，不抢占。cancel只表示明确取消这道题的复习。'
    'propose为改约具体时间，time为{"kind":"relative","minutes":整数}或'
    '{"kind":"calendar","date":"YYYY-MM-DD","time":"HH:mm"}；不猜八点的早晚或放学的时刻。'
    'confirm仅用于time_proposal阶段明确同意刚读回的时间。其他meaning的time必须null。'
    '尚未完成反馈时要求继续反馈用help，不捏造另一份答案。状态查询用status。'
    'retry_proposal阶段刚说明原点评结果未知，询问是否重新调用云端生成。明确同意重新生成为retry_confirm；'
    '明确不重试或稍后再说为retry_decline，只保留原题不取消复习。仅问状态或说继续核对不算同意付费重生成。'
    'retry_confirm/retry_decline仅适用于retry_proposal阶段，time必须null。'
)


def parse(content, transcript, anchor, *, native=False):
    raw = json.loads(content)
    goal.require(isinstance(raw, dict), 'REVISIT_SEMANTICS_INVALID')
    meaning = raw.get('meaning')
    concept=raw.pop('query',None)
    revision=raw.pop('revision',None)
    location=raw.pop('location',None)
    undo=raw.pop('undo',None)
    if meaning=='undo_revision' and re.search(r'第[一二两三四五六七八九十百零\d]+段|上一段|最后一段|那一步|那步|那句话',transcript):
        goal.require(undo is not None,'DOCUMENT_UNDO_TARGET_REQUIRED')
    if undo is not None:
        from .document_spoken_location import parse as parse_location
        goal.require(meaning=='undo_revision','DOCUMENT_UNDO_ROLE')
        undo=parse_location(undo,transcript)
    if meaning=='locate_revision':
        from .document_spoken_location import parse as parse_location
        goal.require(revision is None,'DOCUMENT_LOCATION_ROLE')
        location=parse_location(location,transcript)
    else:
        goal.require(location is None,'DOCUMENT_LOCATION_UNEXPECTED')
    if revision is not None:
        from .document_spoken_revision import parse as parse_revision
        goal.require(meaning=='answer', 'DOCUMENT_SPOKEN_REVISION_ROLE')
        revision=parse_revision(revision,transcript)
    if meaning in {'describe_target','revise_target'}:
        goal.require(isinstance(concept,str) and 0<len(concept.strip())<=80
            and concept==concept.strip() and not any(ord(ch)<32 for ch in concept),'REVISIT_TARGET_QUERY_INVALID')
    else:
        goal.require(concept is None,'REVISIT_TARGET_QUERY_UNEXPECTED')
    extra = meaning in {'start','answer','help','retry_answer','independent_check','finish_check','retry_confirm','retry_decline','select_1','select_2','select_3','resume_decline','describe_target','revise_target','locate_revision','undo_revision','review_explanation'}
    value = parse_time(json.dumps({**raw, 'meaning':'status' if extra else meaning}), transcript, anchor, native=native)
    if extra and value['meaning'] != 'unclear':
        value['meaning'] = meaning
        if meaning in {'describe_target','revise_target'}:
            value['query']=concept
        if revision is not None:
            value['revision']=revision
        if location is not None:
            value['location']=location
        if undo is not None:
            value['undo']=undo
    value['status'] = 'document_revisit'
    return value


def parse_journal(raw,transcript,anchor):
    if raw.get('request_kind')=='native_document_control_v1':
        from .realtime_native_document_controls import parse_native_journal
        return parse_native_journal('learning.review_revisit',raw,transcript,anchor)
    fields={k:raw[k] for k in ('meaning','quote','time','confidence')}
    if 'query' in raw:
        fields['query']=raw['query']
    if 'revision' in raw:
        fields['revision']=raw['revision']
    if 'location' in raw:
        fields['location']=raw['location']
    if 'undo' in raw:
        fields['undo']=raw['undo']
    return parse(json.dumps(fields),transcript,anchor)


def retained(c, commitment_id, child):
    """Historical evidence check. This grants neither execution nor cloud access."""
    commitment = c.execute('SELECT * FROM agent_commitment WHERE commitment_id=? AND child_id=?',
        (commitment_id, child)).fetchone()
    goal.require(commitment and commitment['commitment_type'] == 'reminder', 'REVISIT_NOT_FOUND')
    payload = json.loads(commitment['payload_json'])
    goal.require(isinstance(payload, dict) and payload.get('origin') == ORIGIN, 'REVISIT_SOURCE_CHANGED')
    root = goal.root_row(c, payload.get('root_turn_id'))
    goal.require(root['child_id'] == child and root['objective_id'] == commitment['objective_id'], 'REVISIT_OWNER_CHANGED')
    state = json.loads(root['context_manifest_json'])[goal.KEY]
    goal.require(state['root_fingerprint'] == goal.root_fingerprint(root)
        and root['tool_status'] == root['step_status'] == root['plan_status'] == 'completed'
        and not root['tool_error_code'] and not root['error_code'], 'REVISIT_SOURCE_CHANGED')
    goal.require(state.get('commitment_id') == commitment_id and state.get('due_at') == commitment['due_at']
        and all(payload.get(k) == state.get(k) for k in
            ('artifact_id','artifact_hash','confirmation_id','tutor_turn_id','tutor_hash')),
        'REVISIT_SOURCE_CHANGED')
    _, fingerprint = goal.artifact(c, root)
    goal.require(fingerprint == payload['artifact_hash'], 'REVISIT_SOURCE_CHANGED')
    confirmed = goal.confirmation(c, root, payload['confirmation_id'], historical=True, expected_hash=state['confirmation_hash'])
    teacher = c.execute('SELECT * FROM tutor_turn WHERE turn_id=?', (payload['tutor_turn_id'],)).fetchone()
    goal.require(teacher and teacher['status']=='completed' and teacher['safety_status']=='passed'
        and goal.tutor_proof(teacher) == payload['tutor_hash']
        and goal.teaching_fingerprint(c, state['teaching_turn_id']) == state['teaching_fingerprint'], 'REVISIT_TEACHING_CHANGED')
    if state.get('time_voice'):
        from .document_review_schedule import validate_voice
        goal.validate_confirmation_voice(c, root, state['confirmation_voice']) if state.get('confirmation_voice') else None
        validate_voice(c, root, state['time_voice'])
    goal.require(all(payload.get(k) is v for k, v in goal.LOCAL.items()), 'REVISIT_BOUNDARY_CHANGED')
    return dict(commitment), payload, root, state, confirmed, dict(teacher)


def current_session(c, session, child):
    row = c.execute('''SELECT s.status,s.state,p.status AS child_status FROM daily_session s
        JOIN child_profile p USING(child_id) WHERE s.session_id=? AND s.child_id=?''', (session,child)).fetchone()
    goal.require(row and row['status'] == row['child_status'] == 'active'
        and row['state'] not in {'completed','evolving'}, 'REVISIT_SESSION_CHANGED')
    if c.execute("SELECT 1 FROM sqlite_master WHERE name='deletion_job'").fetchone():
        goal.require(not c.execute("""SELECT 1 FROM deletion_job WHERE child_id=?
            AND scope IN ('learning','media','tutor','all_history') AND status!='completed' LIMIT 1""", (child,)).fetchone(),
            'REVISIT_DELETION_PENDING')


def due(c, child, now):
    rows = c.execute("""SELECT commitment_id FROM agent_commitment WHERE child_id=?
        AND status IN ('pending','due') AND due_at<=? AND CASE WHEN json_valid(payload_json)
        THEN json_extract(payload_json,'$.origin') END=? ORDER BY due_at,commitment_id""", (child,now,ORIGIN))
    for row in rows:
        try:
            packet = retained(c, row[0], child)
            reference = offer_reference(packet[0])
            if offer_current(c,child,reference,now):
                return reference
        except (ValueError, TypeError, KeyError):
            continue
    return None


def offer_reference(item):
    return item['commitment_id']+':'+hashlib.sha256(item['due_at'].encode()).hexdigest()[:16]


def offer_current(c, child, reference, now):
    try:
        item, payload, *_ = retained(c, reference.partition(':')[0], child)
        work = payload.get('practice',{})
        active = c.execute("SELECT 1 FROM daily_session WHERE session_id=? AND child_id=? AND status='active'",
            (work.get('session_id'),child)).fetchone()
        return (reference == offer_reference(item) and item['status'] in {'pending','due'}
            and goal.instant(item['due_at']) <= goal.instant(now)
            and not(active and work.get('phase') in {'waiting_answer','answer_saved','feedback_ready','time_proposal','retry_proposal','checking_new_question','check_feedback','target_choice','target_search_pending'}))
    except (ValueError, TypeError, KeyError):
        return False


def focus(c, session, child, key, *, native=False):
    current_session(c, session, child)
    latest = c.execute("SELECT agent_turn_id FROM agent_turn WHERE session_id=? AND (?=0 OR intent!='qwen_native_dialogue_unverified') ORDER BY rowid DESC LIMIT 1", (session,int(native))).fetchone()
    from .document_memory_resume import focus as memory_focus
    recalled = memory_focus(c, session, child, latest)
    if recalled:
        return recalled
    active = c.execute("""SELECT commitment_id FROM agent_commitment WHERE child_id=? AND status IN ('pending','due')
        AND CASE WHEN json_valid(payload_json) THEN json_extract(payload_json,'$.practice.session_id') END=?
        ORDER BY rowid DESC""", (child,session)).fetchall()
    candidate = None
    for row in active:
        try:
            packet = retained(c, row[0], child)
        except (ValueError, TypeError, KeyError):
            continue
        work = packet[1].get('practice', {})
        from .document_open_feedback import owns_latest_evaluation
        owns_latest = latest and (latest[0] == work.get('last_turn_id')
            or owns_latest_evaluation(c, packet, latest[0]))
        if owns_latest and work.get('phase') in {'waiting_answer','answer_saved','feedback_ready','time_proposal','retry_proposal','check_feedback','target_choice','target_search_pending'}:
            candidate = packet
            break
    if candidate is None:
        offer = c.execute("""SELECT * FROM agent_proactive_prompt WHERE session_id=? AND child_id=?
            AND source_type='document_revisit' AND status='delivered' ORDER BY rowid DESC LIMIT 1""", (session,child)).fetchone()
        if not offer or goal.instant(offer['expires_at']) <= goal.instant(utc_now()) or not offer_current(c,child,offer['source_id'],utc_now()):
            return None
        newer = c.execute('SELECT 1 FROM agent_turn WHERE session_id=? AND created_at>? LIMIT 1', (session,offer['created_at'])).fetchone()
        if newer:
            return None
        candidate = retained(c,offer['source_id'].partition(':')[0],child)
    item, payload, root, state, confirmed, teacher = candidate
    work = payload.get('practice', {}) if payload.get('practice', {}).get('session_id') == session else {}
    from .voice_control_intent import VoiceControlIntent
    consent = VoiceControlIntent.text_consent_version(c)
    return dict(commitment_id=item['commitment_id'], source_hash=goal.digest(item),
        phase=work.get('phase','offered'), session_id=session, child_id=child,
        consent=list(consent) if consent else None)


def assert_focus(c, session, child, key, binding):
    goal.require(focus(c,session,child,key) == binding, 'REVISIT_FOCUS_CHANGED')


def persist(orchestrator, session, child, session_state, transcript, key, result, voice):
    from .agent_runtime import AgentPlanDecision, ObjectiveDraft, PlannedStep, action_from_plan
    target = result['binding']['document_revisit']
    cap = orchestrator.capability_registry.require('learning.review_revisit', session_state)
    with orchestrator.database.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        old = c.execute('SELECT * FROM agent_turn WHERE client_action_id=?',(key,)).fetchone()
        if old:
            orchestrator._assert_action_payload(old,session,transcript)
            orchestrator._assert_replayed_voice(old,voice['voice_input_id'])
            return orchestrator._get_replayed(c,old['agent_turn_id'])
        assert_focus(c,session,child,key,target)
        goal.require(voice, 'REVISIT_VOICE_REQUIRED')
        if not result.get('request_key'):
            request_key = hashlib.sha256(('voice-document-revisit:'+key).encode()).hexdigest()
            fingerprint = hashlib.sha256(json.dumps([child,session,transcript],ensure_ascii=False).encode()).hexdigest()
            c.execute('''INSERT INTO voice_control_intent_request(request_key,child_id,session_id,fingerprint,
                binding_json,status,decision_json,created_at,completed_at) VALUES(?,?,?,?,?,'completed',?,?,?)''',
                (request_key,child,session,fingerprint,json.dumps(result['binding']),
                 json.dumps({'status':'unavailable','reason':result.get('reason','CONTROL_MODEL_FAILED')}),utc_now(),utc_now()))
            result = {**result,'request_key':request_key}
        item = {'commitment_id': None}
        prior = {}
        if not target.get('memory_resume'):
            item, payload, root, state, confirmed, teacher = retained(c,target['commitment_id'],child)
            prior = payload.get('practice', {})
        mode = 'continue' if prior.get('session_id') == session and prior.get('objective_id') else 'new'
        from .document_open_feedback import spoken_prompt
        preserve_criteria = (mode == 'continue' and spoken_prompt(c,(item,payload,root,state,confirmed,teacher)) is not None)
        decision = AgentPlanDecision(ObjectiveDraft(mode,'learning','回顾已确认的原题',
            () if preserve_criteria else ('保留自己的解题过程并核对反馈',),'check'), PlannedStep(cap,{},'Resume only the bound original question',cap.expected_evidence),
            '我先核对原来的复习安排。', False,1)
        turn = str(uuid4())
        action = {**action_from_plan(decision,transcript,None), **goal.LOCAL,
            'document_revisit_commitment':item['commitment_id'], 'document_revisit_request':result['request_key']}
        if result.get('meaning')=='locate_revision' and not target.get('memory_resume') and prior.get('phase')=='check_feedback':
            from .document_spoken_location import KEY, prepare
            action[KEY]=prepare(c,retained(c,item['commitment_id'],child),result['location'])
        if result.get('meaning')=='undo_revision' and not target.get('memory_resume') and prior.get('phase')=='check_feedback':
            from .document_spoken_undo import KEY,prepare
            action[KEY]=prepare(c,retained(c,item['commitment_id'],child),result.get('undo'))
        if result.get('meaning')=='review_explanation' and not target.get('memory_resume') and prior.get('phase')=='check_feedback':
            from .document_spoken_review import KEY,prepare
            try:
                action[KEY]=prepare(c,retained(c,item['commitment_id'],child))
            except (ValueError,TypeError,KeyError):
                action[KEY]={'status':'source_unavailable'}
        if result.get('meaning') == 'independent_check' and not target.get('memory_resume'):
            from .document_independent_check import plan
            action['document_independent_check'] = plan(c, retained(c,item['commitment_id'],child), session)
        if result.get('meaning') in {'retry_answer','answer'} and not target.get('memory_resume') and prior.get('phase')=='check_feedback':
            from .document_spoken_retry import plan
            try:
                action['document_independent_check'] = plan(c, retained(c,item['commitment_id'],child), session)
                if result['meaning']=='answer':
                    from .document_spoken_supplement import KEY, marker
                    from .document_spoken_revision import prepare as prepare_revision
                    revision=prepare_revision(c,retained(c,item['commitment_id'],child),result.get('revision'))
                    action[KEY]=marker(transcript,voice['voice_input_id'],action['document_independent_check'],revision=revision)
                    action['source_voice_input_id']=voice['voice_input_id']
            except (ValueError, TypeError, KeyError):
                action['document_independent_check'] = {'status':'feedback_unavailable'}
        manifest = {'document_revisit':target}
        if result['binding'].get('document_feedback_projection'):
            action['document_feedback_projection'] = result['binding']['document_feedback_projection']
        orchestrator._freeze_local_voice(c,session,child,transcript,voice,action,manifest)
        c.execute('''INSERT INTO agent_turn(agent_turn_id,child_id,session_id,transcript,intent,response_text,
            action_json,orchestrator_version,created_at,client_action_id) VALUES(?,?,?,?,?,?,?,?,?,?)''',
            (turn,child,session,transcript,VERSION,decision.spoken_transition,json.dumps(action,ensure_ascii=False),VERSION,utc_now(),key))
        metadata = orchestrator.agent_runtime.persist_plan(c, child_id=child,session_id=session,agent_turn_id=turn,
            client_action_id=key,decision=decision,context_manifest=manifest)
        c.execute("UPDATE agent_turn SET action_json=json_set(action_json,'$.agent_runtime',json(?)) WHERE agent_turn_id=?",
            (json.dumps(metadata),turn))
        c.commit()
        return orchestrator._get(c,turn)


def execute(database, turn_id):
    from .agent_runtime import AgentRuntimeStore
    from .voice_control_intent import VoiceControlIntent
    with database.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        turn = c.execute('SELECT * FROM agent_turn WHERE agent_turn_id=?',(turn_id,)).fetchone()
        goal.require(turn,'REVISIT_TURN_MISSING')
        action = json.loads(turn['action_json'])
        from .document_feedback_projection import validate_turn
        validate_turn(c,turn_id)
        journal = c.execute('SELECT * FROM voice_control_intent_request WHERE request_key=?',
            (action.get('document_revisit_request'),)).fetchone()
        from .realtime_native_document_controls import require_request,latest_voice as native_latest_voice
        native=require_request(c,turn,'learning.review_revisit',journal)
        expected = hashlib.sha256(json.dumps([turn['child_id'],turn['session_id'],turn['transcript']],ensure_ascii=False).encode()).hexdigest()
        goal.require(journal and journal['fingerprint'] == expected and journal['status']=='completed'
            and (journal['session_id'],journal['child_id']) == (turn['session_id'],turn['child_id']), 'REVISIT_REQUEST_CHANGED')
        if journal['execution_receipt_json']:
            saved = json.loads(journal['execution_receipt_json'])
            goal.require(saved['agent_turn_id'] == turn_id,'REVISIT_REQUEST_CHANGED')
            from .document_spoken_conversation import validate_receipt
            validate_receipt(c, turn_id, saved)
            frozen = json.loads(journal['binding_json'])
            if frozen.get('document_feedback_projection'):
                from .document_feedback_projection import current
                goal.require(current(c,turn['child_id'],turn['session_id'],turn['transcript'],frozen),
                    'DOCUMENT_FEEDBACK_PROJECTION_CHANGED')
            from .document_independent_check import replayed
            return replayed(saved)
        binding = json.loads(journal['binding_json'])
        target = binding['document_revisit']
        current_session(c,turn['session_id'],turn['child_id'])
        goal.require(action.get('document_revisit_commitment') == target.get('commitment_id'), 'REVISIT_TARGET_CHANGED')
        voice = AgentRuntimeStore.resolve_captured_voice(c,session_id=turn['session_id'],child_id=turn['child_id'],
            voice_input_id=action.get('voice_input_id'),transcript=turn['transcript'])
        goal.require(AgentRuntimeStore.voice_capture_snapshot(voice) == action.get('voice_input_binding'), 'REVISIT_VOICE_CHANGED')
        latest_voice = native_latest_voice(c,turn['session_id'],turn['child_id']) if native else c.execute("""SELECT voice_input_id FROM voice_input WHERE session_id=? AND child_id=?
            AND purpose='agent_command' AND status='completed' ORDER BY rowid DESC LIMIT 1""",
            (turn['session_id'],turn['child_id'])).fetchone()
        goal.require(latest_voice and latest_voice[0] == voice['voice_input_id'], 'REVISIT_NEWER_VOICE')
        VoiceControlIntent.assert_current_input(c,turn['session_id'],turn['child_id'],turn['client_action_id'],turn['transcript'],voice)
        latest = c.execute('SELECT agent_turn_id FROM agent_turn WHERE session_id=? ORDER BY rowid DESC LIMIT 1',(turn['session_id'],)).fetchone()
        goal.require(latest and latest[0] == turn_id,'REVISIT_NEWER_INPUT')
        consent = VoiceControlIntent.text_consent_version(c)
        goal.require((list(consent) if consent else None) == target['consent'], 'REVISIT_CONSENT_CHANGED')
        raw = json.loads(journal['decision_json'])
        meaning = 'unclear'
        if raw.get('status') != 'unavailable':
            value = parse_journal(raw,turn['transcript'],binding['review_clock'])
            goal.require(value == raw,'REVISIT_DECISION_CHANGED')
            meaning = value['meaning']
            if binding.get('document_feedback_projection'):
                from .document_feedback_projection import current
                goal.require(meaning=='help' and current(c,turn['child_id'],turn['session_id'],turn['transcript'],binding),
                    'DOCUMENT_FEEDBACK_READ_ONLY')
        from_memory = bool(target.get('memory_resume'))
        if from_memory:
            from .document_memory_resume import resolve
            choice, response = resolve(c, turn, target, meaning)
            if response:
                c.execute('UPDATE voice_control_intent_request SET execution_receipt_json=? WHERE request_key=?',
                    (json.dumps(response, ensure_ascii=False), journal['request_key']))
                c.commit()
                return response
            target = {**target, **choice}
            # The journal freezes the full choice list; downstream evidence also
            # needs the selected original identity, never a model-provided ID.
            action['document_revisit_commitment'] = choice['commitment_id']
            c.execute('UPDATE agent_turn SET action_json=? WHERE agent_turn_id=?',
                (json.dumps(action, ensure_ascii=False), turn_id))
            meaning = 'start'
        item, payload, root, state, confirmed, teacher = retained(c,target['commitment_id'],turn['child_id'])
        goal.require(goal.digest(item) == target['source_hash'], 'REVISIT_SOURCE_CHANGED')
        work = payload.get('practice', {})
        if from_memory or work.get('session_id') != turn['session_id']:
            work = {**work, 'phase':'offered','responses':work.get('responses',[]), 'session_id':turn['session_id']}
        response = '这道题的复习安排还在。你想现在试一试，还是另约一个具体时间？'
        check = None
        location_event = None
        undo_event = None
        location_chain = None
        if raw.get('status') == 'unavailable':
            response = '这次语音理解没有成功，原题和作答都保留着，没有改动复习安排。'
        elif meaning == 'start' and work['phase'] == 'offered':
            work.update(phase='waiting_answer', started_at=utc_now(), start_turn_id=turn_id)
            response = '原题是：'+confirmed['confirmed_text']+'。先自己试着说出答案和思路；需要帮助也可以告诉我。'
        elif work['phase'] in {'target_search_pending','target_choice'} and meaning in {'describe_target','revise_target'}:
            from .document_target_clarification import append
            response=append(c,(item,payload,root,state,confirmed,teacher),turn,value,action)
            work['phase']=('target_choice' if payload.get('independent_target_hypothesis',{}).get('status')=='proposed'
                else 'target_search_pending')
        elif work['phase']=='target_choice' and meaning in {'select_1','select_2','select_3','start'}:
            from .document_target_hypothesis import select,KEY
            from .document_independent_check import plan,begin
            packet=(item,payload,root,state,confirmed,teacher)
            selected=select(c,packet,turn,meaning)
            if selected is None:
                response='还没有明确选定，请说候选项的序号，或说都不对。'
                if payload[KEY]['status']=='stale':
                    work['phase']='waiting_answer'
                    response='刚才候选的来源发生了变化，未按旧选择出题。原题保留，可以重新查找目标。'
            else:
                action['document_target_selection']=goal.digest(payload[KEY])
                c.execute('UPDATE agent_turn SET action_json=? WHERE agent_turn_id=?',(json.dumps(action),turn_id))
                action['document_independent_check']=plan(c,packet,turn['session_id'])
                c.execute('UPDATE agent_turn SET action_json=? WHERE agent_turn_id=?',(json.dumps(action),turn_id))
                check,response=begin(c,turn,packet,action['document_independent_check'])
                work['independent_request']={'turn_id':turn_id,'binding':action['document_independent_check']}
                work['phase']='waiting_answer'
                if check:
                    work.update(phase='checking_new_question',independent_prompt_id=check['prompt']['prompt_id'])
        elif work['phase']=='target_choice' and meaning=='resume_decline':
            from .document_target_hypothesis import KEY
            payload[KEY]['status']='declined'
            work['phase']=work.pop('search_return_phase','waiting_answer')
            if work['phase']=='offered':
                work['phase']='waiting_answer'
            response='先不采用这些目标，原题和你的请求都保留。我不会据此判断薄弱点或掌握。'
        elif work['phase']=='target_choice' and meaning in {'status','unclear','help','answer','independent_check'}:
            from .document_target_hypothesis import readback
            valid,response=readback(c,(item,payload,root,state,confirmed,teacher))
            if not valid:
                work.setdefault('search_return_phase','waiting_answer')
                work['phase']='target_search_pending'
        elif meaning == 'independent_check' and work['phase'] in {'offered','waiting_answer','feedback_ready','check_feedback','target_search_pending'}:
            from .document_independent_check import begin
            check, response = begin(c,turn,(item,payload,root,state,confirmed,teacher),action.get('document_independent_check'))
            work['independent_request'] = {'turn_id':turn_id, 'binding':action['document_independent_check']}
            if check:
                work.update(phase='checking_new_question', independent_prompt_id=check['prompt']['prompt_id'])
            elif payload.get('independent_target_hypothesis',{}).get('status')=='proposed':
                if work['phase']!='target_search_pending':
                    work['search_return_phase']=work['phase']
                work['phase']='target_choice'
            elif payload.get('independent_target_hypothesis',{}).get('status')=='unavailable':
                if work['phase']!='target_search_pending':
                    work['search_return_phase']=work['phase']
                work['phase']='target_search_pending'
        elif work['phase']=='target_search_pending' and meaning in {'status','unclear'}:
            from .document_target_hypothesis import search_message
            response=search_message((item,payload,root,state,confirmed,teacher)) or '上次查找依据需要重新核对，原题还在。'
        elif work['phase']=='target_search_pending' and meaning in {'resume_decline','start'}:
            from .document_target_hypothesis import KEY
            if payload.get(KEY):
                payload[KEY]['status']='declined'
            work['phase']=work.pop('search_return_phase','waiting_answer')
            if work['phase']=='offered':
                work['phase']='waiting_answer'
            response='先不重新查找。原题、已有作答和复习安排都保留着，没有把你判成不会。'
        elif work['phase']=='target_search_pending' and work.get('search_return_phase')=='check_feedback' and meaning in {'help','answer'}:
            from .document_check_followup import feedback
            work['phase']=work.pop('search_return_phase')
            response=feedback(c,(item,payload,root,state,confirmed,teacher),help_requested=meaning=='help')
        elif work['phase']=='check_feedback' and meaning=='undo_revision':
            from .document_spoken_undo import KEY,record,message
            from .document_spoken_conversation import read as conversation,response as readback
            packet=(item,payload,root,state,confirmed,teacher)
            undo_event=record(c,packet,turn,action.get(KEY),value.get('undo'))
            if undo_event:
                location_chain=conversation(c,packet,applying_turn_id=turn_id)
                response='对应的更正已撤回，原音、其他补充和历史评价保留，没有重新评分。'+readback(location_chain,'')
            else:
                response=message(action[KEY]['status'])
        elif work['phase']=='check_feedback' and meaning=='review_explanation':
            from .document_spoken_review import KEY,prepare
            frozen=action.get(KEY) or {}
            if frozen.get('status')=='ready':
                goal.require(prepare(c,(item,payload,root,state,confirmed,teacher))==frozen,'DOCUMENT_WHOLE_SOURCE_CHANGED')
                work['whole_review_turn_id']=turn_id
                response='我把你当前确认的几段思路合起来核对，再一起找下一步。'
            elif frozen.get('status')=='needs_clarification':
                response='还有一处更正没有确定位置。先告诉我要改第几段，再一起核对完整思路。'
            else:
                response='原话都保留着，但当前题目或原音依据还不能核对，暂时不能评价完整思路。'
        elif work['phase']=='check_feedback' and meaning=='locate_revision':
            from .document_spoken_location import KEY, record, messages
            from .document_spoken_conversation import read as conversation, response as readback
            packet=(item,payload,root,state,confirmed,teacher)
            location_event=record(c,packet,turn,action.get(KEY),value['location'])
            if location_event:
                location_chain=conversation(c,packet,applying_turn_id=turn_id)
                response=readback(location_chain,'这次只确认了位置，没有新增作答或重新评分。')
            else:
                response=messages(action[KEY]['status'])
        elif work['phase']=='check_feedback' and (meaning=='retry_answer' or meaning=='answer' and action.get('document_spoken_supplement')):
            from .document_spoken_retry import begin
            retry_binding=action.get('document_independent_check') or {}
            if retry_binding.get('status') != 'ready':
                response='前一次回答保留着，但当前口述题或反馈依据还不能核对，暂时没有开启新的重答。'
            else:
                check,response=begin(c,turn,(item,payload,root,state,confirmed,teacher),retry_binding)
                work.update(phase='checking_new_question', independent_prompt_id=check['prompt']['prompt_id'],
                    independent_request={'turn_id':turn_id,'binding':retry_binding})
        elif work['phase']=='check_feedback' and meaning in {'status','help','unclear','answer'}:
            if binding.get('document_feedback_projection'):
                from .document_feedback_projection import explain
                goal.require(meaning=='help','DOCUMENT_FEEDBACK_READ_ONLY')
                response=explain(c,turn['child_id'],turn['session_id'],turn['transcript'],binding)
            else:
                from .document_check_followup import feedback
                response=feedback(c,(item,payload,root,state,confirmed,teacher),help_requested=meaning=='help')
        elif work['phase']=='check_feedback' and meaning=='finish_check':
            item['status']='completed'
            work.update(phase='check_closed',closed_turn_id=turn_id)
            response='这次原题回顾已结束，新题作答和后续复习安排都保留着；没有把结束回顾当成已经掌握。'
        elif meaning == 'retry_confirm' and work['phase']=='retry_proposal':
            from .document_revisit_retry import confirm
            confirm(c,turn,work,action)
            response='收到，只针对刚才保留的原题和原回答重新生成一次点评。'
        elif meaning == 'retry_decline' and work['phase']=='retry_proposal':
            work.update(phase='answer_saved')
            work.pop('retry_proposal')
            response='先不重新生成。原题和你的回答都保留着，复习安排没有取消。'
        elif work['phase']=='retry_proposal' and meaning in {'help','status','unclear'}:
            from .document_revisit_retry import MESSAGE
            response=MESSAGE
        elif meaning in {'answer','help'} and work['phase'] in {'waiting_answer','feedback_ready','target_search_pending'}:
            current_session_turns = {
                response.get('turn_id')
                for response in work.get('responses', [])
                if isinstance(response, dict) and response.get('session_id') == turn['session_id']
            }
            feedback_in_current_session = any(
                isinstance(feedback, dict) and feedback.get('response_turn_id') in current_session_turns
                for feedback in work.get('feedbacks', [])
            )
            work['responses'].append({'turn_id':turn_id,'voice_input_id':voice['voice_input_id'],
                'text':turn['transcript'],'kind':meaning,'session_id':turn['session_id'],
                'after_feedback':feedback_in_current_session or work['phase']=='feedback_ready','created_at':utc_now()})
            work.update(phase='answer_saved', response_turn_id=turn_id)
            response = '你的思路已经保存。我会围绕这道原题核对，不另换一道题。'
        elif meaning == 'propose' and value['due_at'] and goal.instant(value['due_at']) > goal.instant(utc_now()):
            work.update(phase='time_proposal', proposal={'due_at':value['due_at'], 'turn_id':turn_id,
                'return_phase':work['phase'], 'source_hash':target['source_hash']})
            response = '改到'+time_label(value['due_at'])+'再看这道题，对吗？确认前原安排不变。'
        elif meaning == 'confirm' and work['phase'] == 'time_proposal':
            proposed = work['proposal']
            prior = c.execute('''SELECT t.*,s.status AS step_status FROM agent_turn t
                JOIN agent_plan p USING(agent_turn_id) JOIN agent_step s USING(plan_id)
                WHERE t.agent_turn_id=? AND s.capability_name='learning.review_revisit' AND s.step_index=0''', (proposed['turn_id'],)).fetchone()
            goal.require(prior and prior['step_status']=='completed' and prior['tool_status']=='completed'
                and (prior['session_id'],prior['child_id'])==(turn['session_id'],turn['child_id'])
                and goal.instant(proposed['due_at']) > goal.instant(utc_now()), 'REVISIT_PROPOSAL_STALE')
            prior_action=json.loads(prior['action_json'])
            prior_journal=c.execute('SELECT * FROM voice_control_intent_request WHERE request_key=?',
                (prior_action.get('document_revisit_request'),)).fetchone()
            goal.require(prior_journal and prior_journal['status']=='completed' and prior_journal['execution_receipt_json'],
                'REVISIT_PROPOSAL_STALE')
            prior_binding=json.loads(prior_journal['binding_json'])
            prior_decision=json.loads(prior_journal['decision_json'])
            verified=parse_journal(prior_decision,prior['transcript'],prior_binding['review_clock'])
            prior_voice=AgentRuntimeStore.resolve_captured_voice(c,session_id=turn['session_id'],child_id=turn['child_id'],
                voice_input_id=prior_action.get('voice_input_id'),transcript=prior['transcript'])
            readback='改到'+time_label(proposed['due_at'])+'再看这道题，对吗？确认前原安排不变。'
            goal.require(verified==prior_decision and verified['meaning']=='propose' and verified['due_at']==proposed['due_at']
                and prior_binding['document_revisit']['commitment_id']==item['commitment_id']
                and prior_binding['document_revisit']['source_hash']==proposed['source_hash']
                and AgentRuntimeStore.voice_capture_snapshot(prior_voice)==prior_action.get('voice_input_binding')
                and json.loads(prior_journal['execution_receipt_json'])['response_text']==prior['response_text']==readback,
                'REVISIT_PROPOSAL_CHANGED')
            item['due_at'] = proposed['due_at']
            state.update(due_at=item['due_at'],phase='scheduled',revision=state['revision']+1)
            goal.store(c,root,state)
            work.update(phase='offered')
            work.pop('proposal')
            response = '已改到'+time_label(item['due_at'])+'，原题和已有作答都保留。'
        elif meaning == 'cancel':
            item['status'] = 'cancelled'
            state.update(phase='review_cancelled',revision=state['revision']+1)
            goal.store(c,root,state)
            work.update(phase='cancelled')
            response = '这道题的复习安排已取消，没有删除原题，也没有结束其它学习任务。'
        elif meaning == 'status':
            response = ('原题是：'+confirmed['confirmed_text']+'。你可以继续说自己的思路。' if work['phase']=='waiting_answer' else
                '这道原题和已有记录都保留着，反馈尚未完成。' if work['phase']=='answer_saved' else
                '这道题约定的时间是'+time_label(item['due_at'])+'。')
        elif meaning == 'unclear' and work['phase'] in {'waiting_answer','feedback_ready'}:
            response = '要继续试着说说原题的思路，还是换一道同类新题自己试试？听懂之后，还需要独立作答来确认。'
        elif meaning in {'describe_target','revise_target'}:
            response='现在没有等待补充的目标查找，没有改动原题或你的回答。要继续原题，还是另找题检查？'
        objective_id = c.execute('SELECT objective_id FROM agent_plan WHERE agent_turn_id=?', (turn_id,)).fetchone()[0]
        work.update(last_turn_id=turn_id, objective_id=objective_id)
        payload['practice'] = work
        c.execute('UPDATE agent_commitment SET payload_json=?,status=?,due_at=? WHERE commitment_id=?',
            (json.dumps(payload,ensure_ascii=False),item['status'],item['due_at'],item['commitment_id']))
        result = dict(agent_turn_id=turn_id,response_text=response,result=dict(status=work['phase'],
            commitment_id=item['commitment_id'], root_turn_id=root['agent_turn_id'],
            original_question=confirmed['confirmed_text'],subject=teacher['subject'], **goal.LOCAL))
        if location_event:
            result['result'].update(spoken_revision_event=location_event,spoken_conversation=location_chain)
        if undo_event:
            result['result'].update(spoken_undo_event=undo_event,spoken_conversation=location_chain)
        if meaning=='review_explanation' and work.get('whole_review_turn_id')==turn_id:
            result['result']['whole_review_requested']=True
        if work['phase']=='check_feedback' and meaning in {'status','help','unclear','answer'} and not check and not binding.get('document_feedback_projection'):
            from .document_open_feedback import spoken_prompt
            from .document_spoken_conversation import read as conversation
            from .document_spoken_review import latest,latest_clarification
            packet = (item,payload,root,state,confirmed,teacher)
            if spoken_prompt(c, packet) is not None:
                try:
                    result['result']['spoken_conversation'] = conversation(c, packet)
                    whole=latest(c,packet)
                    if whole:
                        result['result']['whole_review']=whole
                    else:
                        clarification=latest_clarification(c,packet)
                        if clarification:
                            result['result']['whole_review_followup']=clarification
                except (ValueError, TypeError, KeyError):
                    pass  # The feedback reader already returns the source-change message.
        if check:
            result['result']['diagnostic_prompt'] = check
            if (action.get('document_independent_check') or {}).get('spoken_retry'):
                result['result']['practice_kind'] = 'spoken_retry'
        retry_confirmed=(meaning=='retry_confirm' and work.get('retry_permission',{}).get('turn_id')==turn_id)
        if work['phase']=='answer_saved' and (meaning in {'answer','help'} or retry_confirmed):
            result['result']['feedback_response_turn_id'] = work['response_turn_id']
            result['result']['response_hash'] = goal.digest(next(r for r in work['responses'] if r['turn_id']==work['response_turn_id']))
            if retry_confirmed:
                result['result']['retry_permission']=work['retry_permission']
        c.execute('UPDATE voice_control_intent_request SET execution_receipt_json=? WHERE request_key=?',
            (json.dumps(result,ensure_ascii=False),journal['request_key']))
        if check and action.get('document_spoken_supplement'):
            from .document_spoken_supplement import submit
            answered=submit(c,turn,check['prompt'])
            goal.require(answered['status']=='answered','DOCUMENT_SUPPLEMENT_NOT_SAVED')
            result['response_text']='这次补充和原音已经单独保存，我按同一道题的要求重新核对，不覆盖前一次回答。'
            result['result'].update(status='check_feedback',practice_kind='spoken_supplement',diagnostic_prompt=answered)
            c.execute('UPDATE voice_control_intent_request SET execution_receipt_json=? WHERE request_key=?',
                (json.dumps(result,ensure_ascii=False),journal['request_key']))
        c.commit()
        return result
