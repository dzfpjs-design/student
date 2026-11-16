"""Create legal synthetic reports, then simulate loss in this new fixture only."""
import argparse
import json
import runpy
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument('--database-path', type=Path, required=True)
parser.add_argument('--receipt-path', type=Path, required=True)
args = parser.parse_args()
seed = runpy.run_path(str(Path(__file__).with_name('seed-parent-report-delivery.py')))['seed']
first = seed(args.database_path)

from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.database import Database

db = Database(args.database_path)
settings = Settings(data_dir=db.path.parent, database_path=db.path, maintenance_enabled=False,
                    cloud_asr_provider='disabled', tts_provider='disabled', model_api_key=None)
with TestClient(create_app(settings)) as client:
    def post(path, headers=None, **body):
        response = client.post(path, headers=headers, json=body)
        assert response.status_code in (200, 201), response.text
        return response.json()
    headers = {'X-HSC-Parent': post('/v1/parent/setup', pin='2468')['parent_token']}
    for index, child in enumerate(('child-junior-3', 'child-primary-4')):
        action = f'source-native-{index}'
        sid = post('/v1/sessions', child_id=child, client_action_id=action)['session_id']
        def move(before, after):
            post(f'/v1/sessions/{sid}/transition', expected_state=before, target_state=after,
                 reason='synthetic_no_learning', client_action_id=f'{action}:{after}')
        move('check_in', 'classroom_recall')
        post(f'/v1/sessions/{sid}/recalls/none', client_action_id=f'{action}:recall')
        move('classroom_recall', 'homework_capture')
        post(f'/v1/sessions/{sid}/homework/none', client_action_id=f'{action}:homework')
        post(f'/v1/sessions/{sid}/plan/generate', client_action_id=f'{action}:plan')
        move('plan_review', 'study')
        move('study', 'reflection')
        post(f'/v1/sessions/{sid}/reflection/complete', learned_text='本机合成验证，尚未开始学习。',
             difficult_text='暂无课堂记录。', feeling='平静', next_action='下次核对。',
             skipped=False, client_action_id=f'{action}:reflection')
    snapshots = client.get('/v1/parent/children/child-junior-3/report-snapshots', headers=headers).json()['items']
    current = next(row for row in snapshots if row['report_id'] == first['report_id'] and row['status'] == 'draft')
    post(f"/v1/parent/report-snapshots/{current['snapshot_id']}/approve", headers=headers)
    with db.connect() as c:
        original = dict(c.execute('SELECT * FROM daily_reflection WHERE session_id=?', (first['session_id'],)).fetchone())
        c.execute('DELETE FROM daily_reflection WHERE session_id=?', (first['session_id'],))
        c.execute("UPDATE daily_report SET evidence_json='{}' WHERE report_id=?", (first['report_id'],))
        assert c.execute("SELECT COUNT(*) FROM plan_item WHERE status='completed'").fetchone()[0] == 0
        c.commit()
result = {'synthetic_only': True, 'fault': 'missing_reflection_and_stale_report',
          'first': first, 'approved_snapshot_id': current['snapshot_id'], 'original_reflection': original,
          'daily_reports': 3, 'completed_learning_tasks': 0, 'external_delivery': False}
with args.receipt_path.open('x', encoding='utf-8') as stream:
    args.receipt_path.chmod(0o600)
    json.dump(result, stream, ensure_ascii=False, indent=2)
