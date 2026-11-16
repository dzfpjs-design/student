"""Purpose-limited local cropped-image storage and parent display receipts.

No original frame storage, cloud permission, model dispatch, or scoring. All
public mutations are only exposed through the injected parent-auth router.
"""
import base64
from dataclasses import dataclass
from hashlib import sha256
import hmac
from io import BytesIO
import json
import os
from pathlib import Path
import time
from threading import RLock
from uuid import uuid4

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.exceptions import InvalidTag
from PIL import Image

from . import visual_material as core
from .local_supply import LocalSupplyError, digest
from .pet_media_files import read_managed_media, write_new_managed_media

PURPOSE = 'local_visual_material_review_v1'
ACTION = 'visual_material_host_v1'
QUARANTINE = 'visual_material_storage_quarantined_v1'
MAX_PNG = 2 * 1024 * 1024
_SEAL = object()


@dataclass(frozen=True)
class _DisplayProof:
    connection: object
    identity: str
    seal: object


def valid_display_proof(proof, c, scope, region_id, region_version, facts_version):
    return (type(proof) is _DisplayProof and proof.seal is _SEAL
        and proof.connection is c and c.in_transaction and proof.identity == digest(
            dict(**scope, region_id=region_id, region_version=region_version, facts_version=facts_version)))


def _now():
    from .repository import utc_now
    return utc_now()


def _png(raw, descriptor):
    core.require(isinstance(raw, bytes) and len(raw) <= MAX_PNG
        and sha256(raw).hexdigest() == descriptor['sha256'], 'IMAGE_CHANGED')
    try:
        with Image.open(BytesIO(raw)) as picture:
            core.require(picture.format == 'PNG' and picture.size == (descriptor['width'], descriptor['height'])
                and picture.width * picture.height <= 4_000_000 and getattr(picture, 'n_frames', 1) == 1, 'IMAGE_CHANGED')
            picture.verify()
    except (OSError, ValueError) as error:
        raise LocalSupplyError('VISUAL_MATERIAL_IMAGE_CHANGED') from error
    return raw


def assert_host_visual_current(c, child_id, binding, *, require_managed=False):
    """Recheck managed images in the caller's existing source snapshot.

    Standalone core records have no managed host grant. Once a managed host
    record exists, neither an older core confirmation nor a new storage grant
    can bypass the original displayed/confirmed grant identity.
    """
    records=c.execute('SELECT summary_json FROM audit_event WHERE child_id=? AND action=?',(child_id,ACTION)).fetchall()
    managed=any(core._json(r[0]).get('artifact_id')==binding['artifact_id']
        and core._json(r[0]).get('region_id')==binding['region_id'] for r in records)
    if not managed:
        core.require(not require_managed, 'MANAGED_IMAGE_REQUIRED')
        return
    from .database import local_media_scope
    runtime=local_media_scope(c)
    core.require(runtime.get('media_root') is not None,'MEDIA_RUNTIME_REQUIRED')
    # No DB connection is created or guessed from its filename.
    service=VisualMaterialHost(None,data_dir=runtime['media_root'],data_encryption_key=runtime.get('data_encryption_key'))
    scope,_,region=service._scope(c,child_id,binding['artifact_id'],binding['region_id'],binding['revision_id'],binding['region_version'])
    _,grant=service._grant(c,child_id,binding['artifact_id'],binding['region_id'],binding['region_version'])
    core.require(service._confirmed_grant(c,child_id,binding['artifact_id'],binding['region_id'],region,grant),'CONFIRMATION_CHANGED')
    try:
        service._asset(c,scope,binding['region_id'],binding['region_version'],grant)
    except OSError as error:
        raise LocalSupplyError('VISUAL_MATERIAL_IMAGE_UNAVAILABLE') from error


