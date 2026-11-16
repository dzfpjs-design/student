"""Synthetic two-material publication for the actual ParentCenter only."""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'services/local_agent/src'), str(ROOT / 'services/local_agent/tests')]


def local_only(event, args):
    if event == 'socket.connect' and isinstance(args[1], tuple) and args[1][0] not in ('localhost', '127.0.0.1', '::1'):
        raise RuntimeError('Synthetic material acceptance prohibits external networking')


sys.addaudithook(local_only)
parser = argparse.ArgumentParser()
parser.add_argument('--data-dir', required=True)
parser.add_argument('--receipt-path', required=True)
args = parser.parse_args()
data = Path(args.data_dir).resolve()
if not data.parent.name.startswith('hsc-material-purpose-native-') or (data / 'open-supply.db').exists():
    raise RuntimeError('A new owned synthetic fixture directory is required')
data.mkdir(parents=True, exist_ok=True)
from test_open_response_materials import material_case, publish_open, CHILD
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from home_study_agent.writing_question_authorization import WritingQuestionAuthorizationService

db, supply, spec, source, _, _ = material_case(data, material_count=2,
    question_license='public', material_license='public')
publication = publish_open(supply, spec)
privacy = PrivacyLifecycleService(db, data)
privacy.initialize()
privacy.update_preferences({'cloud_model_text_allowed': True})
permission = WritingQuestionAuthorizationService(db)
permission.initialize()
view = permission.read(CHILD, diagnostic_item_id=publication['diagnostic_item_id'])
assert view['purpose_version'] == 'writing_question_material_formative_text_v2'
assert len(view['preview']['materials']) == 2 and view['status'] == 'authorization_required'
with db.connect() as c:
    assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
    assert c.execute('SELECT COUNT(*) FROM writing_question_authorization_event').fetchone()[0] == 0
Path(args.receipt_path).write_text(json.dumps({'synthetic_only': True, 'child_id': CHILD,
    'candidate_id': publication['candidate_id'], 'diagnostic_item_id': publication['diagnostic_item_id'],
    'question': view['preview']['question'], 'materials': view['preview']['materials'],
    'database_path': str(db.path)}, ensure_ascii=False, indent=2))
