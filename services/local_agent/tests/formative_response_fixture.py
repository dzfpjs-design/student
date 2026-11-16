"""Explicit transport double; never a semantic model or calibration corpus."""
from home_study_agent.formative_criterion_review import VERSION


def criterion_reply(payload, result):
    supported=result['understanding']=='independent'
    status='supported' if supported else 'partial'
    return dict(version=VERSION,confidence=result['confidence'],rubric_dimensions=result['rubric_dimensions'],
        criterion_checks=[dict(criterion_index=i,status=status,response_quotes=[payload['response']],
            rationale=result['evidence_summary']) for i in range(len(payload['rubric']['task_points']))],
        feedback=dict(acknowledgement=result['spoken_feedback'],focus_index=None if supported else 0,
            question=None if supported else result['next_check']),next_check=result['next_check'])
