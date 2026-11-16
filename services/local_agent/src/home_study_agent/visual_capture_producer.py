"""Bounded local handoff between separate OCR and page-revision requests.

The candidate page remains in memory only until a revision is accepted. At that
point the configured media store encrypts one source page; all other frames are
discarded. Image facts and cloud authorization are never carried here.
"""
import asyncio
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import logging
import sqlite3
from threading import RLock
import time

from . import visual_material as core
from .local_supply import digest
from .visual_diagram_detection import MonochromeDiagramDetector
from .restore_publication import RestorePublicationError

VERSION = 'visual_capture_candidates_v1'
TTL_SECONDS = 120
MAX_BATCHES = 4
MAX_BYTES = 72 * 1024 * 1024
logger = logging.getLogger(__name__)
_PUBLICATION_HOLDS = frozenset({
    'RESTORE_PUBLICATION_BUSY',
    'RESTORE_PUBLICATION_RECOVERY_REQUIRED',
    'RESTORE_PUBLICATION_CLOSE_UNCONFIRMED',
})


def _retryable_maintenance_error(error):
    if isinstance(error, RestorePublicationError):
        return len(error.args) == 1 and type(error.args[0]) is str and error.args[0] in _PUBLICATION_HOLDS
    if isinstance(error, OSError):
        return True
    code = getattr(error, 'sqlite_errorcode', None)
    if isinstance(code, int):
        return code & 255 in {5, 6, 10, 13, 14}  # BUSY, LOCKED, IOERR, FULL, CANTOPEN
    # Python 3.9 does not expose sqlite_errorcode. Do not retry invalid SQL/schema.
    return str(error).lower() in {
        'database is locked', 'database table is locked', 'database schema is locked',
        'disk i/o error', 'database or disk is full', 'unable to open database file',
    }


@dataclass(frozen=True)
class _Batch:
    child_id: str
    session_id: str
    artifact_id: str
    frame_sha256: str
    frame_bytes: bytes
    generation: int
    prepared: tuple
    reason: object
    deadline: float
    expires_at: str
    retention_policy_id: object