class VisualMaterialHost:
    def __init__(self, database, *, data_dir, data_encryption_key):
        self.database = database
        self.data_dir = Path(data_dir)
        self.media_root = self.data_dir / 'media'
        self.key = data_encryption_key
        self._staged = {}
        self._stage_meta = {}
        self._stage_lock = RLock()
        self._stage_clock = time.monotonic
        self._token_key = os.urandom(32)

    def _scope(self, c, child, artifact, region_id, revision_id, region_version):
        row = c.execute('SELECT session_id FROM capture_artifact WHERE child_id=? AND artifact_id=?', (child, artifact)).fetchone()
        if not row:
            raise KeyError(artifact)
        scope = dict(child_id=child, session_id=row[0], artifact_id=artifact, revision_id=revision_id)
        page, _, region = core._region(c, scope, region_id, region_version, live=False)
        return scope, page, region

    def stage_prepared(self, child_id, artifact_id, region_id, *, revision_id, region_version, prepared,
                       connection=None, live_generation=None):
        """Internal capture hook. Only the sealed pixel gate can supply bytes."""
        from .document_vision import is_prepared_diagram
        core.require(is_prepared_diagram(prepared), 'PREPARED_REQUIRED')
        def verify(c):
            scope, _, region = self._scope(c, child_id, artifact_id, region_id, revision_id, region_version)
            core.require(prepared.frame_sha256 == region['frame_sha256']
                and prepared.gate_version == region['gate_version']
                and list(prepared.region_xyxy) == region['region_xyxy'], 'FRAME_CHANGED')
            core.require(sha256(prepared.png).hexdigest() == region['safe_png_sha256'], 'IMAGE_CHANGED')
            if live_generation is not None:
                row, _, _ = core._page(c, scope, live=True)
                core.require(row['generation'] == live_generation, 'GENERATION_CHANGED')
            return scope['session_id']
        if connection is None:
            with self.database.connect() as c:
                c.execute('BEGIN')
                session_id = verify(c)
        else:
            core.require(connection.in_transaction, 'WRITE_TRANSACTION_REQUIRED')
            session_id = verify(connection)
        identity = (child_id, artifact_id, region_id, revision_id, region_version)
        with self._stage_lock:
            self.expire_prepared()
            core.require(identity in self._staged or len(self._staged) < 8, 'PREPARATION_CAPACITY')
            core.require(len(prepared.png) <= MAX_PNG, 'IMAGE_TOO_LARGE')
            if identity not in self._staged:
                self._staged[identity] = prepared
                self._stage_meta[identity] = (self._stage_clock()+300, session_id, live_generation)
                return identity
        return None

    def discard_prepared(self, identity):
        with self._stage_lock:
            self._staged.pop(identity, None)
            self._stage_meta.pop(identity, None)

    def clear_prepared(self, *, child_id=None, session_id=None):
        with self._stage_lock:
            for identity, meta in list(self._stage_meta.items()):
                if ((child_id is None or identity[0] == child_id)
                        and (session_id is None or meta[1] == session_id)):
                    self.discard_prepared(identity)

    def expire_prepared(self):
        with self._stage_lock:
            for identity, meta in list(self._stage_meta.items()):
                if meta[0] <= self._stage_clock():
                    self.discard_prepared(identity)

    def sweep_prepared(self):
        self.expire_prepared()
        with self._stage_lock:
            identities = list(self._stage_meta)
        with self.database.connect() as c:
            c.execute('BEGIN')
            for identity in identities:
                try:
                    self._scope(c, identity[0], identity[1], identity[2], identity[3], identity[4])
                    self._prepared(c, identity)
                except (LocalSupplyError, KeyError):
                    self.discard_prepared(identity)

    def _prepared(self, c, identity):
        with self._stage_lock:
            self.expire_prepared()
            meta = self._stage_meta.get(identity)
            prepared = self._staged.get(identity)
        if meta and meta[2] is not None:
            scope = dict(child_id=identity[0], session_id=meta[1], artifact_id=identity[1], revision_id=identity[3])
            try:
                row, _, _ = core._page(c, scope, live=True)
                core.require(row['generation'] == meta[2], 'GENERATION_CHANGED')
            except LocalSupplyError:
                self.discard_prepared(identity)
                return None
        return prepared

    def _events(self, c, child, artifact, region_id):
        rows = c.execute('SELECT audit_id,summary_json FROM audit_event WHERE child_id=? AND action=? ORDER BY rowid',
            (child, ACTION)).fetchall()
        result = []
        for row in rows:
            data = core._json(row['summary_json'])
            if data.get('artifact_id') == artifact and data.get('region_id') == region_id:
                result.append((row['audit_id'], data))
        return result

    def _prior(self, c, child, artifact, region_id, key, request=None):
        matches = [(i,e) for i,e in self._events(c,child,artifact,region_id) if e.get('client_action_id') == key]
        core.require(len(matches) <= 1, 'RECORD_INVALID')
        if matches:
            event_id, event = matches[0]
            core.require(request is None or event.get('request_hash') == digest(request), 'REQUEST_CONFLICT')
            return dict(receipt_id=event_id, **event['result'])

    def _append(self, c, child, artifact, region_id, request, result, *, grant_id=None):
        event_id = 'visual-host:' + uuid4().hex
        data = dict(artifact_id=artifact,region_id=region_id,client_action_id=request['client_action_id'],
            request_hash=digest(request),request=request,result=result,grant_id=grant_id)
        c.execute('INSERT INTO audit_event(audit_id,actor,action,child_id,summary_json,occurred_at) VALUES(?,?,?,?,?,?)',
            (event_id,'system_capture' if request.get('delegation') else 'parent_ui',ACTION,child,json.dumps(data),_now()))
        return dict(receipt_id=event_id, **result)

    def _grant(self, c, child, artifact, region_id, region_version):
        entries = [(i,e) for i,e in self._events(c,child,artifact,region_id)
            if e.get('request',{}).get('kind') in {'storage','retract'}
            and e['request'].get('region_version') == region_version]
        if not entries:
            return 'not_stored', None
        event_id, event = entries[-1]
        if event['request']['kind'] == 'retract':
            return 'revoked', None
        quarantines = c.execute('SELECT summary_json FROM audit_event WHERE child_id=? AND action=?', (child, QUARANTINE)).fetchall()
        if any(core._json(r[0]).get('authority_audit_id') == event_id for r in quarantines):
            return 'quarantined', None
        if event['request'].get('delegation'):
            from .visual_retention import delegation_current, VisualRetentionError
            try:
                delegation_current(c,child,event['request']['delegation'])
            except (VisualRetentionError,KeyError):
                return 'revoked', None
        return 'stored', dict(event['result'], grant_id=event_id)

    def _confirmed_grant(self,c,child,artifact,region_id,region,grant):
        if not grant or region.get('review_status')!='confirmed':
            return False
        for _,event in self._events(c,child,artifact,region_id):
            if (event.get('request',{}).get('kind')=='confirm'
                    and event.get('result',{}).get('binding',{}).get('confirmation_id')==region.get('confirmation_id')
                    and event.get('grant_id')==grant['grant_id']):
                return True
        return False

    def _reset_retracted(self,c,scope,region_id,region_version):
        row,details,region=core._region(c,scope,region_id,region_version)
        if region.get('review_status')=='retracted':
            region.update(review_status='unconfirmed',confirmation_id=None)
            core._save(c,row,details)

    def _asset(self, c, scope, region_id, region_version, grant):
        row = c.execute("SELECT * FROM capture_artifact WHERE child_id=? AND session_id=? AND artifact_id=? AND artifact_type='document_visual_region'",
            (scope['child_id'],scope['session_id'],grant['asset_id'])).fetchone()
        core.require(row and row['status'] == 'completed', 'IMAGE_UNAVAILABLE')
        core.require(isinstance(row['local_path'],str) and bool(row['local_path']), 'IMAGE_UNAVAILABLE')
        details = core._json(row['details_json'])
        core.require(details.get('source_binding') == dict(**scope,region_id=region_id,region_version=region_version)
            and details.get('image_descriptor') == grant['image_descriptor'], 'IMAGE_CHANGED')
        core.require(self.key is not None and len(self.key)==32, 'ENCRYPTION_REQUIRED')
        blob = read_managed_media(row['local_path'], media_root=self.media_root, max_bytes=MAX_PNG+32)
        core.require(blob.startswith(b'HSC1') and len(blob) >= 32, 'IMAGE_CHANGED')
        try:
            raw = AESGCM(self.key).decrypt(blob[4:16],blob[16:],b'hsc-raw-media-v1')
        except InvalidTag as error:
            raise LocalSupplyError('VISUAL_MATERIAL_IMAGE_CHANGED') from error
        return _png(raw, grant['image_descriptor'])

    def _detail(self, c, child, artifact, region_id, revision_id, region_version):
        try:
            scope, page, region = self._scope(c,child,artifact,region_id,revision_id,region_version)
        except LocalSupplyError as error:
            original=c.execute('SELECT * FROM capture_artifact WHERE child_id=? AND artifact_id=?',(child,artifact)).fetchone()
            if not original:
                raise KeyError(artifact)
            saved=core._json(original['details_json']).get('visual_materials',{}).get(region_id,{})
            core.require(saved.get('region_version')==region_version,'REGION_CHANGED')
            binding=dict(artifact_id=artifact,revision_id=revision_id,region_id=region_id,region_version=region_version,
                facts_version=saved.get('facts_version'),confirmation_id=saved.get('confirmation_id'))
            return dict(child_id=child,session_id=original['session_id'],binding=binding,
                image_descriptor=core._image_descriptor(binding,saved),observations=[],uncertainties=[],
                facts_version=saved.get('facts_version'),status='source_changed',fact_status='unconfirmed',
                storage_status=self._grant(c,child,artifact,region_id,region_version)[0],
                image_availability='unavailable',source_current=False,parent_confirmation_ready=False,reason=str(error),**core.FLAGS)
        binding = core._binding(scope,region_id,region)
        descriptor = core._image_descriptor(binding,region)
        storage, grant = self._grant(c,child,artifact,region_id,region_version)
        availability, reason = 'unavailable', None
        if grant:
            try:
                self._asset(c,scope,region_id,region_version,grant)
                availability = 'available'
            except FileNotFoundError:
                storage, reason = 'missing', 'VISUAL_MATERIAL_IMAGE_MISSING'
            except (OSError, LocalSupplyError):
                storage, reason = 'corrupt', 'VISUAL_MATERIAL_IMAGE_UNAVAILABLE'
        facts = region.get('facts')
        if facts is not None:
            core.validate_facts(facts)
            core.require(digest(dict(region_version=region_version,facts=facts)) == region.get('facts_version'), 'FACTS_CHANGED')
        fact_status = region.get('review_status','unconfirmed')
        if fact_status=='confirmed' and not self._confirmed_grant(c,child,artifact,region_id,region,grant):
            fact_status='unconfirmed'
        status = ('withdrawn' if fact_status == 'retracted' else 'confirmed' if fact_status=='confirmed'
            else 'needs_confirmation' if facts else 'needs_storage')
        ready = bool(facts and not facts['uncertainties'] and availability=='available' and fact_status!='retracted')
        return dict(child_id=child,session_id=scope['session_id'],binding=binding,image_descriptor=descriptor,
            observations=facts['observations'] if facts else [],uncertainties=facts['uncertainties'] if facts else [],
            facts_version=region.get('facts_version'), status=status, fact_status=fact_status,
            storage_status=storage,image_availability=availability,source_current=True,
            parent_confirmation_ready=ready,reason=reason,**core.FLAGS)

    def read(self, child_id, artifact_id, region_id, *, revision_id, region_version):
        with self.database.external_effect_guard(), self.database.connect() as c:
            c.execute('BEGIN')
            return self._detail(c,child_id,artifact_id,region_id,revision_id,region_version)

    def listing(self, child_id, *, offset=0, limit=20):
        with self.database.external_effect_guard(), self.database.connect() as c:
            c.execute('BEGIN')
            if not c.execute("SELECT 1 FROM child_profile WHERE child_id=? AND status='active'",(child_id,)).fetchone():
                raise KeyError(child_id)
            rows = c.execute("SELECT * FROM capture_artifact WHERE child_id=? AND artifact_type='document_ocr' ORDER BY created_at DESC,artifact_id",(child_id,)).fetchall()
            items=[]
            for row in rows:
                regions=core._json(row['details_json']).get('visual_materials',{})
                core.require(isinstance(regions,dict),'RECORD_INVALID')
                revisions=c.execute('SELECT revision_id FROM document_page_revision WHERE artifact_id=? AND child_id=?', (row['artifact_id'],child_id)).fetchall()
                for region_id,region in regions.items():
                    for revision in revisions:
                        try:
                            detail=self._detail(c,child_id,row['artifact_id'],region_id,revision[0],region['region_version'])
                        except LocalSupplyError:
                            continue
                        items.append(dict(artifact_id=row['artifact_id'],region_id=region_id,revision_id=revision[0],
                            region_version=region['region_version'],title='纸面图形',captured_at=row['created_at'],
                            status=detail['status'],facts_version=detail['facts_version'],storage_status=detail['storage_status']))
            return dict(child_id=child_id,items=items[offset:offset+limit],offset=offset,total=len(items),**core.FLAGS)

    def storage(self, child_id, artifact_id, region_id, *, revision_id,region_version,facts_version,client_action_id,purpose,_delegation=None):
        from .visual_retention import PURPOSE as RETENTION_PURPOSE
        core.require(purpose==(PURPOSE if _delegation is None else RETENTION_PURPOSE),'PURPOSE_INVALID')
        core.require(self.key is not None and len(self.key)==32,'ENCRYPTION_REQUIRED')
        request=dict(kind='storage',revision_id=revision_id,region_version=region_version,facts_version=facts_version,
            client_action_id=client_action_id,purpose=purpose)
        if _delegation is not None:
            from .visual_retention import _Permit, _SEAL, verify_permit
            core.require(isinstance(_delegation,_Permit) and _delegation._seal is _SEAL,'DELEGATION_INVALID')
            request['delegation']=_delegation.metadata
        identity=(child_id,artifact_id,region_id,revision_id,region_version)
        with self.database.external_effect_guard():
            with self.database.connect() as c:
                c.execute('BEGIN IMMEDIATE')
                prior=self._prior(c,child_id,artifact_id,region_id,client_action_id,request)
                if prior:
                    return dict(receipt=prior,detail=self._detail(c,child_id,artifact_id,region_id,revision_id,region_version))
                scope,_,region=self._scope(c,child_id,artifact_id,region_id,revision_id,region_version)
                if _delegation is not None:
                    verify_permit(c,_delegation,scope,region_id,region_version,asset_exists=True)
                core.require(region.get('facts_version')==facts_version,'FACTS_CHANGED')
                descriptor=core._image_descriptor(core._binding(scope,region_id,region),region)
                reusable=[]
                reserved=[]
                for saved in c.execute("SELECT * FROM capture_artifact WHERE child_id=? AND session_id=? AND artifact_type='document_visual_region'",(child_id,scope['session_id'])).fetchall():
                    metadata=core._json(saved['details_json'])
                    if metadata.get('source_binding')==dict(**scope,region_id=region_id,region_version=region_version) and metadata.get('image_descriptor')==descriptor:
                        if saved['status']=='completed':
                            reusable.append(dict(asset_id=saved['artifact_id'],image_descriptor=descriptor))
                        elif saved['status']=='processing':
                            core.require(metadata.get('storage_request_hash')==digest(request),'STORAGE_IN_PROGRESS')
                            reserved.append(saved)
                core.require(len(reserved)<=1 and not (reserved and reusable),'IMAGE_AMBIGUOUS')
                if reserved:
                    saved=reserved[0]
                    core.require(saved['local_path']==str(self.media_root/(saved['artifact_id']+'.png.enc')),'IMAGE_CHANGED')
                    candidate=dict(asset_id=saved['artifact_id'],image_descriptor=descriptor)
                    c.execute("UPDATE capture_artifact SET status='completed' WHERE artifact_id=? AND status='processing'",(saved['artifact_id'],))
                    try:
                        self._asset(c,scope,region_id,region_version,candidate)
                    except OSError as error:
                        raise LocalSupplyError('VISUAL_MATERIAL_IMAGE_INCOMPLETE') from error
                    reusable.append(candidate)
                core.require(len(reusable)<=1,'IMAGE_AMBIGUOUS')
                if reusable:
                    if _delegation is not None:
                        verify_permit(c,_delegation,scope,region_id,region_version,asset_exists=True)
                    self._asset(c,scope,region_id,region_version,reusable[0])
                    self._reset_retracted(c,scope,region_id,region_version)
                    receipt=self._append(c,child_id,artifact_id,region_id,request,dict(status='stored',**reusable[0]))
                    detail=self._detail(c,child_id,artifact_id,region_id,revision_id,region_version)
                    c.commit()
                    return dict(receipt=receipt,detail=detail)
                prepared=self._prepared(c, identity)
                core.require(prepared is not None,'PREPARATION_UNAVAILABLE')
                _png(prepared.png,descriptor)
                if _delegation is not None:
                    core.require(_delegation.metadata['stored_bytes']==len(prepared.png)+32,'IMAGE_CHANGED')
                    verify_permit(c,_delegation,scope,region_id,region_version)
                asset_id=uuid4().hex
                filename=asset_id+'.png.enc'
                self.media_root.mkdir(parents=True,exist_ok=True)
                path=self.media_root/filename
                details=dict(source_binding=dict(**scope,region_id=region_id,region_version=region_version),
                    image_descriptor=descriptor,storage_request_hash=digest(request))
                if _delegation is not None:
                    details['retention']=_delegation.metadata
                c.execute('''INSERT INTO capture_artifact(artifact_id,child_id,session_id,artifact_type,local_path,
                    extracted_text,confidence,details_json,status,created_at,client_action_id)
                    VALUES(?,?,?,'document_visual_region',?,NULL,NULL,?,'processing',?,?)''',
                    (asset_id,child_id,scope['session_id'],str(path),json.dumps(details),_now(),'visual-asset:'+asset_id))
                c.commit()
            try:
                nonce=os.urandom(12)
                write_new_managed_media(b'HSC1'+nonce+AESGCM(self.key).encrypt(nonce,prepared.png,b'hsc-raw-media-v1'),
                    media_root=self.media_root,filename=filename)
                with self.database.connect() as c:
                    c.execute('BEGIN IMMEDIATE')
                    _,_,current=self._scope(c,child_id,artifact_id,region_id,revision_id,region_version)
                    if _delegation is not None:
                        verify_permit(c,_delegation,scope,region_id,region_version,asset_exists=True)
                    core.require(current.get('facts_version')==facts_version,'FACTS_CHANGED')
                    changed=c.execute("UPDATE capture_artifact SET status='completed' WHERE artifact_id=? AND child_id=? AND status='processing'",(asset_id,child_id))
                    core.require(changed.rowcount==1,'SOURCE_CHANGED')
                    self._reset_retracted(c,scope,region_id,region_version)
                    receipt=self._append(c,child_id,artifact_id,region_id,request,
                        dict(status='stored',asset_id=asset_id,image_descriptor=descriptor))
                    detail=self._detail(c,child_id,artifact_id,region_id,revision_id,region_version)
                    core.require(detail['image_availability']=='available','IMAGE_UNAVAILABLE')
                    c.commit()
                self.discard_prepared(identity)
                return dict(receipt=receipt,detail=detail)
            except Exception:
                # The durable artifact reservation owns any file, including a
                # failed cleanup. Use the existing deletion job/retry mechanism.
                from .privacy_lifecycle import PrivacyLifecycleService
                cleanup=PrivacyLifecycleService(self.database,self.data_dir,self.key)
                with self.database.connect() as c:
                    still_exists=c.execute('SELECT 1 FROM capture_artifact WHERE artifact_id=? AND child_id=?',
                        (asset_id,child_id)).fetchone()
                    deletion=c.execute("SELECT deletion_id FROM deletion_job WHERE child_id=? AND target_type='capture_artifact' AND target_id=? ORDER BY created_at DESC LIMIT 1",
                        (child_id,asset_id)).fetchone()
                if not still_exists and deletion:
                    cleanup.retry_deletion_job(deletion['deletion_id'],'确认删除')
                else:
                    cleanup.delete_capture_artifact(asset_id,'确认删除','visual-cleanup:'+asset_id)
                raise

    def image(self, child_id, artifact_id, region_id, *, revision_id,region_version,sha256):
        with self.database.external_effect_guard(),self.database.connect() as c:
            c.execute('BEGIN')
            scope,_,region=self._scope(c,child_id,artifact_id,region_id,revision_id,region_version)
            _,grant=self._grant(c,child_id,artifact_id,region_id,region_version)
            core.require(grant and grant['image_descriptor']['sha256']==sha256,'IMAGE_UNAVAILABLE')
            raw=self._asset(c,scope,region_id,region_version,grant)
            token_scope=dict(**scope,region_id=region_id,region_version=region_version,
                facts_version=region.get('facts_version'),grant_id=grant['grant_id'],descriptor=grant['image_descriptor'],expires=int(time.time())+600)
            body=base64.urlsafe_b64encode(json.dumps(token_scope,sort_keys=True).encode()).decode()
            signature=hmac.new(self._token_key,body.encode(),'sha256').hexdigest()
            return raw,body+'.'+signature

    def _token(self, token):
        try:
            body,signature=token.split('.')
            core.require(hmac.compare_digest(signature,hmac.new(self._token_key,body.encode(),'sha256').hexdigest()),'DISPLAY_REQUIRED')
            data=core._json(base64.urlsafe_b64decode(body))
            core.require(type(data.get('expires')) is int and data['expires']>=time.time(),'DISPLAY_EXPIRED')
            return data
        except (ValueError,TypeError) as error:
            raise LocalSupplyError('VISUAL_MATERIAL_DISPLAY_REQUIRED') from error

    def displayed(self, child_id,artifact_id,region_id,*,revision_id,region_version,facts_version,
                  client_action_id,display_token,sha256,width,height):
        request=dict(kind='displayed',revision_id=revision_id,region_version=region_version,facts_version=facts_version,
            client_action_id=client_action_id,token_hash=digest(display_token),sha256=sha256,width=width,height=height)
        with self.database.external_effect_guard(),self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            prior=self._prior(c,child_id,artifact_id,region_id,client_action_id,request)
            if prior:
                return dict(display_receipt_id=prior['receipt_id'],detail=self._detail(c,child_id,artifact_id,region_id,revision_id,region_version))
            scope,_,region=self._scope(c,child_id,artifact_id,region_id,revision_id,region_version)
            data=self._token(display_token)
            _,grant=self._grant(c,child_id,artifact_id,region_id,region_version)
            core.require(grant and all(data.get(k)==v for k,v in dict(**scope,region_id=region_id,region_version=region_version,
                facts_version=facts_version,grant_id=grant['grant_id']).items()) and region.get('facts_version')==facts_version,'DISPLAY_CHANGED')
            core.require(data['descriptor']==grant['image_descriptor'] and sha256==data['descriptor']['sha256']
                and width==data['descriptor']['width'] and height==data['descriptor']['height'],'DISPLAY_CHANGED')
            self._asset(c,scope,region_id,region_version,grant)
            receipt=self._append(c,child_id,artifact_id,region_id,request,dict(status='displayed',grant_id=grant['grant_id']))
            detail=self._detail(c,child_id,artifact_id,region_id,revision_id,region_version)
            c.commit()
            return dict(display_receipt_id=receipt['receipt_id'],detail=detail)

    def decide(self,child_id,artifact_id,region_id,*,revision_id,region_version,facts_version,
               client_action_id,decision,display_receipt_id=None):
        request=dict(kind=decision,revision_id=revision_id,region_version=region_version,facts_version=facts_version,
            client_action_id=client_action_id,display_receipt_id=display_receipt_id)
        with self.database.external_effect_guard(),self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            prior=self._prior(c,child_id,artifact_id,region_id,client_action_id,request)
            if prior:
                return dict(receipt=prior,detail=self._detail(c,child_id,artifact_id,region_id,revision_id,region_version))
            scope,_,region=self._scope(c,child_id,artifact_id,region_id,revision_id,region_version)
            proof=None
            grant=None
            if decision=='confirm':
                _,grant=self._grant(c,child_id,artifact_id,region_id,region_version)
                core.require(grant,'DISPLAY_REQUIRED')
                event=c.execute('SELECT summary_json FROM audit_event WHERE audit_id=? AND child_id=? AND actor=? AND action=?',
                    (display_receipt_id,child_id,'parent_ui',ACTION)).fetchone()
                data=core._json(event[0]) if event else {}
                req=data.get('request',{})
                core.require(data.get('artifact_id')==artifact_id and data.get('region_id')==region_id
                    and req.get('kind')=='displayed' and req.get('revision_id')==revision_id
                    and req.get('region_version')==region_version and req.get('facts_version')==facts_version
                    and data.get('result',{}).get('grant_id')==grant['grant_id'],'DISPLAY_REQUIRED')
                self._asset(c,scope,region_id,region_version,grant)
                proof=_DisplayProof(c,digest(dict(**scope,region_id=region_id,region_version=region_version,facts_version=facts_version)),_SEAL)
            if decision=='retract' and facts_version is None:
                row,details,region=core._region(c,scope,region_id,region_version)
                core.require(region.get('facts_version') is None,'FACTS_CHANGED')
                region.update(review_status='retracted',confirmation_id=None)
                core._save(c,row,details)
                result=dict(status='withdrawn',binding=core._binding(scope,region_id,region),**core.FLAGS)
            else:
                result=core.confirm_facts(c,**scope,region_id=region_id,region_version=region_version,facts_version=facts_version,
                    decision=decision,client_action_id='host:'+digest(request),_display_proof=proof)
            receipt=self._append(c,child_id,artifact_id,region_id,request,result,
                grant_id=grant['grant_id'] if decision=='confirm' else None)
            detail=self._detail(c,child_id,artifact_id,region_id,revision_id,region_version)
            c.commit()
            return dict(receipt=receipt,detail=detail)

    def command(self,child_id,artifact_id,region_id,client_action_id):
        with self.database.connect() as c:
            c.execute('BEGIN')
            receipt=self._prior(c,child_id,artifact_id,region_id,client_action_id)
            if not receipt:
                raise KeyError(client_action_id)
            return dict(receipt=receipt,**core.FLAGS)
