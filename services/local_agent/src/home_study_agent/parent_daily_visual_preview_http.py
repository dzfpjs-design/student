"""Mount with the existing parent PIN and local application session guards."""
from ipaddress import ip_address
import json
from pathlib import Path
import sqlite3
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from .parent_auth import ParentSessionRequiredError
from .parent_daily_visual_preview import parent_daily_visual_preview


class DailyPreparationRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    recipient_id: str = Field(min_length=1, max_length=128)
    expected_source_id: Optional[str] = Field(default=None, min_length=1, max_length=128)


class DailySendRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    recipient_id: str = Field(min_length=1, max_length=128)
    source_id: str = Field(min_length=1, max_length=128)


def create_parent_daily_visual_preview_router(database, parent_auth, local_session_dependency, privacy_lifecycle=None):
    if not callable(local_session_dependency):
        raise ValueError('LOCAL_SESSION_DEPENDENCY_REQUIRED')

    def authorize(request: Request, response: Response):
        local_session_dependency(request)
        try:
            if not request.client or not ip_address(request.client.host).is_loopback:
                raise HTTPException(403, detail='LOCAL_ONLY')
        except ValueError:
            raise HTTPException(403, detail='LOCAL_ONLY') from None
        try:
            parent_auth.require(request.headers.get('X-HSC-Parent'))
        except ParentSessionRequiredError:
            raise HTTPException(401, detail='PARENT_SESSION_REQUIRED') from None
        if request.query_params:
            raise HTTPException(422, detail='QUERY_NOT_SUPPORTED')
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'

    router = APIRouter(prefix='/api/parent/children/{child_id}/daily-visual-preview', dependencies=[Depends(authorize)])

    def recheck(request):
        local_session_dependency(request)
        try:
            parent_auth.require(request.headers.get('X-HSC-Parent'))
        except ParentSessionRequiredError:
            raise HTTPException(401, detail='PARENT_SESSION_REQUIRED') from None

    def source_error(error):
        code = str(error)
        if code in {'CHILD_UNKNOWN'}:
            return HTTPException(404, detail=code)
        if code in {'INVALID_DAY_OR_CHILD', 'INVALID_DAY', 'INVALID_ID'}:
            return HTTPException(422, detail=code)
        return HTTPException(409, detail=code if code in {
            'FORMAL_DELIVERY_EXISTS', 'RECIPIENT_NOT_AUTHORIZED', 'RECIPIENT_ID_CHANGED',
            'OBSERVATION_STALE', 'OBSERVATION_CONFLICT', 'OBSERVATION_TOO_LARGE',
            'VISUAL_EVIDENCE_DELETION_PENDING', 'PAGE_OBSERVATION_DELETION_PENDING', 'DELETION_PENDING',
            'PRIOR_DELIVERY_UNRESOLVED_OR_SENT', 'RESTORE_OR_HISTORY_RECONCILIATION_REQUIRED',
            'REPORT_TASK_SOURCE_UNCONFIRMED', 'REPORT_TASK_SOURCE_MISSING'
        } else 'PREPARATION_UNAVAILABLE')

    @router.get('/{day}/delivery-preparation')
    def preparation_status(child_id: str, day: str, request: Request):
        from .daily_report_observation import DailyReportObservationService
        try:
            result = DailyReportObservationService(database).preparation_status(child_id, day)
            recheck(request)
            return result
        except ValueError as error:
            raise source_error(error) from None

    @router.post('/{day}/delivery-preparation')
    def prepare_source(child_id: str, day: str, payload: DailyPreparationRequest, request: Request):
        from .daily_report_observation import DailyReportObservationService
        try:
            return DailyReportObservationService(database).prepare(child_id, day, payload.recipient_id,
                payload.expected_source_id, admission_check=lambda: recheck(request))
        except ValueError as error:
            raise source_error(error) from None

    @router.post('/{day}/send')
    def send_visual_report(child_id: str, day: str, payload: DailySendRequest, request: Request):
        # A standalone visual email would suppress a same-day formal report via
        # the shared duplicate barrier. Do not send until both contents are
        # bound and revalidated together in one persisted report.
        recheck(request)
        raise HTTPException(409, detail={'code':'COMBINED_DAILY_REPORT_NOT_READY',
            'message':'综合日报尚未准备完成，暂不能发送；不会遗漏原有日报内容。'})

    @router.get('/deletion-receipt/{artifact_id}/{action_id}')
    def deletion_receipt(child_id: str, artifact_id: str, action_id: str):
        if any(not value or len(value) > 128 for value in (child_id, artifact_id, action_id)):
            raise HTTPException(422, detail='INVALID_REQUEST')
        with database.external_effect_guard(timeout=10), database.connect() as c:
            c.execute('BEGIN')
            rows = c.execute('''SELECT * FROM deletion_job WHERE client_action_id=?''', (action_id,)).fetchall()
            if len(rows) != 1:
                return {'verified': False}
            row = rows[0]
            if row['child_id'] != child_id or row['target_type'] != 'capture_artifact' or row['target_id'] != artifact_id or row['scope'] != 'capture_artifact':
                return {'verified': False}
            try:
                verification = json.loads(row['verification_json'] or '{}')
                residual = json.loads(row['residual_paths_json'] or '[]')
                rotation = verification.get('recovery_rotation')
                counts = verification.get('remaining_counts')
                replacement = rotation.get('replacement_backup_id') if isinstance(rotation, dict) else None
                manifest = c.execute('''SELECT * FROM backup_manifest WHERE backup_id=?''', (replacement,)).fetchone() if isinstance(replacement, str) and replacement else None
                references = c.execute('''SELECT (SELECT count(*) FROM capture_artifact WHERE artifact_id=?) +
                    (SELECT count(*) FROM homework_item WHERE source_artifact_id=?) +
                    (SELECT count(*) FROM plan_item WHERE completion_artifact_id=?) +
                    (SELECT count(*) FROM daily_observation_report_source WHERE child_id=?) +
                    (SELECT count(*) FROM daily_observation_report_snapshot WHERE child_id=?)''', (artifact_id,artifact_id,artifact_id,child_id,child_id)).fetchone()[0]
                complete = (row['status'] == 'completed' and row['completed_at'] is not None
                    and isinstance(verification, dict) and verification.get('database_delete_committed') is True
                    and type(verification.get('remaining_file_count')) is int and verification['remaining_file_count'] == 0
                    and isinstance(counts, dict) and set(counts) in (
                        {'capture_artifact', 'homework_reference', 'plan_reference'},
                        {'capture_artifact', 'homework_reference', 'plan_reference', 'daily_observation_report_source'})
                    and all(type(value) is int and value == 0 for value in counts.values()) and references == 0
                    and isinstance(residual, list) and not residual and isinstance(rotation, dict)
                    and rotation.get('status') == 'completed' and isinstance(replacement, str) and bool(replacement)
                    and all(type(rotation.get(key)) is int and rotation[key] >= 0 for key in ('invalidated_backup_count','removed_backup_bundle_files','removed_upgrade_snapshot_files'))
                    and manifest is not None and manifest['reason'] == 'post_deletion' and manifest['integrity_status'] == 'ok'
                    and c.execute('SELECT julianday(?) <= julianday(?) AND julianday(?) <= julianday(?)',
                                  (row['created_at'], manifest['created_at'], manifest['created_at'], row['completed_at'])).fetchone()[0] == 1)
            except (TypeError, ValueError, AttributeError):
                complete = False
            if complete:
                try:
                    if privacy_lifecycle is None: raise ValueError('BACKUP_CHECK_UNAVAILABLE')
                    path = Path(manifest['path'])
                    allowed = (Path(privacy_lifecycle.data_dir) / 'backups').resolve(strict=True)
                    if not path.is_absolute() or path.parent.resolve(strict=True) != allowed or path.is_symlink() or not path.is_file():
                        raise ValueError('BACKUP_LOCATION_INVALID')
                    digest, size = privacy_lifecycle._backup_checksum(path)
                    if digest != manifest['sha256'] or size != manifest['size_bytes']:
                        raise ValueError('BACKUP_CHECKSUM_MISMATCH')
                    with sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True) as backup:
                        if backup.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                            raise ValueError('BACKUP_INTEGRITY_FAILED')
                except Exception:
                    complete = False
            return {'verified': bool(complete), 'child_id': child_id, 'artifact_id': artifact_id, 'action_id': action_id}

    @router.get('/session')
    def session(request: Request):
        try:
            return {'expires_at': parent_auth.expires_at(request.headers.get('X-HSC-Parent')).isoformat()}
        except ParentSessionRequiredError:
            raise HTTPException(401, detail='PARENT_SESSION_REQUIRED') from None

    @router.get('/{day}')
    def preview(child_id: str, day: str):
        if len(child_id) > 128 or not child_id.strip():
            raise HTTPException(422, detail='CHILD_INVALID')
        try:
            return parent_daily_visual_preview(database, child_id, day)
        except ValueError as error:
            code = str(error)
            if code in {'VISUAL_EVIDENCE_DELETION_PENDING', 'PAGE_OBSERVATION_DELETION_PENDING'}:
                raise HTTPException(409, detail='DELETION_PENDING') from None
            if code in {'REPORT_TASK_SOURCE_UNCONFIRMED', 'REPORT_TASK_SOURCE_MISSING'}:
                raise HTTPException(409, detail=code) from None
            if code == 'PAGE_OBSERVATION_CHILD_UNKNOWN':
                raise HTTPException(404, detail='CHILD_UNKNOWN') from None
            raise HTTPException(422, detail='INVALID_REQUEST') from None

    return router
