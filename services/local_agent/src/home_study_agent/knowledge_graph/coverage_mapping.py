"""Checkpointed, strict lexical candidate mapping over local transcript segments."""
from __future__ import annotations

import json, re
from datetime import datetime, timedelta, timezone
from threading import Event, Thread
from uuid import uuid4

from ..database import Database
from ..repository import utc_now
from .schema import ensure_schema


class AtomicCoverageMapper:
    source_ref = "coverage_exact_terms_v1"

    def __init__(self, database: Database):
        self.database = database
        self.run_lease_seconds = 120
        self.heartbeat_interval_seconds = 20

    @staticmethod
    def _automatic_term_eligible(title: str) -> bool:
        # A bare Han character in long-form transcript text is almost always
        # polysemous. Character atoms enter through the textbook/OCR evidence
        # flow instead; generic corpus coverage must not flood a parent queue.
        return not (len(title) == 1 and '\u4e00' <= title <= '\u9fff')

    def start(self, *, idempotency_key: str) -> str:
        now=utc_now()
        with self.database.connect() as con:
            ensure_schema(con,now)
            prior=con.execute("SELECT mapping_job_id FROM kg_candidate_mapping_job WHERE idempotency_key=?",(idempotency_key,)).fetchone()
            if prior: return prior[0]
            job=str(uuid4()); total=con.execute("SELECT COUNT(*) FROM kg_resource").fetchone()[0]
            con.execute("INSERT INTO kg_candidate_mapping_job(mapping_job_id,idempotency_key,status,source_ref,total_resources,options_json,created_at,updated_at) VALUES(?,?, 'pending',?,?,?, ?,?)",
                (job,idempotency_key,self.source_ref,total,json.dumps({'strict_lexical':True}),now,now));con.commit();return job

    def run(self, job_id: str, *, max_resources: int = 25) -> dict:
        now=utc_now()
        with self.database.connect() as con:
            ensure_schema(con,now);con.commit();job=con.execute("SELECT * FROM kg_candidate_mapping_job WHERE mapping_job_id=?",(job_id,)).fetchone()
            if not job: raise ValueError('unknown mapping job')
            if job['status']=='completed': return dict(job)
            run_token=self._claim_run(con,job_id)
            if run_token is None: return dict(con.execute("SELECT * FROM kg_candidate_mapping_job WHERE mapping_job_id=?",(job_id,)).fetchone())
            job=con.execute("SELECT * FROM kg_candidate_mapping_job WHERE mapping_job_id=?",(job_id,)).fetchone()
            cursor=job['checkpoint_resource_id'] or '';con.commit()
            resources=con.execute("SELECT resource_id,title,subject_scope,grade_scope FROM kg_resource WHERE resource_id>? ORDER BY resource_id LIMIT ?",(cursor,max_resources)).fetchall()
        stop_heartbeat,heartbeat=self._start_heartbeat(job_id,run_token)
        try:
            for resource in resources:
                try: mapped=self._map_one(dict(resource),job_id,run_token)
                except RuntimeError:
                    raise
                except Exception as error: self._failure(job_id,resource['resource_id'],error); mapped=0
                with self.database.connect() as con:
                    updated=con.execute("""UPDATE kg_candidate_mapping_job SET checkpoint_resource_id=?,
                        processed_resources=(SELECT COUNT(*) FROM kg_resource WHERE resource_id<=?),
                        failed_resources=(SELECT COUNT(*) FROM kg_candidate_mapping_failure WHERE mapping_job_id=kg_candidate_mapping_job.mapping_job_id AND status='open'),
                        mapped_rows=mapped_rows+?,updated_at=?,run_lease_expires_at=? WHERE mapping_job_id=? AND run_token=?""",(resource['resource_id'],resource['resource_id'],mapped,utc_now(),self._lease_until(),job_id,run_token))
                    if updated.rowcount != 1:
                        con.rollback();raise RuntimeError('mapping checkpoint lease was taken by a recovery worker')
                    con.commit()
        finally:
            stop_heartbeat.set()
            heartbeat.join(timeout=1)
        with self.database.connect() as con:
            remaining=con.execute("SELECT COUNT(*) FROM kg_resource WHERE resource_id>COALESCE((SELECT checkpoint_resource_id FROM kg_candidate_mapping_job WHERE mapping_job_id=?),'')",(job_id,)).fetchone()[0]
            if not remaining:
                con.execute("UPDATE kg_candidate_mapping_job SET status='completed',completed_at=?,updated_at=?,run_token=NULL,run_lease_expires_at=NULL WHERE mapping_job_id=? AND run_token=?",(utc_now(),utc_now(),job_id,run_token));con.commit()
            else:
                con.execute("UPDATE kg_candidate_mapping_job SET run_token=NULL,run_lease_expires_at=NULL,updated_at=? WHERE mapping_job_id=? AND run_token=?",(utc_now(),job_id,run_token));con.commit()
            return dict(con.execute("SELECT * FROM kg_candidate_mapping_job WHERE mapping_job_id=?",(job_id,)).fetchone())

    def _lease_until(self) -> str:
        return (datetime.now(timezone.utc) + timedelta(seconds=self.run_lease_seconds)).isoformat()

    def _start_heartbeat(self, job_id: str, run_token: str) -> tuple[Event, Thread]:
        stop=Event()
        def renew() -> None:
            while not stop.wait(self.heartbeat_interval_seconds):
                with self.database.connect() as con:
                    con.execute("UPDATE kg_candidate_mapping_job SET run_lease_expires_at=?,updated_at=? WHERE mapping_job_id=? AND run_token=?",(self._lease_until(),utc_now(),job_id,run_token));con.commit()
        worker=Thread(target=renew,daemon=True,name=f'kg-map-heartbeat-{job_id[:8]}')
        worker.start()
        return stop,worker

    def _claim_run(self, connection, job_id: str) -> str | None:
        connection.execute('BEGIN IMMEDIATE')
        token=str(uuid4())
        result=connection.execute("""UPDATE kg_candidate_mapping_job SET status='processing',run_token=?,run_lease_expires_at=?,updated_at=?
            WHERE mapping_job_id=? AND status!='completed' AND (
                status!='processing' OR run_token IS NULL OR run_lease_expires_at IS NULL
                OR julianday(run_lease_expires_at)<=julianday('now')
            )""",(token,self._lease_until(),utc_now(),job_id))
        if result.rowcount != 1:
            connection.commit();return None
        connection.commit();return token

    def _map_one(self, resource: dict, job_id: str, run_token: str) -> int:
        with self.database.connect() as con:
            nodes=con.execute("""SELECT node_id,title FROM kg_node WHERE node_type='knowledge_point' AND status='active'
                AND (json_extract(scope_json,'$.subject') IS NULL OR ? IS NULL OR json_extract(scope_json,'$.subject')=?)
                AND (json_extract(scope_json,'$.grade') IS NULL OR ? IS NULL OR json_extract(scope_json,'$.grade')=?)""",(resource['subject_scope'],resource['subject_scope'],resource['grade_scope'],resource['grade_scope'])).fetchall()
            terms={r['title']:r['node_id'] for r in nodes if not re.search(r'^[A-Za-z ]{1,3}$',r['title']) and self._automatic_term_eligible(r['title'])}
            if not terms:return 0
            # Case-insensitive English atoms must still be unambiguous.  A
            # malformed catalog containing both ``apple`` and ``Apple`` is not
            # a reason to guess which atom owns a transcript mention.
            casefold_groups: dict[str,list[str]]={}
            for term in terms:
                casefold_groups.setdefault(term.casefold(),[]).append(term)
            canonical_terms_by_casefold={fold:values[0] for fold,values in casefold_groups.items() if len(values)==1}
            terms={term:terms[term] for term in canonical_terms_by_casefold.values()}
            if not terms:return 0
            # Chinese terms are unaffected; English lexical atoms should match
            # normal textbook capitalization (``Apple`` vs canonical ``apple``).
            pattern=re.compile('|'.join(re.escape(term) for term in sorted(terms,key=len,reverse=True)), re.IGNORECASE)
            # A duplicated transcript is represented by an occurrence linked to
            # one canonical segment.  Candidate mapping must follow that link;
            # otherwise the later resource is silently unsearchable despite
            # having the same local evidence.
            segments=con.execute("""SELECT s.segment_id,s.text FROM kg_segment_occurrence o
                JOIN kg_segment s ON s.segment_id=o.segment_id WHERE o.resource_id=? ORDER BY o.ordinal""",
                (resource['resource_id'],)).fetchall(); now=utc_now(); count=0
            con.execute('BEGIN IMMEDIATE')
            owner=con.execute("SELECT 1 FROM kg_candidate_mapping_job WHERE mapping_job_id=? AND run_token=? AND julianday(run_lease_expires_at)>julianday('now')",(job_id,run_token)).fetchone()
            if not owner:
                con.rollback();raise RuntimeError('mapping resource write lost its checkpoint lease')
            con.execute("DELETE FROM kg_resource_node WHERE resource_id=? AND source_ref=? AND review_status='unreviewed'",(resource['resource_id'],self.source_ref))
            for segment in segments:
                found={canonical_terms_by_casefold[match.casefold()] for match in pattern.findall(segment['text'])}
                found |= {term for term in terms if term.casefold() in resource['title'].casefold()}
                for term in found:
                    con.execute("""INSERT INTO kg_resource_node(resource_id,segment_id,node_id,relation_type,confidence,review_status,source_ref,created_at)
                        VALUES(?,?,?,'covers',.66,'unreviewed',?,?) ON CONFLICT(resource_id,segment_id,node_id,relation_type) DO UPDATE SET confidence=excluded.confidence,source_ref=excluded.source_ref,created_at=excluded.created_at WHERE kg_resource_node.review_status='unreviewed'""",(resource['resource_id'],segment['segment_id'],terms[term],self.source_ref,now));count+=con.execute('SELECT changes()').fetchone()[0]
            self._before_resource_commit()
            renewed=con.execute("UPDATE kg_candidate_mapping_job SET run_lease_expires_at=?,updated_at=? WHERE mapping_job_id=? AND run_token=?",(self._lease_until(),utc_now(),job_id,run_token))
            if renewed.rowcount != 1:
                con.rollback();raise RuntimeError('mapping resource write lost its checkpoint lease')
            con.commit();return count

    def _before_resource_commit(self) -> None:
        """Test seam; production intentionally performs no work here."""

    def _failure(self,job_id:str,resource_id:str,error:Exception)->None:
        now=utc_now()
        with self.database.connect() as con:
            con.execute("INSERT INTO kg_candidate_mapping_failure(failure_id,mapping_job_id,resource_id,error_code,detail,status,created_at,updated_at) VALUES(?,?,?,?,?,'open',?,?) ON CONFLICT(mapping_job_id,resource_id) DO UPDATE SET retry_count=retry_count+1,detail=excluded.detail,updated_at=excluded.updated_at",(str(uuid4()),job_id,resource_id,type(error).__name__,str(error)[:400],now,now));con.execute("UPDATE kg_candidate_mapping_job SET failed_resources=failed_resources+1 WHERE mapping_job_id=?",(job_id,));con.commit()