class VisualCaptureProducer:
    def __init__(self, database, host, monitor, *, page_source_store=None, detector=None, clock=time.monotonic,
                 mistake_capture=None):
        self.database, self.host, self.monitor = database, host, monitor
        self.page_source_store = page_source_store
        self.mistake_capture = mistake_capture
        self.detector = detector or MonochromeDiagramDetector()
        self.clock = clock
        from .visual_retention import VisualRetentionService
        self.retention = VisualRetentionService(database,host)
        self._pending = {}
        self._lock = RLock()

    def prepare(self, session_id, artifact_id, frame_bytes, generation):
        """Worker-only CPU work with one TTL-bound local source page."""
        with self.database.connect() as c:
            row = c.execute('SELECT child_id FROM capture_artifact WHERE artifact_id=? AND session_id=?',
                            (artifact_id, session_id)).fetchone()
            core.require(row is not None, 'SOURCE_CHANGED')
            names = tuple(r[0] for r in c.execute('SELECT nickname FROM child_profile'))
            policy_id = self.retention.capture_policy(c,row['child_id'],session_id)
        candidates = self.detector.prepare(frame_bytes, known_child_names=names)
        return _Batch(row['child_id'], session_id, artifact_id, sha256(frame_bytes).hexdigest(), frame_bytes, generation,
            candidates.prepared, candidates.reason, self.clock()+TTL_SECONDS,
            (datetime.now(timezone.utc)+timedelta(seconds=TTL_SECONDS)).isoformat(),policy_id)

    def _current(self, c, batch):
        monitor = self.monitor._active_monitor(c, batch.session_id)
        core.require(monitor['child_id'] == batch.child_id and monitor['generation'] == batch.generation,
                     'GENERATION_CHANGED')
        row = c.execute('SELECT * FROM capture_artifact WHERE artifact_id=? AND child_id=? AND session_id=?',
                        (batch.artifact_id, batch.child_id, batch.session_id)).fetchone()
        core.require(row and row['artifact_type'] == 'document_ocr'
                     and row['status'] in {'completed', 'quality_warning'}, 'SOURCE_CHANGED')
        details = core._json(row['details_json'])
        core.require(details.get('monitor_frame_sha256') == batch.frame_sha256
                     and details.get('monitor_generation') == batch.generation, 'FRAME_CHANGED')
        return row, details

    def publish(self, batch):
        core.require(isinstance(batch, _Batch), 'PREPARED_REQUIRED')
        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row, details = self._current(c, batch)
            with self._lock:
                self._expire()
                reason = batch.reason
                if batch.deadline <= self.clock():
                    reason = 'VISUAL_PREPARATION_EXPIRED'
                size = len(batch.frame_bytes) + sum(len(p.png) for p in batch.prepared)
                occupied = sum(len(b.frame_bytes) + sum(len(p.png) for p in b.prepared)
                               for b in self._pending.values())
                if len(self._pending) >= MAX_BATCHES or size+occupied > MAX_BYTES:
                    reason = 'VISUAL_PREPARATION_CAPACITY'
                ready = bool(batch.prepared) and reason is None
                projection = dict(version=VERSION, status='prepared' if ready else 'unsupported',
                    reason=reason, candidate_count=len(batch.prepared) if ready else 0,
                    expires_at=batch.expires_at if ready else None,
                    retention_policy_id=batch.retention_policy_id,
                    local_only=True, cloud_transmission_allowed=False)
                details['visual_capture'] = projection
                core._save(c, row, details)
                c.commit()
                # The whole page and visual crops have different eligibility:
                # an ordinary text page can have no diagram crop and still be
                # the accepted source for this revision.
                if batch.deadline > self.clock() and size+occupied <= MAX_BYTES and len(self._pending) < MAX_BATCHES:
                    self._pending[batch.artifact_id] = batch
        return details

    def record_revision(self, session_id, artifact_id, content_signature, client_action_id, *, expected_generation):
        staged = []
        staged_sources = []
        staged_commit = ExitStack()

        def bind(c, revision):
            with self._lock:
                self._expire()
                batch = self._pending.get(artifact_id)
            row = c.execute('SELECT * FROM capture_artifact WHERE artifact_id=? AND session_id=?',
                            (artifact_id, session_id)).fetchone()
            core.require(row is not None, 'SOURCE_CHANGED')
            details = core._json(row['details_json'])
            scope = dict(child_id=row['child_id'], session_id=session_id, artifact_id=artifact_id,
                         revision_id=revision['revision_id'])
            regions = details.get('visual_materials') or {}
            if batch:
                self._current(c, batch)
                core.require(batch.session_id == session_id and batch.child_id == row['child_id'], 'SOURCE_CHANGED')
                if self.mistake_capture is not None:
                    self.mistake_capture.on_revision(c, revision, row, batch.frame_bytes)
                    revision.update({key: value for key, value in self.monitor._revision(c, revision['revision_id']).items()
                                     if key in {'auto_saved_mistakes', 'queued_mistake_candidate_count'}})
                core.require(self.page_source_store is not None, 'PAGE_SOURCE_STORE_UNAVAILABLE')
                page_source, staged_path = self.page_source_store.retain_document_page_source(
                    c, artifact_id, batch.frame_bytes, batch.frame_sha256)
                if staged_path:
                    staged_sources.append(staged_path)
                revision['page_source'] = page_source
                details = core._json(c.execute(
                    'SELECT details_json FROM capture_artifact WHERE artifact_id=?', (artifact_id,)
                ).fetchone()[0])
                if batch.prepared:
                    # DB write is already held. Keep the original stage lock
                    # through this transaction's commit/rollback, so maintenance
                    # never judges new RAM entries against a precommit snapshot.
                    staged_commit.enter_context(self.host._stage_lock)
                for index, prepared in enumerate(batch.prepared):
                    region_id = 'capture_' + str(index+1)
                    result = core.record_region(c, **scope, region_id=region_id, prepared=prepared,
                        expected_details_version=digest(details))
                    identity = (row['child_id'], artifact_id, region_id, revision['revision_id'], result['region_version'])
                    with self.host._stage_lock:
                        if identity not in self.host._staged:
                            staged.append(identity)
                    self.host.stage_prepared(row['child_id'], artifact_id, region_id,
                        revision_id=revision['revision_id'], region_version=result['region_version'],
                        prepared=prepared, connection=c, live_generation=batch.generation)
                    details = core._json(c.execute('SELECT details_json FROM capture_artifact WHERE artifact_id=?',
                                                   (artifact_id,)).fetchone()[0])
                regions = details.get('visual_materials') or {}
            else:
                page_source = details.get('page_source')
                if isinstance(page_source, dict):
                    revision['page_source'] = page_source
                elif self.page_source_store is not None and details.get('monitor_frame_sha256'):
                    raise ValueError('PAGE_SOURCE_PREPARATION_UNAVAILABLE')
            items = []
            for region_id, region in regions.items():
                # Do not attach an old page's regions to a newly requested revision.
                _, _, current = core._region(c, scope, region_id, region['region_version'], live=True)
                binding = {**scope, **core._binding(scope, region_id, current)}
                detail = self.host._detail(c, row['child_id'], artifact_id, region_id,
                                          revision['revision_id'], region['region_version'])
                items.append(dict(binding=binding, image_descriptor=core._image_descriptor(binding, current),
                    facts_version=current.get('facts_version'), fact_status=detail['fact_status'],
                    storage_status=detail['storage_status'], image_availability=detail['image_availability'],
                    source_current=detail['source_current'], local_only=True, cloud_transmission_allowed=False))
            meta = details.get('visual_capture') or {}
            revision['visual_capture'] = dict(version=VERSION,
                status='awaiting_verification' if items else 'unsupported', items=items,
                reason=None if items else ('VISUAL_PREPARATION_UNAVAILABLE' if meta.get('status') == 'prepared'
                                          else meta.get('reason', 'VISUAL_NO_PREPARATION')),
                local_only=True, cloud_transmission_allowed=False)
        try:
            with staged_commit:
                result = self.monitor.record_revision(session_id, artifact_id, content_signature, client_action_id,
                    expected_generation=expected_generation, _on_recorded=bind)
        except BaseException:
            for identity in staged:
                self.host.discard_prepared(identity)
            for path in staged_sources:
                self.page_source_store.discard_unreferenced_capture_source(path)
            raise
        with self._lock:
            self._pending.pop(artifact_id, None)
        return self.retention.store_revision(result)

    def _expire(self):
        for key, batch in list(self._pending.items()):
            if batch.deadline <= self.clock():
                self._pending.pop(key, None)

    def clear(self, *, child_id=None, session_id=None):
        with self._lock:
            for key, batch in list(self._pending.items()):
                if ((child_id is None or batch.child_id == child_id)
                        and (session_id is None or batch.session_id == session_id)):
                    self._pending.pop(key, None)
        self.host.clear_prepared(child_id=child_id, session_id=session_id)

    def sweep(self):
        with self._lock:
            self._expire()
            batches = list(self._pending.values())
        for batch in batches:
            with self.database.connect() as c:
                row = c.execute('''SELECT 1 FROM capture_artifact a JOIN document_monitor_session m
                    ON m.session_id=a.session_id AND m.child_id=a.child_id JOIN daily_session s
                    ON s.session_id=a.session_id AND s.child_id=a.child_id
                    WHERE a.artifact_id=? AND a.child_id=? AND a.status IN ('completed','quality_warning')
                    AND m.status='active' AND m.generation=? AND s.status='active' AND s.state='study' ''',
                    (batch.artifact_id, batch.child_id, batch.generation)).fetchone()
            if not row:
                with self._lock:
                    self._pending.pop(batch.artifact_id, None)
        self.host.sweep_prepared()

    async def maintain(self):
        jobs = [('cleanup', self.retention.cleanup, 30), ('sweep', self.sweep, 1)]
        due = {name: self.clock() for name, _, _ in jobs}
        failures = {name: 0 for name, _, _ in jobs}
        try:
            while True:
                for name, work, interval in jobs:
                    if self.clock() < due[name]:
                        continue
                    try:
                        await asyncio.to_thread(work)
                    except (OSError, sqlite3.OperationalError, RestorePublicationError) as error:
                        if not _retryable_maintenance_error(error):
                            raise
                        failures[name] += 1
                        delay = min(30, 2 ** min(failures[name] - 1, 5))
                        due[name] = self.clock() + delay
                        reason = error.args[0] if isinstance(error, RestorePublicationError) else 'IO_OR_SQL_BUSY'
                        logger.warning('visual_capture_maintenance_retry operation=%s error_type=%s reason=%s attempt=%s retry_seconds=%s',
                            name, type(error).__name__, reason, failures[name], delay, extra={
                            'operation': name, 'error_type': type(error).__name__,
                            'reason_code': reason,
                            'consecutive_failures': failures[name], 'retry_seconds': delay,
                        })
                    else:
                        if failures[name]:
                            logger.info('visual_capture_maintenance_recovered operation=%s failures=%s',
                                name, failures[name], extra={
                                'operation': name, 'consecutive_failures': failures[name],
                            })
                        failures[name] = 0
                        due[name] = self.clock() + interval
                await asyncio.sleep(max(0.01, min(due.values()) - self.clock()))
        finally:
            self.clear()
