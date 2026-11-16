"""Child-scoped, purpose-limited delegation for local safe crop retention."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from uuid import uuid4

from .local_supply import digest, LocalSupplyError
from .visual_material import _json, FLAGS

PURPOSE = 'local_derived_visual_retention_v1'
ACTION = 'visual_retention_policy_v1'
QUARANTINE = 'visual_retention_policy_quarantined_v1'
LIMITS = dict(retention_days=[1,30], authorization_days=[1,365], max_bytes=[1048576,268435456])
_SEAL = object()


class VisualRetentionError(ValueError):
    pass


def require(condition, code):
    if not condition:
        raise VisualRetentionError('VISUAL_RETENTION_'+code)


def now():
    return datetime.now(timezone.utc)


def timestamp(value):
    try:
        result = datetime.fromisoformat(value)
        require(result.tzinfo is not None, 'RECORD_INVALID')
        return result
    except (TypeError, ValueError) as error:
        raise VisualRetentionError('VISUAL_RETENTION_RECORD_INVALID') from error


def events(c, child):
    return [(r['audit_id'],_json(r['summary_json'])) for r in c.execute(
        'SELECT audit_id,summary_json FROM audit_event WHERE child_id=? AND actor=? AND action=? ORDER BY rowid',
        (child,'parent_ui',ACTION)).fetchall()]


def assets(c, child):
    result = []
    for row in c.execute("SELECT * FROM capture_artifact WHERE child_id=? AND artifact_type='document_visual_region'",
                         (child,)).fetchall():
        metadata = _json(row['details_json'])
        if metadata.get('retention') is not None:
            item = metadata['retention']
            require(isinstance(item,dict) and type(item.get('stored_bytes')) is int
                    and item['stored_bytes'] > 0, 'RECORD_INVALID')
            result.append((row,metadata))
    return result


def current(c, child):
    if not c.execute("SELECT 1 FROM child_profile WHERE child_id=? AND status='active'", (child,)).fetchone():
        raise KeyError(child)
    used = sum(meta['retention']['stored_bytes'] for _,meta in assets(c,child))
    rows = events(c,child)
    result = dict(purpose=PURPOSE,status='disabled',revision=None,scope=None,retention_days=None,
                  authorization_expires_at=None,max_bytes=None,used_bytes=used,reason='VISUAL_RETENTION_NOT_ENABLED')
    if not rows:
        return result
    identity, data = rows[-1]
    result.update(data['policy'], revision=identity,used_bytes=used)
    if result['status']=='enabled':
        quarantined = any(_json(r[0]).get('authority_audit_id')==identity for r in c.execute(
            'SELECT summary_json FROM audit_event WHERE child_id=? AND action=?',(child,QUARANTINE)).fetchall())
        if quarantined:
            result.update(status='quarantined',reason='VISUAL_RETENTION_RESTORE_REAUTHORIZATION_REQUIRED')
        elif timestamp(result['authorization_expires_at']) <= now():
            result.update(status='expired',reason='VISUAL_RETENTION_EXPIRED')
    return result


def delegation_current(c, child, delegation):
    require(isinstance(delegation,dict) and delegation.get('purpose')==PURPOSE
            and delegation.get('child_id')==child, 'DELEGATION_INVALID')
    policy=current(c,child)
    require(policy['status']=='enabled', policy['reason'].removeprefix('VISUAL_RETENTION_') if policy['reason'] else 'NOT_ENABLED')
    require(policy['revision']==delegation.get('policy_id'), 'POLICY_CHANGED')
    scope=policy['scope']
    require(scope['child_id']==child and (scope['session_id'] is None
            or scope['session_id']==delegation.get('session_id')), 'SCOPE_CHANGED')
    binding=delegation.get('source_binding')
    require(isinstance(binding,dict) and binding.get('child_id')==child
            and binding.get('session_id')==delegation.get('session_id'),'SCOPE_CHANGED')
    from .visual_material import _region
    try:
        page,_,_=_region(c,{k:binding[k] for k in ('child_id','session_id','artifact_id','revision_id')},
                         binding['region_id'],binding['region_version'])
    except LocalSupplyError as error:
        raise VisualRetentionError('VISUAL_RETENTION_SOURCE_CHANGED') from error
    expected_expiry=min(timestamp(page['created_at'])+timedelta(days=policy['retention_days']),
                        timestamp(policy['authorization_expires_at']))
    require(timestamp(delegation.get('expires_at'))==expected_expiry,'DELEGATION_INVALID')
    require(timestamp(delegation.get('expires_at')) > now(), 'EXPIRED')
    return policy


@dataclass(frozen=True)
class _Permit:
    metadata: dict
    binding: dict
    _seal: object


def verify_permit(c, permit, scope, region_id, region_version, *, asset_exists=False):
    require(isinstance(permit,_Permit) and permit._seal is _SEAL, 'DELEGATION_INVALID')
    require(permit.binding == dict(**scope,region_id=region_id,region_version=region_version), 'SCOPE_CHANGED')
    policy=delegation_current(c,scope['child_id'],permit.metadata)
    from .visual_material import _region
    _, details, _ = _region(c,scope,region_id,region_version,live=True)
    require(details.get('visual_capture',{}).get('retention_policy_id')==policy['revision'], 'CAPTURE_NOT_AUTHORIZED')
    require(policy['used_bytes']+(0 if asset_exists else permit.metadata['stored_bytes']) <= policy['max_bytes'], 'CAPACITY')
    return permit.metadata


class VisualRetentionService:
    def __init__(self,database,host):
        self.database,self.host=database,host

    def read(self,child_id):
        with self.database.external_effect_guard(),self.database.connect() as c:
            c.execute('BEGIN')
            return dict(child_id=child_id,policy=current(c,child_id),limits=LIMITS,**FLAGS)

    def command(self,child_id,*,action,expected_revision,client_action_id,session_id=None,
                retention_days=None,authorization_days=None,max_bytes=None):
        require(action in {'enable','update','revoke'},'ACTION_INVALID')
        require(isinstance(client_action_id,str) and 1<=len(client_action_id)<=128,'REQUEST_INVALID')
        request=dict(action=action,expected_revision=expected_revision,client_action_id=client_action_id,
                     session_id=session_id,retention_days=retention_days,authorization_days=authorization_days,max_bytes=max_bytes)
        with self.database.external_effect_guard(), self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            policy=current(c,child_id)
            prior=[data for _,data in events(c,child_id) if data['request']['client_action_id']==client_action_id]
            require(len(prior)<=1,'RECORD_INVALID')
            if prior:
                require(prior[0]['request_hash']==digest(request),'REQUEST_CONFLICT')
                return dict(child_id=child_id,receipt=prior[0]['receipt'],policy=policy,limits=LIMITS,replayed=True,**FLAGS)
            require(policy['revision']==expected_revision,'REVISION_CONFLICT')
            if action=='revoke':
                require(all(v is None for v in (session_id,retention_days,authorization_days,max_bytes)),'REQUEST_INVALID')
                policy.update(status='revoked',reason='VISUAL_RETENTION_REVOKED')
            else:
                for key,value in [('retention_days',retention_days),('authorization_days',authorization_days),('max_bytes',max_bytes)]:
                    low,high=LIMITS[key]
                    require(type(value) is int and low<=value<=high,'LIMIT_INVALID')
                require(self.host.key is not None and len(self.host.key)==32,'ENCRYPTION_REQUIRED')
                if session_id is not None:
                    require(isinstance(session_id,str) and bool(c.execute(
                        "SELECT 1 FROM daily_session WHERE session_id=? AND child_id=? AND status='active'",(session_id,child_id)).fetchone()),'SCOPE_CHANGED')
                policy.update(status='enabled',reason=None,scope=dict(child_id=child_id,session_id=session_id,session_state='study'),
                    retention_days=retention_days,authorization_expires_at=(now()+timedelta(days=authorization_days)).isoformat(),max_bytes=max_bytes)
            identity='visual-retention:'+uuid4().hex
            policy['revision']=identity
            receipt=dict(receipt_id=identity,child_id=child_id,client_action_id=client_action_id,action=action,
                status={'enable':'enabled','update':'updated','revoke':'revoked'}[action],revision=identity,request_hash=digest(request))
            c.execute('INSERT INTO audit_event(audit_id,actor,action,child_id,summary_json,occurred_at) VALUES(?,?,?,?,?,?)',
                (identity,'parent_ui',ACTION,child_id,json.dumps(dict(request=request,request_hash=digest(request),receipt=receipt,policy=policy)),now().isoformat()))
            c.commit()
        self.cleanup(child_id=child_id)
        return {**self.read(child_id), 'receipt':receipt,'replayed':False}

    def capture_policy(self,c,child_id,session_id):
        policy=current(c,child_id)
        if policy['status']=='enabled' and policy['scope']['session_id'] in {None,session_id}:
            return policy['revision']
        return None

    def store_item(self,item):
        binding=item['binding']
        child=binding['child_id']
        try:
            with self.database.connect() as c:
                c.execute('BEGIN')
                scope,page,region=self.host._scope(c,child,binding['artifact_id'],binding['region_id'],binding['revision_id'],binding['region_version'])
                policy=current(c,child)
                require(policy['status']=='enabled',policy['reason'].removeprefix('VISUAL_RETENTION_') if policy['reason'] else 'NOT_ENABLED')
                details=_json(page['details_json'])
                require(details.get('visual_capture',{}).get('retention_policy_id')==policy['revision'],'CAPTURE_NOT_AUTHORIZED')
                key='visual-auto:'+digest(dict(binding=binding,policy_id=policy['revision']))
                identity=(child,binding['artifact_id'],binding['region_id'],binding['revision_id'],binding['region_version'])
                prepared=self.host._prepared(c,identity)
                matching=[m['retention'] for r,m in assets(c,child) if m.get('source_binding')==dict(**scope,
                    region_id=binding['region_id'],region_version=binding['region_version']) and m['retention'].get('policy_id')==policy['revision']]
                require(len(matching)<=1,'IMAGE_AMBIGUOUS')
                if matching:
                    metadata=matching[0]
                else:
                    require(prepared is not None,'PREPARATION_UNAVAILABLE')
                    metadata=dict(purpose=PURPOSE,policy_id=policy['revision'],child_id=child,session_id=scope['session_id'],
                        expires_at=min(timestamp(page['created_at'])+timedelta(days=policy['retention_days']),
                                       timestamp(policy['authorization_expires_at'])).isoformat(),stored_bytes=len(prepared.png)+32,
                        source_binding=dict(**scope,region_id=binding['region_id'],region_version=binding['region_version']))
                permit=_Permit(metadata,dict(**scope,region_id=binding['region_id'],region_version=binding['region_version']),_SEAL)
            result=self.host.storage(child,binding['artifact_id'],binding['region_id'],revision_id=binding['revision_id'],
                region_version=binding['region_version'],facts_version=binding['facts_version'],client_action_id=key,
                purpose=PURPOSE,_delegation=permit)
            return dict(status='stored',reason=None,policy_id=metadata['policy_id'],receipt_id=result['receipt']['receipt_id'])
        except VisualRetentionError as error:
            return dict(status='not_stored',reason=str(error),policy_id=None)

    def store_revision(self,revision):
        for item in revision.get('visual_capture',{}).get('items',[]):
            item['retention']=self.store_item(item)
            b=item['binding']
            detail=self.host.read(b['child_id'],b['artifact_id'],b['region_id'],revision_id=b['revision_id'],region_version=b['region_version'])
            for key in ('storage_status','image_availability','source_current'):
                item[key]=detail[key]
        return revision

    def cleanup(self,*,child_id=None):
        from .privacy_lifecycle import PrivacyLifecycleService
        with self.database.external_effect_guard(),self.database.connect() as c:
            c.execute('BEGIN')
            children=[child_id] if child_id is not None else [r[0] for r in c.execute('SELECT child_id FROM child_profile').fetchall()]
            expired=[]
            for child in children:
                for row,meta in assets(c,child):
                    # Explicit later parent storage has its own purpose/retention authority.
                    source=meta['source_binding']
                    entries=self.host._events(c,child,source['artifact_id'],source['region_id'])
                    latest=[e for _,e in entries if e.get('request',{}).get('kind') in {'storage','retract'}]
                    if latest and latest[-1]['request']['kind']=='storage' and not latest[-1]['request'].get('delegation') and latest[-1]['result'].get('asset_id')==row['artifact_id']:
                        continue
                    try:
                        delegation_current(c,child,meta['retention'])
                    except (VisualRetentionError,KeyError):
                        expired.append(row['artifact_id'])
            c.commit()
            for asset in expired:
                cleanup=PrivacyLifecycleService(self.database,self.host.data_dir,self.host.key)
                cleanup.delete_capture_artifact(asset,'确认删除','visual-retention-expire:'+asset)
        return len(expired)
