from __future__ import annotations

from contextlib import closing
import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
import stat
from threading import Event, Thread
from uuid import uuid4

from ..database import Database
from ..backup_local_io import io_path, io_glob
from ..repository import utc_now
from ..retrieval.hybrid import HashingVectorizer, index_segment
from ..knowledge_graph.schema import ensure_schema


def _corpus_io(path):
    # Plain-corpus CLI paths may be relative. Keep their original spelling and
    # authority; only an already absolute path receives the native IO adapter.
    return io_path(path) if path.is_absolute() else path


def _corpus_glob(root,pattern):
    return io_glob(root,'**/'+pattern) if root.is_absolute() else root.rglob(pattern)


@dataclass(frozen=True)
class CorpusSpec:
    corpus_id: str
    title: str
    source_kind: str  # seyk or bilibili
    transcript_root: Path
    video_root: Path | None
    source_ref: str
    license_boundary: str
    metadata_db_path: Path | None = None
    version_label: str = "local-2026-08"
    pdf_root: Path | None = None
    image_root: Path | None = None


class IngestRunAlreadyOwned(RuntimeError):
    """A second process tried to resume an active checkpoint lease."""


class LocalCorpusIngestor:
    """Checkpointed, streaming importer.  It never transmits local material."""
    def __init__(self, database: Database, vectorizer: HashingVectorizer | None = None):
        self.database = database
        self.vectorizer = vectorizer or HashingVectorizer()
        self.run_lease_seconds = 120
        self.heartbeat_interval_seconds = 20

    def register(self, spec: CorpusSpec) -> None:
        if not _corpus_io(spec.transcript_root).is_dir():
            raise ValueError(f"missing transcript root: {spec.transcript_root}")
        now=utc_now()
        with self.database.connect() as con:
            ensure_schema(con,now)
            con.commit();con.execute("BEGIN IMMEDIATE")
            prior=con.execute("SELECT * FROM kg_corpus WHERE corpus_id=?",(spec.corpus_id,)).fetchone()
            pdf_root,image_root=self._registered_document_roots(spec,con,prior)
            con.execute("""INSERT INTO kg_corpus(corpus_id,title,source_kind,transcript_root,video_root,metadata_db_path,source_ref,license_boundary,review_status,version_label,created_at,updated_at,pdf_root,image_root)
            VALUES(?,?,?,?,?,?,?,?,'restricted',?,?,?,?,?) ON CONFLICT(corpus_id) DO UPDATE SET title=excluded.title,transcript_root=excluded.transcript_root,video_root=excluded.video_root,metadata_db_path=excluded.metadata_db_path,source_ref=excluded.source_ref,license_boundary=excluded.license_boundary,version_label=excluded.version_label,updated_at=excluded.updated_at,pdf_root=excluded.pdf_root,image_root=excluded.image_root""",
            (spec.corpus_id,spec.title,spec.source_kind,str(spec.transcript_root),str(spec.video_root) if spec.video_root else None,str(spec.metadata_db_path) if spec.metadata_db_path else None,spec.source_ref,spec.license_boundary,spec.version_label,now,now,pdf_root,image_root)); con.commit()

    @staticmethod
    def _registered_path(value: Path, *, directory: bool) -> Path:
        """Validate an explicitly selected root/file; never resolve an alias into authority."""
        if (not isinstance(value,Path) or not value.is_absolute() or '\x00' in str(value)
                or str(value).startswith(('//','\\\\')) or (directory and value.parent==value)):
            raise ValueError('REGISTERED_ASSET_ROOT_REQUIRED')
        for part in value.parts[1:]:
            if part in ('.','..') or ':' in part or '\\' in part or part.endswith((' ','.')) or PureWindowsPath(part).is_reserved():
                raise ValueError('REGISTERED_ASSET_ROOT_REQUIRED')
        try:
            for path in (value,*value.parents):
                info=(_corpus_io(path) if path.parent!=path else path).lstat()
                if stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0)&0x400:
                    raise ValueError('REGISTERED_ASSET_ROOT_REQUIRED')
            info=_corpus_io(value).stat()
            if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode) and info.st_nlink==1):
                raise ValueError('REGISTERED_ASSET_ROOT_REQUIRED')
        except OSError:
            raise ValueError('REGISTERED_ASSET_ROOT_REQUIRED') from None
        return value

    @classmethod
    def _registered_file(cls,root:Path,relative:str)->Path:
        if (type(relative) is not str or not relative or '\\' in relative or ':' in relative
                or PurePosixPath(relative).is_absolute() or any(p in ('','.','..') for p in relative.split('/'))):
            raise ValueError('REGISTERED_ASSET_ROOT_REQUIRED')
        return cls._registered_path(root/relative,directory=False)

    def _registered_document_roots(self,spec:CorpusSpec,con,prior)->tuple[str|None,str|None]:
        # Caller selection and these original registry columns are the authority.
        # Registering roots never runs/restarts an import or edits learning evidence.
        bound=prior is not None and bool(prior['pdf_root'] or prior['image_root'])
        selected=spec.pdf_root is not None or spec.image_root is not None
        if prior is not None and (bound or selected):
            identity={'source_kind':spec.source_kind,'transcript_root':str(spec.transcript_root),
                      'video_root':str(spec.video_root) if spec.video_root else None,
                      'metadata_db_path':str(spec.metadata_db_path) if spec.metadata_db_path else None,
                      'source_ref':spec.source_ref,'license_boundary':spec.license_boundary,'version_label':spec.version_label}
            if any(prior[k]!=v for k,v in identity.items()):raise ValueError('REGISTERED_ASSET_SOURCE_CONFLICT')
        roots=[]
        for name in ('pdf_root','image_root'):
            value=getattr(spec,name);old=prior[name] if prior is not None else None
            if value is not None and old is not None and str(value)!=old:raise ValueError('REGISTERED_ASSET_ROOT_CONFLICT')
            root=self._registered_path(value if value is not None else Path(old),directory=True) if value is not None or old is not None else None
            roots.append(root)
        pdf_root,image_root=roots
        if any(roots) and spec.source_kind!='local_scanned_document':raise ValueError('REGISTERED_DOCUMENT_ROOTS_REQUIRED')
        if spec.source_kind=='local_scanned_document' and (spec.metadata_db_path is not None or pdf_root is not None):
            if spec.metadata_db_path is None:raise ValueError('REGISTERED_PDF_ASSOCIATION_REQUIRED')
            metadata=self._registered_path(spec.metadata_db_path,directory=False)
            from ..registered_asset_snapshot import _open_readonly
            with _open_readonly(metadata) as source:
                source.execute('PRAGMA query_only=ON');source.execute('PRAGMA trusted_schema=OFF')
                columns={r[1] for r in source.execute('PRAGMA table_info(textbook_book)')}
                if not {'relative_pdf_path','fingerprint'}<=columns:
                    if pdf_root is not None:raise ValueError('REGISTERED_PDF_ASSOCIATION_REQUIRED')
                else:
                    count=0
                    for relative,fingerprint in source.execute('SELECT relative_pdf_path,fingerprint FROM textbook_book ORDER BY book_id'):
                        count+=1
                        if pdf_root is None:raise ValueError('MISSING_REGISTERED_PDF_ROOT')
                        original=self._registered_file(pdf_root,relative)
                        if not fingerprint or self._fingerprint(original)!=fingerprint:raise ValueError('REGISTERED_PDF_SOURCE_MISMATCH')
                    if pdf_root is not None and not count:raise ValueError('REGISTERED_PDF_ASSOCIATION_REQUIRED')
        if image_root is not None and (prior is None or prior['image_root'] is None):
            pages=con.execute('''SELECT p.image_relative_path,p.image_sha256,p.image_fingerprint
                FROM kg_document_page p JOIN kg_resource r ON r.resource_id=p.resource_id
                WHERE r.corpus_id=? AND p.image_relative_path IS NOT NULL''',(spec.corpus_id,))
            for relative,digest,fingerprint in pages:
                original=self._registered_file(image_root,relative)
                if not digest and not fingerprint:raise ValueError('REGISTERED_IMAGE_EVIDENCE_REQUIRED')
                if ((digest and self._sha256(original)!=digest) or (fingerprint and self._fingerprint(original)!=fingerprint)):
                    raise ValueError('REGISTERED_IMAGE_SOURCE_MISMATCH')
        return tuple(str(root) if root is not None else None for root in roots)

    def start(self, spec: CorpusSpec, *, idempotency_key: str, hash_videos: bool = False) -> str:
        self.register(spec); now=utc_now()
        paths=sorted(_corpus_glob(spec.transcript_root,"*.txt"), key=lambda item:item.name)
        video_ids=self._video_files(spec)
        with self.database.connect() as con:
            prior=con.execute("SELECT job_id FROM kg_ingest_job WHERE idempotency_key=?",(idempotency_key,)).fetchone()
            if prior: return prior["job_id"]
            job_id=str(uuid4())
            con.execute("INSERT INTO kg_ingest_job(job_id,corpus_id,idempotency_key,status,options_json,total_count,started_at,updated_at) VALUES(?,?,?,'pending',?,?,?,?)",
                        (job_id,spec.corpus_id,idempotency_key,json.dumps({"hash_videos":hash_videos}),len({path.stem for path in paths} | set(video_ids)),now,now));con.commit()
        return job_id

    def refresh_metadata(self,spec:CorpusSpec)->dict:
        """Repair curriculum scope without re-reading transcripts or videos."""
        self.register(spec);metadata_by_id=self._metadata(spec.metadata_db_path,spec.source_kind)
        valid_external_ids={path.stem for path in _corpus_glob(spec.transcript_root,"*.txt")} | set(self._video_files(spec))
        updated=missing=stale_removed=0;now=utc_now()
        with self.database.connect() as con:
            rows=con.execute("SELECT resource_id,external_id,metadata_json FROM kg_resource WHERE corpus_id=? ORDER BY external_id",(spec.corpus_id,)).fetchall()
            con.execute("BEGIN IMMEDIATE")
            for row in rows:
                if row["external_id"] not in valid_external_ids:
                    evidence=con.execute("SELECT 1 FROM kg_segment_occurrence WHERE resource_id=? LIMIT 1",(row["resource_id"],)).fetchone()
                    owns_segment=con.execute("SELECT 1 FROM kg_segment WHERE resource_id=? LIMIT 1",(row["resource_id"],)).fetchone()
                    if not evidence and not owns_segment:
                        con.execute("DELETE FROM kg_resource WHERE resource_id=?",(row["resource_id"],));stale_removed+=1
                    continue
                incoming=metadata_by_id.get(row["external_id"])
                if not incoming:
                    missing+=1;continue
                existing=json.loads(row["metadata_json"] or "{}")
                merged={**existing,**{key:value for key,value in incoming.items() if value not in (None,"")}}
                con.execute("""UPDATE kg_resource SET title=COALESCE(NULLIF(?,''),title),grade_scope=?,subject_scope=?,
                    semester_scope=?,metadata_json=?,updated_at=? WHERE resource_id=?""",
                    (incoming.get("title"),incoming.get("grade") or None,incoming.get("subject") or None,
                     incoming.get("semester") or None,json.dumps(merged,ensure_ascii=False),now,row["resource_id"]))
                # Canonical segments retain one owning resource.  Refresh that
                # resource's FTS scope; occurrences from duplicate resources stay
                # available through their own provenance rows.
                con.execute("""UPDATE kg_segment_fts SET title=COALESCE(NULLIF(?,''),title),subject_scope=?,grade_scope=?
                    WHERE segment_id IN (SELECT segment_id FROM kg_segment WHERE resource_id=?)""",
                    (incoming.get("title"),incoming.get("subject") or "",incoming.get("grade") or "",row["resource_id"]))
                updated+=1
            con.commit()
        return {"corpus_id":spec.corpus_id,"resources":len(rows)-stale_removed,"updated":updated,"metadata_missing":missing,
                "stale_video_fragments_removed":stale_removed,
                "source_kind":spec.source_kind,"metadata_db_path":str(spec.metadata_db_path) if spec.metadata_db_path else None}

    def run(self, spec: CorpusSpec, job_id: str, *, max_resources: int | None = None,
            throttle_ms: int = 0) -> dict:
        """May be called repeatedly after interruption; cursor and failed records remain durable."""
        self.register(spec)
        paths=sorted(_corpus_glob(spec.transcript_root,"*.txt"),key=lambda item:item.name)
        transcripts={path.stem:path for path in paths}
        videos=self._video_files(spec)
        by_id={external_id: transcripts.get(external_id) for external_id in set(transcripts) | set(videos)}
        titles=self._metadata(spec.metadata_db_path, spec.source_kind)
        with self.database.connect() as con:
            job=con.execute("SELECT * FROM kg_ingest_job WHERE job_id=? AND corpus_id=?",(job_id,spec.corpus_id)).fetchone()
            if not job: raise ValueError("unknown ingest job")
            if job["status"]=="completed": return self.job(job_id)
            run_token = self._claim_run(con, job_id)
            if run_token is None:
                return self.job(job_id)
            job=con.execute("SELECT * FROM kg_ingest_job WHERE job_id=?",(job_id,)).fetchone()
            options=json.loads(job["options_json"]); cursor=job["checkpoint_cursor"] or ""
            retry_ids=[row[0] for row in con.execute("SELECT external_id FROM kg_ingest_failure WHERE job_id=? AND status='open' ORDER BY external_id",(job_id,))]
            con.commit()
        count=0
        # Failed items are replayed before the cursor advances; a repaired file is
        # therefore never stranded behind an otherwise completed checkpoint.
        work_ids=[item for item in retry_ids if item in by_id]
        work_ids.extend(item for item in sorted(by_id) if item > cursor and item not in set(work_ids))
        stop_heartbeat,heartbeat=self._start_heartbeat(job_id,run_token)
        try:
            for external_id in work_ids:
                if max_resources is not None and count >= max_resources: break
                path=by_id[external_id]
                video=videos.get(external_id)
                try:
                    self._upsert_file(spec,job_id,run_token,external_id,path,video,titles.get(external_id,{}),bool(options.get("hash_videos")))
                    self._resolve_failure(job_id, external_id)
                except IngestRunAlreadyOwned:
                    raise
                except (OSError,UnicodeError,sqlite3.Error,ValueError) as error:
                    self._failure(job_id,external_id,path or video or spec.transcript_root,error)
                with self.database.connect() as con:
                    con.execute("BEGIN IMMEDIATE")
                    updated=con.execute("""UPDATE kg_ingest_job SET checkpoint_cursor=CASE WHEN checkpoint_cursor > ? THEN checkpoint_cursor ELSE ? END,
                       processed_count=(SELECT COUNT(*) FROM kg_resource WHERE corpus_id=kg_ingest_job.corpus_id),
                       failed_count=(SELECT COUNT(*) FROM kg_ingest_failure WHERE job_id=kg_ingest_job.job_id AND status='open'),
                       updated_at=?,run_lease_expires_at=? WHERE job_id=? AND run_token=?""",
                                (external_id,external_id,utc_now(),self._lease_until(),job_id,run_token))
                    if updated.rowcount != 1:
                        con.rollback();raise IngestRunAlreadyOwned("ingest checkpoint lease was taken by a recovery worker")
                    con.commit()
                count += 1
                if throttle_ms > 0:
                    import time
                    time.sleep(min(5_000, throttle_ms) / 1_000)
        finally:
            stop_heartbeat.set()
            heartbeat.join(timeout=1)
        with self.database.connect() as con:
            job=con.execute("SELECT checkpoint_cursor,total_count,processed_count,failed_count FROM kg_ingest_job WHERE job_id=?",(job_id,)).fetchone()
            open_failures=con.execute("SELECT COUNT(*) FROM kg_ingest_failure WHERE job_id=? AND status='open'",(job_id,)).fetchone()[0]
            is_complete = not max_resources or (job["checkpoint_cursor"] or "") >= (max(by_id) if by_id else "")
            if is_complete:
                con.execute("UPDATE kg_ingest_job SET status=?,completed_at=?,updated_at=?,run_token=NULL,run_lease_expires_at=NULL WHERE job_id=? AND run_token=?",("completed" if not open_failures else "failed",utc_now(),utc_now(),job_id,run_token));con.commit()
            else:
                # The command intentionally yielded after a bounded batch.  Its
                # checkpoint is durable, but it no longer owns the next batch.
                con.execute("UPDATE kg_ingest_job SET run_token=NULL,run_lease_expires_at=NULL,updated_at=? WHERE job_id=? AND run_token=?",(utc_now(),job_id,run_token));con.commit()
        return self.job(job_id)

    def _lease_until(self) -> str:
        return (datetime.now(timezone.utc) + timedelta(seconds=self.run_lease_seconds)).isoformat()

    def _start_heartbeat(self, job_id: str, run_token: str) -> tuple[Event, Thread]:
        stop=Event()
        def renew() -> None:
            while not stop.wait(self.heartbeat_interval_seconds):
                try:
                    with self.database.connect() as con:
                        con.execute("BEGIN IMMEDIATE")
                        con.execute("UPDATE kg_ingest_job SET run_lease_expires_at=?,updated_at=? WHERE job_id=? AND run_token=?",(self._lease_until(),utc_now(),job_id,run_token));con.commit()
                except sqlite3.OperationalError as error:
                    # A resource transaction can legitimately hold the only
                    # SQLite writer slot. The same transaction renews the lease
                    # before commit, so a transient lock must not kill the
                    # process heartbeat for the rest of a long import.
                    if "locked" not in str(error).lower():
                        raise
        worker=Thread(target=renew,daemon=True,name=f'kg-ingest-heartbeat-{job_id[:8]}')
        worker.start()
        return stop,worker

    def _claim_run(self, connection, job_id: str) -> str | None:
        """Persistently own one resumable run; an active owner is never duplicated."""
        connection.execute("BEGIN IMMEDIATE")
        token = str(uuid4())
        result = connection.execute("""UPDATE kg_ingest_job
            SET status='processing',run_token=?,run_lease_expires_at=?,updated_at=?
            WHERE job_id=? AND status!='completed' AND (
                status!='processing' OR run_token IS NULL OR run_lease_expires_at IS NULL
                OR julianday(run_lease_expires_at)<=julianday('now')
            )""", (token,self._lease_until(),utc_now(),job_id))
        if result.rowcount != 1:
            connection.commit()
            return None
        connection.commit()
        return token

    def job(self, job_id: str) -> dict:
        with self.database.connect() as con:
            row=con.execute("SELECT * FROM kg_ingest_job WHERE job_id=?",(job_id,)).fetchone()
            failures=con.execute("SELECT COUNT(*) FROM kg_ingest_failure WHERE job_id=? AND status='open'",(job_id,)).fetchone()[0]
        result=dict(row);result["open_failures"]=failures;return result

    def _upsert_file(self,spec: CorpusSpec,job_id:str,run_token:str,external_id:str,path:Path|None,video:Path|None,metadata:dict,hash_videos:bool) -> None:
        digest,segments=self._stream_hash_and_segments(path) if path else (hashlib.sha256(b"").hexdigest(), [])
        now=utc_now()
        video_hash=self._sha256(video) if video and _corpus_io(video).is_file() and hash_videos else None
        fingerprint=self._fingerprint(video) if video and _corpus_io(video).is_file() else None
        title=(metadata.get("title") or f"{spec.title} · {external_id}").strip()
        source_ref=(metadata.get("source_ref") or spec.source_ref).strip()
        scope={key:metadata.get(key) for key in ("grade","subject","semester")}
        resource_id=f"kg-res-{hashlib.sha256((spec.corpus_id+':'+external_id).encode()).hexdigest()[:24]}"
        with self.database.connect() as con:
            ensure_schema(con,now)
            # Schema setup inserts its migration marker, so finish that implicit
            # transaction before taking the explicit atomic resource transaction.
            con.commit(); con.execute("BEGIN IMMEDIATE")
            # Once BEGIN IMMEDIATE succeeds no recovery worker can change the
            # owner until this transaction commits.  The token, not a scheduler-
            # sensitive wall-clock comparison, is therefore the authoritative
            # ownership check.  Renew immediately so a delayed worker does not
            # reject itself when no takeover actually occurred.
            owner=con.execute("SELECT 1 FROM kg_ingest_job WHERE job_id=? AND run_token=?",(job_id,run_token)).fetchone()
            if not owner:
                con.rollback();raise IngestRunAlreadyOwned("ingest resource write lost its checkpoint lease")
            con.execute(
                "UPDATE kg_ingest_job SET run_lease_expires_at=?,updated_at=? WHERE job_id=? AND run_token=?",
                (self._lease_until(),utc_now(),job_id,run_token),
            )
            con.execute("""INSERT INTO kg_resource(resource_id,corpus_id,external_id,title,grade_scope,subject_scope,semester_scope,transcript_relative_path,video_relative_path,transcript_sha256,video_sha256,video_fingerprint,metadata_json,source_ref,license_boundary,confidence,review_status,version_label,ingested_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,.55,'restricted',?,?,?) ON CONFLICT(corpus_id,external_id) DO UPDATE SET title=excluded.title,grade_scope=excluded.grade_scope,subject_scope=excluded.subject_scope,semester_scope=excluded.semester_scope,transcript_relative_path=excluded.transcript_relative_path,transcript_sha256=excluded.transcript_sha256,video_sha256=COALESCE(excluded.video_sha256,kg_resource.video_sha256),video_fingerprint=excluded.video_fingerprint,metadata_json=excluded.metadata_json,source_ref=excluded.source_ref,updated_at=excluded.updated_at""",
            (resource_id,spec.corpus_id,external_id,title,scope["grade"],scope["subject"],scope["semester"],path.relative_to(spec.transcript_root).as_posix() if path else "",video.relative_to(spec.video_root).as_posix() if video else None,digest,video_hash,fingerprint,json.dumps(metadata,ensure_ascii=False),source_ref,spec.license_boundary,spec.version_label,now,now))
            # Remove this resource's occurrences first.  Canonical text survives if a
            # second resource points to it, which makes reruns safe for duplicates.
            con.execute("DELETE FROM kg_segment_occurrence WHERE resource_id=?", (resource_id,))
            con.execute("DELETE FROM kg_segment_fts WHERE segment_id IN (SELECT segment_id FROM kg_segment s WHERE s.resource_id=? AND NOT EXISTS (SELECT 1 FROM kg_segment_occurrence o WHERE o.segment_id=s.segment_id))",(resource_id,))
            con.execute("DELETE FROM kg_segment WHERE resource_id=? AND NOT EXISTS (SELECT 1 FROM kg_segment_occurrence o WHERE o.segment_id=kg_segment.segment_id)",(resource_id,))
            for ordinal,(start,end,text) in enumerate(segments):
                text_hash=hashlib.sha256(text.encode()).hexdigest()
                # Deduplicate exact repeated snippets *within and across resources* by
                # retaining provenance mappings while avoiding an embedding/index copy.
                prior=con.execute("SELECT segment_id FROM kg_segment WHERE text_sha256=? LIMIT 1",(text_hash,)).fetchone()
                segment_id=prior["segment_id"] if prior else str(uuid4())
                if not prior:
                    con.execute("INSERT INTO kg_segment(segment_id,resource_id,ordinal,start_char,end_char,text,text_sha256,source_ref,confidence,review_status,created_at) VALUES(?,?,?,?,?,?,?,?,.55,'restricted',?)",(segment_id,resource_id,ordinal,start,end,text,text_hash,source_ref,now))
                    con.execute("INSERT INTO kg_segment_fts(segment_id,text,title,subject_scope,grade_scope) VALUES(?,?,?,?,?)",(segment_id,text,title,scope["subject"] or "",scope["grade"] or ""))
                    index_segment(con,segment_id,text,self.vectorizer)
                # Keep every source occurrence even when the actual text/index is deduplicated.
                con.execute("INSERT OR REPLACE INTO kg_segment_occurrence(resource_id,segment_id,ordinal,start_char,end_char) VALUES(?,?,?,?,?)",(resource_id,segment_id,ordinal,start,end))
            self._before_resource_commit()
            renewed=con.execute("UPDATE kg_ingest_job SET run_lease_expires_at=?,updated_at=? WHERE job_id=? AND run_token=?",(self._lease_until(),utc_now(),job_id,run_token))
            if renewed.rowcount != 1:
                con.rollback();raise IngestRunAlreadyOwned("ingest resource write lost its checkpoint lease")
            con.commit()

    def _before_resource_commit(self) -> None:
        """Test seam; production intentionally performs no work here."""

    @staticmethod
    def _stream_hash_and_segments(path:Path, chunk_size:int=64*1024, target_chars:int=900) -> tuple[str,list[tuple[int,int,str]]]:
        digest=hashlib.sha256(); decoder=None; buffer=""; start=0; segments=[]
        import codecs; decoder=codecs.getincrementaldecoder("utf-8")(errors="replace")
        with _corpus_io(path).open("rb") as handle:
            while data:=handle.read(chunk_size):
                digest.update(data); buffer+=decoder.decode(data)
                while len(buffer)>=target_chars:
                    split=max(buffer.rfind("。",0,target_chars),buffer.rfind("！",0,target_chars),buffer.rfind("？",0,target_chars),buffer.rfind("\n",0,target_chars),target_chars-1)+1
                    text=buffer[:split].strip(); raw_len=split; buffer=buffer[split:]
                    if text: segments.append((start,start+raw_len,text));start+=raw_len
        buffer+=decoder.decode(b"",final=True)
        text=buffer.strip()
        if text: segments.append((start,start+len(buffer),text))
        return digest.hexdigest(),segments

    @staticmethod
    def _metadata(path:Path|None, source_kind:str)->dict[str,dict]:
        if not path or not _corpus_io(path).is_file(): return {}
        from ..registered_asset_snapshot import _open_readonly
        from urllib.parse import quote
        with (_open_readonly(path)if path.is_absolute()else closing(sqlite3.connect("file:"+quote(str(path),safe="/")+"?mode=ro",uri=True)))as con:
            con.row_factory=sqlite3.Row
            con.execute('PRAGMA query_only=ON');con.execute('PRAGMA trusted_schema=OFF')
            if source_kind == "seyk":
                rows=con.execute("""SELECT r.resource_id,r.title,r.description,r.teacher,
                    tree.stage_title,tree.grade_title,tree.subject_title,tree.version_title,
                    tree.volumn_title,tree.unit_path,tree.chapter_title
                    FROM resources r LEFT JOIN (
                      SELECT resource_id,MIN(stage_title) stage_title,MIN(grade_title) grade_title,
                        MIN(subject_title) subject_title,MIN(version_title) version_title,
                        MIN(volumn_title) volumn_title,MIN(unit_path) unit_path,
                        MIN(chapter_title) chapter_title
                      FROM resource_tree GROUP BY resource_id
                    ) tree ON tree.resource_id=r.resource_id""")
                return {str(row["resource_id"]):LocalCorpusIngestor._normalize_seyk_metadata(dict(row)) for row in rows}
            if source_kind == "local_scanned_document":
                rows=con.execute("""SELECT p.page_id,p.page_number,p.ocr_confidence,p.manual_review_required,
                    p.formula_review_required,b.book_id,b.relative_pdf_path,b.title book_title,b.stage,b.grade,
                    b.subject,b.semester,b.volume,b.edition
                    FROM textbook_page p JOIN textbook_book b ON b.book_id=p.book_id
                    WHERE p.status='completed'""")
                result={}
                for row in rows:
                    item=dict(row)
                    item.update({
                        "title":f"{item['book_title']} 第{item['page_number']}页",
                        "unit_path":f"{item['book_title']}/PDF第{item['page_number']}页",
                        "source_ref":f"local-textbook-pdf:{item['relative_pdf_path']}#page={item['page_number']}",
                    })
                    result[item["page_id"]]=item
                return result
            rows=con.execute("""WITH candidates AS (
                SELECT bvid,grade,subject,semester,3 priority FROM selected_v2
                UNION ALL SELECT bvid,grade,subject,NULL,2 FROM selected
                UNION ALL SELECT bvid,grade,subject,semester,1 FROM selected_aoshu
              ), ranked AS (
                SELECT *,ROW_NUMBER() OVER(PARTITION BY bvid ORDER BY priority DESC) rank FROM candidates
              )
              SELECT ranked.bvid,ranked.grade,ranked.subject,ranked.semester,d.title,d.author,d.desc
              FROM ranked LEFT JOIN details d ON d.bvid=ranked.bvid WHERE ranked.rank=1""")
            result={}
            for row in rows:
                item=dict(row)
                if item.get("subject") == "政治": item["subject"]="道德与法治"
                result[item["bvid"]]=item
            return result


    @staticmethod
    def _normalize_seyk_metadata(metadata:dict)->dict:
        subject=str(metadata.get("subject_title") or "").strip()
        for prefix in ("小学", "初中", "高中"):
            if subject.startswith(prefix):
                subject=subject[len(prefix):]
                break
        volume=str(metadata.get("volumn_title") or "").strip()
        semester="上学期" if "上" in volume else "下学期" if "下" in volume else volume or None
        return {
            "title":metadata.get("title"), "description":metadata.get("description"),
            "teacher":metadata.get("teacher"), "stage":metadata.get("stage_title"),
            "grade":metadata.get("grade_title"), "subject":subject or None,
            "edition":metadata.get("version_title"), "semester":semester,
            "volume":volume or None, "unit_path":metadata.get("unit_path"),
            "chapter_title":metadata.get("chapter_title"),
        }

    @staticmethod
    def _video_files(spec: CorpusSpec) -> dict[str, Path]:
        if not spec.video_root or not _corpus_io(spec.video_root).is_dir():
            return {}
        paths=(path for path in _corpus_glob(spec.video_root,"*.mp4") if _corpus_io(path).is_file())
        if spec.source_kind == "bilibili":
            paths=(path for path in paths
                   if not any(part.startswith(".") for part in path.relative_to(spec.video_root).parts))
        return {path.stem: path for path in paths}
    @staticmethod
    def _sha256(path:Path)->str:
        digest=hashlib.sha256()
        with _corpus_io(path).open("rb") as handle:
            while block:=handle.read(1024*1024):digest.update(block)
        return digest.hexdigest()
    @staticmethod
    def _fingerprint(path:Path|None)->str|None:
        if not path:return None
        stat=_corpus_io(path).stat();return f"size:{stat.st_size}:mtime_ns:{stat.st_mtime_ns}"
    def _failure(self,job_id:str,external_id:str,path:Path,error:Exception)->None:
        now=utc_now()
        with self.database.connect() as con:
            con.execute("""INSERT INTO kg_ingest_failure(failure_id,job_id,external_id,relative_path,error_code,detail,retry_count,status,created_at,updated_at)
            VALUES(?,?,?,?,?,?,1,'open',?,?) ON CONFLICT(job_id,external_id) DO UPDATE SET detail=excluded.detail,retry_count=kg_ingest_failure.retry_count+1,updated_at=excluded.updated_at""",
            (str(uuid4()),job_id,external_id,path.name,type(error).__name__,str(error)[:500],now,now));con.commit()

    def _resolve_failure(self, job_id: str, external_id: str) -> None:
        # Resource upsert has already committed; resolving this queue item is a
        # separate, idempotent checkpoint. A crash here merely replays one resource.
        with self.database.connect() as con:
            con.execute("UPDATE kg_ingest_failure SET status='resolved',updated_at=? WHERE job_id=? AND external_id=? AND status='open'",
                        (utc_now(), job_id, external_id))
            con.commit()
