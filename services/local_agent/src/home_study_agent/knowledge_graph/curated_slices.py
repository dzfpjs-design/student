from __future__ import annotations

from ..database import Database
from .service import KnowledgeGraphService


class CuratedLocalSlices:
    """Small source-grounded slices used to prove the full graph loop without an LLM."""
    def __init__(self, database: Database): self.database=database; self.graph=KnowledgeGraphService(database)

    def p4_large_number_rewrite(self) -> dict:
        """Project the explicit 四年级 lesson 11665; source text remains the authority."""
        self.graph.ensure_ready()
        with self.database.connect() as connection:
            resource=connection.execute("SELECT resource_id FROM kg_resource WHERE external_id='11665'").fetchone()
            if not resource: return {"status":"source_unavailable"}
            segments=connection.execute("SELECT segment_id,text FROM kg_segment WHERE resource_id=? AND (instr(text,'万位')>0 OR instr(text,'亿位')>0 OR instr(text,'改写')>0)",(resource['resource_id'],)).fetchall()
        source_ref='local_course:seyk:11665'
        place=self.graph.upsert_node(canonical_key='cn-p4-math-place-value',node_type='knowledge_point',title='万、亿位值与数位分级',description='理解万位、亿位及数位分级，作为大数改写依据。',scope={'track':'school','subject':'数学','grade':'四年级'},source_ref=source_ref,version_label='local-course-11665',license_boundary='local_course',confidence=.7,review_status='unreviewed')
        rewrite=self.graph.upsert_node(canonical_key='cn-p4-math-rewrite-wan-yi',node_type='knowledge_point',title='把大数改写成用万或亿作单位的数',description='在万位或亿位右边点小数点、化简并标明单位。',scope={'track':'school','subject':'数学','grade':'四年级'},source_ref=source_ref,version_label='local-course-11665',license_boundary='local_course',confidence=.75,review_status='unreviewed')
        error=self.graph.upsert_node(canonical_key='misconception-p4-rewrite-decimal-place',node_type='misconception',title='把小数点点在错误数位',description='改写大数时未在万位或亿位右边定位小数点。',scope={'track':'school','subject':'数学','grade':'四年级'},source_ref=source_ref,version_label='local-course-11665',license_boundary='local_course',confidence=.7,review_status='unreviewed')
        exercise=self.graph.upsert_node(canonical_key='exercise-p4-rewrite-384400',node_type='exercise',title='把384400改写成用万作单位的数',description='来源课程中的例题；应说明数位与小数部分含义。',scope={'track':'school','subject':'数学','grade':'四年级'},source_ref=source_ref,version_label='local-course-11665',license_boundary='local_course',confidence=.8,review_status='unreviewed')
        self.graph.link(place,rewrite,'prerequisite_of',rationale='课程说明先识别万位/亿位再定位小数点',source_ref=source_ref,version_label='local-course-11665',confidence=.75,review_status='unreviewed')
        self.graph.link(error,rewrite,'misconception_of',rationale='课程反复强调小数点在万位或亿位右边',source_ref=source_ref,version_label='local-course-11665',confidence=.7,review_status='unreviewed')
        self.graph.link(rewrite,exercise,'assessed_by',rationale='课程例题可用于复测该改写方法',source_ref=source_ref,version_label='local-course-11665',confidence=.8,review_status='unreviewed')
        for segment in segments:
            self.graph.map_segment(resource['resource_id'],segment['segment_id'],rewrite,'explains',confidence=.75,source_ref=source_ref)
            if '万位' in segment['text'] or '亿位' in segment['text']:
                self.graph.map_segment(resource['resource_id'],segment['segment_id'],place,'covers',confidence=.7,source_ref=source_ref)
        return {'status':'projected','resource_id':resource['resource_id'],'segment_count':len(segments),'knowledge_node_id':rewrite,'exercise_node_id':exercise}

    def j3_quadratic_formula_retest(self) -> dict:
        """Attach a clearly labelled teacher-authored variant to the existing J3 node."""
        self.graph.ensure_ready()
        with self.database.connect() as connection:
            node = connection.execute("SELECT node_id FROM kg_node WHERE canonical_key='starter:J3.MATH.QUADRATIC.FORMULA'").fetchone()
        if not node: return {'status':'starter_node_unavailable'}
        source_ref='teacher_authored:quadratic_formula_variant_v1'
        exercise=self.graph.upsert_node(canonical_key='exercise-j3-quadratic-formula-v1',node_type='exercise',title='用求根公式解 x²-5x+6=0，并代回检验',description='变式复测；要求写出a、b、c，代入求根公式，并把结果代回原方程。',scope={'track':'school','subject':'数学','grade':'初三'},source_ref=source_ref,version_label='teacher-v1',license_boundary='teacher_authored',confidence=.95,review_status='approved')
        self.graph.link(node['node_id'],exercise,'assessed_by',rationale='覆盖公式条件、代入和结果检验的本地变式复测',source_ref=source_ref,version_label='teacher-v1',confidence=.95,review_status='approved')
        return {'status':'projected','knowledge_node_id':node['node_id'],'exercise_node_id':exercise}
