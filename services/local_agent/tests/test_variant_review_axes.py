"""Review timing is separate from the immutable candidate's transfer form."""
import pytest

from home_study_agent.local_supply import LocalSupplyError, validate_item
from home_study_agent.learning_obligations import qualified_exact_score
from home_study_agent.variant_supply import VariantSupplyService
from home_study_agent.mistake_book import MistakeBookService
from test_variant_supply import _prepared


@pytest.mark.parametrize('stage', ['delayed_48h', 'delayed_7d'])
def test_delayed_review_keeps_original_transfer_source_and_scope(tmp_path, stage):
    db, _, mistake_id = _prepared(tmp_path)
    child = 'child-junior-3'
    supply = VariantSupplyService(db)
    supply.enqueue_local(mistake_id, child, 'variants')
    supply.process_local_pending()
    candidate = next(row for row in supply.list(child)['items'] if row['stage'] == 'near_transfer')
    item = supply.qualify_local(candidate['candidate_id'], child, 'qualify')
    with db.connect() as c:
        verified = validate_item(c, child, item['diagnostic_item_id'], stage=stage, mistake_id=mistake_id)
        assert qualified_exact_score(c, child, item['diagnostic_item_id'], item['expected_answer']['exact_answers'][0],
                                     stage=stage, mistake_id=mistake_id)[1] == 1
        _, eligible = MistakeBookService._item_scope(c, child, stage, mistake_id)
        assert item['diagnostic_item_id'] in eligible
        assert c.execute('SELECT stage FROM variant_candidate WHERE candidate_id=?', (candidate['candidate_id'],)).fetchone()[0] == 'near_transfer'
        for scope in ({'stage': 'far_transfer', 'mistake_id': mistake_id},
                      {'stage': stage, 'mistake_id': 'different-mistake'}):
            with pytest.raises(LocalSupplyError):
                validate_item(c, child, verified['diagnostic_item_id'], **scope)
        with pytest.raises(LocalSupplyError):
            validate_item(c, 'child-primary-4', verified['diagnostic_item_id'], stage=stage, mistake_id=mistake_id)
        c.execute("UPDATE variant_candidate SET status='rejected' WHERE candidate_id=?", (candidate['candidate_id'],))
        assert qualified_exact_score(c, child, item['diagnostic_item_id'], item['expected_answer']['exact_answers'][0],
                                     stage=stage, mistake_id=mistake_id) is None
