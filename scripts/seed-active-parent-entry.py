"""New active synthetic session; no cancellation or learning completion."""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'services/local_agent/src'))
parser = argparse.ArgumentParser()
parser.add_argument('--data-dir', required=True)
parser.add_argument('--receipt-path', required=True)
args = parser.parse_args()
data = Path(args.data_dir).resolve()
if not data.parent.name.startswith('hsc-active-parent-entry-') or (data / 'agent.db').exists():
    raise RuntimeError('Only a new owned synthetic fixture is allowed')
data.mkdir(parents=True, exist_ok=True)
from home_study_agent.database import Database
from home_study_agent.repository import StudyRepository
from home_study_agent.parent_auth import ParentAuth
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService

db = Database(data / 'agent.db')
db.initialize()
repo = StudyRepository(db)
repo.seed_family()
privacy = PrivacyLifecycleService(db, data)
privacy.initialize()
privacy.update_preferences({'cloud_model_text_allowed': False, 'cloud_audio_allowed': False,
                            'raw_media_storage_allowed': False})
ParentAuth(db).setup('2468')
session = repo.start_session('child-primary-4', 'synthetic-active-entry-original')
assert session['status'] == 'active'
Path(args.receipt_path).write_text(json.dumps({'synthetic_only': True, 'session_id': session['session_id'],
    'child_id': session['child_id'], 'status': session['status'], 'parent_configured': True,
    'database_path': str(db.path), 'session_count': 1}, indent=2))
