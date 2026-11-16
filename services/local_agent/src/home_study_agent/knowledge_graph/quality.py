from __future__ import annotations

from collections import defaultdict
import re
from uuid import uuid4

from ..database import Database
from ..repository import utc_now
from .schema import ensure_schema


class GraphQualityService:
    """Evidence-first audit and review queue; metrics never turn candidates into facts."""
    def __init__(self, database: Database):
        self.database = database

    def report(self) -> dict:
        now=utc_now()
        with self.database.connect() as connection:
            ensure_schema(connection,now); connection.commit()
            edge_rows=connection.execute("SELECT relation_type,review_status,source_ref,COUNT(*) count FROM kg_edge GROUP BY relation_type,review_status,source_ref ORDER BY relation_type,source_ref").fetchall()
            mapping=connection.execute("""SELECT COUNT(*) total, SUM(review_status='approved') approved,
                SUM(review_status='unreviewed') unreviewed, SUM(review_status='rejected') rejected,
                COUNT(DISTINCT CASE WHEN review_status IN ('unreviewed','approved') THEN segment_id END) candidate_segments,
                COUNT(DISTINCT CASE WHEN review_status='approved' THEN segment_id END) approved_segments
                FROM kg_resource_node WHERE segment_id IS NOT NULL""").fetchone()
            total_segments=connection.execute("SELECT COUNT(*) FROM kg_segment").fetchone()[0]
            conflict=connection.execute("""SELECT COUNT(*) FROM (SELECT segment_id FROM kg_resource_node
                WHERE segment_id IS NOT NULL AND review_status IN ('unreviewed','approved')
                GROUP BY segment_id HAVING COUNT(DISTINCT node_id)>3)""").fetchone()[0]
            reviews=connection.execute("SELECT decision,COUNT(*) count FROM kg_mapping_review GROUP BY decision").fetchall()
            mapping_sources=connection.execute("""SELECT source_ref,review_status,COUNT(*) count
                FROM kg_resource_node WHERE segment_id IS NOT NULL
                GROUP BY source_ref,review_status ORDER BY count DESC,source_ref""").fetchall()
            tracks=connection.execute("""SELECT json_extract(n.scope_json,'$.grade') grade_scope,
                json_extract(n.scope_json,'$.track') track,COUNT(DISTINCT n.node_id) nodes,
                COUNT(DISTINCT CASE WHEN rn.review_status IN ('unreviewed','approved') THEN n.node_id END) candidate_or_approved_covered_nodes,
                COUNT(DISTINCT CASE WHEN rn.review_status='approved' THEN n.node_id END) approved_covered_nodes,
                COUNT(DISTINCT CASE WHEN rn.review_status IN ('unreviewed','approved') THEN rn.segment_id END) candidate_or_approved_mapped_segments,
                COUNT(DISTINCT CASE WHEN rn.review_status='approved' THEN rn.segment_id END) approved_mapped_segments FROM kg_node n
                LEFT JOIN kg_resource_node rn ON rn.node_id=n.node_id AND rn.segment_id IS NOT NULL
                WHERE n.node_type='knowledge_point' GROUP BY grade_scope,track ORDER BY grade_scope,track""").fetchall()
        review_by={row['decision']:row['count'] for row in reviews}
        binary_decisions=review_by.get('approved',0)+review_by.get('rejected',0)
        decided=binary_decisions+review_by.get('needs_split',0)
        target_coverage=[]
        for row in tracks:
            item=dict(row); nodes=item['nodes'] or 0
            item['candidate_atom_coverage_ratio']=round((item['candidate_or_approved_covered_nodes'] or 0)/nodes,4) if nodes else 0
            item['approved_atom_coverage_ratio']=round((item['approved_covered_nodes'] or 0)/nodes,4) if nodes else 0
            target_coverage.append(item)
        return {"edge_breakdown":[dict(row) for row in edge_rows],"mapping_source_breakdown":[dict(row) for row in mapping_sources],"mapping":{
            "rows":mapping['total'] or 0,"approved_rows":mapping['approved'] or 0,"unreviewed_rows":mapping['unreviewed'] or 0,
            "rejected_rows":mapping['rejected'] or 0,"candidate_or_approved_mapped_segments":mapping['candidate_segments'] or 0,
            "approved_mapped_segments":mapping['approved_segments'] or 0,"mapped_segments":mapping['candidate_segments'] or 0,"total_segments":total_segments,
            "unmapped_segment_rate":round(1-(mapping['candidate_segments'] or 0)/total_segments,4) if total_segments else 0,
            "potential_conflict_segments":conflict,"human_reviewed_rows":decided,
            "human_mapping_accuracy":None,
            "human_decision_acceptance_rate":round(review_by.get('approved',0)/binary_decisions,4) if binary_decisions else None,
        },"target_coverage":target_coverage}

    def review_queue(self, per_track: int = 8) -> list[dict]:
        """Stable, bounded human-review sample across J3, P4, PET and competition tracks."""
        self._ensure_ready()
        with self.database.connect() as connection:
            rows=connection.execute("""SELECT rn.resource_id,rn.segment_id,rn.node_id,rn.relation_type,rn.confidence,
                rn.source_ref AS mapping_source_ref,s.text,n.title AS knowledge_point_title,n.canonical_key,
                json_extract(n.scope_json,'$.grade') grade_scope,json_extract(n.scope_json,'$.track') track,
                r.title AS resource_title,r.source_ref AS resource_source_ref,r.license_boundary,
                r.transcript_relative_path,r.video_relative_path
                FROM kg_resource_node rn JOIN kg_segment s ON s.segment_id=rn.segment_id JOIN kg_node n ON n.node_id=rn.node_id
                JOIN kg_resource r ON r.resource_id=rn.resource_id
                WHERE rn.review_status='unreviewed' AND n.node_type='knowledge_point'
                AND NOT EXISTS (SELECT 1 FROM kg_mapping_review split WHERE split.resource_id=rn.resource_id
                    AND split.segment_id=rn.segment_id AND split.node_id=rn.node_id AND split.relation_type=rn.relation_type
                    AND split.decision='needs_split')
                ORDER BY grade_scope,track,n.title,s.segment_id""").fetchall()
        selected=[]; counts=defaultdict(int)
        for row in rows:
            key=(row['grade_scope'],row['track'])
            if counts[key] >= per_track: continue
            selected.append({**dict(row),"excerpt":row['text'][:420]}); selected[-1].pop('text')
            counts[key]+=1
        return selected

    def resource_review_queue(self, limit: int = 50) -> list[dict]:
        """Parent-facing aggregation: one decision covers all evidence segments."""
        self._ensure_ready()
        with self.database.connect() as c:
            rows=c.execute("""SELECT rn.resource_id,rn.node_id,rn.relation_type,COUNT(*) evidence_segment_count,
                MAX(rn.confidence) max_confidence,n.title knowledge_point_title,json_extract(n.scope_json,'$.grade') grade_scope,
                json_extract(n.scope_json,'$.track') track,r.title resource_title,r.source_ref resource_source_ref,
                r.license_boundary
                FROM kg_resource_node rn JOIN kg_node n ON n.node_id=rn.node_id JOIN kg_resource r ON r.resource_id=rn.resource_id
                WHERE rn.review_status='unreviewed' AND rn.segment_id IS NOT NULL AND n.node_type='knowledge_point'
                AND NOT EXISTS (SELECT 1 FROM kg_mapping_review split WHERE split.resource_id=rn.resource_id
                    AND split.node_id=rn.node_id AND split.relation_type=rn.relation_type AND split.decision='needs_split')
                GROUP BY rn.resource_id,rn.node_id,rn.relation_type ORDER BY evidence_segment_count DESC,max_confidence DESC,n.title LIMIT ?""",(max(1,min(limit,200)),)).fetchall()
            source_rows=c.execute("""SELECT resource_id,node_id,relation_type,source_ref FROM kg_resource_node
                WHERE review_status='unreviewed' AND segment_id IS NOT NULL
                GROUP BY resource_id,node_id,relation_type,source_ref
                ORDER BY resource_id,node_id,relation_type,source_ref""").fetchall()
        sources=defaultdict(list); source_counts=defaultdict(int)
        for row in source_rows:
            key=(row['resource_id'],row['node_id'],row['relation_type'])
            source_counts[key]+=1
            label=self._safe_source_label(row['source_ref'])
            if label not in sources[key]: sources[key].append(label)
        return [{**dict(row),"resource_source_ref":self._safe_source_label(row['resource_source_ref']),
                 "mapping_sources":sources[(row['resource_id'],row['node_id'],row['relation_type'])],
                 "mapping_source_count":source_counts[(row['resource_id'],row['node_id'],row['relation_type'])]} for row in rows]

    def resource_review_summary(self) -> dict:
        """Bounded, transcript-free audit workload metrics for the parent queue."""
        self._ensure_ready()
        with self.database.connect() as c:
            rows=c.execute("""SELECT resource_id,COUNT(*) review_items,SUM(evidence_segment_count) evidence_segments FROM (
                    SELECT rn.resource_id,rn.node_id,rn.relation_type,COUNT(*) evidence_segment_count
                    FROM kg_resource_node rn JOIN kg_node n ON n.node_id=rn.node_id
                    WHERE rn.review_status='unreviewed' AND rn.segment_id IS NOT NULL AND n.node_type='knowledge_point'
                    AND NOT EXISTS (SELECT 1 FROM kg_mapping_review split WHERE split.resource_id=rn.resource_id
                        AND split.node_id=rn.node_id AND split.relation_type=rn.relation_type AND split.decision='needs_split')
                    GROUP BY rn.resource_id,rn.node_id,rn.relation_type
                ) GROUP BY resource_id""").fetchall()
        burdens=sorted(row['review_items'] for row in rows)
        p95=burdens[max(0,(len(burdens)*95+99)//100-1)] if burdens else 0
        return {"pending_resource_review_items":sum(row['review_items'] for row in rows),
                "pending_resources":len(rows),"pending_evidence_segments":sum(row['evidence_segments'] for row in rows),
                "review_items_per_resource_p95":p95}

    def resource_review_evidence(self, *, resource_id: str, node_id: str, relation_type: str, limit: int = 3) -> list[dict]:
        """Return a deliberately small, redacted local preview for a parent decision.

        This is not a transcript export: no file path, full segment, or resource body
        leaves the local API.  The client may request it only from the parent review UI.
        """
        self._ensure_ready()
        with self.database.connect() as c:
            rows=c.execute("""SELECT s.ordinal,s.text,rn.source_ref FROM kg_resource_node rn
                JOIN kg_segment s ON s.segment_id=rn.segment_id
                WHERE rn.resource_id=? AND rn.node_id=? AND rn.relation_type=?
                  AND rn.segment_id IS NOT NULL AND rn.review_status='unreviewed'
                ORDER BY rn.confidence DESC,s.ordinal ASC LIMIT ?""",
                (resource_id,node_id,relation_type,max(1,min(limit,3)))).fetchall()
        return [{"sequence":row['ordinal']+1,"source":self._safe_source_label(row['source_ref']),
                 "preview":self._redacted_preview(row['text'])} for row in rows]

    def resource_review_history(self, reviewer: str, limit: int = 12) -> list[dict]:
        """Recent durable resource-level decisions, suitable for a parent undo control."""
        if not reviewer.strip(): raise ValueError('reviewer is required')
        self._ensure_ready()
        with self.database.connect() as c:
            rows=c.execute("""SELECT MIN(mr.review_id) review_key,mr.resource_id,mr.node_id,mr.relation_type,mr.reviewer,
                GROUP_CONCAT(DISTINCT mr.decision) decisions,
                MAX(mr.created_at) decided_at,COUNT(*) affected_segment_count,n.title knowledge_point_title,
                json_extract(n.scope_json,'$.grade') grade_scope,json_extract(n.scope_json,'$.track') track,
                r.title resource_title,r.source_ref resource_source_ref,r.license_boundary
                FROM kg_mapping_review mr JOIN kg_node n ON n.node_id=mr.node_id
                JOIN kg_resource r ON r.resource_id=mr.resource_id
                WHERE mr.segment_id IS NOT NULL AND n.node_type='knowledge_point' AND mr.reviewer=?
                GROUP BY mr.resource_id,mr.node_id,mr.relation_type,mr.reviewer
                ORDER BY decided_at DESC LIMIT ?""",(reviewer,max(1,min(limit,50)))).fetchall()
        items=[]
        for row in rows:
            values=sorted(set((row['decisions'] or '').split(',')))
            item=dict(row); item.pop('decisions')
            item['decision_values']=values
            item['decision']=values[0] if len(values)==1 else 'mixed_legacy'
            item['resource_source_ref']=self._safe_source_label(row['resource_source_ref'])
            items.append(item)
        return items

    @staticmethod
    def _redacted_preview(text: str) -> str:
        normalized=' '.join((text or '').split())
        normalized=re.sub(r"https?://\S+|[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+",'[已省略]',normalized)
        normalized=re.sub(r"(?<!\d)\d{4,}(?!\d)",'[数字]',normalized)
        return normalized[:180] + ('…' if len(normalized)>180 else '')

    @staticmethod
    def _safe_source_label(source_ref: str | None) -> str:
        """Keep useful provenance labels, but never expose a local filesystem locator."""
        value=(source_ref or '').strip()
        if re.search(r"(?:^file:|^~[\\/]|[\\/]|^[A-Za-z]:)",value,re.IGNORECASE):
            return '本机来源（路径已隐藏）'
        return value[:120] or '来源待补充'

    def record_resource_review(self, *, resource_id: str, node_id: str, relation_type: str, decision: str, reviewer: str, client_action_id: str, note: str = "") -> dict:
        if decision not in {'approved','rejected','needs_split'} or not reviewer.strip(): raise ValueError('invalid resource review')
        now=utc_now()
        with self.database.connect() as c:
            ensure_schema(c,now); rows=c.execute("SELECT segment_id FROM kg_resource_node WHERE resource_id=? AND node_id=? AND relation_type=? AND segment_id IS NOT NULL",(resource_id,node_id,relation_type)).fetchall()
            if not rows: raise ValueError('resource review evidence was not found')
            had_approved = bool(c.execute("""SELECT 1 FROM kg_resource_node WHERE resource_id=? AND node_id=?
                AND relation_type=? AND review_status='approved' LIMIT 1""", (resource_id,node_id,relation_type)).fetchone())
            for row in rows:
                action=f"{client_action_id}:{row['segment_id']}"
                if c.execute("SELECT 1 FROM kg_mapping_review_event WHERE client_action_id=?",(action,)).fetchone(): continue
                previous=c.execute("SELECT decision FROM kg_mapping_review WHERE resource_id=? AND segment_id=? AND node_id=? AND relation_type=? AND reviewer=?",(resource_id,row['segment_id'],node_id,relation_type,reviewer)).fetchone()
                c.execute("INSERT INTO kg_mapping_review(review_id,resource_id,segment_id,node_id,relation_type,decision,reviewer,note,created_at) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(resource_id,segment_id,node_id,relation_type,reviewer) DO UPDATE SET decision=excluded.decision,note=excluded.note,created_at=excluded.created_at",(str(uuid4()),resource_id,row['segment_id'],node_id,relation_type,decision,reviewer,note,now))
                c.execute("INSERT INTO kg_mapping_review_event(event_id,resource_id,segment_id,node_id,relation_type,action,decision,reviewer,note,previous_decision,client_action_id,created_at) VALUES(?,?,?,?,?,'decision',?,?,?,?,?,?)",(str(uuid4()),resource_id,row['segment_id'],node_id,relation_type,decision,reviewer,note,previous['decision'] if previous else None,action,now))
                self._refresh_mapping_status(c,resource_id,row['segment_id'],node_id,relation_type)
            if had_approved and decision != 'approved':
                self._supersede_active_plans_for_node(c, node_id, now)
            c.commit()
            states=c.execute("SELECT review_status,COUNT(*) count FROM kg_resource_node WHERE resource_id=? AND node_id=? AND relation_type=? GROUP BY review_status",(resource_id,node_id,relation_type)).fetchall()
        return {"resource_id":resource_id,"node_id":node_id,"relation_type":relation_type,"affected_segment_count":len(rows),"statuses":{r['review_status']:r['count'] for r in states}}

    def undo_resource_review(self, *, resource_id: str, node_id: str, relation_type: str, reviewer: str, client_action_id: str, note: str = "") -> dict:
        now=utc_now()
        with self.database.connect() as c:
            rows=c.execute("SELECT segment_id FROM kg_resource_node WHERE resource_id=? AND node_id=? AND relation_type=? AND segment_id IS NOT NULL",(resource_id,node_id,relation_type)).fetchall()
            if not rows: raise ValueError('resource review evidence was not found')
            for row in rows:
                action=f"{client_action_id}:{row['segment_id']}"
                if c.execute("SELECT 1 FROM kg_mapping_review_event WHERE client_action_id=?",(action,)).fetchone(): continue
                prior=c.execute("SELECT decision FROM kg_mapping_review WHERE resource_id=? AND segment_id=? AND node_id=? AND relation_type=? AND reviewer=?",(resource_id,row['segment_id'],node_id,relation_type,reviewer)).fetchone()
                if not prior: continue
                c.execute("DELETE FROM kg_mapping_review WHERE resource_id=? AND segment_id=? AND node_id=? AND relation_type=? AND reviewer=?",(resource_id,row['segment_id'],node_id,relation_type,reviewer))
                c.execute("INSERT INTO kg_mapping_review_event(event_id,resource_id,segment_id,node_id,relation_type,action,decision,reviewer,note,previous_decision,client_action_id,created_at) VALUES(?,?,?,?,?,'undo',NULL,?,?,?,?,?)",(str(uuid4()),resource_id,row['segment_id'],node_id,relation_type,reviewer,note,prior['decision'],action,now))
                self._refresh_mapping_status(c,resource_id,row['segment_id'],node_id,relation_type)
            # Plan JSON deliberately carries the specific local material chosen
            # at generation time. Once a parent reverses a resource decision it
            # must never remain child-visible through that cached plan, even if
            # another source later becomes available.
            self._supersede_active_plans_for_node(c, node_id, now)
            states=c.execute("SELECT review_status,COUNT(*) count FROM kg_resource_node WHERE resource_id=? AND node_id=? AND relation_type=? GROUP BY review_status",(resource_id,node_id,relation_type)).fetchall(); c.commit()
        return {"resource_id":resource_id,"node_id":node_id,"relation_type":relation_type,"affected_segment_count":len(rows),"statuses":{r['review_status']:r['count'] for r in states}}

    @staticmethod
    def _supersede_active_plans_for_node(connection, node_id: str, now: str) -> None:
        connection.execute("""UPDATE kg_review_plan SET status='superseded',updated_at=?
            WHERE status='active' AND EXISTS (
                SELECT 1 FROM json_each(kg_review_plan.plan_json,'$.items') item
                WHERE json_extract(item.value,'$.knowledge_node_id')=?
            )""", (now,node_id))

    def record_review(self, *, resource_id: str, segment_id: str, node_id: str, relation_type: str,
                      decision: str, reviewer: str, note: str = "", client_action_id: str | None = None) -> dict:
        """Persist an idempotent operator decision and refresh the material projection.

        ``kg_mapping_review`` is the current decision per reviewer.  The companion
        event table deliberately never changes, so an operator can explain why a
        segment became available (or unavailable) to a student after an app restart.
        The most recently saved reviewer decision wins until that reviewer undoes it.
        """
        if decision not in {'approved','rejected','needs_split'}:
            raise ValueError('invalid review decision')
        if not reviewer.strip():
            raise ValueError('reviewer is required')
        now=utc_now()
        client_action_id = client_action_id or f"review:{uuid4()}"
        with self.database.connect() as connection:
            ensure_schema(connection,now)
            existing_event = connection.execute(
                "SELECT 1 FROM kg_mapping_review_event WHERE client_action_id=?", (client_action_id,)
            ).fetchone()
            if existing_event:
                return self._mapping_state(connection, resource_id, segment_id, node_id, relation_type)
            mapping = connection.execute("""SELECT 1 FROM kg_resource_node
                WHERE resource_id=? AND segment_id=? AND node_id=? AND relation_type=?""",
                (resource_id,segment_id,node_id,relation_type)).fetchone()
            if not mapping:
                raise ValueError('mapping to review was not found')
            previous = connection.execute("""SELECT decision FROM kg_mapping_review
                WHERE resource_id=? AND segment_id=? AND node_id=? AND relation_type=? AND reviewer=?""",
                (resource_id,segment_id,node_id,relation_type,reviewer)).fetchone()
            connection.execute("""INSERT INTO kg_mapping_review(review_id,resource_id,segment_id,node_id,relation_type,decision,reviewer,note,created_at)
            VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(resource_id,segment_id,node_id,relation_type,reviewer)
            DO UPDATE SET decision=excluded.decision,note=excluded.note,created_at=excluded.created_at""",
            (str(uuid4()),resource_id,segment_id,node_id,relation_type,decision,reviewer,note,now))
            connection.execute("""INSERT INTO kg_mapping_review_event(event_id,resource_id,segment_id,node_id,relation_type,
                action,decision,reviewer,note,previous_decision,client_action_id,created_at)
                VALUES(?,?,?,?,?,'decision',?,?,?,?,?,?)""",
                (str(uuid4()),resource_id,segment_id,node_id,relation_type,decision,reviewer,note,
                 previous['decision'] if previous else None,client_action_id,now))
            self._refresh_mapping_status(connection, resource_id, segment_id, node_id, relation_type)
            connection.commit()
            return self._mapping_state(connection, resource_id, segment_id, node_id, relation_type)

    def undo_review(self, *, resource_id: str, segment_id: str, node_id: str, relation_type: str,
                    reviewer: str, client_action_id: str | None = None, note: str = "") -> dict:
        """Undo one reviewer's current decision without erasing the audit event."""
        if not reviewer.strip():
            raise ValueError('reviewer is required')
        now = utc_now()
        client_action_id = client_action_id or f"undo:{uuid4()}"
        with self.database.connect() as connection:
            ensure_schema(connection, now)
            if connection.execute("SELECT 1 FROM kg_mapping_review_event WHERE client_action_id=?", (client_action_id,)).fetchone():
                return self._mapping_state(connection, resource_id, segment_id, node_id, relation_type)
            existing = connection.execute("""SELECT decision FROM kg_mapping_review
                WHERE resource_id=? AND segment_id=? AND node_id=? AND relation_type=? AND reviewer=?""",
                (resource_id,segment_id,node_id,relation_type,reviewer)).fetchone()
            if not existing:
                raise ValueError('reviewer has no current decision to undo')
            connection.execute("""DELETE FROM kg_mapping_review WHERE resource_id=? AND segment_id=? AND node_id=?
                AND relation_type=? AND reviewer=?""", (resource_id,segment_id,node_id,relation_type,reviewer))
            connection.execute("""INSERT INTO kg_mapping_review_event(event_id,resource_id,segment_id,node_id,relation_type,
                action,decision,reviewer,note,previous_decision,client_action_id,created_at)
                VALUES(?,?,?,?,?,'undo',NULL,?,?,?,?,?)""",
                (str(uuid4()),resource_id,segment_id,node_id,relation_type,reviewer,note,existing['decision'],client_action_id,now))
            self._refresh_mapping_status(connection, resource_id, segment_id, node_id, relation_type)
            connection.commit()
            return self._mapping_state(connection, resource_id, segment_id, node_id, relation_type)

    def review_history(self, *, resource_id: str, segment_id: str, node_id: str, relation_type: str) -> list[dict]:
        self._ensure_ready()
        with self.database.connect() as connection:
            rows = connection.execute("""SELECT action,decision,reviewer,note,previous_decision,created_at
                FROM kg_mapping_review_event WHERE resource_id=? AND segment_id=? AND node_id=? AND relation_type=?
                ORDER BY created_at,event_id""", (resource_id,segment_id,node_id,relation_type)).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _refresh_mapping_status(connection, resource_id: str, segment_id: str, node_id: str, relation_type: str) -> None:
        latest = connection.execute("""SELECT decision FROM kg_mapping_review
            WHERE resource_id=? AND segment_id=? AND node_id=? AND relation_type=?
            ORDER BY created_at DESC,review_id DESC LIMIT 1""", (resource_id,segment_id,node_id,relation_type)).fetchone()
        status = latest['decision'] if latest and latest['decision'] in {'approved','rejected'} else 'unreviewed'
        connection.execute("""UPDATE kg_resource_node SET review_status=? WHERE resource_id=? AND segment_id=?
            AND node_id=? AND relation_type=?""", (status,resource_id,segment_id,node_id,relation_type))

    @staticmethod
    def _mapping_state(connection, resource_id: str, segment_id: str, node_id: str, relation_type: str) -> dict:
        row = connection.execute("""SELECT review_status,confidence,source_ref FROM kg_resource_node
            WHERE resource_id=? AND segment_id=? AND node_id=? AND relation_type=?""",
            (resource_id,segment_id,node_id,relation_type)).fetchone()
        if not row:
            raise ValueError('mapping to review was not found')
        return {"resource_id":resource_id,"segment_id":segment_id,"node_id":node_id,"relation_type":relation_type,
                "review_status":row['review_status'],"confidence":row['confidence'],"mapping_source_ref":row['source_ref']}

    def _ensure_ready(self) -> None:
        now = utc_now()
        with self.database.connect() as connection:
            ensure_schema(connection, now)
            connection.commit()
