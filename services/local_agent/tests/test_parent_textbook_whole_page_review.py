"""Whole material page review keeps source and genuine ambiguity gates."""
import pytest
from home_study_agent.database import Database, LocalMediaRuntime
from home_study_agent.knowledge_graph import parent_textbook_capture as candidate
from test_parent_textbook_capture import Adapter, setup


@pytest.mark.parametrize('confidence,quality,reasons,expected', [
    (.995, True, ['missing_question_start'], 'ready'),
    (.62, True, ['missing_question_start'], 'needs_review'),
    (.995, False, ['missing_question_start'], 'needs_review'),
    (.995, True, ['missing_question_start', 'handwriting'], 'needs_review'),
    (.995, True, ['missing_question_start', 'formula_ambiguous'], 'needs_review'),
    (.995, True, ['missing_question_start', 'orientation_ambiguous'], 'needs_review'),
    (.995, True, [], 'needs_review'),
    (.995, True, [{'unknown_reason': True}], 'needs_review'),
])
def test_whole_page_ocr_retains_real_ambiguity_and_original_source(tmp_path, confidence, quality, reasons, expected):
    class WholePageAdapter(Adapter):
        def run_ocr_claim(self, _claim, _image):
            self.result = {'confidence': confidence, 'engine': 'synthetic-local',
                'quality': {'passed': quality, 'issues': [] if quality else ['blur']},
                'lines': [{'text': 'Read the paragraph and find its main idea.', 'confidence': confidence}],
                'manual_review': {'required': True, 'reasons': reasons, 'auto_accept_allowed': False},
                'privacy': {'raw_image_uploaded': False}}
            return {'status': 'completed', 'result': self.result}
    database = Database(tmp_path / 'synthetic.db')
    setup(database)
    adapter = WholePageAdapter()
    service = candidate.ParentTextbookCaptureService(database, adapter)
    lease = service.acquire('child-4', 'bound-document-hash', {})
    artifact = service.recognize('child-4', lease['lease_id'],
        'aGVsbG8gdGV4dGJvb2sgcGFnZSBpbWFnZQ==', {'title': 'Public printed page'}, 'public-page-1')
    assert artifact['status'] == expected
    assert artifact['extracted_text'] == 'Read the paragraph and find its main idea.'
    assert adapter.result['manual_review']['required'] is True
    assert adapter.result['manual_review']['reasons'] == reasons
    assert service.source_image('child-4', artifact['capture_ref'])[0] == b'hello textbook page image'
    reopened = Database(database.path)
    reopened.local_media_runtime = LocalMediaRuntime(tmp_path, b'0' * 32)
    restarted = candidate.ParentTextbookCaptureService(reopened, adapter)
    stored = restarted.pending('child-4')[0]
    assert stored['status'] == expected
    if expected == 'ready':
        review = stored['details']['local_pipeline']['manual_review']
        assert review['required'] is False
        assert review['original_question_boundary_review']['reasons'] == ['missing_question_start']
        assert review['original_question_boundary_review']['required'] is True
