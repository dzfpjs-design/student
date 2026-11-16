"""Read-only household work projection. Business services remain the only writers."""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import sqlite3
from datetime import datetime, timezone

from .curriculum_supply import CurriculumSupplyService
from .learning_rubric import RubricEvidenceService
from .parent_reports import ParentReportService


KINDS = ('safety', 'ocr', 'variant', 'specialized', 'rubric', 'report_review', 'delivery_unknown', 'curriculum', 'supply_followup', 'original_question')
PARENT_DECISION_KINDS = frozenset(('safety', 'delivery_unknown'))
TABLES = {
    'original_question': ('agent_commitment', 'agent_turn'),
    'supply_followup': ('plan_constraint_decision', 'daily_plan', 'daily_session', 'plan_item_event',
                        'agent_commitment', 'agent_objective', 'diagnostic_prompt', 'curriculum_supply_package'),
    'safety': ('child_safety_event',), 'ocr': ('ocr_review_item',),
    'variant': ('variant_candidate',),
    'specialized': ('specialized_supply_candidate', 'curriculum_supply_package'),
    'rubric': ('rubric_assessment', 'rubric_review_event'),
    'report_review': ('parent_report_snapshot',),
    'delivery_unknown': ('report_delivery_outbox', 'report_delivery_recipient', 'parent_report_snapshot'),
    'curriculum': ('curriculum_supply_package', 'curriculum_supply_selection', 'specialized_supply_job'),
}
PANELS = {'original_question':'parent-work-original-check', 'supply_followup':'parent-work-supply-followup', 'safety': 'parent-work-safety', 'ocr': 'parent-work-ocr', 'variant': 'parent-work-variant',
          'specialized': 'parent-work-specialized', 'rubric': 'parent-work-rubric',
          'report_review': 'parent-work-reports', 'delivery_unknown': 'parent-work-reports',
          'curriculum': 'parent-work-curriculum'}


class WorkQueueConflict(ValueError):
    pass


class WorkQueueNotFound(KeyError):
    pass


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _text(value, limit=220):
    return ' '.join(str(value or '').split())[:limit]


