"""Freeze saved paired-page evidence into the existing source/review pipeline.

No new publication state or mastery authority lives here. Draft identity is a
submission CAS, not an ongoing dependency on the parent's reusable editor.
"""
from __future__ import annotations

import hashlib
import json
from contextlib import nullcontext
from types import SimpleNamespace

from ..curriculum_objective_source import SCHEMA, VALIDATOR, parse as parse_objective, exact_rubric
from ..curriculum_supply import _selection
from ..knowledge_base import KnowledgeBaseService
from ..local_supply import LocalSupplyError, validate_item
from ..repository import utc_now
from ..privacy_operation_receipt import generation
from .parent_textbook_drafts import ParentTextbookDraftService
from .parent_textbook_capture import ParentTextbookCaptureService
from ..standard_relocation_provenance import snapshot_read

PREFIX='parent_paired_paper_objective:'
ACTION='parent_paired_paper_objective_submitted'
EXPLANATION='按家长核对的完整原页参考答案评分；送审本身不计算答案或认证孩子掌握。'


def _json(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _need(value, code='PAPER_OBJECTIVE_SOURCE_CHANGED'):
    if not value:raise LocalSupplyError(code)


def answer_rubric(objective, semantics):
    answers=objective['official_answers']
    _need(isinstance(semantics,dict) and isinstance(answers,list)
          and 1<=len(answers)<=24 and all(isinstance(v,str) and 0<len(v.strip())<=200 for v in answers),
          'PAPER_OBJECTIVE_ANSWER_MEANING_REQUIRED')
    if semantics=={'kind':'whole_answer_variants'}:
        return {'local_rule':'local_exact_v1','exact_answers':answers.copy()}
    _need(set(semantics)=={'kind','numeric_kind','tuple_size'}
          and semantics['kind']=='ordered_numeric_parts' and type(semantics['tuple_size']) is int
          and len(answers)==semantics['tuple_size'], 'PAPER_OBJECTIVE_ORDERED_RUBRIC_REQUIRED')
    rubric=dict(local_rule='local_exact_v1',answer_format='reviewed_spoken_numeric_tuple_v1',
                numeric_kind=semantics['numeric_kind'],tuple_size=semantics['tuple_size'],
                exact_answers=[','.join(v.strip() for v in answers)])
    from ..spoken_numeric_tuple import validate
    try:validate(rubric)
    except (ValueError,TypeError,KeyError,ZeroDivisionError):
        raise LocalSupplyError('PAPER_OBJECTIVE_ORDERED_RUBRIC_REQUIRED') from None
    return rubric


def _page_snapshot(c,db,child,ref,*,authoring=True):
    # Reuse the original encrypted-page reader on the caller's transaction and
    # connection-bound runtime; never guess/reopen a database from its filename.
    if db is None:
        db=SimpleNamespace(local_media_runtime=getattr(c,'local_media_runtime',None),connect=lambda:nullcontext(c))
    capture=ParentTextbookCaptureService(db,None)
    row=c.execute('SELECT * FROM kg_parent_textbook_ocr_artifact WHERE artifact_id=? AND child_id=?',
                  (capture._id_for_ref(ref['capture_ref']),child)).fetchone()
    _need(row is not None and row['status'] in {'needs_review','ready','imported'}
          and (not authoring or capture.source_version(row)==ref['source_version']))
    try:
        capture._validate_retained_source(c,row)
        # Authenticate the complete retained original, without exposing bytes or paths.
        image=capture._read_raw_page(child,row['artifact_id'])
    except (ValueError,OSError):
        raise LocalSupplyError('PAPER_OBJECTIVE_ORIGINAL_UNAVAILABLE') from None
    _need(hashlib.sha256(image).hexdigest()==row['image_sha256'])
    return {'capture_ref':ref['capture_ref'],'source_version':ref['source_version'],
            'image_sha256':row['image_sha256'],'page_context_sha256':row['page_context_sha256'],
            'byte_length':row['raw_image_byte_length']}


def _standards(c,child,node,selection,*,legacy=None):
    scope=json.loads(selection['scope_json']);result=[]
    from ..manual_standard_scope import ManualScopeError, manual_requirement
    for raw in c.execute('''SELECT req.*,page.page_number,page.content_text AS page_text,
          css.status AS source_status FROM curriculum_standard_requirement req
          JOIN curriculum_standard_source css USING(source_id)
          JOIN curriculum_standard_page page ON page.source_id=req.source_id
             AND page.page_number BETWEEN req.page_start AND req.page_end
          WHERE req.subject_title=? AND req.stage_scope=? AND req.maturity='verified'
          ORDER BY req.requirement_id,page.page_number''',(scope['subject'],scope['standard_scope'])):
        row=dict(raw);quote=next((t for t in (row['title'],row['requirement_text'])
                                if 8<=len(t)<=1200 and t in row['page_text']),'')
        linked=(c.execute('SELECT * FROM curriculum_manual_requirement WHERE requirement_id=?',
                          (row['requirement_id'],)).fetchone()
                if c.execute("SELECT 1 FROM sqlite_master WHERE name='curriculum_manual_requirement'").fetchone() else None)
        if linked:
            if linked['child_id']!=child:continue
            reviewed=json.loads(linked['payload_json'])
            if reviewed['page_number']!=row['page_number']:continue
            quote=reviewed['quote']
            try:manual_requirement(c,child,row['requirement_id'],row['page_number'],quote,knowledge_node_id=node['knowledge_node_id'])
            except ManualScopeError:continue
        elif row['source_status'] not in {'ocr_ready','structured'} or not quote:continue
        source=dict(c.execute('SELECT * FROM curriculum_standard_source WHERE source_id=?',(row['source_id'],)).fetchone())
        page=dict(c.execute('SELECT * FROM curriculum_standard_page WHERE source_id=? AND page_number=?',
                            (row['source_id'],row['page_number'])).fetchone())
        requirement=dict(c.execute('SELECT * FROM curriculum_standard_requirement WHERE requirement_id=?',
                                   (row['requirement_id'],)).fetchone())
        key=row['requirement_id']+':'+str(row['page_number'])
        digest=_hash([requirement,source,page,dict(linked) if linked else None])
        if legacy is not None and key==legacy['key']and digest!=legacy['binding']:
            from ..standard_relocation_provenance import old_roots,StandardRelocationError
            try:
                for old_root in old_roots(getattr(c,'connection',c),source):
                    old={**source,'local_root_path':old_root}
                    if _hash([requirement,old,page,dict(linked)if linked else None])==legacy['binding']:
                        digest=legacy['binding'];break
            except StandardRelocationError:
                raise LocalSupplyError('PAPER_OBJECTIVE_SOURCE_CHANGED')from None
            except Exception as error:
                from ..restore_publication import RestorePublicationError
                if isinstance(error,RestorePublicationError)and str(error)=='ORIGINAL_CONNECTION_READ_REQUIRED':
                    raise LocalSupplyError('PAPER_OBJECTIVE_SOURCE_CHANGED')from None
                raise
        result.append({'key':key,
                       'requirement_id':row['requirement_id'],'title':row['title'],'standard_page':row['page_number'],
                       'standard_quote':quote,'binding':digest})
    return result


def _seeds(c,child,node):
    from ..curriculum_objective_source import node_allowed
    result=[]
    for row in c.execute("SELECT diagnostic_item_id FROM diagnostic_item WHERE knowledge_node_id=? AND status='active' ORDER BY diagnostic_item_id",(node['knowledge_node_id'],)).fetchall():
        try:item=validate_item(c,child,row[0])
        except LocalSupplyError:continue
        if node_allowed(node,item):
            result.append({'diagnostic_item_id':item['diagnostic_item_id'],'prompt':item['prompt'],
                           'binding':_hash(item)})
    return result


@snapshot_read
def assert_source_current(c,db,child,source):
    if not isinstance(source.get('source_ref'),str)or not source['source_ref'].startswith(PREFIX):return None
    from ..original_connection_read import _verification,trusted_sql
    # The existing healthy hash path needs no generation capability, including
    # its legacy raw-connection callers. Both paths use original SQLite cursors.
    with trusted_sql(c)as sql:
        actual=sql.execute('SELECT * FROM knowledge_source WHERE source_id=?',(source['source_id'],)).fetchone()
        _need(actual is not None and all(key in source and source[key]==actual[key]for key in actual.keys()))
        try:return _assert_source_current(sql,db,child,source)
        except LocalSupplyError as error:
            if str(error)!='PAPER_OBJECTIVE_SOURCE_CHANGED':raise
    # Re-read the entire original block in this same snapshot before allowing
    # the only new explanation: an authenticated physical-root substitution.
    with _verification(c,db)as(sql,resources,original_db):
        actual=sql.execute('SELECT * FROM knowledge_source WHERE source_id=?',(source['source_id'],)).fetchone()
        _need(actual is not None and all(key in source and source[key]==actual[key]for key in actual.keys()))
        return _assert_source_current(sql,original_db,child,source,relocated=True)


def _assert_source_current(c,db,child,source,*,relocated=False):
    """Only the exact server-created receipt source gets supplemental-source scope."""
    ref=source['source_ref']
    if not isinstance(ref,str) or not ref.startswith(PREFIX):return None
    row=c.execute('SELECT * FROM audit_event WHERE audit_id=? AND action=? AND actor=\'parent\' AND child_id=?',
                  (ref[len(PREFIX):],ACTION,child)).fetchone()
    _need(row is not None)
    receipt=json.loads(row['summary_json']);binding=receipt['binding']
    _need(receipt['source_id']==source['source_id'] and receipt['content_hash']==source['content_hash']
          and source['child_id']==child and source['source_type']=='school_material'
          and source['review_status']=='approved' and source['current_version']==1
          and source['license_status'] in {'public','family_owned_index'})
    node=c.execute("SELECT * FROM knowledge_node WHERE knowledge_node_id=? AND status='active'",(binding['node']['knowledge_node_id'],)).fetchone()
    _need(node is not None and _hash(dict(node))==binding['node']['binding'])
    for role in ('paper','answer_key'):
        # OCR correction/import bookkeeping is not a revision of the frozen
        # original. Reauthenticate its exact retained asset; submission still
        # requires the current full authoring source_version.
        _need(_page_snapshot(c,db,child,binding[role],authoring=False)==binding[role])
    # Stored revision/generation/action are provenance. Restored complete receipt
    # and originals may qualify; a later Q7 journal does not revoke this Q6.
    excerpts=c.execute('SELECT content_text,content_hash,sequence FROM knowledge_excerpt WHERE source_id=? ORDER BY sequence',
                       (source['source_id'],)).fetchall()
    _need(len(excerpts)==1 and excerpts[0]['sequence']==1 and excerpts[0]['content_hash']==receipt['content_hash']
          and hashlib.sha256(excerpts[0]['content_text'].encode()).hexdigest()==receipt['content_hash'])
    original=parse_objective(excerpts[0]['content_text'])
    selection=_selection(c,child,node['subject'])
    scope=json.loads(selection['scope_json'])
    _need(_hash(original)==receipt['objective_hash'] and _hash(selection)==binding['selection']['binding']
          and (source['subject'],source['grade_band'],source['version_label'])==(scope['subject'],scope['grade'],scope['edition'])
          and original['knowledge_node_id']==binding['node']['knowledge_node_id']
          and any(v['key']==binding['standard']['key'] and v['binding']==binding['standard']['binding']
                  for v in _standards(c,child,dict(node),selection,legacy=binding['standard']if relocated else None)))
    matching=next(v for v in _standards(c,child,dict(node),selection,legacy=binding['standard']if relocated else None)
                  if v['key']==binding['standard']['key'] and v['binding']==binding['standard']['binding'])
    return {**binding,'receipt_sha256':_hash(receipt),'standard':{**binding['standard'],'standard_quote':matching['standard_quote']}}


def _result(c,receipt):
    record=receipt['output'];binding=receipt['binding']
    node=dict(c.execute('SELECT * FROM knowledge_node WHERE knowledge_node_id=?',(binding['node']['knowledge_node_id'],)).fetchone())
    selection=_selection(c,binding['child_id'],node['subject'])
    standard=next(v for v in _standards(c,binding['child_id'],node,selection,legacy=binding['standard'])
                  if v['key']==binding['standard']['key'] and v['binding']==binding['standard']['binding'])
    spec={k:standard[k] for k in ('requirement_id','standard_page','standard_quote')}
    spec.update(excerpt_id=record['excerpt_id'],explanation=EXPLANATION,difficulty=record['difficulty'])
    if record['route']=='parallel':spec.update(seed_item_id=record['seed_item_id'],stage='parallel',validator=VALIDATOR)
    elif record['route']=='first_source':spec['knowledge_node_id']=node['knowledge_node_id']
    return dict(receipt_id=record['receipt_id'],source_id=record['source_id'],route=record['route'],source_spec=spec,
                seed_item_id=record['seed_item_id'],candidate_id=None,diagnostic_item_id=record['diagnostic_item_id'],
                expected_scope_version=binding['selection']['version'],
                state='existing_qualified_original' if record['route']=='reuse' else 'awaiting_formal_validation',
                eligible=record['route']=='reuse',local_only=True,cloud_transmission_allowed=False)


class PaperObjectiveBridge:
    def __init__(self,db):
        self.db=db;self.drafts=ParentTextbookDraftService(db)
        from ..curriculum_supply import CurriculumSupplyService
        CurriculumSupplyService(db).ensure_schema()

    def _saved(self,c,child):
        self.drafts._check_owner(c,child,'capture')
        row=c.execute("SELECT * FROM kg_parent_textbook_form_draft WHERE child_id=? AND scope='capture'",(child,)).fetchone()
        saved=self.drafts._public(c,child,'capture',row)
        _need(saved['payload'] is not None and saved['payload'].get('objective_draft') is not None
              and not saved['stale_refs'],'PAPER_OBJECTIVE_SAVED_DRAFT_REQUIRED')
        return saved

    def _options(self,c,child):
        saved=self._saved(c,child);objective=saved['payload']['objective_draft']
        node=c.execute("SELECT * FROM knowledge_node WHERE knowledge_node_id=? AND status='active'",(objective['candidate_node_id'],)).fetchone()
        grade=c.execute("SELECT grade FROM child_profile WHERE child_id=? AND status='active'",(child,)).fetchone()
        _need(node is not None and grade is not None and node['grade_band']==grade['grade']
              and node['track']=='school' and node['node_type']=='skill','PAPER_OBJECTIVE_NODE_REQUIRED')
        node=dict(node);selection=_selection(c,child,node['subject']);scope=json.loads(selection['scope_json'])
        _need(scope['grade']==grade['grade'],'CURRICULUM_SCOPE_MISMATCH')
        return saved,node,selection,_standards(c,child,node,selection)

    def options(self,child):
        with self.db.connect() as c:
            c.execute('BEGIN');self.drafts._check_owner(c,child,'capture')
            person=dict(c.execute('SELECT child_id,nickname,grade FROM child_profile WHERE child_id=?',(child,)).fetchone())
            try:saved,node,selection,standards=self._options(c,child)
            except LocalSupplyError as error:
                return {'status':'blocked','child':person,'blockers':[str(error)],'standards':[],
                        'local_only':True,'cloud_transmission_allowed':False}
            return {'status':'ready','child':person,'child_id':child,'blockers':[],
                    'draft_identity':{k:saved[k] for k in ('revision','storage_generation','last_action_id')},
                    'selection':{'version':selection['version'],'scope':json.loads(selection['scope_json'])},
                    'node':{'id':node['knowledge_node_id'],'title':node['title'],'subject':node['subject'],'binding':_hash(node)},
                    'standards':standards,'seeds':_seeds(c,child,node),'local_only':True,'cloud_transmission_allowed':False}

    def read(self,child,receipt_id):
        with self.db.connect() as c:
            c.execute('BEGIN')
            from ..standard_relocation_provenance import business_snapshot
            with business_snapshot(c):
                self.drafts._check_owner(c,child,'capture')
                row=c.execute("SELECT * FROM audit_event WHERE audit_id=? AND child_id=? AND actor='parent' AND action=?",
                              (receipt_id,child,ACTION)).fetchone()
                _need(row is not None,'PAPER_OBJECTIVE_RECEIPT_UNAVAILABLE')
                receipt=json.loads(row['summary_json']);result=receipt['output']
                if result['route']=='reuse':
                    validate_item(c,child,result['diagnostic_item_id'])
                else:
                    source=c.execute('SELECT * FROM knowledge_source WHERE source_id=?',(result['source_id'],)).fetchone()
                    _need(source is not None)
                    assert_source_current(c,self.db,child,dict(source))
                return _result(c,receipt)

    def submissions(self,child):
        with self.db.connect() as c:
            self.drafts._check_owner(c,child,'capture')
            rows=c.execute("SELECT audit_id,occurred_at,summary_json FROM audit_event WHERE child_id=? AND actor='parent' AND action=? ORDER BY occurred_at DESC,audit_id LIMIT 20",(child,ACTION)).fetchall()
            items=[]
            for row in rows:
                output=json.loads(row['summary_json'])['output'];state='awaiting_formal_validation';title='已保存的单题送审'
                if output['route']=='reuse':
                    item=c.execute('SELECT prompt FROM diagnostic_item WHERE diagnostic_item_id=?',(output['diagnostic_item_id'],)).fetchone()
                    title='沿用已审核原题：'+(item['prompt'][:80] if item else '原题待核对');state='reused'
                else:
                    source=c.execute('SELECT title FROM knowledge_source WHERE source_id=? AND child_id=?',(output['source_id'],child)).fetchone()
                    if source:title=source['title'][:160]
                    table='curriculum_supply_package' if output['route']=='parallel' else 'curriculum_first_source'
                    if c.execute("SELECT 1 FROM sqlite_master WHERE name=?",(table,)).fetchone():
                        package=c.execute(f"SELECT state FROM {table} WHERE child_id=? AND json_extract(payload_json,'$.excerpt_id')=? ORDER BY created_at DESC LIMIT 1",(child,output['excerpt_id'])).fetchone()
                        if package:state=package['state']
                items.append({'audit_id':row['audit_id'],'occurred_at':row['occurred_at'],'title':title,'state':state})
            return {'items':items,'local_only':True,'cloud_transmission_allowed':False}

    def generate(self,child,receipt_id,*,expected_generation):
        # An explicit current parent action may resume a restored immutable receipt.
        # Old pre-restore requests cannot silently create a fresh candidate.
        from ..curriculum_supply import CurriculumSupplyService
        from ..curriculum_first_source import CurriculumFirstSourceService
        with self.db.external_effect_guard():
            with self.db.connect() as c:
                _need(expected_generation==generation(c),'PAPER_OBJECTIVE_STORAGE_CHANGED')
                person=dict(c.execute('SELECT child_id,nickname,grade FROM child_profile WHERE child_id=?',(child,)).fetchone())
            record=self.read(child,receipt_id)
            if record['route']=='reuse':return {'receipt':record,'candidate':None,'child':person}
            action='paired:'+receipt_id
            if record['route']=='parallel':
                candidate=CurriculumSupplyService(self.db).generate_draft(child,record['source_spec'],
                    author='local:reviewed_source_extractor_v1',action_id=action,
                    expected_scope_version=record['expected_scope_version'])
            else:
                candidate=CurriculumFirstSourceService(self.db).generate(child,record['source_spec'],
                    action_id=action,expected_scope_version=record['expected_scope_version'])
            return {'receipt':record,'candidate':candidate,'child':person}

    def submit(self,child,*,draft_identity,expected_selection_version,expected_node_binding,standard_key,standard_binding,
               answer_semantics,originals_confirmed,license_status,difficulty,action_id,seed_choice=None):
        _need(originals_confirmed is True and license_status in {'public','family_owned_index'},'PAPER_OBJECTIVE_ORIGINAL_CONFIRMATION_REQUIRED')
        _need(isinstance(action_id,str) and 8<=len(action_id)<=120
              and type(difficulty) is int and 1<=difficulty<=5,'PAPER_OBJECTIVE_INPUT')
        request=dict(draft_identity=draft_identity,expected_selection_version=expected_selection_version,expected_node_binding=expected_node_binding,
                     standard_key=standard_key,standard_binding=standard_binding,answer_semantics=answer_semantics,
                     originals_confirmed=originals_confirmed,license_status=license_status,
                     difficulty=difficulty,action_id=action_id,seed_choice=seed_choice)
        aid='paper-objective-'+_hash([child,action_id])
        with self.db.external_effect_guard(),self.db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            from ..standard_relocation_provenance import business_snapshot
            with business_snapshot(c):
                saved,node,selection,standards=self._options(c,child)
                _need(draft_identity=={k:saved[k] for k in ('revision','storage_generation','last_action_id')},'PAPER_OBJECTIVE_DRAFT_CONFLICT')
                _need(type(expected_selection_version) is int and expected_selection_version==selection['version'],'CURRICULUM_SCOPE_VERSION_CONFLICT')
                _need(expected_node_binding==_hash(node),'PAPER_OBJECTIVE_NODE_CHANGED')
                standard=next((v for v in standards if v['key']==standard_key and v['binding']==standard_binding),None)
                _need(standard is not None,'PAPER_OBJECTIVE_STANDARD_CHANGED')
                prior=c.execute('SELECT * FROM audit_event WHERE audit_id=?',(aid,)).fetchone()
                if prior:
                    receipt=json.loads(prior['summary_json']);_need(receipt['request_hash']==_hash(request),'PAPER_OBJECTIVE_ACTION_CONFLICT')
                    if receipt['output']['route']=='reuse':
                        validate_item(c,child,receipt['output']['diagnostic_item_id'])
                    else:
                        source=dict(c.execute('SELECT * FROM knowledge_source WHERE source_id=?',(receipt['source_id'],)).fetchone())
                        assert_source_current(c,self.db,child,source)
                    return _result(c,receipt)
                objective=saved['payload']['objective_draft'];rubric=answer_rubric(objective,answer_semantics)
                packet=dict(schema=SCHEMA,subject=node['subject'],grade=node['grade_band'],knowledge_node_id=node['knowledge_node_id'],
                            assessment_mode='objective',answer_type='text',prompt=objective['full_question'],expected_answer=rubric)
                parse_objective(_json(packet));scope=json.loads(selection['scope_json'])
                binding={'schema':'parent_paired_paper_objective_v1','child_id':child,'scope':'capture',
                         'draft_identity':draft_identity,'draft_payload_sha256':_hash(saved['payload']),
                         'node':{'knowledge_node_id':node['knowledge_node_id'],'binding':_hash(node)},
                         'selection':{'selection_id':selection['selection_id'],'version':selection['version'],'binding':_hash(selection)},
                         'standard':{k:standard[k] for k in ('key','binding','requirement_id','standard_page')},
                         'answer_semantics':answer_semantics,
                         **{role:_page_snapshot(c,self.db,child,objective[role]) for role in ('paper','answer_key')}}
                content=_json(packet);digest=hashlib.sha256(content.encode()).hexdigest()
                seeds=_seeds(c,child,node)
                chosen=next((v for v in seeds if seed_choice=={k:v[k] for k in ('diagnostic_item_id','binding')}),None)
                _need(chosen is not None if seeds else seed_choice is None,'PAPER_OBJECTIVE_ORIGIN_REQUIRED')
                seed=None
                for candidate in seeds:
                    current=validate_item(c,child,candidate['diagnostic_item_id'])
                    if current['prompt']==packet['prompt']:
                        _need(json.loads(current['expected_answer_json'])==exact_rubric(packet),'PAPER_OBJECTIVE_EXISTING_ANSWER_CONFLICT')
                        seed={'reuse':current['diagnostic_item_id']};break
                if seed is None and chosen:seed={'parallel':chosen['diagnostic_item_id']}
                route='reuse' if seed and 'reuse' in seed else 'parallel' if seed else 'first_source'
                binding['seed_choice']=seed_choice
                if route=='reuse':
                    source_id=None;excerpt=None
                else:
                    # Do not silently adopt a duplicate created by a different source path.
                    prior_source=c.execute('SELECT * FROM knowledge_source WHERE child_id=? AND content_hash=? AND current_version=1',(child,digest)).fetchone()
                    _need(prior_source is None,'PAPER_OBJECTIVE_EXISTING_SOURCE_REVIEW_REQUIRED')
                    source=KnowledgeBaseService(self.db).import_source(child,dict(source_type='school_material',
                        title='单题原页核对 · '+objective['question_number'],subject=node['subject'],grade_band=node['grade_band'],
                        version_label=scope['edition'],source_ref=PREFIX+aid,license_status=license_status,
                        parent_reviewed=True,content_text=content),_connection=c)
                    excerpt=c.execute('SELECT excerpt_id FROM knowledge_excerpt WHERE source_id=?',(source['source_id'],)).fetchone()[0]
                    source_id=source['source_id']
                output=dict(receipt_id=aid,source_id=source_id,route=route,excerpt_id=excerpt,difficulty=difficulty,
                            seed_item_id=seed.get('parallel') if seed else None,
                            diagnostic_item_id=seed.get('reuse') if seed else None)
                receipt=dict(binding=binding,request_hash=_hash(request),source_id=source_id,content_hash=digest,
                             objective_hash=_hash(packet),output=output)
                c.execute("INSERT INTO audit_event(audit_id,actor,action,child_id,summary_json,occurred_at) VALUES(?,'parent',?,?,?,?)",
                          (aid,ACTION,child,_json(receipt),utc_now()))
                result=_result(c,receipt)
            c.commit();return result

# Original immutable code identity, not a caller-writable connection property.
_ASSERT_RELOCATION_READ_CODE=assert_source_current.__wrapped__.__code__
