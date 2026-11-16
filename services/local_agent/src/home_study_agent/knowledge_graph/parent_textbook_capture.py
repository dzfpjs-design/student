"""Parent textbook capture bridge over the existing local CPU OCR adapter.

Unlike the continuous child-study camera stream, this is an explicit parent
import. The exact submitted page is encrypted locally before OCR so later
review, retry, backup, restore, and deletion use the original source.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import math
from io import BytesIO
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ..database import Database
from ..repository import utc_now
from ..local_ocr.resource_budget import ResourceSnapshot
from .schema import ensure_schema


class CaptureConflictError(ValueError):
    """A replayed command carries a different payload or immutable action."""


class CaptureCapacityUnavailableError(ValueError):
    """The local CPU queue rejected a retained textbook OCR page."""


class CaptureNotFoundError(ValueError):
    """The opaque handle does not resolve to a parent OCR artifact."""


class ParentTextbookCaptureService:
    def __init__(self, database: Database, ocr_adapter):
        self.database = database
        self.ocr_adapter = ocr_adapter
        self.boot_id = str(uuid4())

    def acquire(self, child_id: str, system_id_hash: str, capabilities: dict) -> dict:
        now = utc_now()
        with self.database.connect() as con:
            ensure_schema(con, now); con.commit(); con.execute("BEGIN IMMEDIATE")
            con.execute("UPDATE kg_parent_textbook_capture_lease SET status='released',released_at=?,release_reason='expired' WHERE system_id_hash=? AND status='active' AND expires_at<=datetime('now')", (now,system_id_hash))
            child = con.execute("SELECT 1 FROM child_profile WHERE child_id=? AND status='active'", (child_id,)).fetchone()
            binding = con.execute("SELECT label FROM device_binding WHERE device_type='document_camera' AND system_id_hash=?", (system_id_hash,)).fetchone()
            busy = con.execute("""SELECT 1 FROM sensor_lease l JOIN device_profile d ON d.device_id=l.device_id
                WHERE d.system_id_hash=? AND l.status='active' AND l.expires_at>datetime('now') LIMIT 1""", (system_id_hash,)).fetchone()
            parent_busy = con.execute("SELECT 1 FROM kg_parent_textbook_capture_lease WHERE system_id_hash=? AND status='active' AND expires_at>datetime('now') LIMIT 1", (system_id_hash,)).fetchone()
            if not child: con.commit(); raise CaptureNotFoundError('child profile is unavailable')
            if not binding: con.commit(); raise CaptureNotFoundError('selected camera is not the bound document camera')
            if busy or parent_busy: con.commit(); raise ValueError('document camera is already in use; it was not opened')
            lease_id = str(uuid4())
            con.execute("INSERT INTO kg_parent_textbook_capture_lease(lease_id,child_id,system_id_hash,capabilities_json,status,opened_at,last_renewed_at,expires_at) VALUES(?,?,?,?, 'active',?,?,datetime('now','+3 minutes'))", (lease_id,child_id,system_id_hash,json.dumps(capabilities,ensure_ascii=False),now,now))
            con.commit()
        return {'lease_id': lease_id, 'status': 'active', 'label': binding['label']}

    def renew(self, lease_id: str) -> dict:
        with self.database.connect() as con:
            lease = con.execute("SELECT status,expires_at FROM kg_parent_textbook_capture_lease WHERE lease_id=?", (lease_id,)).fetchone()
            if not lease: raise CaptureNotFoundError('textbook capture lease not found')
            if lease['status'] != 'active' or lease['expires_at'] <= datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'):
                raise ValueError('textbook capture lease expired')
            con.execute("UPDATE kg_parent_textbook_capture_lease SET last_renewed_at=?,expires_at=datetime('now','+3 minutes') WHERE lease_id=?", (utc_now(),lease_id));con.commit()
        return {'lease_id':lease_id,'status':'active'}

    def release(self, lease_id: str, reason: str) -> dict:
        with self.database.connect() as con:
            known=con.execute("SELECT 1 FROM kg_parent_textbook_capture_lease WHERE lease_id=?",(lease_id,)).fetchone()
            if not known: raise CaptureNotFoundError('textbook capture lease not found')
            con.execute("UPDATE kg_parent_textbook_capture_lease SET status='released',released_at=?,release_reason=? WHERE lease_id=? AND status='active'", (utc_now(),reason[:80],lease_id));con.commit()
        return {'lease_id':lease_id,'status':'released'}

    def recognize(self, child_id: str, lease_id: str, image_base64: str, page_context: dict, client_action_id: str,
                  image_transform: dict | None = None) -> dict:
        self._require_lease(child_id, lease_id)
        return self._recognize(child_id, lease_id, 'camera', image_base64, page_context, client_action_id, image_transform)

    def recognize_file(self, child_id: str, image_base64: str, page_context: dict, client_action_id: str,
                       image_transform: dict | None = None) -> dict:
        return self._recognize(child_id, None, 'file', image_base64, page_context, client_action_id, image_transform)

    def _recognize(self, child_id: str, lease_id: str | None, source_kind: str, image_base64: str,
                   page_context: dict, client_action_id: str, image_transform: dict | None) -> dict:
        try: image = base64.b64decode(image_base64, validate=True)
        except Exception as error: raise ValueError('invalid image') from error
        if not image or len(image) > 18_000_000:
            raise ValueError('invalid image size')
        if source_kind == 'file':
            from PIL import Image, UnidentifiedImageError
            try:
                with Image.open(BytesIO(image)) as source:
                    if source.format not in {'PNG','JPEG','TIFF','WEBP'} or source.width * source.height > 30_000_000:
                        raise ValueError('unsupported file page or pixel limit exceeded')
                    source.verify()
            except (OSError, UnidentifiedImageError, Image.DecompressionBombError) as error:
                raise ValueError('file page is not a valid image') from error
        transform = self._normalize_transform(image_transform)
        ocr_image = self._derive_image(image, transform)
        if not isinstance(page_context, dict) or not str(page_context.get('title','')).strip(): raise ValueError('textbook page title is required before capture')
        image_hash=hashlib.sha256(image).hexdigest();context_hash=hashlib.sha256(json.dumps(page_context,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        artifact_id=str(uuid4()); now=utc_now()
        stored_path = None
        try:
            with self.database.connect() as con:
                ensure_schema(con,now); con.commit(); con.execute("BEGIN IMMEDIATE")
                if not con.execute("SELECT 1 FROM child_profile WHERE child_id=? AND status='active'", (child_id,)).fetchone():
                    raise CaptureNotFoundError('child profile is unavailable')
                prior=con.execute("SELECT * FROM kg_parent_textbook_ocr_artifact WHERE client_action_id=?", (client_action_id,)).fetchone()
                if prior:
                    if (prior['child_id'] != child_id or prior['lease_id'] != lease_id or prior['source_kind'] != source_kind
                            or prior['image_sha256'] != image_hash or prior['page_context_sha256'] != context_hash
                            or self._recorded_transform(con, prior['artifact_id']) != transform):
                        con.commit()
                        raise CaptureConflictError('client action conflicts with a different child, page, lease, or image')
                    self._validate_retained_source(con, prior)
                    con.commit()
                    return self._public(prior)
                if source_kind == 'camera' and not con.execute("SELECT 1 FROM kg_parent_textbook_capture_lease WHERE lease_id=? AND child_id=? AND status='active' AND expires_at>datetime('now')", (lease_id,child_id)).fetchone():
                    raise ValueError('document camera lease is unavailable')
                media_type = self._media_type(image)
                file_source_id = str(uuid4()) if source_kind == 'file' else None
                request = {'image_sha256':image_hash,'page_context_sha256':context_hash,
                    'raw_image_byte_length':len(image),'raw_image_media_type':media_type,
                    'image_transform':transform,'source_kind':source_kind,'file_source_id':file_source_id,
                    'ocr_image_sha256':hashlib.sha256(ocr_image).hexdigest()}
                if file_source_id:
                    con.execute('''INSERT INTO kg_parent_textbook_file_source
                        (file_source_id,child_id,image_sha256,page_context_sha256,client_action_id,request_sha256,created_at)
                        VALUES(?,?,?,?,?,?,?)''', (file_source_id,child_id,image_hash,context_hash,client_action_id,self._payload_hash(request),now))
                stored_path = self._store_raw_page(child_id,artifact_id,image)
                privacy={'raw_image_uploaded':False,'raw_image_retained':True,
                    'raw_image_encrypted':True,'cloud_transmission_allowed':False}
                con.execute("""INSERT INTO kg_parent_textbook_ocr_artifact(
                    artifact_id,child_id,lease_id,source_kind,file_source_id,page_context_json,extracted_text,confidence,
                    details_json,image_sha256,page_context_sha256,raw_image_path,
                    raw_image_media_type,raw_image_byte_length,raw_image_encryption_version,
                    status,created_at,updated_at,client_action_id)
                    VALUES(?,?,?,?,?,?, '',0,?,?,?,?,?,?,?,'processing',?,?,?)""",
                    (artifact_id,child_id,lease_id,source_kind,file_source_id,json.dumps(page_context,ensure_ascii=False),
                     json.dumps({'privacy':privacy},ensure_ascii=False),image_hash,context_hash,
                     stored_path,media_type,len(image),'hsc-raw-media-v1',now,now,client_action_id))
                self._event(con,artifact_id,'created',{'request':request},client_action_id);con.commit()
        except BaseException:
            if stored_path:
                with self.database.connect() as check:
                    owned=check.execute("SELECT 1 FROM kg_parent_textbook_ocr_artifact WHERE raw_image_path=?",(stored_path,)).fetchone()
                if not owned:
                    Path(stored_path).unlink(missing_ok=True)
            raise
        return self._run_ocr(artifact_id,ocr_image,client_action_id)

    @staticmethod
    def _normalize_transform(value: dict | None) -> dict:
        if value is None:
            value = {}
        if not isinstance(value, dict) or set(value) - {'rotation', 'crop'}:
            raise ValueError('invalid page transform')
        rotation = value.get('rotation', 0)
        crop = value.get('crop', {})
        if type(rotation) is not int or rotation not in {0,90,180,270}:
            raise ValueError('invalid page rotation')
        if not isinstance(crop, dict) or set(crop) - {'left','right','top','bottom'}:
            raise ValueError('invalid page crop')
        crop = {side:crop.get(side,0) for side in ('left','right','top','bottom')}
        if any(type(n) not in {int,float} or not math.isfinite(n) or not 0 <= n <= 95 for n in crop.values()):
            raise ValueError('invalid page crop')
        if crop['left'] + crop['right'] >= 95 or crop['top'] + crop['bottom'] >= 95:
            raise ValueError('page crop removes the page')
        return {'rotation':rotation,'crop':crop}

    @classmethod
    def _recorded_transform(cls, con, artifact_id: str) -> dict:
        rows = con.execute("SELECT payload_json,payload_sha256 FROM kg_parent_textbook_ocr_review_event WHERE artifact_id=? AND event_type='created'",(artifact_id,)).fetchall()
        if len(rows) != 1:
            raise ValueError('original page derivation provenance is unavailable')
        row = rows[0]
        try:
            request = json.loads(row['payload_json'])['request']
        except (TypeError, ValueError, KeyError) as error:
            raise ValueError('original page derivation provenance is invalid') from error
        if not isinstance(request, dict) or (row['payload_sha256'] != cls._payload_hash(request)
                and not (row['payload_sha256'] == 'legacy' and 'image_transform' not in request)):
            raise ValueError('original page derivation provenance changed')
        # Old retained bytes are kept exactly as submitted; no invented transform.
        return cls._normalize_transform(request.get('image_transform'))

    @staticmethod
    def _derive_image(image: bytes, transform: dict) -> bytes:
        crop = transform['crop']
        if not transform['rotation'] and not any(crop.values()):
            return image
        from PIL import Image, UnidentifiedImageError
        try:
            with Image.open(BytesIO(image)) as source:
                width, height = source.size
                if width * height > 30_000_000:
                    raise ValueError('page pixels exceed local processing limit')
                box = (round(width*crop['left']/100),round(height*crop['top']/100),
                       round(width*(1-crop['right']/100)),round(height*(1-crop['bottom']/100)))
                if box[2] <= box[0] or box[3] <= box[1]:
                    raise ValueError('page crop is empty')
                derived = source.convert('RGB').crop(box).rotate(-transform['rotation'], expand=True)
                output = BytesIO()
                derived.save(output, format='PNG')
                return output.getvalue()
        except (OSError, UnidentifiedImageError, Image.DecompressionBombError) as error:
            raise ValueError('page cannot be transformed') from error

    def _run_ocr(self, artifact_id: str, image: bytes, action_id: str) -> dict:
        scheduled = None
        try:
            scheduled = self.ocr_adapter.enqueue_ocr(image, request_id=f'parent-textbook:{action_id}')
            claim = self.ocr_adapter.claim_ocr(scheduled, ResourceSnapshot(cpu_percent=0, memory_mb=0))
            if not claim:
                self._cancel_scheduled(scheduled, 'capacity_unavailable')
                self._fail(artifact_id, 'capacity_unavailable')
                raise CaptureCapacityUnavailableError('local CPU is busy; the original page is retained and can be retried later')
            job = self.ocr_adapter.run_ocr_claim(claim, image)
        except ValueError:
            raise
        except Exception as error:
            if scheduled:
                self._cancel_scheduled(scheduled, type(error).__name__)
            self._fail(artifact_id, type(error).__name__)
            raise ValueError('local OCR failed') from error
        if job['status'] != 'completed' or not job.get('result'):
            self._cancel_scheduled(scheduled,job.get('error_code') or 'local OCR failed')
            self._fail(artifact_id,job.get('error_code') or 'local OCR failed'); raise ValueError(job.get('error_code') or 'local OCR failed')
        result = job['result']; text='\n'.join(str(line.get('text','')) for line in result.get('lines',[])).strip()
        manual_review = copy.deepcopy(result['manual_review'])
        reasons = manual_review.get('reasons')
        confidence = result.get('confidence')
        quality = result.get('quality')
        # Whole-page material capture retains text and its source. It does not
        # need a worksheet question start, and never grants teaching authority.
        if (manual_review.get('required') is True and isinstance(reasons, list)
                and reasons and all(isinstance(reason, str) for reason in reasons)
                and set(reasons) == {'missing_question_start'}
                and isinstance(quality, dict) and quality.get('passed') is True
                and not quality.get('issues') and text
                and isinstance(confidence, (int, float)) and not isinstance(confidence, bool)
                and math.isfinite(confidence) and 0.95 <= confidence <= 1
                and manual_review.get('handwriting') != 'review_required'
                and manual_review.get('math') != 'review_required'):
            manual_review = {**manual_review, 'required': False, 'reasons': [],
                'auto_accept_allowed': True, 'review_scope': 'whole_page_text_v1',
                'original_question_boundary_review': copy.deepcopy(result['manual_review'])}
        needs_review = bool(manual_review['required']) or not text
        with self.database.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            current = self._row(con, artifact_id)
            if (not current or current['status'] != 'processing' or not current['raw_image_path']
                    or not con.execute("SELECT 1 FROM child_profile WHERE child_id=? AND status='active'", (current['child_id'],)).fetchone()):
                raise ValueError('original page source was invalidated during OCR')
            request = self._validate_retained_source(con,current)
            if request.get('ocr_image_sha256',current['image_sha256']) != hashlib.sha256(image).hexdigest():
                raise ValueError('original page derivation source changed during OCR')
            status='needs_review' if needs_review else 'ready'
            privacy={**(result.get('privacy') or {}),'raw_image_uploaded':False,
                'raw_image_retained':True,'raw_image_encrypted':True,
                'cloud_transmission_allowed':False}
            con.execute("UPDATE kg_parent_textbook_ocr_artifact SET extracted_text=?,confidence=?,details_json=?,status=?,updated_at=? WHERE artifact_id=?", (text,float(result['confidence']),json.dumps({'quality':result['quality'],'manual_review':manual_review,'engine':result['engine'],'privacy':privacy},ensure_ascii=False),status,utc_now(),artifact_id));con.commit()
            return self._public(self._row(con,artifact_id))

    def pending(self, child_id: str) -> list[dict]:
        self._reconcile_raw_page_storage()
        self._recover_interrupted_processing()
        self._recover_interrupted_importing()
        with self.database.connect() as con:
            rows=con.execute("SELECT * FROM kg_parent_textbook_ocr_artifact WHERE child_id=? AND status IN ('processing','needs_review','ready','importing','failed','skipped') ORDER BY updated_at DESC", (child_id,)).fetchall()
        return [self._public(row) for row in rows]

    def action_receipt(self, child_id: str, capture_ref: str, client_action_id: str) -> dict:
        from ..privacy_operation_receipt import generation
        artifact_id = self._id_for_ref(capture_ref)
        with self.database.connect() as con:
            row = self._row(con, artifact_id)
            if not row or row['child_id'] != child_id:
                raise CaptureNotFoundError('OCR artifact not found')
            event = con.execute('SELECT * FROM kg_parent_textbook_ocr_review_event WHERE artifact_id=? AND client_action_id=?', (artifact_id, client_action_id)).fetchone()
            if not event:
                return {'status':'unknown', 'client_action_id':client_action_id, 'cloud_transmission_allowed':False}
            request = json.loads(event['payload_json']).get('request', {})
            if self._payload_hash(request) != event['payload_sha256']:
                raise ValueError('OCR command receipt could not be verified')
            return {'status':'confirmed', 'action':event['event_type'], 'request':request,
                    'child_id':child_id, 'storage_generation':generation(con),
                    'client_action_id':client_action_id, 'artifact':self._public(row), 'cloud_transmission_allowed':False}

    def review(self, child_id: str, capture_ref: str, text: str, client_action_id: str) -> dict:
        artifact_id=self._id_for_ref(capture_ref); text=text.strip()
        if not text: raise ValueError('corrected OCR text is required')
        with self.database.connect() as con:
            row=self._row(con,artifact_id)
            if not row or row['child_id']!=child_id: raise CaptureNotFoundError('OCR artifact not found')
            if self._replayed(con,artifact_id,'reviewed',client_action_id,{'extracted_text':text}): return self._public(row)
            if row['status']!='needs_review': raise ValueError('only OCR artifacts awaiting review can be corrected')
            con.execute("UPDATE kg_parent_textbook_ocr_artifact SET extracted_text=?,status='ready',updated_at=? WHERE artifact_id=?", (text,utc_now(),artifact_id));self._event(con,artifact_id,'reviewed',{'request':{'extracted_text':text},'previous':{'status':row['status'],'extracted_text':row['extracted_text']}},client_action_id);con.commit()
            return self._public(self._row(con,artifact_id))

    def undo_review(self, child_id: str, capture_ref: str, client_action_id: str) -> dict:
        artifact_id=self._id_for_ref(capture_ref)
        with self.database.connect() as con:
            row=self._row(con,artifact_id)
            if not row or row['child_id']!=child_id: raise CaptureNotFoundError('OCR artifact not found')
            if self._replayed(con,artifact_id,'review_undone',client_action_id,{}): return self._public(row)
            if row['status']!='ready': raise ValueError('only a ready OCR artifact can have its correction undone')
            event=con.execute("SELECT payload_json FROM kg_parent_textbook_ocr_review_event WHERE artifact_id=? AND event_type='reviewed' ORDER BY rowid DESC LIMIT 1",(artifact_id,)).fetchone()
            if not event: raise ValueError('no correction is available to undo')
            previous=json.loads(event['payload_json']).get('previous',{})
            con.execute("UPDATE kg_parent_textbook_ocr_artifact SET extracted_text=?,status='needs_review',updated_at=? WHERE artifact_id=?",(str(previous.get('extracted_text','')),utc_now(),artifact_id))
            self._event(con,artifact_id,'review_undone',{'request':{},'previous':{'status':row['status'],'extracted_text':row['extracted_text']}},client_action_id);con.commit()
            return self._public(self._row(con,artifact_id))

    def skip(self, child_id: str, capture_ref: str, client_action_id: str) -> dict:
        return self._transition(child_id,capture_ref,{'needs_review','ready','failed'},'skipped','skipped',client_action_id)

    def restore(self, child_id: str, capture_ref: str, client_action_id: str) -> dict:
        artifact_id=self._id_for_ref(capture_ref)
        with self.database.connect() as con:
            row=self._row(con,artifact_id)
            if not row or row['child_id']!=child_id: raise CaptureNotFoundError('OCR artifact not found')
            if self._replayed(con,artifact_id,'restored',client_action_id,{}): return self._public(row)
            if row['status']!='skipped': raise ValueError('only skipped OCR artifacts can be restored')
            event=con.execute("SELECT payload_json FROM kg_parent_textbook_ocr_review_event WHERE artifact_id=? AND event_type='skipped' ORDER BY rowid DESC LIMIT 1",(artifact_id,)).fetchone(); prior=json.loads(event['payload_json']).get('previous_status','needs_review') if event else 'needs_review'
            con.execute("UPDATE kg_parent_textbook_ocr_artifact SET status=?,updated_at=? WHERE artifact_id=?",(prior,utc_now(),artifact_id));self._event(con,artifact_id,'restored',{'request':{},'restored_status':prior},client_action_id);con.commit();return self._public(self._row(con,artifact_id))

    def retry_requested(self, child_id: str, capture_ref: str, client_action_id: str) -> dict:
        artifact_id=self._id_for_ref(capture_ref)
        image=self._read_raw_page(child_id,artifact_id)
        with self.database.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row=self._row(con,artifact_id)
            if not row or row['child_id']!=child_id: raise CaptureNotFoundError('OCR artifact not found')
            image = self._derive_image(image, self._recorded_transform(con, artifact_id))
            self._validate_retained_source(con, row)
            if not con.execute("SELECT 1 FROM child_profile WHERE child_id=? AND status='active'", (child_id,)).fetchone():
                raise CaptureNotFoundError('child profile is unavailable')
            if self._replayed(con,artifact_id,'retry_requested',client_action_id,{}):
                con.commit()
                return self._public(row)
            if row['status'] not in {'failed','needs_review'}: raise ValueError('OCR artifact cannot be retried')
            con.execute("UPDATE kg_parent_textbook_ocr_artifact SET status='processing',updated_at=? WHERE artifact_id=?",(utc_now(),artifact_id))
            self._event(con,artifact_id,'retry_requested',{'request':{},'raw_image_available':True},client_action_id)
            con.commit()
        return self._run_ocr(artifact_id,image,client_action_id)

    def source_image(self, child_id: str, capture_ref: str) -> tuple[bytes,str]:
        """Return one authenticated parent import without exposing its path."""
        artifact_id=self._id_for_ref(capture_ref)
        image=self._read_raw_page(child_id,artifact_id)
        with self.database.connect() as con:
            row=self._row(con,artifact_id)
        if not row or row['child_id'] != child_id:
            raise CaptureNotFoundError('OCR artifact not found')
        return image,str(row['raw_image_media_type'] or 'application/octet-stream')

    def page_source_ref(self, capture_ref: str) -> str:
        """Return the stable, non-user-facing source identity for one artifact."""
        self._id_for_ref(capture_ref)
        return f'local_parent_textbook_capture:{capture_ref}'

    def reserve_page_import(self, child_id: str, capture_ref: str, client_action_id: str, request: dict) -> bool:
        """Atomically reserve page mutation; True means an imported replay."""
        artifact_id=self._id_for_ref(capture_ref)
        with self.database.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row=self._row(con,artifact_id)
            if not row or row['child_id'] != child_id: raise CaptureNotFoundError('OCR artifact not found')
            self._validate_retained_source(con, row)
            if row['status'] == 'imported':
                replay=self._replayed(con,artifact_id,'imported',client_action_id,request)
                con.commit()
                return replay
            request_hash=self._payload_hash(request)
            if row['status'] == 'importing':
                if row['import_action_id'] != client_action_id or row['import_payload_sha256'] != request_hash:
                    con.commit()
                    raise CaptureConflictError('OCR artifact import is reserved by a different action or payload')
                if row['import_owner_boot_id'] != self.boot_id:
                    con.commit()
                    raise CaptureConflictError('OCR artifact import belongs to a previous app start and requires recovery')
                con.commit()
                return False
            if row['status'] != 'ready': raise ValueError('reviewed OCR artifact is unavailable for this child')
            con.execute("UPDATE kg_parent_textbook_ocr_artifact SET status='importing',import_action_id=?,import_payload_sha256=?,import_request_json=?,import_claimed_at=?,import_owner_boot_id=?,updated_at=? WHERE artifact_id=? AND status='ready'",(client_action_id,request_hash,json.dumps(request,ensure_ascii=False),utc_now(),self.boot_id,utc_now(),artifact_id))
            con.commit()
        return False

    def finalize_page_import(self, child_id: str, capture_ref: str, client_action_id: str, request: dict) -> None:
        artifact_id=self._id_for_ref(capture_ref)
        with self.database.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row=self._row(con,artifact_id)
            if not row or row['child_id'] != child_id: raise CaptureNotFoundError('OCR artifact not found')
            if self._replayed(con,artifact_id,'imported',client_action_id,request): return
            if row['status'] != 'importing' or row['import_action_id'] != client_action_id or row['import_payload_sha256'] != self._payload_hash(request):
                raise ValueError('reviewed OCR artifact is unavailable for this child')
            result=con.execute("UPDATE kg_parent_textbook_ocr_artifact SET status='imported',updated_at=? WHERE artifact_id=? AND child_id=? AND status='importing' AND import_action_id=? AND import_payload_sha256=? AND import_owner_boot_id=?", (utc_now(),artifact_id,child_id,client_action_id,self._payload_hash(request),self.boot_id))
            if result.rowcount != 1: raise ValueError('reviewed OCR artifact is unavailable for this child')
            self._event(con,artifact_id,'imported',{'request':request},client_action_id)
            con.commit()

    def assert_ready(self, child_id: str, capture_ref: str) -> None:
        artifact_id=self._id_for_ref(capture_ref)
        with self.database.connect() as con:
            row=con.execute("SELECT 1 FROM kg_parent_textbook_ocr_artifact WHERE artifact_id=? AND child_id=? AND status='ready'", (artifact_id,child_id)).fetchone()
        if not row: raise ValueError('reviewed OCR artifact is unavailable for this child')

    def _require_lease(self, child_id: str, lease_id: str) -> None:
        with self.database.connect() as con:
            row=con.execute("SELECT 1 FROM kg_parent_textbook_capture_lease WHERE lease_id=? AND child_id=? AND status='active' AND expires_at>datetime('now')", (lease_id,child_id)).fetchone()
        if not row: raise ValueError('document camera lease is unavailable')

    @classmethod
    def _validate_retained_source(cls, con, row) -> dict:
        rows = con.execute("SELECT payload_json,payload_sha256 FROM kg_parent_textbook_ocr_review_event WHERE artifact_id=? AND event_type='created'", (row['artifact_id'],)).fetchall()
        if len(rows) != 1:
            raise ValueError('original page source is unavailable')
        cls._recorded_transform(con, row['artifact_id'])
        request = json.loads(rows[0]['payload_json'])['request']
        if request.get('image_sha256') != row['image_sha256'] or request.get('page_context_sha256') != row['page_context_sha256']:
            raise ValueError('original page source changed')
        if row['source_kind'] == 'file':
            grant = con.execute('SELECT * FROM kg_parent_textbook_file_source WHERE file_source_id=?', (row['file_source_id'],)).fetchone()
            if (row['lease_id'] is not None or not grant
                    or request.get('source_kind') != 'file' or request.get('file_source_id') != row['file_source_id']
                    or any(grant[name] != row[name] for name in ('child_id','image_sha256','page_context_sha256','client_action_id'))
                    or grant['request_sha256'] != cls._payload_hash(request)):
                raise ValueError('original file source is unavailable or changed')
        elif (row['source_kind'] != 'camera' or request.get('source_kind') not in {None,'camera'}
                or request.get('file_source_id') is not None or row['file_source_id'] is not None or not con.execute(
                'SELECT 1 FROM kg_parent_textbook_capture_lease WHERE lease_id=? AND child_id=?',
                (row['lease_id'],row['child_id'])).fetchone()):
            raise ValueError('original camera source is unavailable')
        return request

    def _transition(self, child_id: str, capture_ref: str, allowed: set[str], target: str, event_type: str, client_action_id: str) -> dict:
        artifact_id=self._id_for_ref(capture_ref)
        with self.database.connect() as con:
            row=self._row(con,artifact_id)
            if not row or row['child_id']!=child_id: raise CaptureNotFoundError('OCR artifact not found')
            if self._replayed(con,artifact_id,event_type,client_action_id,{}): return self._public(row)
            if row['status'] not in allowed: raise ValueError('OCR artifact cannot change state')
            con.execute("UPDATE kg_parent_textbook_ocr_artifact SET status=?,updated_at=? WHERE artifact_id=?",(target,utc_now(),artifact_id));self._event(con,artifact_id,event_type,{'request':{},'previous_status':row['status']},client_action_id);con.commit();return self._public(self._row(con,artifact_id))
    @staticmethod
    def _payload_hash(payload: dict) -> str:
        return hashlib.sha256(json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    def _event(self, con, artifact_id: str, event_type: str, payload: dict, client_action_id: str) -> None:
        request=payload.get('request',payload)
        con.execute("INSERT INTO kg_parent_textbook_ocr_review_event(event_id,artifact_id,event_type,payload_json,payload_sha256,client_action_id,created_at) VALUES(?,?,?,?,?,?,?)",(str(uuid4()),artifact_id,event_type,json.dumps(payload,ensure_ascii=False),self._payload_hash(request),client_action_id,utc_now()))
    def _replayed(self, con, artifact_id: str, event_type: str, client_action_id: str, request: dict) -> bool:
        event=con.execute("SELECT artifact_id,event_type,payload_sha256 FROM kg_parent_textbook_ocr_review_event WHERE client_action_id=?",(client_action_id,)).fetchone()
        if not event: return False
        if event['artifact_id'] != artifact_id or event['event_type'] != event_type or event['payload_sha256'] != self._payload_hash(request):
            raise CaptureConflictError('client action conflicts with a different OCR command or payload')
        return True
    def _fail(self, artifact_id: str, reason: str) -> None:
        with self.database.connect() as con:
            con.execute("UPDATE kg_parent_textbook_ocr_artifact SET status='failed',updated_at=? WHERE artifact_id=?",(utc_now(),artifact_id));self._event(con,artifact_id,'failed',{'request':{'reason':reason}},f'system-failed:{artifact_id}:{uuid4()}');con.commit()

    def _cancel_scheduled(self, scheduled: dict, reason: str) -> None:
        cancel=getattr(self.ocr_adapter,'cancel_ocr',None)
        if not callable(cancel):
            raise RuntimeError('OCR adapter cannot cancel a stranded CPU task')
        cancel(scheduled,reason)

    @staticmethod
    def _media_type(image: bytes) -> str:
        if image.startswith(b'\xff\xd8\xff'):
            return 'image/jpeg'
        if image.startswith(b'\x89PNG\r\n\x1a\n'):
            return 'image/png'
        if image.startswith((b'II*\x00',b'MM\x00*')):
            return 'image/tiff'
        if len(image) >= 12 and image[:4] == b'RIFF' and image[8:12] == b'WEBP':
            return 'image/webp'
        return 'application/octet-stream'

    def _media_runtime(self) -> tuple[Path,bytes]:
        runtime=self.database.local_media_runtime
        if runtime is None:
            raise ValueError('local encrypted media storage is unavailable')
        key=runtime.data_encryption_key
        if not isinstance(key,bytes) or len(key) not in {16,24,32}:
            raise ValueError('local encrypted media key is unavailable')
        root=Path(runtime.media_root).resolve()
        return root,key

    def _store_raw_page(self, child_id: str, artifact_id: str, image: bytes) -> str:
        root,key=self._media_runtime()
        directory=root/'media'/child_id/'parent-textbook'
        directory.mkdir(parents=True,exist_ok=True)
        try:
            directory.chmod(0o700)
        except OSError:
            pass
        path=directory/f'{artifact_id}.page.enc'
        nonce=os.urandom(12)
        payload=b'HSC1'+nonce+AESGCM(key).encrypt(nonce,image,b'hsc-raw-media-v1')
        descriptor=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        try:
            with os.fdopen(descriptor,'wb') as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return str(path)

    def _read_raw_page(self, child_id: str, artifact_id: str) -> bytes:
        root,key=self._media_runtime()
        with self.database.connect() as con:
            row=con.execute("SELECT child_id,raw_image_path,raw_image_byte_length,image_sha256 FROM kg_parent_textbook_ocr_artifact WHERE artifact_id=?",(artifact_id,)).fetchone()
        if not row or row['child_id'] != child_id:
            raise CaptureNotFoundError('OCR artifact not found')
        if not row['raw_image_path']:
            raise ValueError('the retained original page is unavailable')
        path=Path(row['raw_image_path']).resolve()
        managed=(root/'media').resolve()
        try:
            path.relative_to(managed)
        except ValueError as error:
            raise ValueError('the retained original page is outside managed storage') from error
        try:
            payload=path.read_bytes()
        except OSError as error:
            raise ValueError('the retained original page is unavailable') from error
        if not payload.startswith(b'HSC1') or len(payload) < 32:
            raise ValueError('the retained original page is corrupted')
        try:
            image=AESGCM(key).decrypt(payload[4:16],payload[16:],b'hsc-raw-media-v1')
        except Exception as error:
            raise ValueError('the retained original page cannot be decrypted') from error
        if len(image) != int(row['raw_image_byte_length'] or 0) or hashlib.sha256(image).hexdigest() != row['image_sha256']:
            raise ValueError('the retained original page failed integrity verification')
        return image

    def _reconcile_raw_page_storage(self, orphan_grace_seconds: int = 300) -> dict[str,int]:
        """Repair database/file drift without touching a live staged write.

        A page file is created before its database row so a process crash can
        leave an orphan. Conversely an external disk cleanup can remove a file
        while metadata still claims that it is available. Only old files in the
        exact managed parent-textbook namespace are eligible for orphan cleanup.
        """
        if type(orphan_grace_seconds) is not int or orphan_grace_seconds < 60:
            raise ValueError('parent textbook orphan grace period is invalid')
        if self.database.local_media_runtime is None:
            return {'missing_reconciled':0,'orphans_removed':0}
        root,_key=self._media_runtime()
        managed=(root/'media').resolve()
        referenced: set[Path]=set()
        missing=0
        now=utc_now()
        with self.database.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            rows=con.execute("""SELECT artifact_id,raw_image_path FROM kg_parent_textbook_ocr_artifact
                WHERE raw_image_path IS NOT NULL AND raw_image_path!=''""").fetchall()
            for row in rows:
                try:
                    path=Path(row['raw_image_path']).resolve()
                    path.relative_to(managed)
                    current=path.is_file()
                except (OSError,ValueError,TypeError):
                    current=False
                    path=None
                if current and path is not None:
                    referenced.add(path)
                    continue
                con.execute("""UPDATE kg_parent_textbook_ocr_artifact
                    SET raw_image_path=NULL,raw_image_media_type=NULL,raw_image_byte_length=0,
                        raw_image_encryption_version=NULL,updated_at=? WHERE artifact_id=?""",
                    (now,row['artifact_id']))
                self._event(con,row['artifact_id'],'raw_source_missing',
                    {'request':{'reason':'managed_source_missing_or_unsafe'},
                     'cloud_transmission_allowed':False},
                    f'system-raw-source-missing:{row["artifact_id"]}:{uuid4()}')
                missing+=1
            con.commit()
        removed=0
        cutoff=datetime.now(timezone.utc).timestamp()-orphan_grace_seconds
        if managed.is_dir():
            for candidate in managed.glob('*/parent-textbook/*.page.enc'):
                try:
                    resolved=candidate.resolve()
                    resolved.relative_to(managed)
                    if resolved in referenced or resolved.stat().st_mtime > cutoff:
                        continue
                    resolved.unlink()
                    removed+=1
                except (FileNotFoundError,OSError,ValueError):
                    continue
        return {'missing_reconciled':missing,'orphans_removed':removed}

    def _recover_interrupted_processing(self, max_age_seconds: int = 120) -> None:
        cutoff=datetime.now(timezone.utc)-timedelta(seconds=max_age_seconds)
        with self.database.connect() as con:
            rows=con.execute("SELECT artifact_id,updated_at FROM kg_parent_textbook_ocr_artifact WHERE status='processing'").fetchall()
            for row in rows:
                try: updated=datetime.fromisoformat(row['updated_at']).astimezone(timezone.utc)
                except (TypeError,ValueError): updated=datetime.min.replace(tzinfo=timezone.utc)
                if updated <= cutoff:
                    con.execute("UPDATE kg_parent_textbook_ocr_artifact SET status='failed',updated_at=? WHERE artifact_id=? AND status='processing'",(utc_now(),row['artifact_id']))
                    self._event(con,row['artifact_id'],'recovery_required',{'request':{'reason':'processing_interrupted'},'reason':'processing_interrupted'},f'system-recovery:{row["artifact_id"]}:{uuid4()}')
            con.commit()

    def _recover_interrupted_importing(self) -> None:
        """Only a new app start compensates an abandoned import reservation."""
        with self.database.connect() as con:
            rows=con.execute("SELECT artifact_id,import_action_id,import_request_json,import_owner_boot_id,updated_at FROM kg_parent_textbook_ocr_artifact WHERE status='importing'").fetchall()
            for row in rows:
                # A live import in this application start owns its reservation
                # until it finalizes; polling must never steal it by elapsed time.
                if row['import_owner_boot_id'] == self.boot_id:
                    continue
                request=json.loads(row['import_request_json'] or '{}')
                source_ref=str(request.get('source_ref',''))
                page=con.execute("SELECT resource_id FROM kg_resource WHERE corpus_id='family-textbook-ocr-pages-v1' AND source_ref=?",(source_ref,)).fetchone() if source_ref else None
                if page and row['import_action_id']:
                    # Source-row existence proves only the page transaction. Run
                    # the idempotent post-write reconciliation before publishing
                    # `imported`, so a crash cannot silently skip stale cleanup.
                    try:
                        from .language_candidate_service import TextbookFocusCandidateService
                        text='\n'.join(item[0] for item in con.execute("SELECT text FROM kg_segment WHERE resource_id=? ORDER BY ordinal",(page['resource_id'],)))
                        con.commit()
                        TextbookFocusCandidateService(self.database).reconcile_page(page['resource_id'],text)
                    except Exception as error:
                        self._event(con,row['artifact_id'],'recovery_required',{'request':{'reason':'import_reconciliation_failed'},'reason':type(error).__name__},f'system-import-reconcile:{row["artifact_id"]}:{uuid4()}')
                        con.commit()
                        continue
                    con.execute("UPDATE kg_parent_textbook_ocr_artifact SET status='imported',updated_at=? WHERE artifact_id=? AND status='importing'",(utc_now(),row['artifact_id']))
                    self._event(con,row['artifact_id'],'imported',{'request':request},row['import_action_id'])
                else:
                    con.execute("UPDATE kg_parent_textbook_ocr_artifact SET status='ready',import_action_id=NULL,import_payload_sha256=NULL,import_request_json=NULL,import_claimed_at=NULL,import_owner_boot_id=NULL,updated_at=? WHERE artifact_id=? AND status='importing'",(utc_now(),row['artifact_id']))
                    self._event(con,row['artifact_id'],'import_recovered',{'request':{'reason':'importing_interrupted'},'reason':'importing_interrupted'},f'system-import-recovery:{row["artifact_id"]}:{uuid4()}')
            con.commit()

    def _id_for_ref(self, capture_ref: str) -> str:
        with self.database.connect() as con: rows=con.execute("SELECT artifact_id FROM kg_parent_textbook_ocr_artifact").fetchall()
        for row in rows:
            if self._ref(row['artifact_id'])==capture_ref:return str(row['artifact_id'])
        raise CaptureNotFoundError('OCR artifact not found')
    @staticmethod
    def _ref(artifact_id: str) -> str: return 'textbook-ocr-'+hashlib.sha256(artifact_id.encode()).hexdigest()[:24]
    def _row(self, con, artifact_id: str): return con.execute("SELECT * FROM kg_parent_textbook_ocr_artifact WHERE artifact_id=?", (artifact_id,)).fetchone()
    @staticmethod
    def source_version(row) -> str:
        fields = ['artifact_id', 'child_id', 'status', 'updated_at', 'extracted_text', 'image_sha256', 'page_context_sha256']
        return hashlib.sha256(json.dumps([row[name] for name in fields], ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()

    def _public(self, row) -> dict:
        value=dict(row);details=json.loads(value.pop('details_json'));page_context=json.loads(value.pop('page_context_json'))
        raw_path=value.pop('raw_image_path',None);raw_media_type=value.pop('raw_image_media_type',None);raw_byte_length=int(value.pop('raw_image_byte_length',0) or 0);value.pop('raw_image_encryption_version',None)
        value.pop('artifact_id');value.pop('child_id');value.pop('lease_id');value.pop('file_source_id',None);value.pop('client_action_id');value.pop('image_sha256');value.pop('page_context_sha256');value.pop('import_action_id');value.pop('import_payload_sha256');value.pop('import_request_json');value.pop('import_claimed_at');value.pop('import_owner_boot_id',None);value['capture_ref']=self._ref(row['artifact_id']);value['page_context']=page_context
        quality=details.get('quality',{'passed':False,'issues':['processing']});manual=details.get('manual_review',{'required':True,'reasons':['processing'],'handwriting':'review_required','math':'review_required'});engine=details.get('engine','local-ocr-pending');privacy={**details.get('privacy',{}),'raw_image_uploaded':False,'raw_image_retained':bool(raw_path),'raw_image_encrypted':bool(raw_path),'cloud_transmission_allowed':False}
        value['source_version']=self.source_version(row)
        value['raw_image_retained']=bool(raw_path);value['raw_image_media_type']=raw_media_type;value['raw_image_byte_length']=raw_byte_length
        value['local_path']=None;value['details']={'ocr_confidence':value['confidence'],'quality':quality,'engine':engine,'local_pipeline':{'quality':quality,'manual_review':manual,'privacy':privacy,'engine':engine,'route':'cpu','question_blocks':[],'queue':{}}}
        return value
