"""Read current practice inventory, never create a scoring qualification."""
import json

from .learning_obligations import _source_question, had_recent_item_exposure, recent_same_item_answer
from .local_supply import LocalSupplyError, validate_item
from .pet_skill_gate import pet_skill_gate
from .specialized_stages import _item_source


def practice_item(c, child_id, item_id, now, *, allow_recent=False):
    try:
        item = validate_item(c, child_id, item_id)
        if not allow_recent and recent_same_item_answer(c, child_id, item_id, now.isoformat()):
            return None
        if not allow_recent and had_recent_item_exposure(c, child_id, item_id, now.isoformat()):
            return None
        modality = pet_skill_gate(c, child_id, item_id)
        # Listening playback and original writing responses only exist after the
        # task starts. Their scoring gates remain exclusively in answer admission.
        if modality['modality'] == 'listening' or item['answer_type'] in {'writing', 'open_response'}:
            source, error = _item_source(c, child_id, item, now)
            return item if not error and source and source['stage'] == 'baseline' and source['score_qualified'] else None
        if not modality['skill_scoring_allowed']:
            return None
        reference = _source_question(c, item)
        if not reference or any((item.get(k) or '') != (reference.get(k) or '')
                                for k in ('prompt', 'stimulus_text', 'stimulus_asset')):
            return None
        rubric = json.loads(item['expected_answer_json'])
        if (rubric.get('local_rule') != 'local_exact_v1' or rubric.get('keyword_groups') or rubric.get('task_points')
                or item.get('assessment_mode') == 'pet_reference_rubric'):
            return None
        answers = rubric.get('exact_answers')
        if not isinstance(answers, list) or not answers or not all(isinstance(v, (str, int, float)) and str(v).strip()
                                                                and not isinstance(v, bool) for v in answers):
            return None
        return item
    except (LocalSupplyError, ValueError, TypeError, KeyError):
        return None


def shown_question_identities(c, child_id):
    """All prior displayed originals, including cancelled/withdrawn material.

    Current permission is separate from exposure. Missing historical identity
    must never turn a previously displayed question into a new one.
    """
    from .mistake_book import MistakeBookService
    from .diagnostic_history_identity import historical_identity
    identities, seen = MistakeBookService._question_history(c, child_id)
    shown = set(seen)
    if c.execute("SELECT 1 FROM sqlite_master WHERE name='diagnostic_prompt'").fetchone():
        for prompt in c.execute('SELECT * FROM diagnostic_prompt WHERE child_id=?', (child_id,)):
            shown.add(historical_identity(c, child_id, prompt, None))
    return identities, shown


def unexposed_practice_item(c, child_id, item_id, now, history):
    identities, shown = history
    identity = identities.get(item_id)
    if not identity or identity in shown:
        return None
    return practice_item(c, child_id, item_id, now)