class ParentWorkQueueService:
    def __init__(self, database):
        self.database = database

    def period_supply_followups(self, child_id, snapshot_id=None, *, period_report_id=None):
        from .parent_supply_followup import current_followups
        with self.database.connect() as c:
            c.execute('PRAGMA query_only=ON')
            c.execute('BEGIN')
            if period_report_id is not None:
                if not c.execute('''SELECT 1 FROM period_report r JOIN child_profile p USING(child_id)
                        WHERE r.period_report_id=? AND r.child_id=? AND p.status='active' ''',
                        (period_report_id, child_id)).fetchone():
                    raise WorkQueueNotFound('WORK_OBJECT_NOT_FOUND')
                selected = c.execute('''SELECT snapshot_id FROM parent_report_snapshot
                    WHERE child_id=? AND source_kind='period_report'
                    AND substr(source_id,1,instr(source_id,'@')-1)=?
                    ORDER BY created_at DESC,rowid DESC LIMIT 1''', (child_id, period_report_id)).fetchone()
                snapshot_id = selected['snapshot_id'] if selected else None
                if snapshot_id is None:
                    return dict(child_id=child_id, snapshot_id=None, period_report_id=period_report_id,
                        as_of=None, status='unavailable', unfulfilled_count=None, items=[], local_only=True,
                        cloud_transmission_allowed=False, mastery_claim_allowed=False)
            row = c.execute('''SELECT s.content_json,s.source_id FROM parent_report_snapshot s
                JOIN child_profile p ON p.child_id=s.child_id AND p.status='active'
                WHERE s.child_id=? AND s.snapshot_id=? AND s.source_kind='period_report' ''',
                (child_id, snapshot_id)).fetchone()
            if not row:
                raise WorkQueueNotFound('WORK_OBJECT_NOT_FOUND')
            content = json.loads(row['content_json'])
            if not isinstance(content, dict) or not isinstance(content.get('summary'), dict):
                raise ValueError('PERIOD_SUPPLY_HISTORY_INVALID')
            history = content['summary'].get('supply_history')
            result = dict(child_id=child_id, snapshot_id=snapshot_id,
                period_report_id=row['source_id'].split('@',1)[0], as_of=None, status='unavailable',
                unfulfilled_count=None, items=[], local_only=True,
                cloud_transmission_allowed=False, mastery_claim_allowed=False)
            if history is None:
                return result
            if not isinstance(history, dict) or history.get('version')!='period_supply_history_v1' or history.get('status')!='available':
                raise ValueError('PERIOD_SUPPLY_HISTORY_INVALID')
            if not isinstance(history.get('items'), list) or any(not isinstance(h, dict) for h in history['items']):
                raise ValueError('PERIOD_SUPPLY_HISTORY_INVALID')
            live = {r['root_obligation_id']:r for r in current_followups(c, child_id)}
            items = [{'historical': h, 'current': live.get(h['root_obligation_id'])} for h in history['items']]
            roots = [item['historical']['root_obligation_id'] for item in items]
            if len(roots) != len(set(roots)):
                raise ValueError('PERIOD_SUPPLY_DUPLICATE_ROOT')
            result.update(status='ready', items=items, as_of=history['as_of'],
                unfulfilled_count=sum(r['current']['status'] not in {'practice_completed','cancelled'} for r in items)
                if all(r['current'] is not None for r in items) else None)
            return result

    def supply_followup(self, child_id, root_id):
        from .parent_supply_followup import current_followups
        with self.database.connect() as c:
            c.execute('PRAGMA query_only=ON')
            c.execute('BEGIN')
            try:
                rows = current_followups(c, child_id)
            except KeyError:
                raise WorkQueueNotFound('WORK_OBJECT_NOT_FOUND') from None
            row = next((r for r in rows if r['root_obligation_id']==root_id), None)
            if not row:
                raise WorkQueueNotFound('WORK_OBJECT_NOT_FOUND')
            return row

    def supply_gap(self, child_id, job_id):
        with self.database.connect() as c:
            c.execute('PRAGMA query_only=ON')
            c.execute('BEGIN')
            row = c.execute('''SELECT j.*,d.prompt FROM specialized_supply_job j
                JOIN child_profile p ON p.child_id=j.child_id AND p.status='active'
                LEFT JOIN diagnostic_item d ON d.diagnostic_item_id=j.seed_item_id
                WHERE j.job_id=? AND j.child_id=?''', (job_id, child_id)).fetchone()
            if not row:
                raise WorkQueueNotFound('WORK_OBJECT_NOT_FOUND')
            stages = {'parallel':'同类练习', 'discrimination':'概念辨析', 'transfer':'迁移', 'near_transfer':'近迁移',
                      'representation_transfer':'换表达迁移', 'far_transfer':'远迁移'}
            return {'child_id': child_id, 'job_id': job_id, 'status': row['status'],
                    'version': row['updated_at'], 'error_code': row['error_code'],
                    'evidence': [{'label':'原题', 'text':_text(row['prompt'], 1200)},
                                 {'label':'所需阶段', 'text':'、'.join(stages.get(s,'待核对阶段') for s in json.loads(row['stages_json']))},
                                 {'label':'实际阻断', 'text':self._gap_reason(row['error_code'])},
                                 {'label':'下一步', 'text':'核对当前教材和已审核来源，补齐所需阶段的供给；本页不会生成、重试或发送。'}],
                    'local_only': True, 'cloud_transmission_allowed': False}

    @staticmethod
    def _gap_reason(code):
        return {'LOCAL_TEMPLATE_UNAVAILABLE':'该阶段缺少经校验的本地供给方案',
                'LOCAL_STAGE_UNSUPPORTED':'当前生成方式不支持所需迁移阶段'}.get(code, '需在原供给记录中核对来源与材料，不能降低原要求。')

    def list(self, *, child_id=None, limit=20, cursor=None, audience='all', connection=None):
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError('WORK_LIMIT_INVALID')
        if audience not in {'all', 'parent_decisions'}:
            raise ValueError('WORK_AUDIENCE_INVALID')
        if connection is None:
            with self.database.connect() as c:
                c.execute('PRAGMA query_only=ON')
                c.execute('BEGIN')
                return self.list(child_id=child_id, limit=limit, cursor=cursor, audience=audience, connection=c)
        c = connection
        children = [dict(r) for r in c.execute("SELECT child_id,nickname FROM child_profile WHERE status='active' ORDER BY child_id")]
        if child_id is not None:
            children = [r for r in children if r['child_id']==child_id]
            if not children:
                raise WorkQueueNotFound('WORK_CHILD_NOT_FOUND')
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        items, sources = [], []
        for child in children:
            for kind in KINDS:
                status = {'child_id': child['child_id'], 'kind': kind, 'status': 'ready', 'total': 0, 'error_code': None}
                try:
                    if not set(TABLES[kind]) <= tables:
                        raise sqlite3.OperationalError('source schema unavailable')
                    projected = list(self._read(c, child, kind))
                    if any(r['child_id'] != child['child_id'] or r['kind'] != kind for r in projected):
                        raise ValueError('source ownership mismatch')
                    items.extend(projected)
                    status['total'] = len(projected)
                except (sqlite3.Error, ValueError, KeyError, TypeError):
                    # One unread source must not hide another source, or appear empty.
                    status.update(status='unavailable', total=None, error_code='WORK_SOURCE_UNAVAILABLE')
                sources.append(status)
        system_items = [item for item in items if item['kind'] not in PARENT_DECISION_KINDS]
        visible_items = [item for item in items if item['kind'] in PARENT_DECISION_KINDS] if audience == 'parent_decisions' else items
        visible_items.sort(key=lambda r: (r['priority'], r['created_at'], r['child_id'], r['work_id']))
        relevant_sources = [source for source in sources if audience == 'all' or source['kind'] in PARENT_DECISION_KINDS]
        system_sources = [source for source in sources if source['kind'] not in PARENT_DECISION_KINDS]
        system_total = len(system_items) if all(source['status']=='ready' for source in system_sources) else None
        revision = _digest({'scope': {'child_id': child_id, 'limit': limit, 'audience': audience}, 'children': children, 'items': visible_items, 'sources': relevant_sources})
        offset = 0
        if cursor is not None:
            try:
                if len(cursor)>400:
                    raise ValueError()
                decoded = json.loads(base64.b64decode(cursor.encode(), altchars=b'-_', validate=True).decode())
                if set(decoded) != {'revision', 'offset'} or type(decoded['offset']) is not int or decoded['offset'] < 0:
                    raise ValueError()
                offset = decoded['offset']
            except (ValueError, TypeError, KeyError, UnicodeError, binascii.Error):
                raise ValueError('WORK_CURSOR_INVALID') from None
            if decoded['revision'] != revision:
                raise WorkQueueConflict('WORK_QUEUE_CHANGED')
            if offset > len(visible_items):
                raise ValueError('WORK_CURSOR_INVALID')
        end = offset + limit
        next_cursor = base64.urlsafe_b64encode(json.dumps({'revision': revision, 'offset': end}).encode()).decode() if end<len(visible_items) else None
        return {'items': visible_items[offset:end], 'sources': relevant_sources, 'children': children,
                'total': len(visible_items) if all(s['status']=='ready' for s in relevant_sources) else None,
                'known_total': len(visible_items), 'system_verification_total': system_total,
                'revision': revision, 'next_cursor': next_cursor,
                'read_at': datetime.now(timezone.utc).isoformat(), 'local_only': True, 'cloud_transmission_allowed': False}

    @staticmethod
    def _item(child, kind, object_id, version, title, impact, priority, created_at, evidence=(), related_id=None, bulk=False):
        return {'work_id': kind+':'+object_id, 'child_id': child['child_id'], 'child_name': child['nickname'],
                'kind': kind, 'object_id': object_id, 'version': str(version), 'title': title, 'impact': impact,
                'priority': priority, 'urgency': 'immediate' if priority==0 else 'today' if priority<=20 else 'review',
                'created_at': created_at or '', 'evidence': [{'label': label, 'text': _text(value)} for label,value in evidence],
                'target': {'panel': PANELS[kind], 'object_id': object_id, 'related_id': related_id},
                'bulk_action': 'ocr_skip' if bulk else None}

    def _read(self, c, child, kind):
        cid = child['child_id']
        item = lambda *args, **kwargs: self._item(child, kind, *args, **kwargs)
        if kind == 'supply_followup':
            from .parent_supply_followup import current_followups
            for r in current_followups(c, cid):
                if r['status'] in {'practice_completed','cancelled'}:
                    continue
                yield item(r['root_obligation_id'], r['version'], r['title'], r['next_action'], 20,
                    r['original_date'], [('原安排日期',r['original_date']),('原原因',r['original_reason']),
                     ('当前情况',r['next_action'])], related_id=r['plan_item_id'])
        if kind == 'safety':
            for r in c.execute("SELECT * FROM child_safety_event WHERE child_id=? AND status='pending'", (cid,)):
                guide = json.loads(r['guidance_json'])
                yield item(r['safety_event_id'], r['created_at'], '线下确认孩子安全',
                    '由家长现实确认，不将系统提醒当作事实诊断。', {'R3':0, 'R2':1}.get(r['risk_level'], 2), r['created_at'],
                    [('提醒等级', r['risk_level']), ('建议', guide.get('parent_message') or guide.get('parent_guidance') or '请查看原始安全提醒并线下确认。')])
        elif kind == 'ocr':
            names = {'text': '文字', 'handwriting': '手写', 'formula': '公式', 'marking': '批改痕迹'}
            for r in c.execute("SELECT * FROM ocr_review_item WHERE child_id=? AND status='pending'", (cid,)):
                yield item(r['review_id'], r['updated_at'], '校对'+names.get(r['review_kind'], '识别内容'),
                    '未核对的识别内容可能影响题目与后续教学。', 30, r['created_at'],
                    [('识别候选（不是已核对原文）', r['proposed_text']), ('位置', '第'+str(r['region_index']+1)+'个区域')],
                    related_id=r['artifact_id'], bulk=r['review_kind']=='text')
        elif kind in ('variant', 'specialized'):
            table = 'variant_candidate' if kind=='variant' else 'specialized_supply_candidate'
            exclusion = '' if kind=='variant' else ' AND NOT EXISTS (SELECT 1 FROM curriculum_supply_package p WHERE p.candidate_id=x.candidate_id)'
            for r in c.execute(f"SELECT x.* FROM {table} x WHERE child_id=? AND status='needs_review'"+exclusion, (cid,)):
                yield item(r['candidate_id'], r['content_fingerprint'], '审核错题变式' if kind=='variant' else '审核专项题',
                    '候选题尚不能当作可靠复测或掌握证据。', 40 if kind=='variant' else 45, r['created_at'],
                    [('题目', r['prompt']), ('阶段', r['stage'])], related_id=r['mistake_id'] if kind=='variant' else r['seed_item_id'])
        elif kind == 'rubric':
            service = RubricEvidenceService(self.database)
            for r in c.execute('SELECT assessment_id FROM rubric_assessment WHERE child_id=?', (cid,)):
                view = service.get(cid, r[0], connection=c)
                if view['score_status'] not in ('candidate', 'source_changed', 'withdrawn'):
                    continue
                changed = view['source_status'] != 'current'
                if not changed and view['score_status'] == 'candidate' and view['review_revision'] == 0:
                    qualification = service.qualification(cid, r[0], connection=c)
                    if (qualification['score_qualified']
                            and (qualification.get('receipt') or {}).get('score_status') == 'calibrated_model'):
                        # Even a low qualified score needs no mandatory human scoring.
                        continue
                yield item(r[0], view['fingerprint']+':'+str(view['review_revision'])+':'+view['source_status'],
                    '评分来源需重新核对' if changed else '审核开放题评分',
                    '参考评分不等于可靠评分，更不证明孩子掌握。', 15 if changed else 20, view['created_at'],
                    [('题目', view.get('question')), ('评分资格', '来源已变化' if changed else '待家长独立核对')], related_id=view['step_id'])
        elif kind == 'report_review':
            seen = set()
            for r in c.execute('SELECT * FROM parent_report_snapshot WHERE child_id=? ORDER BY rowid DESC', (cid,)):
                root = (r['source_kind'], r['report_id'] or r['source_id'].rpartition('@')[0])
                if root in seen:
                    continue
                seen.add(root)
                if r['status'] != 'draft':
                    continue
                current = ParentReportService.snapshot_source_current(c, r)
                content = json.loads(r['content_json'])
                yield item(r['snapshot_id'], r['evidence_version']+':'+str(current),
                    {'daily':'审核日报', 'weekly':'审核周报', 'monthly':'审核月报'}[r['report_type']] if current else '报告来源已变化，需重建后审核',
                    '先核对证据和家长行动；批准报告不会自动发送。', 50, r['created_at'],
                    [('报告', content.get('headline')), ('依据', '当前来源' if current else '旧来源，不能继续批准')])
        elif kind == 'delivery_unknown':
            for r in c.execute('''SELECT o.*,s.child_id,s.report_type,r.label,r.consent_status
                FROM report_delivery_outbox o JOIN parent_report_snapshot s USING(snapshot_id)
                JOIN report_delivery_recipient r USING(recipient_id)
                WHERE s.child_id=? AND r.child_id=s.child_id AND o.status='unknown' ''', (cid,)):
                yield item(r['delivery_id'], r['updated_at'], '核对报告投递未知结果',
                    '可能已被通道接受；不要当作失败直接重发。', 10, r['created_at'],
                    [('收件人', r['label']), ('授权', '已撤销或未授权' if r['consent_status']!='consented' else '已授权'),
                     ('状态', '是否送达未知，未证明已读')], related_id=r['snapshot_id'])
        elif kind == 'original_question':
            from .document_source_review import queue as original_checks
            for original in original_checks(c, cid):
                entry = item(original['commitment_id'], original['version'], '核对原题学习目标',
                    '孩子请求了同类新题，需要可靠目标与题源；关联不代表孩子已掌握。', 20,
                    original['created_at'], [('原题', original['question']), ('当前状态', {
                        'ready':'题源已具备，等待孩子继续', 'new_material_required':'学习目标已确认，仍需补充不同题目',
                        'source_changed':'原题来源已改变，需重新核对', 'pending_question':'已有待答题，不能覆盖',
                    }.get(original['status'], '目标关联待核对'))])
                entry['target']['panel'] = 'parent-work-original-check'
                yield entry
        elif kind == 'curriculum':
            service = CurriculumSupplyService(self.database)
            selections = c.execute('SELECT 1 FROM curriculum_supply_selection WHERE child_id=? LIMIT 1', (cid,)).fetchone()
            if not selections:
                yield item('selection:'+cid, 'none', '选定当前教材与学期', '当前教材范围尚未建立，不能认证课程供给。', 35, '', [('材料状态', '尚无已保存的教材选择')])
            for r in c.execute('SELECT * FROM curriculum_supply_package WHERE child_id=?', (cid,)):
                view = service._get(c, cid, r['candidate_id'])
                if view['eligible']:
                    continue
                state = {'draft':'校验课程草案', 'validated':'审核课程材料', 'reviewed':'发布已审核课程',
                         'withdrawn':'处理已撤回课程材料', 'rejected':'修订被拒绝课程材料', 'published':'已发布题源需重新核对'}.get(view['state'], '补齐课程材料')
                packet = view.get('review_packet') or {}
                yield item(r['candidate_id'], str(view['version'])+':'+_digest(view['blockers']), state,
                    '未审核或失效材料不能用于可靠教学与复测。', 35, r['created_at'],
                    [('题目草案', view['payload']['prompt']), ('教材依据', (packet.get('textbook') or {}).get('title') or '缺少可审核教材依据')])
            for r in c.execute("SELECT * FROM specialized_supply_job WHERE child_id=? AND status='blocked'", (cid,)):
                if c.execute('SELECT 1 FROM specialized_supply_candidate sc JOIN curriculum_supply_package p USING(candidate_id) WHERE sc.job_id=?', (r['job_id'],)).fetchone():
                    continue
                view = item(r['job_id'], r['updated_at'], '补齐专项课程供给', '不能以降低迁移要求或未审核模板替代。', 35, r['created_at'],
                    [('供给缺口', self._gap_reason(r['error_code'])), ('学习范围', r['subject'])], related_id=r['seed_item_id'])
                view['target']['panel'] = 'parent-work-supply-gap'
                yield view
