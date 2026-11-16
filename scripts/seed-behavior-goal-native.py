"""Synthetic classifier history for actual ParentCenter acceptance, not child outcomes."""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'services/local_agent/src'), str(ROOT / 'services/local_agent/tests')]


def local_only(event, args):
    if event == 'socket.connect' and isinstance(args[1], tuple) and args[1][0] not in ('localhost', '127.0.0.1', '::1'):
        raise RuntimeError('No external networking in synthetic behavior acceptance')


sys.addaudithook(local_only)
parser = argparse.ArgumentParser()
parser.add_argument('--data-dir', required=True)
parser.add_argument('--receipt-path', required=True)
parser.add_argument('--retire-synthetic-session', action='store_true')
args = parser.parse_args()
data = Path(args.data_dir).resolve()
if not data.parent.name.startswith('hsc-behavior-goal-native-'):
    raise RuntimeError('A new owned synthetic fixture directory is required')
if args.retire_synthetic_session:
    import sqlite3
    with sqlite3.connect(str(data / 'agent.db')) as c:
        assert c.execute("SELECT COUNT(*) FROM habit_experiment_event WHERE event_type IN ('goal_behavior_confirmed','goal_behavior_retracted')").fetchone()[0] == 0
        c.execute("UPDATE daily_session SET status='cancelled',state='cancelled',ended_at='2026-09-08T00:00:00+00:00' WHERE session_id='session' AND child_id='child-primary-4'")
        c.commit()
    Path(args.receipt_path).write_text(json.dumps({'synthetic_only': True, 'cancelled_historical_fixture_session': True,
        'learning_completion_added': False, 'original_opportunities_retained': True}))
    sys.exit(0)
if (data / 'agent.db').exists():
    raise RuntimeError('A new owned synthetic fixture directory is required')
data.mkdir(parents=True, exist_ok=True)
from test_behavior_observation_coverage import managed
from test_behavior_goal_confirmation import history, CHILD
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from home_study_agent.goal_program import GoalProgramService

db, service = history(managed.__wrapped__(data))
# This is a parent review of historical synthetic observations, not a live child session.
with db.connect() as c:
    c.execute("UPDATE daily_session SET status='cancelled',state='cancelled',ended_at='2026-09-08T00:00:00+00:00' WHERE session_id='session' AND child_id=?", (CHILD,))
    c.commit()
privacy = PrivacyLifecycleService(db, data)
privacy.initialize()
privacy.update_preferences({'cloud_model_text_allowed': False, 'cloud_audio_allowed': False,
                            'raw_media_storage_allowed': False})
goals = GoalProgramService(db)
goals.seed_defaults()
for child, track, target in [(CHILD, 'pet_path', '2028-01-15'), ('child-junior-3', 'junior_exam_readiness', '2027-06-15')]:
    goals.configure_program(child, track, target_statement=None, target_date=target, target_score=None,
        target_score_label=None, event_name=None, event_rules_url=None, event_version=None, award_target=None,
        registration_deadline=None, weekly_minutes=90, weekly_sessions=3, client_action_id='synthetic-rule-'+track)
view = service.goal_behavior_opportunities(CHILD, 'goal')
assert len(view['opportunities']) == 12
assert all(row['confirmation'] is None for row in view['opportunities'])
Path(args.receipt_path).write_text(json.dumps({'synthetic_only': True, 'child_id': CHILD, 'experiment_id': 'goal',
    'opportunity_count': len(view['opportunities']), 'fixture_source': 'test_behavior_goal_confirmation.history',
    'boundary': 'Synthetic observation and task rows only; no physical device, child behavior success or reliable learning claim.',
    'database_path': str(db.path)}, ensure_ascii=False, indent=2))
