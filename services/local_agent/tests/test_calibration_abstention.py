"""Decision boundary only; signed HTTP replay supplies provenance coverage."""
import pytest

from home_study_agent.rubric_calibration import RubricCalibrationService


@pytest.mark.parametrize('phase',['persist','recover'])
@pytest.mark.parametrize('allow',[False,True])
def test_only_feedback_persistence_can_accept_per_answer_abstention(monkeypatch,phase,allow):
    service = RubricCalibrationService(None)
    row = {'child_id':'synthetic','protocol_id':'protocol'}
    binding = {**row,'assessment_binding':{'protocol_id':'protocol'},
        'source_evidence_id':'original','certificate_event_id':'certificate'}
    abstained = {'status':'reference','score_qualified':False,'blockers':['assessment_abstained']}
    monkeypatch.setattr(service,'_score_binding',lambda *args: binding)
    monkeypatch.setattr(service,'_qualified_score_receipt',lambda *args: abstained)
    def check():
        return service._revalidate_score_binding(None,row,binding,phase,None,'assessment',{},
            _allow_abstention=allow)
    if allow:
        assert check() == abstained
    else:
        with pytest.raises(ValueError,match='SCORE_BINDING_CHANGED'):
            check()


@pytest.mark.parametrize('blockers',[
    ['source_changed'], ['certificate_basis_changed'], ['assessment_abstained','source_changed'], [],
])
def test_feedback_option_does_not_accept_other_unqualified_results(monkeypatch,blockers):
    service = RubricCalibrationService(None)
    row = {'child_id':'synthetic','protocol_id':'protocol'}
    binding = {**row,'assessment_binding':{'protocol_id':'protocol'},
        'source_evidence_id':'original','certificate_event_id':'certificate'}
    monkeypatch.setattr(service,'_score_binding',lambda *args: binding)
    monkeypatch.setattr(service,'_qualified_score_receipt',lambda *args:
        {'status':'reference','score_qualified':False,'blockers':blockers})
    with pytest.raises(ValueError,match='SCORE_BINDING_CHANGED'):
        service._revalidate_score_binding(None,row,binding,'persist',None,'assessment',{},_allow_abstention=True)


def test_feedback_option_does_not_skip_current_binding_validation(monkeypatch):
    service = RubricCalibrationService(None)
    row = {'child_id':'synthetic','protocol_id':'protocol'}
    binding = {**row,'assessment_binding':{'protocol_id':'protocol'},'source_evidence_id':'old'}
    monkeypatch.setattr(service,'_score_binding',lambda *args: {**binding,'source_evidence_id':'new'})
    def unexpected(*args):
        pytest.fail('Must reject changed binding before considering abstention')
    monkeypatch.setattr(service,'_qualified_score_receipt',unexpected)
    with pytest.raises(ValueError,match='SCORE_BINDING_CHANGED'):
        service._revalidate_score_binding(None,row,binding,'persist',None,'assessment',{},_allow_abstention=True)
