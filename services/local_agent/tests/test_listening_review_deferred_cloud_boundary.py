"""Inspect the existing recovery sequence, without adding any model request."""
import json

import test_listening_review_journey as original_journey
from test_listening_review_deferred_bridge import test_frontend_completed_bridge_is_not_an_answer_and_new_confirmation_survives_restart as bridge_journey
from test_pet_media_evidence import media


def test_deferred_receipts_never_enter_subsequent_control_or_answer_model_messages(media, tmp_path, monkeypatch):
    original_case = original_journey.next_day_case
    sent = []

    def traced_case(*args, **kwargs):
        case = original_case(*args, **kwargs)
        _, database, gateway, _, _, _ = case
        complete = gateway.complete

        async def tracked(request):
            with database.connect() as c:
                receipts = [json.loads(row[0]) for row in c.execute('''SELECT j.value
                    FROM agent_turn t,json_each(t.action_json,'$.listening_review_deferred') j
                    WHERE t.child_id=? AND t.session_id=?''',
                    (request.child_id, request.session_id)).fetchall()]
            if receipts:
                # These are the exact messages handed to the existing gateway,
                # captured before await, not a reconstructed safe projection.
                wire = json.dumps(request.messages, ensure_ascii=False, sort_keys=True)
                forbidden = {'listening_review_deferred', 'deferred_answer'}
                for receipt in receipts:
                    forbidden.update(str(receipt[k]) for k in ('request_sha256', 'source_text_sha256',
                        'source_turn_id', 'source_voice_input_id', 'client_action_id') if receipt.get(k))
                sent.append({'request_type': request.request_type, 'messages': json.loads(wire),
                    'forbidden_present': sorted(value for value in forbidden if value in wire)})
                assert not sent[-1]['forbidden_present'], sent[-1]
            return await complete(request)

        monkeypatch.setattr(gateway, 'complete', tracked)
        return case

    monkeypatch.setattr(original_journey, 'next_day_case', traced_case)
    bridge_journey(media, tmp_path, monkeypatch)
    turns = [json.loads(item['messages'][-1]['content']) for item in sent
        if item['request_type'] == 'voice_interaction']
    assert {'继续原任务', '雨衣'} <= {item['transcript'] for item in turns}, turns
    assert all(set(item) == {'transcript', 'state'} for item in turns)
    assert sent and all(not item['forbidden_present'] for item in sent)
    (tmp_path / 'deferred-cloud-boundary.json').write_text(json.dumps(sent, ensure_ascii=False))
