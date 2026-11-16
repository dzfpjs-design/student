from __future__ import annotations
import json,re
import hashlib
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from ..database import Database
from ..repository import utc_now
from .language_atoms import LanguageFocus, TextbookLanguageAtomProjector
from .schema import ensure_schema


class LanguageCandidateConflictError(ValueError):
    """A client action id was reused for a different durable command."""


class TextbookFocusCandidateService:
    """OCR-page-only candidate workflow; no video transcript is eligible."""
    STOP={'this','that','with','from','have','will','your','they','them','were','what','when','where','there','about'}
    def __init__(self,database:Database):self.database=database
    def extract(self,resource_id:str)->list[dict]:
        with self.database.connect() as c:
            page=c.execute('SELECT 1 FROM kg_document_page WHERE resource_id=?',(resource_id,)).fetchone()
            if not page:raise ValueError('only documented OCR pages can create textbook candidates')
            text='\n'.join(r[0] for r in c.execute('SELECT text FROM kg_segment WHERE resource_id=? ORDER BY ordinal',(resource_id,)))
        terms=[]
        # Do not treat every Latin token as vocabulary.  OCR has no dependable
        # layout semantics, so page headers, exercise instructions and whole
        # sentences are unsafe.  A word needs an explicit word-list label; a
        # phrase needs quotation marks (handled below).  Parents can still add a
        # visible page term manually when their textbook uses another layout.
        for m in re.finditer(r"(?:\b(?:word|words|vocabulary)\b|(?:单词|词汇))[：:]\s*([A-Za-z][A-Za-z'-]*(?:\s*,\s*[A-Za-z][A-Za-z'-]*){0,12})",text,re.IGNORECASE):
            for word_match in re.finditer(r"[A-Za-z][A-Za-z'-]*",m.group(1)):
                word=word_match.group(0);norm=word.lower()
                if norm not in self.STOP:
                    start=m.start(1)+word_match.start();terms.append(('english',word,norm,'word',start,start+len(word)))
        # A phrase is only proposed when the page typography itself marks it as a
        # phrase.  Adjacent OCR words are too ambiguous to turn into vocabulary.
        for m in re.finditer(r"[\"“]([A-Za-z][A-Za-z'-]*(?:\s+[A-Za-z][A-Za-z'-]*){1,5})[\"”]",text):
            phrase=m.group(1).strip(); terms.append(('english',phrase,phrase.lower(),'phrase',m.start(1),m.end(1)))
        # Chinese OCR cannot safely infer a textbook 生字表. Only explicitly marked
        # entries become candidates; parent can add multi-pronunciation characters.
        for m in re.finditer(r"(?:生字|多音字|词语)[：:\s]*([\u4e00-\u9fff]{1,4})",text):
            term=m.group(1);terms.append(('chinese',term,term,'character' if len(term)==1 else 'word',m.start(1),m.end(1)))
        now=utc_now()
        with self.database.connect() as c:
            ensure_schema(c,now)
            for lang,term,norm,kind,start,end in terms:
                c.execute("INSERT INTO kg_language_focus_candidate(candidate_id,resource_id,language,term,normalized,focus_kind,status,source_start,source_end,created_at,updated_at) VALUES(?,?,?,?,?,?,'pending',?,?,?,?) ON CONFLICT(resource_id,language,normalized,focus_kind) DO UPDATE SET updated_at=excluded.updated_at WHERE status='pending'",(str(uuid4()),resource_id,lang,term,norm,kind,start,end,now,now))
            c.commit()
        return self.list(resource_id)
    def list(self,resource_id:str)->list[dict]:
        with self.database.connect() as c:rows=c.execute('SELECT * FROM kg_language_focus_candidate WHERE resource_id=? AND status!=\'deleted\' ORDER BY language,term',(resource_id,)).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def public(candidate:dict)->dict:
        value={key:item for key,item in candidate.items() if key not in {'candidate_id','resource_id'}}
        value['candidate_ref']='candidate-'+hashlib.sha256(candidate['candidate_id'].encode('utf-8')).hexdigest()[:24]
        return value

    def candidate_id_for_ref(self,candidate_ref:str)->str:
        if not candidate_ref.startswith('candidate-'): raise ValueError('candidate not found')
        with self.database.connect() as c:
            rows=c.execute('SELECT candidate_id FROM kg_language_focus_candidate').fetchall()
        for row in rows:
            candidate_id=str(row['candidate_id'])
            if 'candidate-'+hashlib.sha256(candidate_id.encode('utf-8')).hexdigest()[:24]==candidate_ref:return candidate_id
        raise ValueError('candidate not found')
    def save(self,candidate_id:str,*,term:str,focus_kind:str,proposed:dict,status:str,client_action_id:str|None=None)->dict:
        if status not in {'pending','confirmed','deleted'} or focus_kind not in {'word','character','phrase'}:raise ValueError('invalid candidate')
        proposed=self._validate_proposed(proposed)
        term=term.strip()
        if not term: raise ValueError('term is required')
        proposed_json=self._canonical_json(proposed)
        action='confirm' if status=='confirmed' else 'save'
        payload={'term':term,'focus_kind':focus_kind,'proposed':proposed,'status':status}
        payload_sha256=self._payload_sha256(payload)
        client_action_id=client_action_id or f'legacy:{action}:{candidate_id}:{payload_sha256}'
        projection_claim: str|None=None
        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute('SELECT resource_id,language,status,term,focus_kind,proposed_json FROM kg_language_focus_candidate WHERE candidate_id=?',(candidate_id,)).fetchone()
            if not row:raise ValueError('candidate not found')
            prior=self._action_replay(c,client_action_id,candidate_id,action,payload_sha256)
            if prior:
                current=dict(c.execute('SELECT * FROM kg_language_focus_candidate WHERE candidate_id=?',(candidate_id,)).fetchone())
                if current['status']!='projecting' or action!='confirm':
                    c.commit(); return current
            else:
                self._record_action(c,client_action_id,candidate_id,action,payload_sha256,row['status'])
            if row['status'] in {'confirmed','conflicted'}:
                c.rollback()
                raise ValueError('confirmed candidates are immutable; create a corrected page version instead')
            if row['status'] not in {'pending','projecting'}:
                c.rollback()
                raise ValueError('only pending candidates can be changed or deleted')
            self._validate_term(row['language'], term, focus_kind)
            if status=='confirmed':
                text='\n'.join(r[0] for r in c.execute('SELECT text FROM kg_segment WHERE resource_id=? ORDER BY ordinal',(row['resource_id'],)))
                if term not in text: raise ValueError('edited term is not evidenced on this OCR page')
            if row['status']=='projecting' and (term != row['term'] or focus_kind != row['focus_kind'] or proposed_json != row['proposed_json']):
                c.rollback()
                raise ValueError('a projecting candidate can only resume the same parent approval')
            next_status='projecting' if status=='confirmed' else status
            c.execute('UPDATE kg_language_focus_candidate SET term=?,normalized=?,focus_kind=?,proposed_json=?,status=?,updated_at=? WHERE candidate_id=?',(term,term.lower() if row['language']=='english' else term,focus_kind,proposed_json,next_status,utc_now(),candidate_id))
            if status=='confirmed':
                projection_claim=self._claim_projection(c, client_action_id)
            c.commit()
        if status=='confirmed':
            if projection_claim:
                focus=LanguageFocus(language=row['language'],term=term,focus_kind=focus_kind,**{k:v for k,v in proposed.items() if k in LanguageFocus.__dataclass_fields__})
                # The action-row lease is durable SQLite state, so another
                # service instance cannot publish a partial sibling projection.
                try:
                    self._projector().project_page(
                        row['resource_id'], [focus], review_status='unreviewed',
                        mapping_source_ref=self._projection_source_ref(candidate_id),
                    )
                    self._finalize_confirmation(candidate_id, client_action_id, projection_claim)
                except Exception:
                    self._release_projection_claim(client_action_id, projection_claim, failed=True)
                    raise
            else:
                # A sibling process owns a durable short lease.  Wait briefly so
                # a double-click normally receives the same completed result;
                # if the owner dies, return visible `projecting` rather than a
                # false failure and the same action can resume after its lease.
                self._await_projection(candidate_id, client_action_id)
        # Deleted candidates intentionally disappear from normal page listings,
        # but the save response must still acknowledge that the parent's deletion
        # reached SQLite instead of looking like a failed click.
        with self.database.connect() as c:
            saved=c.execute('SELECT * FROM kg_language_focus_candidate WHERE candidate_id=?',(candidate_id,)).fetchone()
        return dict(saved)

    def action_status(self, candidate_id: str, client_action_id: str) -> dict:
        """Public-safe status for a durable parent command after a timeout/restart."""
        with self.database.connect() as connection:
            candidate = connection.execute("SELECT * FROM kg_language_focus_candidate WHERE candidate_id=?", (candidate_id,)).fetchone()
            action = connection.execute("""SELECT result_status,projection_lease_expires_at
                FROM kg_language_focus_candidate_action WHERE candidate_id=? AND client_action_id=?""",
                (candidate_id, client_action_id)).fetchone()
        if not candidate or not action:
            raise ValueError("candidate action not found")
        status = "confirmed" if candidate['status'] == 'confirmed' and action['result_status'] == 'confirmed' else (
            "failed_recoverable" if action['result_status'] == 'failed' else "processing")
        return {"item": dict(candidate), "action_status": status,
                "retryable": status == "failed_recoverable",
                "lease_active": bool(action['projection_lease_expires_at'])}

    def _finalize_confirmation(self, candidate_id: str, client_action_id: str, projection_claim: str) -> None:
        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute('SELECT * FROM kg_language_focus_candidate WHERE candidate_id=?',(candidate_id,)).fetchone()
            if not row:
                c.rollback();raise ValueError('candidate approval was superseded before projection completed')
            action=c.execute("SELECT result_status,projection_claim FROM kg_language_focus_candidate_action WHERE client_action_id=? AND candidate_id=? AND action='confirm'",(client_action_id,candidate_id)).fetchone()
            if row['status'] == 'confirmed':
                # Two HTTP retries with the same action can both finish the
                # local projection work.  The second one must acknowledge the
                # already-published decision instead of looking like a failed
                # parent click.
                if action and action['result_status'] == 'confirmed':
                    c.commit(); return
                c.rollback();raise ValueError('candidate approval was superseded before projection completed')
            if row['status'] != 'projecting':
                c.rollback();raise ValueError('candidate approval was superseded before projection completed')
            if not action or action['projection_claim'] != projection_claim:
                c.rollback();raise LanguageCandidateConflictError('candidate projection claim was taken over; retry the same action')
            projection_source=self._projection_source_ref(candidate_id)
            nodes=[item['node_id'] for item in c.execute(
                "SELECT DISTINCT node_id FROM kg_resource_node WHERE resource_id=? AND source_ref=? AND review_status='unreviewed'",
                (row['resource_id'], projection_source),
            )]
            if not nodes:
                c.rollback();raise ValueError('candidate projection did not create assessable atoms')
            placeholders=','.join('?' for _ in nodes)
            promoted=c.execute(f"UPDATE kg_resource_node SET review_status='approved' WHERE resource_id=? AND source_ref=? AND node_id IN ({placeholders}) AND review_status='unreviewed'",(row['resource_id'],projection_source,*nodes)).rowcount
            if promoted <= 0:
                c.rollback();raise ValueError('candidate projection has no pending page mapping to approve')
            c.execute(f"UPDATE kg_node SET status='active',review_status='approved',updated_at=? WHERE node_id IN ({placeholders})",(utc_now(),*nodes))
            c.execute(f"UPDATE kg_edge SET review_status='approved',updated_at=? WHERE source_ref=? AND target_node_id IN ({placeholders})",(utc_now(),projection_source,*nodes))
            c.execute("UPDATE kg_language_focus_candidate SET status='confirmed',updated_at=? WHERE candidate_id=?",(utc_now(),candidate_id))
            c.execute("UPDATE kg_language_focus_candidate_action SET result_status='confirmed',projection_claim=NULL,projection_lease_expires_at=NULL,updated_at=? WHERE client_action_id=?",(utc_now(),client_action_id))
            c.commit()

    def create(self,resource_id:str,*,language:str,term:str,focus_kind:str,proposed:dict)->dict:
        """Add a conservative parent-supplied item that is visibly on this page."""
        if language not in {'english','chinese'} or focus_kind not in {'word','character','phrase'}: raise ValueError('invalid candidate')
        proposed=self._validate_proposed(proposed)
        term=term.strip()
        if not term: raise ValueError('term is required')
        self._validate_term(language, term, focus_kind)
        with self.database.connect() as c:
            page=c.execute('SELECT 1 FROM kg_document_page WHERE resource_id=? AND document_kind=\'textbook\'',(resource_id,)).fetchone()
            text='\n'.join(r[0] for r in c.execute('SELECT text FROM kg_segment WHERE resource_id=? ORDER BY ordinal',(resource_id,)))
            if not page: raise ValueError('only documented OCR pages can create textbook candidates')
            if term not in text: raise ValueError('candidate term is not evidenced on this OCR page')
            start=text.index(term); now=utc_now(); norm=term.lower() if language=='english' else term
            c.execute("INSERT INTO kg_language_focus_candidate(candidate_id,resource_id,language,term,normalized,focus_kind,proposed_json,status,source_start,source_end,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'pending',?,?,?,?) ON CONFLICT(resource_id,language,normalized,focus_kind) DO UPDATE SET term=excluded.term,proposed_json=excluded.proposed_json,updated_at=excluded.updated_at WHERE status='pending'",(str(uuid4()),resource_id,language,term,norm,focus_kind,json.dumps(proposed,ensure_ascii=False),start,start+len(term),now,now));c.commit()
            row=c.execute('SELECT * FROM kg_language_focus_candidate WHERE resource_id=? AND language=? AND normalized=? AND focus_kind=?',(resource_id,language,norm,focus_kind)).fetchone()
        return dict(row)

    def withdraw_confirmation(self, candidate_id: str, *, client_action_id: str|None=None) -> dict:
        """Explicitly revoke one confirmed parent decision and retire its projections."""
        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute('SELECT * FROM kg_language_focus_candidate WHERE candidate_id=?',(candidate_id,)).fetchone()
            if not row: raise ValueError('candidate not found')
            payload_sha256=self._payload_sha256({'candidate_id':candidate_id,'action':'withdraw'})
            client_action_id=client_action_id or f'legacy:withdraw:{candidate_id}:{payload_sha256}'
            if self._action_replay(c,client_action_id,candidate_id,'withdraw',payload_sha256):
                saved=dict(c.execute('SELECT * FROM kg_language_focus_candidate WHERE candidate_id=?',(candidate_id,)).fetchone())
                c.commit(); return saved
            self._record_action(c,client_action_id,candidate_id,'withdraw',payload_sha256,row['status'])
            if row['status'] == 'withdrawn':
                c.execute("UPDATE kg_language_focus_candidate_action SET result_status='withdrawn',updated_at=? WHERE client_action_id=?",(utc_now(),client_action_id))
                c.commit(); return dict(row)
            if row['status'] != 'confirmed':
                c.rollback()
                raise ValueError('only a confirmed candidate can be withdrawn')
            self._retire_projection(c,row['resource_id'],dict(row))
            c.execute("UPDATE kg_language_focus_candidate SET status='withdrawn',updated_at=? WHERE candidate_id=?",(utc_now(),candidate_id))
            c.execute("UPDATE kg_language_focus_candidate_action SET result_status='withdrawn',updated_at=? WHERE client_action_id=?",(utc_now(),client_action_id))
            c.commit()
            saved=c.execute('SELECT * FROM kg_language_focus_candidate WHERE candidate_id=?',(candidate_id,)).fetchone()
        return dict(saved)

    def reconcile_page(self, resource_id: str, text: str) -> dict:
        """Invalidate candidates/projections whose local OCR evidence disappeared."""
        stale = conflicted = 0
        with self.database.connect() as c:
            rows = c.execute("SELECT * FROM kg_language_focus_candidate WHERE resource_id=? AND status IN ('pending','projecting','confirmed')", (resource_id,)).fetchall()
            for row in rows:
                if row['term'] in text:
                    continue
                if row['status'] in {'pending','projecting'}:
                    c.execute("UPDATE kg_language_focus_candidate SET status='stale',updated_at=? WHERE candidate_id=?", (utc_now(), row['candidate_id']))
                    stale += 1
                else:
                    c.execute("UPDATE kg_language_focus_candidate SET status='conflicted',updated_at=? WHERE candidate_id=?", (utc_now(), row['candidate_id']))
                    self._retire_projection(c, resource_id, dict(row))
                    conflicted += 1
            c.commit()
        return {'stale': stale, 'conflicted': conflicted}

    @staticmethod
    def _canonical_json(value: dict) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))

    @classmethod
    def _payload_sha256(cls, value: dict) -> str:
        return hashlib.sha256(cls._canonical_json(value).encode('utf-8')).hexdigest()

    @staticmethod
    def _projection_source_ref(candidate_id: str) -> str:
        return f'textbook_focus_manifest_v1:candidate:{candidate_id}'

    def _projector(self) -> TextbookLanguageAtomProjector:
        return TextbookLanguageAtomProjector(self.database)

    @staticmethod
    def _claim_projection(connection, action_id: str) -> str|None:
        claim=str(uuid4())
        now=datetime.now(timezone.utc)
        lease=(now + timedelta(seconds=120)).isoformat()
        updated=connection.execute("""UPDATE kg_language_focus_candidate_action
            SET projection_claim=?,projection_lease_expires_at=?,updated_at=?
            WHERE client_action_id=? AND action='confirm' AND result_status!='confirmed'
              AND (projection_claim IS NULL OR projection_lease_expires_at<?)
              AND NOT EXISTS(SELECT 1 FROM kg_language_focus_candidate_action other
                WHERE other.candidate_id=(SELECT candidate_id FROM kg_language_focus_candidate_action WHERE client_action_id=?)
                  AND other.client_action_id<>? AND other.projection_claim IS NOT NULL
                  AND other.projection_lease_expires_at>=?)""",
            (claim,lease,utc_now(),action_id,now.isoformat(),action_id,action_id,now.isoformat())).rowcount
        return claim if updated else None

    def _await_projection(self, candidate_id: str, action_id: str) -> None:
        for _ in range(40):
            with self.database.connect() as connection:
                row=connection.execute("SELECT status FROM kg_language_focus_candidate WHERE candidate_id=?",(candidate_id,)).fetchone()
                action=connection.execute("SELECT result_status FROM kg_language_focus_candidate_action WHERE client_action_id=?",(action_id,)).fetchone()
            if row and row['status']=='confirmed' and action and action['result_status']=='confirmed':
                return
            time.sleep(.05)

    def _release_projection_claim(self, action_id: str, claim: str, *, failed: bool = False) -> None:
        with self.database.connect() as connection:
            connection.execute("UPDATE kg_language_focus_candidate_action SET projection_claim=NULL,projection_lease_expires_at=NULL,result_status=CASE WHEN ? THEN 'failed' ELSE result_status END,updated_at=? WHERE client_action_id=? AND projection_claim=?",(int(failed),utc_now(),action_id,claim))
            connection.commit()

    @staticmethod
    def _action_replay(connection, action_id: str, candidate_id: str, action: str, payload_sha256: str) -> bool:
        row=connection.execute("SELECT candidate_id,action,payload_sha256 FROM kg_language_focus_candidate_action WHERE client_action_id=?",(action_id,)).fetchone()
        if not row: return False
        if row['candidate_id'] != candidate_id or row['action'] != action or row['payload_sha256'] != payload_sha256:
            raise LanguageCandidateConflictError('client action id was already used for a different candidate command')
        return True

    @staticmethod
    def _record_action(connection, action_id: str, candidate_id: str, action: str, payload_sha256: str, result_status: str) -> None:
        now=utc_now()
        connection.execute("INSERT INTO kg_language_focus_candidate_action(client_action_id,candidate_id,action,payload_sha256,result_status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",(action_id,candidate_id,action,payload_sha256,result_status,now,now))

    @staticmethod
    def _validate_proposed(proposed: dict) -> dict:
        if not isinstance(proposed, dict): raise ValueError('proposed fields must be an object')
        reserved = {'language', 'term', 'focus_kind'}
        allowed = set(LanguageFocus.__dataclass_fields__) - reserved
        unknown = set(proposed) - allowed
        if unknown: raise ValueError('unknown or reserved proposed field')
        strings = {'part_of_speech','phonics_or_ipa','radical','structure','stroke_order_ref'}
        lists = {'senses','collocations','confusables','pronunciations','compounds','common_errors'}
        for key, value in proposed.items():
            if key in strings and value is not None and not isinstance(value, str): raise ValueError(f'{key} must be text')
            if key in lists and (not isinstance(value, list) or any(not isinstance(item, str) for item in value)): raise ValueError(f'{key} must be a list of text')
        return proposed

    @staticmethod
    def _validate_term(language: str, term: str, focus_kind: str) -> None:
        """Reject punctuation/control syntax before it can become a graph key."""
        if language == 'english':
            word = r"[A-Za-z](?:[A-Za-z'-]*[A-Za-z])?"
            pattern = word if focus_kind == 'word' else rf"{word}(?: {word}){{1,5}}" if focus_kind == 'phrase' else None
            if pattern is None or not re.fullmatch(pattern, term):
                raise ValueError('English candidates must be a word or a 2-6 word phrase')
        elif language == 'chinese':
            if focus_kind == 'character':
                valid = bool(re.fullmatch(r"[\u3400-\u9fff]", term))
            elif focus_kind in {'word','phrase'}:
                valid = bool(re.fullmatch(r"[\u3400-\u9fff]{2,8}", term))
            else:
                valid = False
            if not valid:
                raise ValueError('Chinese candidates must be one character or a 2-8 character word')
        else:
            raise ValueError('invalid candidate language')

    @staticmethod
    def _retire_projection(connection, resource_id: str, candidate: dict) -> None:
        projection_source = TextbookFocusCandidateService._projection_source_ref(candidate['candidate_id'])
        connection.execute("UPDATE kg_edge SET review_status='rejected',updated_at=? WHERE source_ref=? AND review_status='approved'", (utc_now(), projection_source))
        node_rows = connection.execute("SELECT DISTINCT node_id FROM kg_resource_node WHERE resource_id=? AND source_ref=?", (resource_id, projection_source)).fetchall()
        # Page re-ingestion may have already removed old mappings before its
        # follow-up evidence reconciliation runs.  The finite action suffixes
        # are exact canonical keys (not a term LIKE query), so this only repairs
        # this candidate's three assessable atoms and cannot sweep in a sibling
        # candidate that happens to share a prefix.
        if not node_rows:
            actions=('recognition','meaning_in_context','use') if candidate['language']=='english' else ('recognition','writing','meaning_in_context')
            keys=[f"language:{candidate['language']}:{candidate['focus_kind']}:{candidate['term']}:{action}" for action in actions]
            node_rows=connection.execute(
                f"SELECT node_id FROM kg_node WHERE canonical_key IN ({','.join('?' for _ in keys)})", keys
            ).fetchall()
        for node in node_rows:
            node_id = node['node_id']
            connection.execute("UPDATE kg_resource_node SET review_status='rejected' WHERE resource_id=? AND node_id=? AND source_ref=?", (resource_id, node_id, projection_source))
            usable = connection.execute("SELECT 1 FROM kg_resource_node WHERE node_id=? AND review_status='approved' LIMIT 1", (node_id,)).fetchone()
            if not usable:
                connection.execute("UPDATE kg_node SET status='conflicted',review_status='unreviewed',updated_at=? WHERE node_id=?", (utc_now(), node_id))
                connection.execute("""UPDATE kg_review_plan SET status='superseded',updated_at=?
                    WHERE status='active' AND EXISTS (
                      SELECT 1 FROM json_each(kg_review_plan.plan_json,'$.items')
                      WHERE json_extract(json_each.value,'$.knowledge_node_id')=?)""", (utc_now(), node_id))
            else:
                connection.execute("UPDATE kg_node SET status='active',review_status='approved',updated_at=? WHERE node_id=?", (utc_now(), node_id))
        normalized = candidate['term'].lower() if candidate['language'] == 'english' else candidate['term']
        lexemes = connection.execute("SELECT lexeme_id FROM kg_language_lexeme WHERE language=? AND normalized=? AND focus_kind=?", (candidate['language'], normalized, candidate['focus_kind'])).fetchall()
        for lexeme in lexemes:
            lexeme_id = lexeme['lexeme_id']
            connection.execute("DELETE FROM kg_language_lexeme_page WHERE lexeme_id=? AND resource_id=?", (lexeme_id, resource_id))
            still_evidenced = connection.execute("SELECT 1 FROM kg_language_lexeme_page WHERE lexeme_id=? LIMIT 1", (lexeme_id,)).fetchone()
            if not still_evidenced:
                connection.execute("UPDATE kg_node SET status='conflicted',updated_at=? WHERE canonical_key=?", (utc_now(), f"language-lexeme:{candidate['language']}:{candidate['focus_kind']}:{candidate['term']}"))
