"""Teacher-shaped language atoms grounded in a textbook/OCR page.

An atom is not a topic label: it must name one observable student action and
one finite assessment target.  A page may evidence a word/character, but a
caller must explicitly identify it as a learning focus; OCR alone cannot know
whether a familiar word is this week's required vocabulary.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from uuid import uuid4
from ..database import Database
from .service import KnowledgeGraphService


@dataclass(frozen=True)
class LanguageFocus:
    language: str  # english or chinese
    term: str
    focus_kind: str  # word, phrase, or character
    part_of_speech: str | None = None
    senses: list[str] = field(default_factory=list)
    collocations: list[str] = field(default_factory=list)
    confusables: list[str] = field(default_factory=list)
    phonics_or_ipa: str | None = None
    pronunciations: list[str] = field(default_factory=list)
    radical: str | None = None
    structure: str | None = None
    stroke_order_ref: str | None = None
    compounds: list[str] = field(default_factory=list)
    common_errors: list[str] = field(default_factory=list)


class TextbookLanguageAtomProjector:
    """Creates assessable form/meaning/use atoms only when page evidence exists."""
    version = "textbook-language-atoms-v1"
    def __init__(self, database: Database): self.graph=KnowledgeGraphService(database); self.database=database

    def project_page(
        self,
        resource_id: str,
        focuses: list[LanguageFocus],
        *,
        review_status: str = 'unreviewed',
        mapping_source_ref: str = 'textbook_focus_manifest_v1',
    ) -> dict:
        if review_status not in {'unreviewed', 'approved'}:
            raise ValueError('language atom projection requires an explicit evidence review status')
        self.graph.ensure_ready()
        with self.database.connect() as con:
            row=con.execute("""SELECT r.title,r.grade_scope,r.subject_scope,r.transcript_relative_path,
                r.source_ref,r.license_boundary,d.page_number FROM kg_resource r
                JOIN kg_document_page d ON d.resource_id=r.resource_id WHERE r.resource_id=?""",(resource_id,)).fetchone()
            text='\n'.join(item[0] for item in con.execute("SELECT text FROM kg_segment WHERE resource_id=? ORDER BY ordinal",(resource_id,)))
            segments=[dict(item) for item in con.execute("SELECT segment_id,text FROM kg_segment WHERE resource_id=?",(resource_id,))]
        if not row: raise ValueError('resource is not a documented OCR page')
        created=[]; skipped=[]
        for focus in focuses:
            term=focus.term.strip()
            if focus.language not in {'english','chinese'} or focus.focus_kind not in {'word','character','phrase'} or not term or term not in text:
                skipped.append({'term':term,'reason':'unsupported_focus_or_missing_page_evidence'});continue
            source=f"document_page:{resource_id}:page:{row['page_number']}"
            scope={'grade':row['grade_scope'],'subject':row['subject_scope'],'page':row['page_number'],'atom_family':'language'}
            lexeme=self._upsert_lexeme(focus,source,row['license_boundary'])
            with self.database.connect() as con:
                con.execute("INSERT OR REPLACE INTO kg_language_lexeme_page(lexeme_id,resource_id,page_number,evidence_text,created_at) VALUES(?,?,?,?,datetime('now'))",(lexeme,resource_id,row['page_number'],term));con.commit()
            actions=(('recognition','识读/读音'),('meaning_in_context','语境义'),('use','口头或书面运用')) if focus.language=='english' else (('recognition','认读字音'),('writing','字形书写'),('meaning_in_context','语境义与组词'))
            lexeme_node=self.graph.upsert_node(canonical_key=f"language-lexeme:{focus.language}:{focus.focus_kind}:{term}",node_type='unit',title=term,description='教材词字条目；其子节点才是可测语言原子。',scope=scope,source_ref=source,version_label=self.version,license_boundary=row['license_boundary'],confidence=.72,review_status=review_status)
            for action,label in actions:
                key=f"language:{focus.language}:{focus.focus_kind}:{term}:{action}"
                node=self.graph.upsert_node(canonical_key=key,node_type='knowledge_point',title=f"{term}·{label}",description=f"可诊断语言原子：学生能否完成“{label}”；教材 OCR 第{row['page_number']}页为证据，不从页面外推掌握。",scope=scope,source_ref=source,version_label=self.version,license_boundary=row['license_boundary'],confidence=.72,review_status=review_status)
                self.graph.link(lexeme_node,node,'contains',rationale='词字条目拆分为可观察的学习动作',source_ref=mapping_source_ref,version_label=self.version,confidence=.72,review_status=review_status)
                for segment in segments:
                    if term in segment['text']:
                        self.graph.map_segment(resource_id,segment['segment_id'],node,'explains',confidence=.72,source_ref=mapping_source_ref,review_status=review_status)
                created.append(node)
        return {'resource_id':resource_id,'page_number':row['page_number'],'created_atoms':len(created),'skipped':skipped}

    def _upsert_lexeme(self, focus: LanguageFocus, source: str, boundary: str) -> str:
        normalized=focus.term.lower() if focus.language=='english' else focus.term; now='datetime(\'now\')'
        with self.database.connect() as con:
            row=con.execute("SELECT lexeme_id FROM kg_language_lexeme WHERE language=? AND normalized=? AND focus_kind=? AND version_label=?",(focus.language,normalized,focus.focus_kind,self.version)).fetchone()
            lexeme_id=row[0] if row else str(uuid4())
            con.execute("""INSERT INTO kg_language_lexeme(lexeme_id,language,surface,focus_kind,normalized,part_of_speech,senses_json,collocations_json,confusables_json,phonics_or_ipa,pronunciations_json,radical,structure,stroke_order_ref,compounds_json,common_errors_json,source_ref,version_label,license_boundary,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now')) ON CONFLICT(language,normalized,focus_kind,version_label) DO UPDATE SET part_of_speech=excluded.part_of_speech,senses_json=excluded.senses_json,collocations_json=excluded.collocations_json,confusables_json=excluded.confusables_json,phonics_or_ipa=excluded.phonics_or_ipa,pronunciations_json=excluded.pronunciations_json,radical=excluded.radical,structure=excluded.structure,stroke_order_ref=excluded.stroke_order_ref,compounds_json=excluded.compounds_json,common_errors_json=excluded.common_errors_json,updated_at=datetime('now')""",(lexeme_id,focus.language,focus.term,focus.focus_kind,normalized,focus.part_of_speech,json.dumps(focus.senses,ensure_ascii=False),json.dumps(focus.collocations,ensure_ascii=False),json.dumps(focus.confusables,ensure_ascii=False),focus.phonics_or_ipa,json.dumps(focus.pronunciations,ensure_ascii=False),focus.radical,focus.structure,focus.stroke_order_ref,json.dumps(focus.compounds,ensure_ascii=False),json.dumps(focus.common_errors,ensure_ascii=False),source,self.version,boundary));con.commit()
        return lexeme_id
